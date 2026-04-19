import pandas as pd
import MetaTrader5 as mt5
import time
import threading
import telebot
import sqlite3
import csv
import os
import requests
import logging
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Suppress verbose telebot connection error logs — reconnection is handled silently
logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

# ====================================
# CONFIGURATION
# ====================================
TELEGRAM_TOKEN = "8753158491:AAGORecE0YMpI-vIR9XEkJj5dRfFh4tDl4I"
CHAT_ID = "5667584601"

SYMBOL        = "XAUUSD"
TIMEFRAME_M1  = mt5.TIMEFRAME_M1
TIMEFRAME_M15 = mt5.TIMEFRAME_M15
TIMEFRAME_H4  = mt5.TIMEFRAME_H4

RISK_PERCENT        = 0.5 / 100
CONTRACT_SIZE       = 100
SCAN_INTERVAL       = 60
SIGNAL_TIMEOUT      = 600

DAILY_LOSS_LIMIT    = 0.02
MAX_TRADES          = 3
MIN_IFVG_GAP        = 15.0
BREAKEVEN_PCT       = 0.50
TRAIL_RISK_PCT      = 0.50
WIN_RATE_THRESHOLD  = 0.60
HEARTBEAT_INTERVAL  = 3600
NEWS_BUFFER_MIN     = 30
NEWS_RETRY_INTERVAL = 300
DAILY_SUMMARY_HOUR  = 22

# ── Momentum override ──────────────────────────────────────────────────────
# If the number of valid IFVGs detected is >= this threshold, the M15 trend
# filter is bypassed. High IFVG counts signal a blow-off top/bottom where
# the trend is already breaking — requiring M15 agreement would miss the move.
# H4 filter is always kept as a macro safety check.
MOMENTUM_IFVG_THRESHOLD = 15   # ← tune this (was "50" in your spec; 15 is safer)
# ──────────────────────────────────────────────────────────────────────────

CSV_FILE   = "trade_log.csv"
DB_FILE    = "trade_log.db"
USERS_FILE = "users.txt"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ====================================
# THREAD-SAFE SHARED STATE
# ====================================
state_lock = threading.Lock()

shared = {
    "pending_signal":    {},
    "signal_time":       None,
    "last_entry":        None,
    "active_tickets":    [],
    "paused":            False,
    "daily_start_bal":   None,
    "daily_loss_hit":    False,
    "breakeven_tickets": set(),
}

# ====================================
# USER PERSISTENCE
# ====================================
def save_user(chat_id):
    chat_id = str(chat_id)
    try:
        with open(USERS_FILE, "r") as f:
            existing = f.read().splitlines()
    except FileNotFoundError:
        existing = []
    if chat_id not in existing:
        with open(USERS_FILE, "a") as f:
            f.write(chat_id + "\n")
        print(f"New user added: {chat_id}")

def get_all_users():
    try:
        with open(USERS_FILE, "r") as f:
            return [u.strip() for u in f.read().splitlines() if u.strip()]
    except FileNotFoundError:
        return []

def broadcast(message_text, reply_markup=None):
    for user_id in get_all_users():
        try:
            if reply_markup:
                bot.send_message(user_id, message_text, reply_markup=reply_markup)
            else:
                bot.send_message(user_id, message_text)
        except Exception as e:
            print(f"Broadcast error for {user_id}: {e}")

def admin_only(message_text, reply_markup=None):
    try:
        if reply_markup:
            bot.send_message(CHAT_ID, message_text, reply_markup=reply_markup)
        else:
            bot.send_message(CHAT_ID, message_text)
    except Exception as e:
        print(f"Admin message error: {e}")

