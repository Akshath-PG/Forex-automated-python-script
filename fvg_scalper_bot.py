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

logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

# ====================================
# CONFIGURATION
# ====================================
TELEGRAM_TOKEN = "8699832560:AAEQ9is_E5WOJCfu-lvzQCPPYjcUWZxNRzg"
CHAT_ID        = "5667584601"

SYMBOL         = "XAUUSD"
TIMEFRAME_M5   = mt5.TIMEFRAME_M5
TIMEFRAME_H1   = mt5.TIMEFRAME_H1

RISK_PERCENT        = 0.5 / 100
CONTRACT_SIZE       = 100
RR_RATIO            = 2.0         # ← upgraded from 1.5 to 2.0 (momentum filter earns it)
SCAN_INTERVAL       = 30
SIGNAL_TIMEOUT      = 300

MIN_HOLD_SECONDS    = 180
DAILY_LOSS_LIMIT    = 0.02
MAX_TRADES          = 3
MIN_FVG_GAP         = 12.0        # ← upgraded from 8.0 (stronger institutional gaps only)
FVG_LOOKBACK        = 20          # ← tightened from 50 (gaps older than 20 M5 candles = ~1.5hrs are irrelevant)
MOMENTUM_FVG_COUNT  = 3           # ← fresh FVGs needed to override H1 trend block

BREAKEVEN_PCT       = 0.50
TRAIL_RISK_PCT      = 0.50
WIN_RATE_THRESHOLD  = 0.60
HEARTBEAT_INTERVAL  = 3600
NEWS_BUFFER_MIN     = 30
NEWS_RETRY_INTERVAL = 300
DAILY_SUMMARY_HOUR  = 22

CSV_FILE   = "fvg_trade_log.csv"
DB_FILE    = "fvg_trade_log.db"
USERS_FILE = "fvg_users.txt"

NEWS_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]

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
    "trade_open_times":  {},
    "trade_targets":     {},
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

def get_all_users():
    try:
        with open(USERS_FILE, "r") as f:
            return [u.strip() for u in f.read().splitlines() if u.strip()]
    except FileNotFoundError:
        return []

def broadcast(text, reply_markup=None):
    for uid in get_all_users():
        try:
            bot.send_message(uid, text, reply_markup=reply_markup) if reply_markup \
                else bot.send_message(uid, text)
        except Exception as e:
            print(f"Broadcast error {uid}: {e}")

def admin_only(text, reply_markup=None):
    try:
        bot.send_message(CHAT_ID, text, reply_markup=reply_markup) if reply_markup \
            else bot.send_message(CHAT_ID, text)
    except Exception as e:
        print(f"Admin msg error: {e}")

# ====================================
# DATABASE
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
            writer.writerow([ticket, symbol, side, entry, sl, tp, lot, pnl,
                             result, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
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
# TREND FILTER — H1 EMA50 vs EMA200
# ====================================
def get_h1_trend():
    df = load_mt5(SYMBOL, TIMEFRAME_H1, 220)
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

def trend_allows(side):
    """Standard H1 trend check — used as baseline before momentum override."""
    trend = get_h1_trend()
    if side == "BUY":
        return trend == "bullish"
    if side == "SELL":
        return trend == "bearish"
    return False

# ====================================
# NEWS FILTER
# ====================================
_news_cache        = []
_news_cache_time   = 0
_news_last_attempt = 0
_news_backoff      = NEWS_RETRY_INTERVAL

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
                print("News fetch: rate-limited (429) — backing off 1 hour.")
                return _news_cache
            r.raise_for_status()
            if not r.text.strip():
                continue
            events = []
            for e in r.json():
                if e.get("impact") == "High":
                    try:
                        dt = datetime.strptime(
                            e["date"] + " " + e["time"], "%Y-%m-%d %I:%M%p")
                        events.append(dt)
                    except:
                        pass
            _news_cache      = events
            _news_cache_time = now
            _news_backoff    = NEWS_RETRY_INTERVAL
            print(f"News fetch: OK — {len(events)} high-impact events.")
            return _news_cache
        except requests.exceptions.ConnectionError:
            print("News fetch: no internet — using cached data.")
            return _news_cache
        except requests.exceptions.Timeout:
            continue
        except Exception as ex:
            print(f"News fetch error ({url}): {ex}")
            continue
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
            f"Start : {start}\nNow   : {balance}\n"
            f"Loss  : {round(loss_pct*100,2)}%\nBot paused for today.")
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
                f"⚠️ Win rate {round(stats['win_rate']*100,1)}% below "
                f"{int(WIN_RATE_THRESHOLD*100)}% threshold.\n"
                f"Bot auto-paused. Use /resume to restart.")

# ====================================
# FVG DETECTION
# ====================================
def detect_fvg(df):
    """
    Detect all Fair Value Gaps on the dataframe.
    Bullish FVG : candle[i-2].high < candle[i].low  → gap above (expect buy)
    Bearish FVG : candle[i-2].low  > candle[i].high → gap below (expect sell)
    Only gaps >= MIN_FVG_GAP are kept.
    """
    fvgs = []
    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]
        if c1.high < c3.low:
            gap_size = c3.low - c1.high
            if gap_size >= MIN_FVG_GAP:
                fvgs.append({
                    "type":      "bullish",
                    "low":       c1.high,
                    "high":      c3.low,
                    "index":     i,
                    "formed_at": df.index[i],
                })
        if c1.low > c3.high:
            gap_size = c1.low - c3.high
            if gap_size >= MIN_FVG_GAP:
                fvgs.append({
                    "type":      "bearish",
                    "low":       c3.high,
                    "high":      c1.low,
                    "index":     i,
                    "formed_at": df.index[i],
                })
    return fvgs

def get_fresh_fvgs(df, lookback=None):
    """
    Return fresh unmitigated FVGs within the last `lookback` candles.
    Uses FVG_LOOKBACK from config if lookback not specified.
    Filters out any gap that price has already fully closed through.
    """
    if lookback is None:
        lookback = FVG_LOOKBACK
    all_fvgs      = detect_fvg(df)
    recent        = [g for g in all_fvgs if g["index"] >= len(df) - lookback]
    current_price = df.iloc[-1].close
    fresh = []
    for g in recent:
        if g["type"] == "bullish" and current_price < g["low"]:
            continue   # fully mitigated
        if g["type"] == "bearish" and current_price > g["high"]:
            continue   # fully mitigated
        fresh.append(g)
    return fresh

# ====================================
# SIGNAL LOGIC
# ====================================
def trade_signal(df):
    """
    Use the most recent fresh unmitigated FVG as the signal.
    Entry  : FVG midpoint
    SL     : far edge of the FVG
    TP     : entry ± risk × RR_RATIO  (currently 2.0R)
    """
    fvgs = get_fresh_fvgs(df)
    if not fvgs:
        return "WAIT", 0, 0, 0, 0

    gap      = fvgs[-1]
    gap_size = gap["high"] - gap["low"]
    entry    = (gap["low"] + gap["high"]) / 2

    if gap["type"] == "bullish":
        sl   = gap["low"]
        risk = entry - sl
        tp   = entry + (risk * RR_RATIO)
    else:
        sl   = gap["high"]
        risk = sl - entry
        tp   = entry - (risk * RR_RATIO)

    entry = round(entry, 2)
    sl    = round(sl,    2)
    tp    = round(tp,    2)

    return ("BUY" if gap["type"] == "bullish" else "SELL"), entry, tp, sl, gap_size

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
    if info is None:
        return round(price, 2)
    digits    = info.digits if info.digits > 0 else 2
    tick_size = info.trade_tick_size if info.trade_tick_size > 0 else 0.01
    multiplier = round(1.0 / tick_size)
    return round(round(price * multiplier) / multiplier, digits)