# ====================================
# DATABASE SETUP
# ====================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket    INTEGER,
            symbol    TEXT,
            side      TEXT,
            entry     REAL,
            sl        REAL,
            tp        REAL,
            lot       REAL,
            pnl       REAL,
            result    TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_trade_db(ticket, symbol, side, entry, sl, tp, lot, pnl, result):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (ticket,symbol,side,entry,sl,tp,lot,pnl,result,timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (ticket, symbol, side, entry, sl, tp, lot, pnl, result,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB log error:", e)

def log_trade_csv(ticket, symbol, side, entry, sl, tp, lot, pnl, result):
    try:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["ticket","symbol","side","entry","sl","tp",
                                 "lot","pnl","result","timestamp"])
            writer.writerow([ticket, symbol, side, entry, sl, tp, lot, pnl, result,
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except Exception as e:
        print("CSV log error:", e)

def get_stats():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT result, pnl FROM trades WHERE result IN ('WIN','LOSS')")
        rows = c.fetchall()
        conn.close()
        if not rows:
            return None
        wins      = [r for r in rows if r[0] == "WIN"]
        losses    = [r for r in rows if r[0] == "LOSS"]
        total     = len(rows)
        win_rate  = len(wins) / total if total > 0 else 0
        total_pnl = sum(r[1] for r in rows)
        best      = max(rows, key=lambda r: r[1])
        worst     = min(rows, key=lambda r: r[1])
        return {
            "total":     total,
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  win_rate,
            "total_pnl": round(total_pnl, 2),
            "best":      round(best[1], 2),
            "worst":     round(worst[1], 2),
        }
    except Exception as e:
        print("Stats error:", e)
        return None

def get_today_pnl():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn  = sqlite3.connect(DB_FILE)
        c     = conn.cursor()
        c.execute("SELECT SUM(pnl) FROM trades WHERE timestamp LIKE ?", (f"{today}%",))
        row = c.fetchone()
        conn.close()
        return round(row[0] or 0, 2)
    except:
        return 0

# ====================================
# MT5 HELPERS
# ====================================
def ensure_mt5():
    if not mt5.terminal_info():
        print("MT5 disconnected — reconnecting...")
        mt5.shutdown()
        time.sleep(2)
        if not mt5.initialize():
            print("MT5 reconnection failed.")
            return False
    return True

def load_mt5(symbol, timeframe, bars):
    if not ensure_mt5():
        return None
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    return df

def get_balance():
    if not ensure_mt5():
        return 0
    acc = mt5.account_info()
    return round(acc.balance, 2) if acc else 0

def get_open_trade_count():
    if not ensure_mt5():
        return 0
    positions = mt5.positions_get(symbol=SYMBOL)
    return len(positions) if positions else 0

# ====================================
# TREND FILTER — M15 + H4 EMA
# ====================================
def get_trend(timeframe):
    df = load_mt5(SYMBOL, timeframe, 220)
    if df is None or len(df) < 200:
        return "neutral"
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    last = df.iloc[-1]
    if last["ema50"] > last["ema200"]:
        return "bullish"
    elif last["ema50"] < last["ema200"]:
        return "bearish"
    return "neutral"

def trend_allows(side, ifvg_count):
    """
    Normal mode : both M15 and H4 must agree with trade direction.
    Momentum mode: if ifvg_count >= MOMENTUM_IFVG_THRESHOLD, M15 filter
                   is skipped — only H4 must agree. High IFVG counts signal
                   a blow-off top/bottom where M15 may already be reversing.
    Returns (allowed: bool, reason: str)
    """
    m15 = get_trend(TIMEFRAME_M15)
    h4  = get_trend(TIMEFRAME_H4)

    momentum = ifvg_count >= MOMENTUM_IFVG_THRESHOLD

    if side == "BUY":
        h4_ok  = h4  == "bullish"
        m15_ok = m15 == "bullish"
        if momentum:
            allowed = h4_ok   # M15 bypassed
            reason  = f"MOMENTUM ({ifvg_count} IFVGs) — M15 bypassed, H4={h4.upper()}"
        else:
            allowed = h4_ok and m15_ok
            reason  = f"NORMAL — M15={m15.upper()} H4={h4.upper()}"

    elif side == "SELL":
        h4_ok  = h4  == "bearish"
        m15_ok = m15 == "bearish"
        if momentum:
            allowed = h4_ok
            reason  = f"MOMENTUM ({ifvg_count} IFVGs) — M15 bypassed, H4={h4.upper()}"
        else:
            allowed = h4_ok and m15_ok
            reason  = f"NORMAL — M15={m15.upper()} H4={h4.upper()}"
    else:
        allowed = False
        reason  = "unknown side"

    return allowed, reason, m15, h4

# ====================================
# NEWS FILTER
# ====================================
_news_cache        = []
_news_cache_time   = 0
_news_last_attempt = 0
_news_backoff      = NEWS_RETRY_INTERVAL

NEWS_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]

def _parse_news_json(data):
    events = []
    for e in data:
        if e.get("impact") == "High":
            try:
                dt = datetime.strptime(
                    e["date"] + " " + e["time"], "%Y-%m-%d %I:%M%p")
                events.append(dt)
            except:
                pass
    return events

def fetch_news_events():
    global _news_cache, _news_cache_time, _news_last_attempt, _news_backoff
    now = time.time()
    if _news_cache_time > 0 and (now - _news_cache_time < 3600):
        return _news_cache
    if now - _news_last_attempt < _news_backoff:
        return _news_cache
    _news_last_attempt = now
    for url in NEWS_URLS:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 429:
                _news_backoff = 3600
                print("News fetch: rate-limited (429) — backing off for 1 hour.")
                return _news_cache
            r.raise_for_status()
            if not r.text.strip():
                print(f"News fetch: empty response from {url}, trying next...")
                continue
            data   = r.json()
            events = _parse_news_json(data)
            _news_cache      = events
            _news_cache_time = now
            _news_backoff    = NEWS_RETRY_INTERVAL
            print(f"News fetch: OK — {len(events)} high-impact events loaded.")
            return _news_cache
        except requests.exceptions.ConnectionError:
            print("News fetch: no internet — using cached data.")
            return _news_cache
        except requests.exceptions.Timeout:
            print(f"News fetch: timeout on {url}, trying next...")
            continue
        except Exception as ex:
            print(f"News fetch: error on {url}: {ex}, trying next...")
            continue
    print("News fetch: all sources failed — skipping news filter this cycle.")
    return _news_cache

def is_news_window():
    events = fetch_news_events()
    if not events:
        return False
    now = datetime.utcnow()
    buf = timedelta(minutes=NEWS_BUFFER_MIN)
    for ev in events:
        if abs(now - ev) <= buf:
            return True
    return False

# ====================================
# DAILY LOSS GUARD
# ====================================
def check_daily_loss():
    with state_lock:
        if shared["daily_loss_hit"]:
            return True
        if shared["daily_start_bal"] is None:
            shared["daily_start_bal"] = get_balance()
            return False
    balance  = get_balance()
    start    = shared["daily_start_bal"]
    loss_pct = (start - balance) / start if start > 0 else 0
    if loss_pct >= DAILY_LOSS_LIMIT:
        with state_lock:
            shared["daily_loss_hit"] = True
        admin_only(
            f"🚨 DAILY LOSS LIMIT HIT\n"
            f"Start Balance : {start}\n"
            f"Now           : {balance}\n"
            f"Loss          : {round(loss_pct*100,2)}%\n"
            f"Bot paused for today.")
        return True
    return False

def reset_daily_state():
    with state_lock:
        shared["daily_start_bal"] = get_balance()
        shared["daily_loss_hit"]  = False
    print("Daily state reset.")

# ====================================
# WIN RATE GUARD
# ====================================
def check_win_rate():
    stats = get_stats()
    if stats and stats["total"] >= 10:
        if stats["win_rate"] < WIN_RATE_THRESHOLD:
            with state_lock:
                shared["paused"] = True
            admin_only(
                f"⚠️ Win rate dropped to {round(stats['win_rate']*100,1)}%\n"
                f"Threshold : {int(WIN_RATE_THRESHOLD*100)}%\n"
                f"Bot auto-paused. Use /resume to restart.")

# ====================================
# FVG / IFVG DETECTION
# ====================================
def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        c1 = df.iloc[i-2]
        c3 = df.iloc[i]
        if c1.high < c3.low:
            fvgs.append({"type":"bullish","low":c1.high,"high":c3.low,"index":i})
        if c1.low > c3.high:
            fvgs.append({"type":"bearish","low":c3.high,"high":c1.low,"index":i})
    return fvgs

def get_fresh_fvg_count(df, fvgs, lookback=50):
    """
    Count fresh unmitigated FVGs — used for momentum detection only.
    A FVG is fresh if:
      - It formed within the last `lookback` candles
      - Price has NOT fully closed through it yet (unmitigated)
    This is intentionally separate from IFVG signal logic.
    """
    recent        = [g for g in fvgs if g["index"] >= len(df) - lookback]
    current_price = df.iloc[-1].close
    fresh = []
    for g in recent:
        if g["type"] == "bullish" and current_price < g["low"]:
            continue   # fully mitigated — price closed below gap
        if g["type"] == "bearish" and current_price > g["high"]:
            continue   # fully mitigated — price closed above gap
        fresh.append(g)
    return len(fresh)

def detect_ifvg(df, fvgs):
    ifvgs = []
    for gap in fvgs:
        if (gap["high"] - gap["low"]) < MIN_IFVG_GAP:
            continue
        for i in range(gap["index"], len(df)):
            price = df.iloc[i].close
            if gap["type"] == "bullish" and price < gap["low"]:
                ifvgs.append({"type":"bearish_ifvg","low":gap["low"],"high":gap["high"]})
                break
            if gap["type"] == "bearish" and price > gap["high"]:
                ifvgs.append({"type":"bullish_ifvg","low":gap["low"],"high":gap["high"]})
                break
    return ifvgs

# ====================================
# SIGNAL LOGIC
# ====================================
def trade_signal(fvgs, ifvgs):
    if not ifvgs:
        return "WAIT", 0, 0, 0
    gap        = ifvgs[-1]
    entry      = (gap["low"] + gap["high"]) / 2
    risk_range = abs(gap["high"] - gap["low"])
    if risk_range < MIN_IFVG_GAP:
        risk_range = MIN_IFVG_GAP
    if "bearish" in gap["type"]:
        return "SELL", entry, entry - (risk_range * 1.5), entry + risk_range
    else:
        return "BUY",  entry, entry + (risk_range * 1.5), entry - risk_range

# ====================================
# LOT SIZE
# ====================================
def lot_size(entry, sl):
    if entry == 0 or sl == 0 or entry == sl:
        return 0.0
    balance       = get_balance()
    stop_distance = abs(entry - sl)
    lot = (balance * RISK_PERCENT) / (stop_distance * CONTRACT_SIZE)
    lot = max(0.1, min(1.0, lot))
    return round(lot, 2)

# ====================================
# FILLING MODE + PRICE NORMALIZATION
# ====================================
def get_filling_mode(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm == 0:
        return mt5.ORDER_FILLING_RETURN
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_IOC

def normalize_price(symbol, price):
    info = mt5.symbol_info(symbol)
    digits    = info.digits          if info and info.digits > 0          else 2
    tick_size = info.trade_tick_size if info and info.trade_tick_size > 0 else 0.01
    multiplier = round(1.0 / tick_size)
    return round(round(price * multiplier) / multiplier, digits)

# ====================================
# EXECUTE TRADE
# ====================================
def execute_trade(action, symbol, lot, entry_price, sl, tp):
    if not ensure_mt5():
        admin_only("❌ IFVG Trade Failed\nReason: MT5 not connected")
        return None
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        admin_only("❌ IFVG Trade Failed\nReason: No tick data")
        return None
    spread = tick.ask - tick.bid
    print(f"execute_trade: spread={round(spread,2)} ask={tick.ask} bid={tick.bid}")
    if spread > 2:
        admin_only(f"❌ IFVG Trade Skipped\nReason: Spread too high ({round(spread,2)})")
        return None

    filling = get_filling_mode(symbol)

    if action == "BUY":
        if tick.ask <= entry_price:
            order_type   = mt5.ORDER_TYPE_BUY_LIMIT
            trade_action = mt5.TRADE_ACTION_PENDING
            use_price    = entry_price
        else:
            order_type   = mt5.ORDER_TYPE_BUY
            trade_action = mt5.TRADE_ACTION_DEAL
            use_price    = tick.ask
    else:
        if tick.bid >= entry_price:
            order_type   = mt5.ORDER_TYPE_SELL_LIMIT
            trade_action = mt5.TRADE_ACTION_PENDING
            use_price    = entry_price
        else:
            order_type   = mt5.ORDER_TYPE_SELL
            trade_action = mt5.TRADE_ACTION_DEAL
            use_price    = tick.bid

    order_label = "MARKET" if trade_action == mt5.TRADE_ACTION_DEAL else "PENDING"
    use_price = normalize_price(symbol, use_price)
    sl        = normalize_price(symbol, sl)
    tp        = normalize_price(symbol, tp)

    print(f"execute_trade: {order_label} {action} | price={use_price} | sl={sl} | tp={tp} | lot={lot} | filling={filling}")

    request = {
        "action":       trade_action,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        use_price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        101010,
        "comment":      "TG Approved",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    result = mt5.order_send(request)
    if result is None:
        err = mt5.last_error()
        print(f"execute_trade: order_send=None. MT5 error: {err}")
        admin_only(f"❌ IFVG Trade Failed\nReason: order_send=None\nMT5 error: {err}")
        return None

    print(f"execute_trade: retcode={result.retcode} | comment='{result.comment}' | order={result.order}")

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        with state_lock:
            shared["active_tickets"].append(result.order)
        print(f"execute_trade: ✅ {order_label} order placed. Ticket={result.order}")
        admin_only(
            f"✅ IFVG Trade Placed ({order_label})\n"
            f"Ticket  : {result.order}\n"
            f"Side    : {action}\n"
            f"Price   : {use_price}\n"
            f"SL      : {sl}\n"
            f"TP      : {tp}\n"
            f"Lot     : {lot}")
    else:
        print(f"execute_trade: ❌ Failed. retcode={result.retcode} | {result.comment}")
        admin_only(
            f"❌ IFVG Trade Failed\n"
            f"retcode : {result.retcode}\n"
            f"reason  : {result.comment}\n"
            f"action  : {action} {lot} lots @ {use_price}")
    return result

# ====================================
# BREAKEVEN + TRAILING STOP
# ====================================
def modify_sl(ticket, new_sl):
    positions = mt5.positions_get()
    if not positions:
        return
    for pos in positions:
        if pos.ticket != ticket:
            continue
        mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       new_sl,
            "tp":       pos.tp,
        })
        return

def manage_open_trades():
    if not ensure_mt5():
        return
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return
    for pos in positions:
        ticket     = pos.ticket
        open_price = pos.price_open
        current_sl = pos.sl
        tp         = pos.tp
        tick       = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            continue
        if pos.type == mt5.ORDER_TYPE_BUY:
            current_price = tick.bid
            tp_distance   = tp - open_price if tp > 0 else 0
            if tp_distance <= 0 or current_sl <= 0:
                continue

            progress = (current_price - open_price) / tp_distance
            new_sl = current_sl
            locked_msg = ""
            
            # Step 3: Lock 50% profit if 75% to TP
            if progress >= 0.75:
                proposed_sl = open_price + (tp_distance * 0.50)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit"
            # Step 2: Lock 35% profit if 50% to TP
            elif progress >= 0.50:
                proposed_sl = open_price + (tp_distance * 0.35)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit"
            # Step 1: Move to Break Even if 25% to TP
            elif progress >= 0.25:
                proposed_sl = open_price
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "breakeven"

            new_sl = normalize_price(SYMBOL, new_sl)
            if new_sl > normalize_price(SYMBOL, current_sl):
                modify_sl(ticket, new_sl)
                if locked_msg:
                    admin_only(f"🔒 BUY #{ticket}: SL moved to lock {locked_msg}")

        elif pos.type == mt5.ORDER_TYPE_SELL:
            current_price = tick.ask
            tp_distance   = open_price - tp if tp > 0 else 0
            if tp_distance <= 0 or current_sl <= 0:
                continue

            progress = (open_price - current_price) / tp_distance
            new_sl = current_sl
            locked_msg = ""
            
            # Step 3: Lock 50% profit if 75% to TP
            if progress >= 0.75:
                proposed_sl = open_price - (tp_distance * 0.50)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit"
            # Step 2: Lock 35% profit if 50% to TP
            elif progress >= 0.50:
                proposed_sl = open_price - (tp_distance * 0.35)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit"
            # Step 1: Move to Break Even if 25% to TP
            elif progress >= 0.25:
                proposed_sl = open_price
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "breakeven"

            new_sl = normalize_price(SYMBOL, new_sl)
            if new_sl < normalize_price(SYMBOL, current_sl):
                modify_sl(ticket, new_sl)
                if locked_msg:
                    admin_only(f"🔒 SELL #{ticket}: SL moved to lock {locked_msg}")

# ====================================
# TRADE MONITOR
# ====================================
def trade_monitor():
    print("Trade Monitor Started")
    while True:
        try:
            # Always manage open trades so that we can pick up any existing/untracked positions
            manage_open_trades()

            with state_lock:
                tickets = list(shared["active_tickets"])

            if tickets:
                open_positions = mt5.positions_get(symbol=SYMBOL)
                open_tickets   = {p.ticket for p in open_positions} if open_positions else set()
                for ticket in tickets:
                    if ticket not in open_tickets:
                        deals = mt5.history_deals_get(time.time() - 3600, time.time())
                        pnl   = 0
                        if deals:
                            for d in reversed(deals):
                                if d.order == ticket or d.position_id == ticket:
                                    pnl = round(d.profit, 2)
                                    break
                        result  = "WIN" if pnl > 0 else "LOSS"
                        balance = get_balance()
                        log_trade_db(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result)
                        log_trade_csv(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result)
                        emoji = "🎯" if pnl > 0 else "🛑"
                        label = "TP HIT" if pnl > 0 else "SL HIT"
                        broadcast(
                            f"{emoji} {label}\n"
                            f"Ticket  : {ticket}\n"
                            f"PnL     : {pnl}\n"
                            f"Balance : {balance}")
                        with state_lock:
                            if ticket in shared["active_tickets"]:
                                shared["active_tickets"].remove(ticket)
                            shared["breakeven_tickets"].discard(ticket)
                        check_win_rate()
        except Exception as e:
            print("Monitor error:", e)
        time.sleep(5)

# ====================================
# TELEGRAM COMMANDS
# ====================================
@bot.message_handler(commands=["start"])
def start_command(message):
    save_user(message.chat.id)
    bot.reply_to(message,
        "✅ Welcome to Akshath XAU Bot!\n"
        "You are now subscribed to live IFVG signals.\n\n"
        "Commands:\n"
        "/status — view open trades\n"
        "/stats  — win rate & P&L\n"
        "/debug  — diagnose why no signals (admin)\n"
        "/pause  — pause scanning (admin)\n"
        "/resume — resume scanning (admin)")

@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    reset_daily_state()
    with state_lock:
        if "pending_signal" in shared:
            shared["pending_signal"].clear()
    bot.reply_to(message, "🔄 Bot daily reset triggered manually.\nSignal state cleared.\nTrading fresh!")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    if not ensure_mt5():
        bot.reply_to(message, "❌ MT5 not connected")
        return
    positions = mt5.positions_get(symbol=SYMBOL)
    balance   = get_balance()
    with state_lock:
        paused = shared["paused"]
    status_str = "⏸ PAUSED" if paused else "🟢 ACTIVE"
    if not positions:
        bot.reply_to(message,
            f"📊 Status: {status_str}\nNo open trades\nBalance: {balance}")
        return
    lines = [f"📊 Status: {status_str} | Balance: {balance}\n"]
    for p in positions:
        side   = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        profit = round(p.profit, 2)
        emoji  = "🟢" if profit >= 0 else "🔴"
        lines.append(
            f"{emoji} {side} #{p.ticket}\n"
            f"  Entry: {p.price_open}  SL: {p.sl}  TP: {p.tp}\n"
            f"  PnL  : {profit}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    stats = get_stats()
    if not stats:
        bot.reply_to(message, "📈 No closed trades logged yet.")
        return
    today_pnl = get_today_pnl()
    bot.reply_to(message,
        f"📈 Trade Statistics\n\n"
        f"Total Trades : {stats['total']}\n"
        f"Wins         : {stats['wins']}\n"
        f"Losses       : {stats['losses']}\n"
        f"Win Rate     : {round(stats['win_rate']*100,1)}%\n"
        f"Total P&L    : {stats['total_pnl']}\n"
        f"Best Trade   : +{stats['best']}\n"
        f"Worst Trade  : {stats['worst']}\n"
        f"Today's P&L  : {today_pnl}")

@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    with state_lock:
        shared["paused"] = True
    bot.reply_to(message, "⏸ Bot paused. Use /resume to restart scanning.")

@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    with state_lock:
        shared["paused"]         = False
        shared["daily_loss_hit"] = False
    bot.reply_to(message, "▶️ Bot resumed. Scanning for signals.")

@bot.message_handler(commands=["buy"])
def cmd_buy(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    bot.reply_to(message, "⚙️ Executing manual BUY (0.1 lot)...")
    res = execute_trade("BUY", SYMBOL, 0.1, 0, 0, 0)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade_db(res.order, SYMBOL, "BUY", res.price, 0, 0, 0.1, 0, "OPEN")
        log_trade_csv(res.order, SYMBOL, "BUY", res.price, 0, 0, 0.1, 0, "OPEN")
        bot.send_message(CHAT_ID, f"✅ Manual BUY filled @ ticket {res.order}")
    else:
        err_msg = res.comment if res else "Unknown error"
        bot.send_message(CHAT_ID, f"❌ Manual BUY failed: {err_msg}")

@bot.message_handler(commands=["sell"])
def cmd_sell(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    bot.reply_to(message, "⚙️ Executing manual SELL (0.1 lot)...")
    res = execute_trade("SELL", SYMBOL, 0.1, 999999, 0, 0)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade_db(res.order, SYMBOL, "SELL", res.price, 0, 0, 0.1, 0, "OPEN")
        log_trade_csv(res.order, SYMBOL, "SELL", res.price, 0, 0, 0.1, 0, "OPEN")
        bot.send_message(CHAT_ID, f"✅ Manual SELL filled @ ticket {res.order}")
    else:
        err_msg = res.comment if res else "Unknown error"
        bot.send_message(CHAT_ID, f"❌ Manual SELL failed: {err_msg}")

@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only command.")
        return
    bot.reply_to(message, "🔍 Running diagnostics, please wait...")
    try:
        diag = get_scan_diagnostics()
        bot.send_message(CHAT_ID, f"🔍 Scan Diagnostics\n\n{diag}")
    except Exception as e:
        bot.send_message(CHAT_ID, f"❌ Diagnostics error: {e}")

# ====================================
# CALLBACKS
# ====================================
@bot.callback_query_handler(func=lambda call: call.data == "approve_trade")
def handle_approval(call):
    if str(call.message.chat.id) != str(CHAT_ID):
        bot.answer_callback_query(call.id, "⛔ Admin only.")
        return
    with state_lock:
        signal = dict(shared["pending_signal"])
    if not signal:
        bot.answer_callback_query(call.id, "No active signal")
        return
    res = execute_trade(
        signal["side"], SYMBOL, signal["lot"],
        signal["entry"], signal["sl"], signal["tp"])
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade_db(res.order, SYMBOL, signal["side"],
                     signal["entry"], signal["sl"], signal["tp"],
                     signal["lot"], 0, "OPEN")
        log_trade_csv(res.order, SYMBOL, signal["side"],
                      signal["entry"], signal["sl"], signal["tp"],
                      signal["lot"], 0, "OPEN")
    else:
        error_code = res.retcode if res else "Unknown"
        admin_only(f"❌ Trade failed\nError: {error_code}")
    with state_lock:
        shared["pending_signal"].clear()

@bot.callback_query_handler(func=lambda call: call.data == "deny_trade")
def handle_deny(call):
    if str(call.message.chat.id) != str(CHAT_ID):
        bot.answer_callback_query(call.id, "⛔ Admin only.")
        return
    with state_lock:
        shared["pending_signal"].clear()
        shared["last_entry"] = None
    bot.answer_callback_query(call.id, "Signal denied")
    admin_only("❌ Signal rejected. Bot scanning again.")

# ====================================
# HEARTBEAT
# ====================================
def heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            balance = get_balance()
            with state_lock:
                paused = shared["paused"]
                n_open = len(shared["active_tickets"])
            status = "⏸ PAUSED" if paused else "🟢 ACTIVE"
            admin_only(
                f"💓 Heartbeat\n"
                f"Status  : {status}\n"
                f"Balance : {balance}\n"
                f"Open    : {n_open} trade(s)")
        except Exception as e:
            print("Heartbeat error:", e)

# ====================================
# DAILY SUMMARY
# ====================================
def daily_summary():
    last_sent = None
    while True:
        now = datetime.now()
        if now.hour == DAILY_SUMMARY_HOUR and last_sent != now.date():
            try:
                stats     = get_stats()
                today_pnl = get_today_pnl()
                balance   = get_balance()
                last_sent = now.date()
                if stats:
                    admin_only(
                        f"📅 Daily Summary — {now.strftime('%Y-%m-%d')}\n\n"
                        f"Today's P&L  : {today_pnl}\n"
                        f"Balance      : {balance}\n"
                        f"Total Trades : {stats['total']}\n"
                        f"Win Rate     : {round(stats['win_rate']*100,1)}%\n"
                        f"All-time P&L : {stats['total_pnl']}")
                reset_daily_state()
            except Exception as e:
                print("Daily summary error:", e)
        time.sleep(60)

# ====================================
# DIAGNOSTICS
# ====================================
def get_scan_diagnostics():
    lines = []
    with state_lock:
        paused   = shared["paused"]
        loss_hit = shared["daily_loss_hit"]
        pending  = dict(shared["pending_signal"])
        last_ent = shared["last_entry"]

    lines.append(f"Paused        : {'YES ⏸' if paused else 'NO'}")
    lines.append(f"Daily loss hit: {'YES 🚨' if loss_hit else 'NO'}")
    lines.append(f"Open trades   : {get_open_trade_count()} / {MAX_TRADES}")

    events = fetch_news_events()
    in_news = is_news_window()
    lines.append(f"News events   : {len(events)} | In window: {'YES 📰' if in_news else 'NO'}")

    m15 = get_trend(TIMEFRAME_M15)
    h4  = get_trend(TIMEFRAME_H4)
    lines.append(f"Trend M15     : {m15.upper()}")
    lines.append(f"Trend H4      : {h4.upper()}")

    df = load_mt5(SYMBOL, TIMEFRAME_M1, 200)
    if df is not None:
        fvgs           = detect_fvg(df)
        ifvgs          = detect_ifvg(df, fvgs)
        fresh_fvg_count = get_fresh_fvg_count(df, fvgs, lookback=50)
        side, entry, tp, sl = trade_signal(fvgs, ifvgs)
        momentum = fresh_fvg_count >= MOMENTUM_IFVG_THRESHOLD
        lines.append(f"Raw FVGs      : {len(fvgs)}")
        lines.append(f"Fresh FVGs    : {fresh_fvg_count} (unmitigated, last 50 candles)")
        lines.append(f"Valid IFVGs   : {len(ifvgs)} (gap ≥ {MIN_IFVG_GAP} pts)")
        lines.append(f"Momentum mode : {'YES 🚀 (M15 bypassed)' if momentum else f'NO (need {MOMENTUM_IFVG_THRESHOLD} fresh FVGs)'}")
        lines.append(f"Signal        : {side} @ {round(entry,2) if entry else 'none'}")
        if side in ("BUY", "SELL"):
            allowed, reason, _, _ = trend_allows(side, fresh_fvg_count)
            lines.append(f"Filter result : {'✅ ALLOWED' if allowed else '❌ BLOCKED'} — {reason}")
    else:
        lines.append("MT5 data      : unavailable")

    if pending:
        lines.append(f"Pending signal: {pending.get('side')} @ {pending.get('entry')}")
    else:
        lines.append(f"Pending signal: none")

    return "\n".join(lines)

# ====================================
# MARKET SCANNER
# ====================================
def market_scanner():
    print("Market Scanner Started")
    broadcast("🤖 Akshath XAU Bot Online & Scanning XAUUSD")

    scan_count = 0

    while True:
        try:
            scan_count += 1
            with state_lock:
                paused   = shared["paused"]
                pending  = dict(shared["pending_signal"])
                sig_time = shared["signal_time"]
                last_ent = shared["last_entry"]

            if paused or check_daily_loss():
                time.sleep(SCAN_INTERVAL)
                continue

            # Expire stale pending signal
            if pending and sig_time and (time.time() - sig_time > SIGNAL_TIMEOUT):
                with state_lock:
                    shared["pending_signal"].clear()
                    shared["last_entry"] = None
                print(f"[Scan #{scan_count}] Pending signal expired — last_entry reset.")

            # Max trades cap
            open_count = get_open_trade_count()
            if open_count >= MAX_TRADES:
                print(f"[Scan #{scan_count}] Max trades reached ({open_count}/{MAX_TRADES}) — skipping.")
                time.sleep(SCAN_INTERVAL)
                continue

            # News filter
            if is_news_window():
                print(f"[Scan #{scan_count}] News window active — skipping.")
                time.sleep(SCAN_INTERVAL)
                continue

            # Load M1 data and detect IFVGs
            df = load_mt5(SYMBOL, TIMEFRAME_M1, 200)
            if df is None:
                print(f"[Scan #{scan_count}] MT5 data unavailable.")
                time.sleep(10)
                continue

            fvgs  = detect_fvg(df)
            ifvgs = detect_ifvg(df, fvgs)
            side, entry, tp, sl = trade_signal(fvgs, ifvgs)

            # Fresh unmitigated FVG count — used for momentum detection only
            # Trade signal (entry/SL/TP) still uses IFVGs as before
            fresh_fvg_count = get_fresh_fvg_count(df, fvgs, lookback=50)

            print(f"[Scan #{scan_count}] FVGs={len(fvgs)} | Fresh FVGs={fresh_fvg_count} | IFVGs={len(ifvgs)} | Signal={side} @ {round(entry,2) if entry else 0}")

            if side not in ("BUY", "SELL"):
                time.sleep(SCAN_INTERVAL)
                continue

            if entry == last_ent and pending:
                print(f"[Scan #{scan_count}] Signal already pending @ {entry} — waiting for approval.")
                time.sleep(SCAN_INTERVAL)
                continue

            # Trend filter with momentum override (uses fresh FVG count)
            allowed, reason, m15, h4 = trend_allows(side, fresh_fvg_count)
            print(f"[Scan #{scan_count}] Trend check: {reason} → {'ALLOWED ✅' if allowed else 'BLOCKED ❌'}")

            if not allowed:
                time.sleep(SCAN_INTERVAL)
                continue

            lot = lot_size(entry, sl)

            with state_lock:
                shared["pending_signal"] = {
                    "side":  side,
                    "entry": round(entry, 2),
                    "tp":    round(tp, 2),
                    "sl":    round(sl, 2),
                    "lot":   lot,
                }
                shared["signal_time"] = time.time()
                shared["last_entry"]  = entry

            momentum     = fresh_fvg_count >= MOMENTUM_IFVG_THRESHOLD
            mode_label   = f"🚀 MOMENTUM ({fresh_fvg_count} fresh FVGs — M15 bypassed)" if momentum else "📊 NORMAL"
            risk_pts     = round(abs(entry - sl), 2)
            reward_pts   = round(abs(tp - entry), 2)

            signal_msg = (
                f"🚨 XAUUSD IFVG SIGNAL\n\n"
                f"Side       : {side}\n"
                f"Entry      : {round(entry,2)}\n"
                f"TP         : {round(tp,2)}  (+{reward_pts} pts)\n"
                f"SL         : {round(sl,2)}  (-{risk_pts} pts)\n"
                f"Lot        : {lot}\n\n"
                f"Mode       : {mode_label}\n"
                f"Trend M15  : {m15.upper()}\n"
                f"Trend H4   : {h4.upper()}\n"
                f"Balance    : {get_balance()}\n"
            )

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(f"🟢 APPROVE {side} ({lot})", callback_data="approve_trade"),
                InlineKeyboardButton("❌ DENY", callback_data="deny_trade")
            )
            admin_only(signal_msg, reply_markup=markup)

            for uid in get_all_users():
                if str(uid) != str(CHAT_ID):
                    try:
                        bot.send_message(uid, signal_msg)
                    except Exception as e:
                        print(f"Subscriber send error {uid}: {e}")

            print(f"[Scan #{scan_count}] ✅ Signal sent: {side} @ {entry} | Mode: {'MOMENTUM' if momentum else 'NORMAL'} | Fresh FVGs={fresh_fvg_count}")

        except Exception as e:
            print(f"[Scan #{scan_count}] Scanner error: {e}")

        time.sleep(SCAN_INTERVAL)

# ====================================
# MAIN
# ====================================
if __name__ == "__main__":
    init_db()
    reset_daily_state()

    if not mt5.initialize():
        print("MT5 init failed — ensure your terminal is open and logged in.")
    else:
        print("MT5 connected.")

    threading.Thread(target=market_scanner, daemon=True).start()
    threading.Thread(target=trade_monitor,  daemon=True).start()
    threading.Thread(target=heartbeat,      daemon=True).start()
    threading.Thread(target=daily_summary,  daemon=True).start()

    print("Telegram Bot Listening...")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling dropped ({type(e).__name__}): {e} — reconnecting in 15s...")
            time.sleep(15)