# ====================================
# EXECUTE TRADE
# ====================================
def execute_trade(action, symbol, lot, entry_price, sl, tp):
    if not ensure_mt5():
        admin_only("❌ FVG Trade Failed\nReason: MT5 not connected")
        return None

    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info:
        print(f"symbol_info: digits={info.digits} tick={info.trade_tick_size} "
              f"filling={info.filling_mode} vol_min={info.volume_min}")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        admin_only("❌ FVG Trade Failed\nReason: No tick data")
        return None

    spread = tick.ask - tick.bid
    print(f"execute_trade: spread={round(spread,2)} ask={tick.ask} bid={tick.bid}")
    if spread > 2:
        admin_only(f"❌ FVG Trade Skipped\nReason: Spread too high ({round(spread,2)})")
        return None

    filling       = get_filling_mode(symbol)
    sym_info      = mt5.symbol_info(symbol)
    min_stop_dist = sym_info.trade_stops_level * sym_info.point if sym_info else 0.10
    min_stop_dist = max(min_stop_dist, 0.10)
    original_risk = abs(entry_price - sl)

    if action == "BUY":
        if tick.ask <= entry_price:
            order_type   = mt5.ORDER_TYPE_BUY_LIMIT
            trade_action = mt5.TRADE_ACTION_PENDING
            use_price    = entry_price
        else:
            order_type   = mt5.ORDER_TYPE_BUY
            trade_action = mt5.TRADE_ACTION_DEAL
            use_price    = tick.ask
            sl = use_price - max(original_risk, min_stop_dist)
            tp = use_price + max(original_risk * RR_RATIO, min_stop_dist * RR_RATIO)
    else:
        if tick.bid >= entry_price:
            order_type   = mt5.ORDER_TYPE_SELL_LIMIT
            trade_action = mt5.TRADE_ACTION_PENDING
            use_price    = entry_price
        else:
            order_type   = mt5.ORDER_TYPE_SELL
            trade_action = mt5.TRADE_ACTION_DEAL
            use_price    = tick.bid
            sl = use_price + max(original_risk, min_stop_dist)
            tp = use_price - max(original_risk * RR_RATIO, min_stop_dist * RR_RATIO)

    order_label = "MARKET" if trade_action == mt5.TRADE_ACTION_DEAL else "PENDING"
    use_price = normalize_price(symbol, use_price)
    sl        = normalize_price(symbol, sl)
    tp        = normalize_price(symbol, tp)

    if action == "BUY":
        if (use_price - sl) < min_stop_dist or (tp - use_price) < min_stop_dist:
            admin_only(f"❌ FVG Trade Aborted\nSL/TP too close\nprice={use_price} sl={sl} tp={tp}")
            return None
    else:
        if (sl - use_price) < min_stop_dist or (use_price - tp) < min_stop_dist:
            admin_only(f"❌ FVG Trade Aborted\nSL/TP too close\nprice={use_price} sl={sl} tp={tp}")
            return None

    print(f"execute_trade: {order_label} {action} | price={use_price} sl={sl} tp={tp} lot={lot}")

    request = {
        "action":       trade_action,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        use_price,
        "sl":           0.0,    # no SL during hold period
        "tp":           0.0,    # no TP during hold period
        "deviation":    20,
        "magic":        202020,
        "comment":      "FVG Approved",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    result = mt5.order_send(request)
    if result is None:
        err = mt5.last_error()
        admin_only(f"❌ FVG Trade Failed\norder_send=None\nMT5 error: {err}")
        return None

    print(f"execute_trade: retcode={result.retcode} comment='{result.comment}' order={result.order}")

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        with state_lock:
            shared["active_tickets"].append(result.order)
            shared["trade_open_times"][result.order] = time.time()
            shared["trade_targets"][result.order]    = {"sl": sl, "tp": tp}
        print(f"execute_trade: ✅ {order_label} Ticket={result.order} | SL/TP held {MIN_HOLD_SECONDS}s")
        admin_only(
            f"✅ FVG Trade Placed ({order_label})\n"
            f"Ticket : {result.order}\n"
            f"Side   : {action}\n"
            f"Price  : {use_price}\n"
            f"SL     : {sl} (after {MIN_HOLD_SECONDS}s)\n"
            f"TP     : {tp} (after {MIN_HOLD_SECONDS}s)\n"
            f"Lot    : {lot}\n"
            f"⏱ Hold timer started — 3 min minimum")
    else:
        admin_only(
            f"❌ FVG Trade Failed\n"
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

def apply_sltp(ticket, sl, tp):
    positions = mt5.positions_get()
    if not positions:
        return False
    for pos in positions:
        if pos.ticket != ticket:
            continue
        res = mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       sl,
            "tp":       tp,
        })
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"apply_sltp: ✅ #{ticket} SL={sl} TP={tp}")
            return True
        print(f"apply_sltp: ❌ #{ticket} retcode={res.retcode if res else 'None'}")
        return False
    return False

def manage_open_trades():
    if not ensure_mt5():
        return
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return

    now = time.time()

    for pos in positions:
        if pos.magic != 202020:
            continue

        ticket     = pos.ticket
        open_price = pos.price_open
        current_sl = pos.sl
        tp         = pos.tp
        tick       = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            continue

        with state_lock:
            open_time = shared["trade_open_times"].get(ticket, None)
            targets   = shared["trade_targets"].get(ticket, None)

        if open_time is None:
            continue

        hold_remaining = max(0, MIN_HOLD_SECONDS - (now - open_time))

        if hold_remaining > 0:
            print(f"[Hold] #{ticket} — {round(hold_remaining)}s remaining, SL/TP suppressed.")
            continue

        # Apply SL/TP after hold period — recalculate from actual open price
        if targets and (current_sl == 0.0 or current_sl is None) and (tp == 0.0 or tp is None):
            sym_info      = mt5.symbol_info(SYMBOL)
            min_stop_dist = sym_info.trade_stops_level * sym_info.point if sym_info else 0.10
            min_stop_dist = max(min_stop_dist, 0.50)
            original_risk = max(
                abs(targets["sl"] - targets["tp"]) / (1 + RR_RATIO),
                min_stop_dist
            )
            if pos.type == mt5.ORDER_TYPE_BUY:
                real_sl = normalize_price(SYMBOL, open_price - original_risk)
                real_tp = normalize_price(SYMBOL, open_price + original_risk * RR_RATIO)
            else:
                real_sl = normalize_price(SYMBOL, open_price + original_risk)
                real_tp = normalize_price(SYMBOL, open_price - original_risk * RR_RATIO)

            if apply_sltp(ticket, real_sl, real_tp):
                admin_only(
                    f"⏱ 3-min hold complete — SL/TP applied\n"
                    f"Ticket : #{ticket}\nSL : {real_sl}\nTP : {real_tp}")
                with state_lock:
                    shared["trade_targets"].pop(ticket, None)
                current_sl = real_sl
                tp         = real_tp
            else:
                continue  # retry next cycle

        # Breakeven + trailing (only after hold period and SL/TP applied)
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
                    admin_only(f"🔒 FVG BUY #{ticket}: SL moved to lock {locked_msg}")

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
                    admin_only(f"🔒 FVG SELL #{ticket}: SL moved to lock {locked_msg}")

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
                            f"{emoji} FVG {label}\n"
                            f"Ticket  : {ticket}\n"
                            f"PnL     : {pnl}\n"
                            f"Balance : {balance}")
                        with state_lock:
                            if ticket in shared["active_tickets"]:
                                shared["active_tickets"].remove(ticket)
                            shared["breakeven_tickets"].discard(ticket)
                            shared["trade_open_times"].pop(ticket, None)
                            shared["trade_targets"].pop(ticket, None)
                        check_win_rate()
        except Exception as e:
            print("Monitor error:", e)
        time.sleep(5)

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

    lines.append(f"Bot           : FVG Scalper (M5)")
    lines.append(f"Paused        : {'YES ⏸' if paused else 'NO'}")
    lines.append(f"Daily loss hit: {'YES 🚨' if loss_hit else 'NO'}")
    lines.append(f"Open trades   : {get_open_trade_count()} / {MAX_TRADES}")
    lines.append(f"News window   : {'YES 📰' if is_news_window() else 'NO'}")

    h1 = get_h1_trend()
    lines.append(f"Trend H1      : {h1.upper()}")

    df = load_mt5(SYMBOL, TIMEFRAME_M5, 100)
    if df is not None:
        fvgs        = detect_fvg(df)
        fresh       = get_fresh_fvgs(df)
        fresh_count = len(fresh)
        side, entry, tp, sl, gap_size = trade_signal(df)

        can_trade    = trend_allows(side) if side in ("BUY","SELL") else False
        override_on  = (not can_trade) and fresh_count >= MOMENTUM_FVG_COUNT
        final_allow  = can_trade or override_on

        lines.append(f"Raw FVGs      : {len(fvgs)}")
        lines.append(f"Fresh FVGs    : {fresh_count} (last {FVG_LOOKBACK} candles, unmitigated)")
        lines.append(f"Signal        : {side} @ {round(entry,2) if entry else 'none'}")

        if side in ("BUY","SELL"):
            if can_trade:
                lines.append(f"Filter        : ✅ H1 agrees — normal pass")
            elif override_on:
                lines.append(f"Filter        : 🚀 H1 disagrees BUT override active ({fresh_count} fresh FVGs ≥ {MOMENTUM_FVG_COUNT})")
            else:
                lines.append(f"Filter        : ❌ BLOCKED — H1={h1}, only {fresh_count} fresh FVGs (need {MOMENTUM_FVG_COUNT})")
    else:
        lines.append("MT5 data      : unavailable")

    if pending:
        lines.append(f"Pending       : {pending.get('side')} @ {pending.get('entry')}")
    else:
        lines.append(f"Pending       : none")

    return "\n".join(lines)

# ====================================
# TELEGRAM COMMANDS
# ====================================
@bot.message_handler(commands=["start"])
def start_command(message):
    save_user(message.chat.id)
    bot.reply_to(message,
        "✅ Welcome to Akshath FVG Scalper Bot!\n"
        "Subscribed to live M5 FVG signals on XAUUSD.\n\n"
        "Commands:\n"
        "/status — open trades\n"
        "/stats  — win rate & P&L\n"
        "/debug  — diagnostics (admin)\n"
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
        bot.reply_to(message, "⛔ Admin only.")
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
        bot.reply_to(message, f"📊 FVG Bot — {status_str}\nNo open trades\nBalance: {balance}")
        return
    lines = [f"📊 FVG Bot — {status_str} | Balance: {balance}\n"]
    for p in positions:
        if p.magic != 202020:
            continue
        side   = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        profit = round(p.profit, 2)
        emoji  = "🟢" if profit >= 0 else "🔴"

        with state_lock:
            open_time = shared["trade_open_times"].get(p.ticket)
        hold_left = max(0, MIN_HOLD_SECONDS - (time.time() - open_time)) if open_time else 0
        hold_str  = f"  ⏱ Hold: {round(hold_left)}s left\n" if hold_left > 0 else ""

        lines.append(
            f"{emoji} {side} #{p.ticket}\n"
            f"  Entry: {p.price_open}  SL: {p.sl}  TP: {p.tp}\n"
            f"  PnL  : {profit}\n"
            f"{hold_str}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    stats = get_stats()
    if not stats:
        bot.reply_to(message, "📈 No closed trades yet.")
        return
    bot.reply_to(message,
        f"📈 FVG Scalper Statistics\n\n"
        f"Total Trades : {stats['total']}\n"
        f"Wins         : {stats['wins']}\n"
        f"Losses       : {stats['losses']}\n"
        f"Win Rate     : {round(stats['win_rate']*100,1)}%\n"
        f"Total P&L    : {stats['total_pnl']}\n"
        f"Best Trade   : +{stats['best']}\n"
        f"Worst Trade  : {stats['worst']}\n"
        f"Today's P&L  : {get_today_pnl()}")

@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    with state_lock:
        shared["paused"] = True
    bot.reply_to(message, "⏸ FVG Bot paused. Use /resume to restart.")

@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    with state_lock:
        shared["paused"]         = False
        shared["daily_loss_hit"] = False
    bot.reply_to(message, "▶️ FVG Bot resumed.")

@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    bot.reply_to(message, "🔍 Running diagnostics...")
    try:
        bot.send_message(CHAT_ID, f"🔍 FVG Bot Diagnostics\n\n{get_scan_diagnostics()}")
    except Exception as e:
        bot.send_message(CHAT_ID, f"❌ Diagnostics error: {e}")

# ====================================
# CALLBACKS
# ====================================
@bot.callback_query_handler(func=lambda call: call.data == "fvg_approve")
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
        admin_only(f"❌ FVG Trade failed\nError: {error_code}")
    with state_lock:
        shared["pending_signal"].clear()

@bot.callback_query_handler(func=lambda call: call.data == "fvg_deny")
def handle_deny(call):
    if str(call.message.chat.id) != str(CHAT_ID):
        bot.answer_callback_query(call.id, "⛔ Admin only.")
        return
    with state_lock:
        shared["pending_signal"].clear()
        shared["last_entry"] = None
    bot.answer_callback_query(call.id, "Signal denied")
    admin_only("❌ FVG signal rejected. Scanning again.")

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
                f"💓 FVG Bot Heartbeat\n"
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
                balance   = get_balance()
                last_sent = now.date()
                if stats:
                    admin_only(
                        f"📅 FVG Bot Daily Summary — {now.strftime('%Y-%m-%d')}\n\n"
                        f"Today's P&L  : {get_today_pnl()}\n"
                        f"Balance      : {balance}\n"
                        f"Total Trades : {stats['total']}\n"
                        f"Win Rate     : {round(stats['win_rate']*100,1)}%\n"
                        f"All-time P&L : {stats['total_pnl']}")
                reset_daily_state()
            except Exception as e:
                print("Daily summary error:", e)
        time.sleep(60)

# ====================================
# MARKET SCANNER
# ====================================
def market_scanner():
    print("FVG Scalper Scanner Started")
    broadcast("⚡ Akshath FVG Scalper Bot Online — Scanning XAUUSD M5")

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

            if pending and sig_time and (time.time() - sig_time > SIGNAL_TIMEOUT):
                with state_lock:
                    shared["pending_signal"].clear()
                    shared["last_entry"] = None
                print(f"[Scan #{scan_count}] Pending signal expired — last_entry reset.")

            open_count = get_open_trade_count()
            if open_count >= MAX_TRADES:
                print(f"[Scan #{scan_count}] Max trades ({open_count}/{MAX_TRADES}) — skipping.")
                time.sleep(SCAN_INTERVAL)
                continue

            if is_news_window():
                print(f"[Scan #{scan_count}] News window — skipping.")
                time.sleep(SCAN_INTERVAL)
                continue

            df = load_mt5(SYMBOL, TIMEFRAME_M5, 100)
            if df is None:
                print(f"[Scan #{scan_count}] MT5 data unavailable.")
                time.sleep(10)
                continue

            side, entry, tp, sl, gap_size = trade_signal(df)
            fresh_fvgs  = get_fresh_fvgs(df)
            fresh_count = len(fresh_fvgs)

            print(f"[Scan #{scan_count}] Fresh FVGs={fresh_count} | Signal={side} @ {round(entry,2) if entry else 0}")

            if side not in ("BUY", "SELL"):
                time.sleep(SCAN_INTERVAL)
                continue

            if entry == last_ent and pending:
                print(f"[Scan #{scan_count}] Signal already pending @ {entry} — waiting.")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── H1 Trend filter with momentum override ─────────────────
            h1        = get_h1_trend()
            can_trade = trend_allows(side)

            if not can_trade and fresh_count >= MOMENTUM_FVG_COUNT:
                # H1 disagrees but institutional momentum is present
                can_trade    = True
                filter_label = f"🚀 MOMENTUM OVERRIDE (H1={h1.upper()}, {fresh_count} fresh FVGs)"
                print(f"[Scan #{scan_count}] H1 disagrees but MOMENTUM OVERRIDE active (FVGs={fresh_count}) ✅")
            elif can_trade:
                filter_label = f"📊 NORMAL (H1={h1.upper()})"
                print(f"[Scan #{scan_count}] H1 trend agrees → {filter_label}")
            else:
                print(f"[Scan #{scan_count}] BLOCKED — H1={h1.upper()}, fresh FVGs={fresh_count} (need {MOMENTUM_FVG_COUNT})")
                time.sleep(SCAN_INTERVAL)
                continue
            # ───────────────────────────────────────────────────────────

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

            signal_msg = (
                f"⚡ XAUUSD SIGNAL FVG\n"
                f"Side: {side}\n"
                f"Entry: {round(entry, 2)}\n"
                f"TP: {round(tp, 2)}\n"
                f"SL: {round(sl, 2)}\n"
                f"Lot: {lot}\n"
                f"Filter: {filter_label}\n"
            )

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(f"🟢 APPROVE {side} ({lot})", callback_data="fvg_approve"),
                InlineKeyboardButton("❌ DENY", callback_data="fvg_deny")
            )

            admin_only(signal_msg, reply_markup=markup)

            for uid in get_all_users():
                if str(uid) != str(CHAT_ID):
                    try:
                        bot.send_message(uid, signal_msg)
                    except Exception as e:
                        print(f"Subscriber send error {uid}: {e}")

            print(f"[Scan #{scan_count}] ✅ Signal sent: {side} @ {entry} | {filter_label}")

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
        print("MT5 init failed — ensure terminal is open and logged in.")
    else:
        print("MT5 connected.")
        mt5.symbol_select(SYMBOL, True)
        info = mt5.symbol_info(SYMBOL)
        if info:
            print(f"\n{'='*50}")
            print(f"SYMBOL INFO FOR {SYMBOL}")
            print(f"  digits         : {info.digits}")
            print(f"  point          : {info.point}")
            print(f"  trade_tick_size: {info.trade_tick_size}")
            print(f"  trade_tick_value: {info.trade_tick_value}")
            print(f"  filling_mode   : {info.filling_mode}")
            print(f"  volume_min     : {info.volume_min}")
            print(f"  volume_step    : {info.volume_step}")
            print(f"  volume_max     : {info.volume_max}")
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick:
                print(f"  current ask    : {tick.ask}")
                print(f"  current bid    : {tick.bid}")
            print(f"{'='*50}\n")
        print(f"Config: MIN_FVG_GAP={MIN_FVG_GAP} | FVG_LOOKBACK={FVG_LOOKBACK} | "
              f"RR={RR_RATIO} | MOMENTUM_THRESHOLD={MOMENTUM_FVG_COUNT}")

    threading.Thread(target=market_scanner, daemon=True).start()
    threading.Thread(target=trade_monitor,  daemon=True).start()
    threading.Thread(target=heartbeat,      daemon=True).start()
    threading.Thread(target=daily_summary,  daemon=True).start()

    print("FVG Scalper Bot Listening...")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling dropped ({type(e).__name__}): {e} — reconnecting in 15s...")
            time.sleep(15)