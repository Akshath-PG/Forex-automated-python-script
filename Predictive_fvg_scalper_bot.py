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

SYMBOL       = "XAUUSD"
TIMEFRAME_M5 = mt5.TIMEFRAME_M5
TIMEFRAME_H1 = mt5.TIMEFRAME_H1

RISK_PERCENT       = 0.5 / 100
CONTRACT_SIZE      = 100
RR_RATIO           = 2.0
SCAN_INTERVAL      = 15          # faster scan — predictive needs quick reaction

MIN_HOLD_SECONDS   = 180         # 3-min funded account rule
DAILY_LOSS_LIMIT   = 0.02
MAX_TRADES         = 3
MIN_FVG_GAP        = 12.0
FVG_LOOKBACK       = 20
MOMENTUM_FVG_COUNT = 3

BREAKEVEN_PCT      = 0.50
TRAIL_RISK_PCT     = 0.50
WIN_RATE_THRESHOLD = 0.60
HEARTBEAT_INTERVAL = 3600
NEWS_BUFFER_MIN    = 30
NEWS_RETRY_INTERVAL= 300
DAILY_SUMMARY_HOUR = 22

# ── Predictive engine config ───────────────────────────────────────────────
APPROACH_PROXIMITY  = 10.0    # pts: how close price must be to trigger approach alert
ENTRY_INSIDE_PCT    = 0.20    # entry at 20% inside gap from edge
PENDING_EXPIRY_MIN  = 15      # cancel unfilled limit orders after 15 min
STRUCTURE_LOOKBACK  = 30      # candles to look back for swing high/low detection
STRUCTURE_SWING_N   = 5       # N candles each side to confirm a swing point
# ──────────────────────────────────────────────────────────────────────────

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
    # existing
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
    # predictive engine state
    "pred_orders": {},   # ticket → {"trigger","side","entry","sl","tp","placed_at","lot"}
    "fired_gaps":  set(),# gap identifiers already acted on (prevents re-firing same gap)
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

def broadcast(text, reply_markup=None, parse_mode=None):
    for uid in get_all_users():
        try:
            bot.send_message(uid, text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            print(f"Broadcast error {uid}: {e}")

def admin_only(text, reply_markup=None, parse_mode=None):
    try:
        bot.send_message(CHAT_ID, text, reply_markup=reply_markup, parse_mode=parse_mode)
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
            trigger   TEXT,
            timestamp TEXT
        )
    """)
    # Migrate old DB that may not have trigger column
    try:
        c.execute("ALTER TABLE trades ADD COLUMN trigger TEXT DEFAULT 'REACTIVE'")
        print("DB migrated: added trigger column.")
    except Exception:
        pass  # Column already exists
    conn.commit()
    conn.close()

def log_trade_db(ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger="REACTIVE"):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades
            (ticket,symbol,side,entry,sl,tp,lot,pnl,result,trigger,timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB log error:", e)

def log_trade_csv(ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger="REACTIVE"):
    try:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["ticket","symbol","side","entry","sl","tp",
                                 "lot","pnl","result","trigger","timestamp"])
            writer.writerow([ticket, symbol, side, entry, sl, tp, lot, pnl,
                             result, trigger, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except Exception as e:
        print("CSV log error:", e)

def get_stats(trigger=None):
    """Get stats overall or filtered by trigger type.
    WIN    = pnl > 0  (full TP or trailing SL with profit)
    BREAKEVEN = pnl == 0  (SL moved to entry, counted as WIN for win rate)
    LOSS   = pnl < 0  (genuine stop loss)
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if trigger:
            c.execute("SELECT result, pnl FROM trades WHERE result IN ('WIN','LOSS','BREAKEVEN') AND trigger=?",
                      (trigger,))
        else:
            c.execute("SELECT result, pnl FROM trades WHERE result IN ('WIN','LOSS','BREAKEVEN')")
        rows = c.fetchall()
        conn.close()
        if not rows:
            return None
        wins       = [r for r in rows if r[0] == "WIN"]
        losses     = [r for r in rows if r[0] == "LOSS"]
        breakevens = [r for r in rows if r[0] == "BREAKEVEN"]
        total      = len(rows)
        # BREAKEVEN counts as WIN for win rate (no money lost)
        win_rate   = (len(wins) + len(breakevens)) / total if total > 0 else 0
        total_pnl  = sum(r[1] for r in rows)
        best       = max(rows, key=lambda r: r[1])
        worst      = min(rows, key=lambda r: r[1])
        return {
            "total":      total,
            "wins":       len(wins),
            "losses":     len(losses),
            "breakevens": len(breakevens),
            "win_rate":   win_rate,
            "total_pnl":  round(total_pnl, 2),
            "best":       round(best[1], 2),
            "worst":      round(worst[1], 2),
        }
    except Exception as e:
        print("Stats error:", e)
        return None

def get_trigger_winrate(trigger):
    """Return win rate string for a specific trigger type."""
    stats = get_stats(trigger=trigger)
    if not stats or stats["total"] < 3:
        return "N/A (< 3 trades)"
    be = stats.get("breakevens", 0)
    be_str = f"/{be}BE" if be > 0 else ""
    return f"{round(stats['win_rate']*100,1)}% ({stats['wins']}W{be_str}/{stats['losses']}L)"

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

def get_current_price():
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick:
        return (tick.bid + tick.ask) / 2
    return 0

# ====================================
# TREND FILTER
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
            print(f"News fetch: OK — {len(events)} events.")
            return _news_cache
        except requests.exceptions.ConnectionError:
            return _news_cache
        except:
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

def check_win_rate():
    stats = get_stats()
    if stats and stats["total"] >= 10:
        if stats["win_rate"] < WIN_RATE_THRESHOLD:
            with state_lock:
                shared["paused"] = True
            admin_only(
                f"⚠️ Win rate {round(stats['win_rate']*100,1)}% below "
                f"{int(WIN_RATE_THRESHOLD*100)}%.\nBot auto-paused. Use /resume.")

# ====================================
# FVG DETECTION
# ====================================
def detect_fvg(df):
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
                    "gap_id":    f"bull_{round(c1.high,2)}_{round(c3.low,2)}",
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
                    "gap_id":    f"bear_{round(c3.high,2)}_{round(c1.low,2)}",
                    "formed_at": df.index[i],
                })
    return fvgs

def get_fresh_fvgs(df, lookback=None):
    if lookback is None:
        lookback = FVG_LOOKBACK
    all_fvgs      = detect_fvg(df)
    recent        = [g for g in all_fvgs if g["index"] >= len(df) - lookback]
    current_price = df.iloc[-1].close
    fresh = []
    for g in recent:
        if g["type"] == "bullish" and current_price < g["low"]:
            continue
        if g["type"] == "bearish" and current_price > g["high"]:
            continue
        fresh.append(g)
    return fresh

# ====================================
# PRICE NORMALIZATION + FILLING MODE
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

def lot_size(entry, sl):
    if entry == 0 or sl == 0 or entry == sl:
        return 0.0
    balance       = get_balance()
    stop_distance = abs(entry - sl)
    lot = (balance * RISK_PERCENT) / (stop_distance * CONTRACT_SIZE)
    lot = max(0.01, min(1.0, lot))
    return round(lot, 2)

# ====================================
# PLACE PREDICTIVE LIMIT ORDER
# ====================================
def place_limit_order(side, entry, sl, tp, trigger, gap_id):
    """
    Place a limit order at the predicted entry price.
    No SL/TP set initially — applied after MIN_HOLD_SECONDS (funded account rule).
    """
    if not ensure_mt5():
        return None

    lot      = lot_size(entry, sl)
    filling  = get_filling_mode(SYMBOL)
    entry    = normalize_price(SYMBOL, entry)
    sl       = normalize_price(SYMBOL, sl)
    tp       = normalize_price(SYMBOL, tp)

    order_type = mt5.ORDER_TYPE_BUY_LIMIT if side == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

    request = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        entry,
        "sl":           0.0,    # held for 3-min funded account rule
        "tp":           0.0,
        "deviation":    20,
        "magic":        303030,
        "comment":      f"PRED_{trigger[:4]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    result = mt5.order_send(request)
    if result is None:
        print(f"place_limit_order: None returned. {mt5.last_error()}")
        return None

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        with state_lock:
            shared["pred_orders"][result.order] = {
                "trigger":   trigger,
                "side":      side,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "lot":       lot,
                "placed_at": time.time(),
                "gap_id":    gap_id,
                "filled":    False,
            }
            shared["fired_gaps"].add(gap_id)

        win_rate_str = get_trigger_winrate(trigger)

        admin_only(
            f"🔮 PREDICTIVE LIMIT PLACED\n\n"
            f"Trigger   : {trigger}\n"
            f"Side      : {side}\n"
            f"Entry     : {entry}\n"
            f"TP        : {tp}\n"
            f"SL        : {sl}\n"
            f"Lot       : {lot}\n\n"
            f"Win Rate  : {win_rate_str}\n"
            f"Balance   : {get_balance()}\n"
            f"Expires   : {PENDING_EXPIRY_MIN} min if unfilled\n"
            f"Ticket    : #{result.order}")
        print(f"place_limit_order: ✅ {trigger} {side} @ {entry} Ticket={result.order}")
        return result.order
    else:
        print(f"place_limit_order: ❌ retcode={result.retcode} {result.comment}")
        admin_only(
            f"❌ Predictive order failed\n"
            f"Trigger : {trigger}\n"
            f"retcode : {result.retcode}\n"
            f"reason  : {result.comment}")
        return None

def cancel_pending_order(ticket):
    """Cancel an unfilled pending order by ticket."""
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order":  ticket,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"cancel_pending_order: ✅ #{ticket} cancelled")
        return True
    print(f"cancel_pending_order: ❌ #{ticket} retcode={result.retcode if result else 'None'}")
    return False

def execute_market_trade(action, symbol, lot):
    if not ensure_mt5():
        return None
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return None
    
    filling = get_filling_mode(symbol)
    if action == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": 0.0,
        "tp": 0.0,
        "deviation": 20,
        "magic": 101010,
        "comment": f"MAN_{action}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        with state_lock:
            shared["active_tickets"].append(result.order)
        print(f"execute_market_trade: ✅ {action} placed. Ticket: {result.order}")
    else:
        err = result.comment if result else mt5.last_error()
        print(f"execute_market_trade: ❌ {action} failed. Error: {err}")
    return result

# ====================================
# PREDICTIVE ENGINE — 3 TRIGGERS
# ====================================

def trigger1_approach(df, fresh_fvgs, current_price):
    """
    TRIGGER 1 — Price Approaching Zone
    Fires when live price is within APPROACH_PROXIMITY points of a fresh FVG edge.
    H1 trend filter APPLIES for this trigger.
    Entry: 20% inside gap from the near edge.
    """
    signals = []
    for gap in fresh_fvgs:
        gap_id = gap["gap_id"]
        with state_lock:
            if gap_id in shared["fired_gaps"]:
                continue

        if gap["type"] == "bullish":
            # BUY setup — price approaching from above, will pull into gap
            near_edge  = gap["high"]   # top of gap (price enters from here)
            far_edge   = gap["low"]
            dist       = current_price - near_edge
            if 0 < dist <= APPROACH_PROXIMITY:
                entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT  # 20% inside from top
                sl    = far_edge - (gap["high"] - gap["low"]) * 0.1            # just below gap
                tp    = entry + (entry - sl) * RR_RATIO
                signals.append(("BUY", entry, sl, tp, gap_id, "APPROACH"))

        elif gap["type"] == "bearish":
            # SELL setup — price approaching from below, will pull into gap
            near_edge  = gap["low"]    # bottom of gap
            far_edge   = gap["high"]
            dist       = near_edge - current_price
            if 0 < dist <= APPROACH_PROXIMITY:
                entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT  # 20% inside from bottom
                sl    = far_edge + (gap["high"] - gap["low"]) * 0.1            # just above gap
                tp    = entry - (sl - entry) * RR_RATIO
                signals.append(("SELL", entry, sl, tp, gap_id, "APPROACH"))

    return signals


def trigger2_live_forming(df):
    """
    TRIGGER 2 — Live FVG Forming
    Detects when the 3-candle FVG pattern is actively forming on the live candle.
    Candle 1 and 2 are closed. Candle 3 is the live candle (still printing).
    H1 filter BYPASSED — forming gap is self-confirming.
    Entry: 20% inside the forming gap from edge.
    """
    signals = []
    if len(df) < 3:
        return signals

    # Last 3 candles: c1=oldest closed, c2=middle closed, c3=live (current)
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]   # noqa — kept for structural clarity
    c3 = df.iloc[-1]   # live candle (still forming)

    gap_id_bull = f"live_bull_{round(c1.high,2)}_{round(c3.low,2)}"
    gap_id_bear = f"live_bear_{round(c3.high,2)}_{round(c1.low,2)}"

    # Bullish FVG forming: c1.high < c3.low (gap above c1, below c3)
    if c1.high < c3.low:
        gap_size = c3.low - c1.high
        if gap_size >= MIN_FVG_GAP:
            with state_lock:
                if gap_id_bull not in shared["fired_gaps"]:
                    near_edge = c3.low    # price will pull back into gap from above
                    far_edge  = c1.high
                    entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                    sl        = far_edge - gap_size * 0.1
                    tp        = entry + (entry - sl) * RR_RATIO
                    signals.append(("BUY", entry, sl, tp, gap_id_bull, "LIVE_FORMING"))

    # Bearish FVG forming: c1.low > c3.high (gap below c1, above c3)
    if c1.low > c3.high:
        gap_size = c1.low - c3.high
        if gap_size >= MIN_FVG_GAP:
            with state_lock:
                if gap_id_bear not in shared["fired_gaps"]:
                    near_edge = c3.high   # price will pull back into gap from below
                    far_edge  = c1.low
                    entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                    sl        = far_edge + gap_size * 0.1
                    tp        = entry - (sl - entry) * RR_RATIO
                    signals.append(("SELL", entry, sl, tp, gap_id_bear, "LIVE_FORMING"))

    return signals


def trigger3_structure_break(df):
    """
    TRIGGER 3 — Structure Break
    Detects when price breaks a recent swing high or low.
    After a structure break, price typically pulls back to fill a FVG.
    Pre-places limit order at the expected pullback zone.
    H1 filter BYPASSED — structure breaks are directional events.
    Entry: 20% inside the projected FVG from the break level.
    """
    signals = []
    if len(df) < STRUCTURE_LOOKBACK + STRUCTURE_SWING_N * 2:
        return signals

    n      = STRUCTURE_SWING_N
    window = df.iloc[-(STRUCTURE_LOOKBACK):]

    # Find swing highs and lows in the window (excluding last 2 candles = live + previous)
    for i in range(n, len(window) - n - 2):
        candle = window.iloc[i]

        # Swing high: highest point among N candles on each side
        is_swing_high = all(
            candle.high >= window.iloc[i - j].high and
            candle.high >= window.iloc[i + j].high
            for j in range(1, n + 1)
        )
        # Swing low: lowest point
        is_swing_low = all(
            candle.low <= window.iloc[i - j].low and
            candle.low <= window.iloc[i + j].low
            for j in range(1, n + 1)
        )

        current_close = df.iloc[-1].close
        current_high  = df.iloc[-1].high
        current_low   = df.iloc[-1].low

        if is_swing_high:
            # Bullish structure break: current candle closes above the swing high
            swing_level = candle.high
            gap_id      = f"struct_bull_{round(swing_level,2)}"
            with state_lock:
                fired = gap_id in shared["fired_gaps"]
            if not fired and current_close > swing_level:
                # Project pullback zone: entry just above the broken level
                gap_size  = max(current_high - swing_level, MIN_FVG_GAP)
                near_edge = swing_level
                far_edge  = swing_level - gap_size * 0.5
                entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl        = far_edge - gap_size * 0.1
                tp        = entry + (entry - sl) * RR_RATIO
                signals.append(("BUY", entry, sl, tp, gap_id, "STRUCTURE_BREAK"))

        if is_swing_low:
            # Bearish structure break: current candle closes below the swing low
            swing_level = candle.low
            gap_id      = f"struct_bear_{round(swing_level,2)}"
            with state_lock:
                fired = gap_id in shared["fired_gaps"]
            if not fired and current_close < swing_level:
                gap_size  = max(swing_level - current_low, MIN_FVG_GAP)
                near_edge = swing_level
                far_edge  = swing_level + gap_size * 0.5
                entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl        = far_edge + gap_size * 0.1
                tp        = entry - (sl - entry) * RR_RATIO
                signals.append(("SELL", entry, sl, tp, gap_id, "STRUCTURE_BREAK"))

    return signals

# ====================================
# PREDICTIVE ORDER MONITOR
# ====================================
def monitor_predictive_orders():
    """
    Runs in trade_monitor loop.
    1. Checks if any pending predictive orders have been filled → trade is live
    2. Cancels orders unfilled after PENDING_EXPIRY_MIN
    3. Applies SL/TP after MIN_HOLD_SECONDS once filled
    """
    if not ensure_mt5():
        return

    now = time.time()

    with state_lock:
        pred_tickets = dict(shared["pred_orders"])

    for ticket, info in pred_tickets.items():

        # ── Check expiry of unfilled orders ───────────────────────────
        if not info["filled"]:
            age = now - info["placed_at"]
            if age > PENDING_EXPIRY_MIN * 60:
                # Check if still pending (not filled)
                pending_orders = mt5.orders_get(symbol=SYMBOL)
                pending_ids    = {o.ticket for o in pending_orders} if pending_orders else set()
                if ticket in pending_ids:
                    if cancel_pending_order(ticket):
                        admin_only(
                            f"⏱ Predictive order expired & cancelled\n"
                            f"Ticket  : #{ticket}\n"
                            f"Trigger : {info['trigger']}\n"
                            f"Side    : {info['side']}\n"
                            f"Entry   : {info['entry']}")
                        with state_lock:
                            shared["pred_orders"].pop(ticket, None)
                continue

        # ── Check if pending order just got filled ─────────────────────
        if not info["filled"]:
            pending_orders = mt5.orders_get(symbol=SYMBOL)
            pending_ids    = {o.ticket for o in pending_orders} if pending_orders else set()

            # Order is no longer pending — check if it filled via deal history
            if ticket not in pending_ids:
                filled_pos    = None
                real_ticket   = None

                # Search recent deals for one that originated from our order ticket
                deals = mt5.history_deals_get(time.time() - 3600, time.time())
                if deals:
                    for d in reversed(deals):
                        if d.order == ticket:
                            # Found the deal — now find the position it opened
                            positions = mt5.positions_get(symbol=SYMBOL)
                            if positions:
                                for p in positions:
                                    if p.ticket == d.position_id or p.magic == 303030:
                                        # Verify it's ours by position_id match first
                                        if p.ticket == d.position_id:
                                            filled_pos  = p
                                            real_ticket = p.ticket
                                            break
                                # Fallback: match by price if position_id didn't work
                                if not filled_pos:
                                    for p in positions:
                                        if p.magic == 303030 and \
                                           abs(p.price_open - info["entry"]) < 2.0:
                                            filled_pos  = p
                                            real_ticket = p.ticket
                                            break
                            break

                if filled_pos and real_ticket:
                    with state_lock:
                        shared["pred_orders"][ticket]["filled"]      = True
                        shared["pred_orders"][ticket]["real_ticket"]  = real_ticket
                        if real_ticket not in shared["active_tickets"]:
                            shared["active_tickets"].append(real_ticket)
                        shared["trade_open_times"][real_ticket] = now
                        shared["trade_targets"][real_ticket]    = {
                            "sl": info["sl"], "tp": info["tp"]
                        }

                    log_trade_db(real_ticket, SYMBOL, info["side"],
                                 info["entry"], info["sl"], info["tp"],
                                 info["lot"], 0, "OPEN", info["trigger"])
                    log_trade_csv(real_ticket, SYMBOL, info["side"],
                                  info["entry"], info["sl"], info["tp"],
                                  info["lot"], 0, "OPEN", info["trigger"])

                    win_rate_str  = get_trigger_winrate(info["trigger"])
                    current_price = get_current_price()

                    admin_only(
                        f"✅ PREDICTIVE ENTRY FILLED\n\n"
                        f"Trigger   : {info['trigger']}\n"
                        f"Side      : {info['side']}\n"
                        f"Entry     : {filled_pos.price_open}\n"
                        f"TP        : {info['tp']}\n"
                        f"SL        : {info['sl']}\n"
                        f"Lot       : {info['lot']}\n\n"
                        f"Price now : {round(current_price,2)}\n"
                        f"Win Rate  : {win_rate_str}\n"
                        f"Balance   : {get_balance()}\n"
                        f"⏱ SL/TP applied after 3-min hold")

                    print(f"monitor_predictive_orders: ✅ Fill detected "
                          f"order={ticket} → position={real_ticket}")
                else:
                    # Order gone but no fill found — was cancelled externally
                    print(f"monitor_predictive_orders: order #{ticket} gone, no fill found — removing.")
                    with state_lock:
                        shared["pred_orders"].pop(ticket, None)

# ====================================
# APPLY SL/TP — Broker-aware stops level
# ====================================
def get_stops_level(symbol):
    """
    Returns the minimum SL/TP distance from current price in price units.
    Broker enforces this — going closer causes retcode 10016.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return 2.0  # safe fallback for XAUUSD
    tick_size    = info.trade_tick_size if info.trade_tick_size > 0 else 0.01
    stops_points = info.trade_stops_level  # in points (broker setting)
    min_distance = stops_points * tick_size
    # Always enforce a minimum of 2.0 for XAUUSD regardless of broker reporting 0
    return max(min_distance, 2.0)

def apply_sltp(ticket, sl, tp):
    """
    Apply SL/TP to a live position.
    - Fetches current price and broker stops level on each attempt
    - Clamps SL/TP so they are always >= stops_level away from current price
    - Widens by 5 pts per attempt (not 0.5/1.0) to actually clear the stops level
    - Detects if price moved so far that original SL is now on the wrong side
    - Sends Telegram alert if all attempts fail (position is unprotected)
    """
    MAX_ATTEMPTS = 6
    WIDEN_STEP   = 5.0  # points per retry — meaningful for XAUUSD

    positions = mt5.positions_get()
    if not positions:
        return False

    for pos in positions:
        if pos.ticket != ticket:
            continue

        is_buy = (pos.type == mt5.ORDER_TYPE_BUY)

        for attempt in range(MAX_ATTEMPTS):
            # Re-fetch current price each attempt — price moves between retries
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                print(f"apply_sltp: ❌ can't get tick for #{ticket}")
                return False

            current_price = tick.ask if is_buy else tick.bid
            min_dist      = get_stops_level(SYMBOL)
            widen         = attempt * WIDEN_STEP

            if is_buy:
                adj_sl = normalize_price(SYMBOL, sl - widen)
                adj_tp = normalize_price(SYMBOL, tp + widen)
                # Clamp: SL must be below current price by at least min_dist
                max_sl = current_price - min_dist
                min_tp = current_price + min_dist
                if adj_sl >= max_sl:
                    adj_sl = normalize_price(SYMBOL, max_sl - WIDEN_STEP)
                if adj_tp <= min_tp:
                    adj_tp = normalize_price(SYMBOL, min_tp + WIDEN_STEP)
            else:
                adj_sl = normalize_price(SYMBOL, sl + widen)
                adj_tp = normalize_price(SYMBOL, tp - widen)
                # Clamp: SL must be above current price by at least min_dist
                min_sl = current_price + min_dist
                max_tp = current_price - min_dist
                if adj_sl <= min_sl:
                    adj_sl = normalize_price(SYMBOL, min_sl + WIDEN_STEP)
                if adj_tp >= max_tp:
                    adj_tp = normalize_price(SYMBOL, max_tp - WIDEN_STEP)

            res = mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl":       adj_sl,
                "tp":       adj_tp,
            })

            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"apply_sltp: ✅ #{ticket} SL={adj_sl} TP={adj_tp} "
                      f"(attempt {attempt+1}, price={current_price})")
                return True

            if res and res.retcode == 10016:
                print(f"apply_sltp: invalid stops attempt {attempt+1} "
                      f"| price={current_price} min_dist={min_dist} "
                      f"| SL={adj_sl} TP={adj_tp} — widening {WIDEN_STEP}pts more...")
                continue

            # Any other error — don't retry, log and bail
            print(f"apply_sltp: ❌ #{ticket} retcode={res.retcode if res else 'None'} "
                  f"{res.comment if res else mt5.last_error()}")
            return False

        # All attempts exhausted — alert and bail
        print(f"apply_sltp: ❌ #{ticket} failed after {MAX_ATTEMPTS} attempts")
        admin_only(
            f"⚠️ SL/TP Apply FAILED — #{ticket}\n"
            f"Price moved too fast after fill.\n"
            f"Position is UNPROTECTED — check MT5 immediately.")
        return False

    return False  # ticket not found in positions


def modify_sl(ticket, new_sl):
    """Modify SL only (breakeven/trail moves) — stops-level aware."""
    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        if pos.ticket != ticket:
            continue

        is_buy = (pos.type == mt5.ORDER_TYPE_BUY)
        tick   = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            return

        current_price = tick.ask if is_buy else tick.bid
        min_dist      = get_stops_level(SYMBOL)

        # Clamp new_sl so it never violates the broker stops level
        if is_buy:
            new_sl = min(new_sl, current_price - min_dist)
        else:
            new_sl = max(new_sl, current_price + min_dist)

        new_sl = normalize_price(SYMBOL, new_sl)

        res = mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       new_sl,
            "tp":       pos.tp,
        })
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"modify_sl: ✅ #{ticket} new SL={new_sl}")
        else:
            print(f"modify_sl: ❌ #{ticket} retcode={res.retcode if res else 'None'}")
        return

def get_server_time():
    """MT5 server time — same timezone as pos.time. Always use this for hold calculations."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick and tick.time > 0:
        return tick.time
    # fallback: approximate server time from last known offset
    return int(time.time())

def manage_open_trades():
    if not ensure_mt5():
        return
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return

    now = get_server_time()  # CRITICAL: use broker server time, same as pos.time

    for pos in positions:
        if pos.magic not in (202020, 303030):
            continue

        ticket     = pos.ticket
        open_price = pos.price_open
        current_sl = pos.sl
        tp         = pos.tp
        tick       = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            continue

        with state_lock:
            open_time = shared["trade_open_times"].get(ticket)
            targets   = shared["trade_targets"].get(ticket)

        # Safety net: position exists but was never registered (fill detection missed it)
        # Register it now using pos.time (MT5 open timestamp) so hold period is calculated correctly
        if open_time is None:
            print(f"manage_open_trades: untracked position #{ticket} found — registering now.")
            pos_open_epoch = int(pos.time)  # MT5 gives open time as unix timestamp
            with state_lock:
                shared["trade_open_times"][ticket] = int(pos.time)  # broker server time
                if ticket not in shared["active_tickets"]:
                    shared["active_tickets"].append(ticket)
                # Use current SL/TP as targets if already set, else use zeros
                if ticket not in shared["trade_targets"]:
                    shared["trade_targets"][ticket] = {
                        "sl": current_sl if current_sl else 0,
                        "tp": tp         if tp         else 0,
                    }
                open_time = pos_open_epoch
                targets   = shared["trade_targets"][ticket]
            admin_only(
                f"⚠️ Untracked position recovered\n"
                f"Ticket : #{ticket}\n"
                f"Side   : {'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'}\n"
                f"Entry  : {pos.price_open}\n"
                f"SL/TP will be applied if missing.")

        hold_remaining = max(0, MIN_HOLD_SECONDS - (now - open_time))

        if hold_remaining > 0:
            print(f"[Hold] #{ticket} — {round(hold_remaining)}s left")
            continue

        # Apply SL/TP after hold period
        if targets and (current_sl == 0.0 or current_sl is None) and (tp == 0.0 or tp is None):
            sym_info = mt5.symbol_info(SYMBOL)

            # Minimum stop distance from broker (always respect this)
            broker_min = (sym_info.trade_stops_level * sym_info.point
                          if sym_info and sym_info.trade_stops_level > 0 else 0.5)
            # Add a buffer: 2x broker min to avoid borderline rejections
            min_stop_dist = max(broker_min * 2.0, 1.5)

            # Calculate risk from stored targets, but enforce minimum
            if targets["sl"] != 0 and targets["tp"] != 0:
                original_risk = abs(targets["sl"] - targets["tp"]) / (1 + RR_RATIO)
            else:
                # Targets were zero (untracked position) — use 15pt default for XAUUSD
                original_risk = 15.0

            # Always use at least the minimum stop distance
            original_risk = max(original_risk, min_stop_dist)

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
                continue

        # Breakeven + trailing
        # Step 0 — Breakeven  : price reaches 25% of TP distance → SL moves to entry
        # Step 1 — Lock 35%   : price reaches 50% of TP distance → SL locks 35% profit
        # Step 2 — Lock 50%   : price reaches 75% of TP distance → SL locks 50% profit

        if pos.type == mt5.ORDER_TYPE_BUY:
            current_price = tick.bid
            tp_distance   = tp - open_price if tp > 0 else 0
            if tp_distance <= 0 or current_sl <= 0:
                continue

            progress = (current_price - open_price) / tp_distance
            new_sl = current_sl
            locked_msg = ""

            # Step 2: Lock 50% profit if 75% to TP
            if progress >= 0.75:
                proposed_sl = open_price + (tp_distance * 0.50)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit locked 🔒"
            # Step 1: Lock 35% profit if 50% to TP
            elif progress >= 0.50:
                proposed_sl = open_price + (tp_distance * 0.35)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit locked 🔒"
            # Step 0: Breakeven — move SL to entry if 25% to TP
            elif progress >= 0.25:
                proposed_sl = open_price  # exact entry = zero loss
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "BREAKEVEN — SL at entry 🛡"

            new_sl = normalize_price(SYMBOL, new_sl)
            if new_sl > normalize_price(SYMBOL, current_sl):
                modify_sl(ticket, new_sl)
                with state_lock:
                    shared["breakeven_tickets"].add(ticket)
                if locked_msg:
                    admin_only(f"🔒 BUY #{ticket}: SL moved — {locked_msg}", parse_mode=None)

        elif pos.type == mt5.ORDER_TYPE_SELL:
            current_price = tick.ask
            tp_distance   = open_price - tp if tp > 0 else 0
            if tp_distance <= 0 or current_sl <= 0:
                continue

            progress = (open_price - current_price) / tp_distance
            new_sl = current_sl
            locked_msg = ""

            # Step 2: Lock 50% profit if 75% to TP
            if progress >= 0.75:
                proposed_sl = open_price - (tp_distance * 0.50)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit locked 🔒"
            # Step 1: Lock 35% profit if 50% to TP
            elif progress >= 0.50:
                proposed_sl = open_price - (tp_distance * 0.35)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit locked 🔒"
            # Step 0: Breakeven — move SL to entry if 25% to TP
            elif progress >= 0.25:
                proposed_sl = open_price  # exact entry = zero loss
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "BREAKEVEN — SL at entry 🛡"

            new_sl = normalize_price(SYMBOL, new_sl)
            if new_sl < normalize_price(SYMBOL, current_sl):
                modify_sl(ticket, new_sl)
                with state_lock:
                    shared["breakeven_tickets"].add(ticket)
                if locked_msg:
                    admin_only(f"🔒 SELL #{ticket}: SL moved — {locked_msg}", parse_mode=None)

# ====================================
# TRADE MONITOR
# ====================================
def trade_monitor():
    print("Trade Monitor Started")
    while True:
        try:
            # ── Step 1: Monitor predictive orders ────────────────────────────
            monitor_predictive_orders()

            # ── Step 2: ALWAYS manage open trades — even after restart ────────
            # Critical fix: do NOT gate this on active_tickets being non-empty.
            # manage_open_trades() has a safety net that registers and manages
            # any position with our magic number, even ones opened before restart.
            if ensure_mt5():
                manage_open_trades()

            # ── Step 3: Sync active_tickets with real MT5 state ──────────────
            # After a restart, active_tickets is empty. Repopulate it from MT5
            # so the close-detection loop below works correctly.
            if ensure_mt5():
                open_positions = mt5.positions_get(symbol=SYMBOL)
                if open_positions:
                    with state_lock:
                        for p in open_positions:
                            if p.magic in (202020, 303030):
                                if p.ticket not in shared["active_tickets"]:
                                    shared["active_tickets"].append(p.ticket)
                                    print(f"trade_monitor: synced untracked ticket #{p.ticket}")

            # ── Step 4: Detect closed positions and send notifications ────────
            with state_lock:
                tickets = list(shared["active_tickets"])

            if tickets and ensure_mt5():
                open_positions = mt5.positions_get(symbol=SYMBOL)
                open_tickets   = {p.ticket for p in open_positions} if open_positions else set()

                for ticket in tickets:
                    if ticket not in open_tickets:
                        # Position closed — find PnL from deal history
                        # Use 24h window and match by position_id + closing deal
                        deals = mt5.history_deals_get(time.time() - 86400, time.time())
                        pnl   = 0.0
                        if deals:
                            for d in reversed(deals):
                                if d.position_id == ticket and d.entry == 1:
                                    # entry==1 is the closing deal
                                    pnl = round(
                                        d.profit + d.commission + d.swap, 2
                                    )
                                    break

                        # ── Classify result correctly ─────────────────────
                        # pnl > 0  → WIN  (includes trailing SL hit with profit locked)
                        # pnl == 0 → BREAKEVEN  (SL moved to exact entry, no gain/loss)
                        # pnl < 0  → LOSS  (genuine stop loss before breakeven)
                        if pnl > 0:
                            result = "WIN"
                        elif pnl == 0:
                            result = "BREAKEVEN"
                        else:
                            result = "LOSS"

                        balance = get_balance()

                        with state_lock:
                            pred_info = next(
                                (v for v in shared["pred_orders"].values()
                                 if v.get("real_ticket") == ticket),
                                None
                            )
                        trigger = pred_info["trigger"] if pred_info else "REACTIVE"

                        log_trade_db(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result, trigger)
                        log_trade_csv(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result, trigger)

                        # ── Build notification ─────────────────────────────
                        if result == "WIN":
                            emoji = "🎯"
                            label = "TP HIT ✅" if pnl > 0 else "WIN"
                            # Distinguish full TP hit vs trailing SL hit with profit
                            with state_lock:
                                targets = shared["trade_targets"].get(ticket)
                            # If SL was trailed (breakeven_tickets had this ticket), label accordingly
                            with state_lock:
                                was_trailed = ticket in shared["breakeven_tickets"]
                            if was_trailed:
                                label = "TRAILING SL — PROFIT SECURED 🔒"
                        elif result == "BREAKEVEN":
                            emoji = "🔄"
                            label = "BREAKEVEN — SL AT ENTRY"
                        else:
                            emoji = "🛑"
                            label = "SL HIT ❌"

                        pnl_str       = f"+${pnl}" if pnl >= 0 else f"-${abs(pnl)}"
                        current_price = get_current_price()
                        overall_stats = get_stats()
                        trigger_stats = get_trigger_winrate(trigger)
                        win_rate_str  = f"{round(overall_stats['win_rate']*100,1)}%" \
                                        if overall_stats else "N/A"

                        broadcast(
                            f"{emoji} *FVG {label}*\n\n"
                            f"🎫 *Ticket:* #{ticket}\n"
                            f"📌 *Trigger:* {trigger}\n"
                            f"💰 *PnL:* {pnl_str}\n"
                            f"🏦 *Balance:* ${balance}\n"
                            f"💹 *Price now:* {round(current_price, 2)}\n\n"
                            f"📊 *Overall W/R:* {win_rate_str}\n"
                            f"📌 *{trigger} W/R:* {trigger_stats}",
                            parse_mode="Markdown")

                        with state_lock:
                            if ticket in shared["active_tickets"]:
                                shared["active_tickets"].remove(ticket)
                            shared["breakeven_tickets"].discard(ticket)
                            shared["trade_open_times"].pop(ticket, None)
                            shared["trade_targets"].pop(ticket, None)
                            for k, v in list(shared["pred_orders"].items()):
                                if v.get("real_ticket") == ticket:
                                    shared["pred_orders"].pop(k, None)

                        check_win_rate()

        except Exception as e:
            print(f"Monitor error: {e}")
        time.sleep(5)

# ====================================
# TELEGRAM COMMANDS
# ====================================

def is_admin(message):
    return str(message.chat.id) == str(CHAT_ID)

def admin_guard(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ This command is for the admin only.")
        return False
    return True

# ── /start & /help ────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def start_command(message):
    save_user(message.chat.id)
    name = message.from_user.first_name or "Trader"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📊 Status",   callback_data="status"),
        InlineKeyboardButton("📈 Stats",    callback_data="stats"),
        InlineKeyboardButton("📋 Pending",  callback_data="pending"),
        InlineKeyboardButton("🔍 Debug",    callback_data="debug"),
    )
    bot.reply_to(message,
        f"👋 Hey {name}! Welcome to *Akshath FVG Scalper Bot*\n\n"
        f"📍 *Symbol:* XAUUSD  |  *TF:* M5\n"
        f"🤖 *Mode:* Predictive FVG — 3 smart triggers\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Quick Commands:*\n\n"
        f"📊 /status — open trades & hold timers\n"
        f"📈 /stats — full P\\&L breakdown\n"
        f"📋 /pending — limit orders waiting to fill\n"
        f"🔍 /debug — live diagnostics\n"
        f"📏 /stopslevel — broker SL/TP distance\n\n"
        f"⚙️ *Admin Controls:*\n\n"
        f"🟢 /buy — manual BUY 0\\.1 lot\n"
        f"🔴 /sell — manual SELL 0\\.1 lot\n"
        f"🚫 /closeall — close all live trades\n"
        f"🗑 /closepending — cancel all pending orders\n"
        f"⏸ /pause — pause the bot\n"
        f"▶️ /resume — resume the bot\n"
        f"🔄 /reset — full state reset\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Use the buttons below for quick access_ 👇",
        parse_mode="Markdown",
        reply_markup=markup)

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(message,
        "📖 *FVG Bot — Command Reference*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Info Commands:*\n"
        "/status — show open trades with entry, SL, TP, PnL & hold timer\n"
        "/stats — win rate & PnL per trigger type\n"
        "/pending — all limit orders currently waiting to be filled\n"
        "/debug — full diagnostic snapshot of the bot\n"
        "/stopslevel — broker minimum SL/TP distance for XAUUSD\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Trade Commands (Admin):*\n"
        "/buy — place a manual BUY market order \\(0\\.1 lot\\)\n"
        "/sell — place a manual SELL market order \\(0\\.1 lot\\)\n"
        "/closeall — close every open trade immediately\n"
        "/closepending — cancel all unfilled limit orders\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Bot Control (Admin):*\n"
        "/pause — stop the bot from scanning & placing orders\n"
        "/resume — unpause the bot \\(also clears daily loss flag\\)\n"
        "/reset — full memory reset \\(does NOT close open trades\\)\n",
        parse_mode="Markdown")

# ── Inline button callbacks ────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: call.data in ("status","stats","pending","debug"))
def handle_quick_buttons(call):
    bot.answer_callback_query(call.id)
    # Re-use command handlers by faking the message origin as admin
    if call.data == "status":
        cmd_status(call.message)
    elif call.data == "stats":
        cmd_stats(call.message)
    elif call.data == "pending":
        cmd_pending(call.message)
    elif call.data == "debug":
        cmd_debug(call.message)

# ── /status ────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not admin_guard(message):
        return
    if not ensure_mt5():
        bot.reply_to(message,
            "❌ *MT5 Not Connected*\n\n"
            "Please make sure the MetaTrader 5 terminal is open and running.",
            parse_mode="Markdown")
        return

    positions     = mt5.positions_get(symbol=SYMBOL)
    balance       = get_balance()
    current_price = get_current_price()
    h1            = get_h1_trend()
    open_count    = get_open_trade_count()

    with state_lock:
        paused   = shared["paused"]
        loss_hit = shared["daily_loss_hit"]
        n_pred   = len([v for v in shared["pred_orders"].values() if not v["filled"]])

    status_emoji = "⏸" if paused else ("🚨" if loss_hit else "🟢")
    status_label = "PAUSED" if paused else ("DAILY LOSS HIT" if loss_hit else "ACTIVE")
    trend_emoji  = "📈" if h1 == "bullish" else ("📉" if h1 == "bearish" else "➡️")

    header = (
        f"📊 *FVG Bot — Status*\n\n"
        f"{status_emoji} *Status:* {status_label}\n"
        f"💰 *Balance:* ${balance}\n"
        f"💹 *XAUUSD:* {round(current_price, 2)}\n"
        f"{trend_emoji} *H1 Trend:* {h1.upper()}\n"
        f"📂 *Open Trades:* {open_count}/{MAX_TRADES}\n"
        f"⏳ *Pending Orders:* {n_pred}\n"
    )

    if not positions or open_count == 0:
        bot.reply_to(message,
            header + "\n📭 *No open trades right now.*",
            parse_mode="Markdown")
        return

    lines = [header, "━━━━━━━━━━━━━━━━━━━━\n*Open Positions:*\n"]
    for p in positions:
        if p.magic not in (202020, 303030):
            continue
        side    = "BUY 🟢" if p.type == mt5.ORDER_TYPE_BUY else "SELL 🔴"
        profit  = round(p.profit, 2)
        pnl_str = f"+${profit}" if profit >= 0 else f"-${abs(profit)}"
        pnl_emoji = "✅" if profit >= 0 else "❌"

        with state_lock:
            open_time = shared["trade_open_times"].get(p.ticket)
            pred_info = next((v for v in shared["pred_orders"].values()
                              if v.get("real_ticket") == p.ticket), None)

        hold_left = max(0, MIN_HOLD_SECONDS - (time.time() - open_time)) if open_time else 0
        hold_str  = f"⏱ *Hold remaining:* {round(hold_left)}s\n" if hold_left > 0 else ""
        trigger   = pred_info["trigger"] if pred_info else "REACTIVE"

        lines.append(
            f"🎫 *Ticket #{p.ticket}* — {side}\n"
            f"📌 *Trigger:* {trigger}\n"
            f"🔹 *Entry:* {p.price_open}\n"
            f"🛑 *SL:* {p.sl}   🎯 *TP:* {p.tp}\n"
            f"{pnl_emoji} *PnL:* {pnl_str}\n"
            f"{hold_str}"
        )

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

# ── /pending ───────────────────────────────────────────────────────────────
@bot.message_handler(commands=["pending"])
def cmd_pending(message):
    if not admin_guard(message):
        return

    with state_lock:
        pred_orders = dict(shared["pred_orders"])

    unfilled = {t: v for t, v in pred_orders.items() if not v["filled"]}

    if not unfilled:
        bot.reply_to(message,
            "📋 *Pending Orders*\n\n"
            "📭 No limit orders waiting right now.\n"
            "_The bot will place orders when the next FVG trigger fires._",
            parse_mode="Markdown")
        return

    now   = time.time()
    lines = [f"📋 *Pending Limit Orders* — {len(unfilled)} waiting\n\n━━━━━━━━━━━━━━━━━━━━\n"]

    for ticket, info in unfilled.items():
        age_min    = round((now - info["placed_at"]) / 60, 1)
        expires_in = max(0, PENDING_EXPIRY_MIN - age_min)
        side_emoji = "🟢" if info["side"] == "BUY" else "🔴"
        urgency    = "🔥" if expires_in < 5 else "⏳"

        lines.append(
            f"🎫 *Ticket #{ticket}* — {side_emoji} {info['side']}\n"
            f"📌 *Trigger:* {info['trigger']}\n"
            f"🔹 *Entry:* {info['entry']}\n"
            f"🎯 *TP:* {info['tp']}   🛑 *SL:* {info['sl']}\n"
            f"🕐 *Age:* {age_min}m   {urgency} *Expires in:* {round(expires_in, 1)}m\n"
        )

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

# ── /stats ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not admin_guard(message):
        return

    overall = get_stats()
    if not overall:
        bot.reply_to(message,
            "📈 *Statistics*\n\n"
            "📭 No closed trades yet.\n"
            "_Stats will appear here once the first trade closes._",
            parse_mode="Markdown")
        return

    approach  = get_stats(trigger="APPROACH")
    live_form = get_stats(trigger="LIVE_FORMING")
    structure = get_stats(trigger="STRUCTURE_BREAK")
    reactive  = get_stats(trigger="REACTIVE")

    def fmt(s):
        if not s:
            return "➖ No trades yet"
        bar_filled = int(s["win_rate"] * 10)
        bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
        pnl_str = f"+${s['total_pnl']}" if s["total_pnl"] >= 0 else f"-${abs(s['total_pnl'])}"
        be = s.get("breakevens", 0)
        be_str = f" / {be}BE" if be > 0 else ""
        return (
            f"{bar}\n"
            f"   Win Rate: *{round(s['win_rate']*100, 1)}%*  "
            f"({s['wins']}W{be_str} / {s['losses']}L)  |  PnL: *{pnl_str}*"
        )

    today_pnl   = get_today_pnl()
    today_emoji = "✅" if today_pnl >= 0 else "❌"
    overall_pnl_str = f"+${overall['total_pnl']}" if overall["total_pnl"] >= 0 else f"-${abs(overall['total_pnl'])}"

    bot.reply_to(message,
        f"📈 *FVG Scalper — Statistics*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Overall* ({overall['total']} trades)\n"
        f"{fmt(overall)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*By Trigger Type:*\n\n"
        f"🎯 *APPROACH*\n{fmt(approach)}\n\n"
        f"⚡ *LIVE FORMING*\n{fmt(live_form)}\n\n"
        f"💥 *STRUCTURE BREAK*\n{fmt(structure)}\n\n"
        f"🔁 *REACTIVE*\n{fmt(reactive)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{today_emoji} *Today's P&L:* {today_pnl}\n"
        f"💰 *Balance:* ${get_balance()}",
        parse_mode="Markdown")

# ── /stopslevel ────────────────────────────────────────────────────────────
@bot.message_handler(commands=["stopslevel"])
def cmd_stops_level(message):
    if not admin_guard(message):
        return
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        bot.reply_to(message,
            "❌ *Cannot fetch symbol info.*\n"
            "Make sure MT5 is connected and XAUUSD is available.",
            parse_mode="Markdown")
        return
    tick     = mt5.symbol_info_tick(SYMBOL)
    min_dist = get_stops_level(SYMBOL)
    bot.reply_to(message,
        f"📏 *Broker Stops Level — {SYMBOL}*\n\n"
        f"🔢 *Raw stops level:* {info.trade_stops_level} points\n"
        f"📐 *Tick size:* {info.trade_tick_size}\n"
        f"📌 *Min SL/TP distance:* {min_dist} price units\n\n"
        f"💹 *Ask:* {tick.ask if tick else 'N/A'}\n"
        f"💹 *Bid:* {tick.bid if tick else 'N/A'}\n\n"
        f"_SL/TP must be at least {min_dist} pts away from current price._",
        parse_mode="Markdown")

# ── /closeall ──────────────────────────────────────────────────────────────
@bot.message_handler(commands=["closeall"])
def cmd_close_all(message):
    if not admin_guard(message):
        return
    if not ensure_mt5():
        bot.reply_to(message, "❌ *MT5 not connected.* Cannot close trades.", parse_mode="Markdown")
        return

    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        bot.reply_to(message,
            "📭 *No open trades to close.*\n_The slate is already clean._",
            parse_mode="Markdown")
        return

    bot.reply_to(message, "⏳ *Closing all trades...* please wait.", parse_mode="Markdown")

    closed, failed = [], []
    for pos in positions:
        if pos.magic not in (202020, 303030):
            continue
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            failed.append(pos.ticket)
            continue

        close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        order_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

        res = mt5.order_send({
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       pos.volume,
            "type":         order_type,
            "price":        close_price,
            "deviation":    30,
            "magic":        pos.magic,
            "comment":      "manual_closeall",
            "position":     pos.ticket,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": get_filling_mode(SYMBOL),
        })

        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            closed.append(pos.ticket)
            pnl = round(pos.profit, 2)
            with state_lock:
                shared["active_tickets"] = [t for t in shared["active_tickets"] if t != pos.ticket]
                shared["trade_open_times"].pop(pos.ticket, None)
                shared["trade_targets"].pop(pos.ticket, None)
                shared["breakeven_tickets"].discard(pos.ticket)
                for k, v in list(shared["pred_orders"].items()):
                    if v.get("real_ticket") == pos.ticket:
                        shared["pred_orders"].pop(k, None)
            log_trade_db(pos.ticket, SYMBOL, "CLOSED", pos.price_open, pos.sl, pos.tp,
                         pos.volume, pnl, "MANUAL", "MANUAL_CLOSE")
            log_trade_csv(pos.ticket, SYMBOL, "CLOSED", pos.price_open, pos.sl, pos.tp,
                          pos.volume, pnl, "MANUAL", "MANUAL_CLOSE")
        else:
            failed.append(pos.ticket)
            print(f"closeall: ❌ #{pos.ticket} retcode={res.retcode if res else 'None'}")

    result_lines = [f"🔴 *Close All — Done*\n\n"
                    f"✅ *Closed:* {len(closed)}   ❌ *Failed:* {len(failed)}\n"]
    if closed:
        result_lines.append(f"🎫 Tickets closed: {', '.join(f'`#{t}`' for t in closed)}")
    if failed:
        result_lines.append(f"⚠️ Tickets failed: {', '.join(f'`#{t}`' for t in failed)}")
    result_lines.append(f"\n💰 *Balance now:* ${get_balance()}")
    bot.send_message(CHAT_ID, "\n".join(result_lines), parse_mode="Markdown")

# ── /closepending ──────────────────────────────────────────────────────────
@bot.message_handler(commands=["cancelpending", "closepending"])
def cmd_cancel_pending(message):
    if not admin_guard(message):
        return
    if not ensure_mt5():
        bot.reply_to(message, "❌ *MT5 not connected.* Cannot cancel orders.", parse_mode="Markdown")
        return

    pending_orders = mt5.orders_get(symbol=SYMBOL)
    if not pending_orders:
        bot.reply_to(message,
            "📭 *No pending orders to cancel.*\n_Nothing is waiting in the queue._",
            parse_mode="Markdown")
        return

    cancelled, failed = [], []
    for order in pending_orders:
        if order.magic not in (202020, 303030):
            continue
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  order.ticket,
        })
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            cancelled.append(order.ticket)
            with state_lock:
                shared["pred_orders"].pop(order.ticket, None)
                gap_id = next(
                    (v["gap_id"] for k, v in shared["pred_orders"].items()
                     if k == order.ticket), None)
                if gap_id:
                    shared["fired_gaps"].discard(gap_id)
        else:
            failed.append(order.ticket)
            print(f"cancelpending: ❌ #{order.ticket} retcode={res.retcode if res else 'None'}")

    lines = [f"🗑 *Cancel Pending — Done*\n\n"
             f"✅ *Cancelled:* {len(cancelled)}   ❌ *Failed:* {len(failed)}\n"]
    if cancelled:
        lines.append(f"🎫 Cancelled: {', '.join(f'`#{t}`' for t in cancelled)}")
    if failed:
        lines.append(f"⚠️ Failed: {', '.join(f'`#{t}`' for t in failed)}")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

# ── /pause ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    if not admin_guard(message):
        return
    with state_lock:
        shared["paused"] = True
    bot.reply_to(message,
        "⏸ *Bot Paused*\n\n"
        "The scanner has stopped placing new orders.\n"
        "Any open trades will continue to be managed.\n\n"
        "_Use /resume to start trading again._",
        parse_mode="Markdown")

# ── /resume ────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    if not admin_guard(message):
        return
    with state_lock:
        shared["paused"]         = False
        shared["daily_loss_hit"] = False
    bot.reply_to(message,
        "▶️ *Bot Resumed*\n\n"
        "✅ Scanner is active again\n"
        "✅ Daily loss flag cleared\n\n"
        f"💰 *Balance:* ${get_balance()}\n"
        f"💹 *XAUUSD:* {round(get_current_price(), 2)}\n\n"
        "_The bot is now scanning for FVG setups._",
        parse_mode="Markdown")

# ── /buy ───────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["buy"])
def cmd_buy(message):
    if not admin_guard(message):
        return
    bot.reply_to(message,
        "⏳ *Placing manual BUY order...*\n_0.1 lot on XAUUSD_",
        parse_mode="Markdown")
    res = execute_market_trade("BUY", SYMBOL, 0.1)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade_db(res.order, SYMBOL, "BUY", res.price, 0, 0, 0.1, 0, "OPEN", "MANUAL")
        log_trade_csv(res.order, SYMBOL, "BUY", res.price, 0, 0, 0.1, 0, "OPEN", "MANUAL")
        bot.send_message(CHAT_ID,
            f"✅ *Manual BUY Filled!*\n\n"
            f"🎫 *Ticket:* #{res.order}\n"
            f"🔹 *Entry:* {res.price}\n"
            f"📦 *Lot:* 0.1\n"
            f"💰 *Balance:* ${get_balance()}\n\n"
            f"_SL/TP will be applied after the 3-min hold._",
            parse_mode="Markdown")
    else:
        err_msg = res.comment if res else "Unknown error"
        bot.send_message(CHAT_ID,
            f"❌ *Manual BUY Failed*\n\n"
            f"⚠️ *Reason:* {err_msg}\n\n"
            f"_Check that MT5 is connected and the market is open._",
            parse_mode="Markdown")

# ── /sell ──────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["sell"])
def cmd_sell(message):
    if not admin_guard(message):
        return
    bot.reply_to(message,
        "⏳ *Placing manual SELL order...*\n_0.1 lot on XAUUSD_",
        parse_mode="Markdown")
    res = execute_market_trade("SELL", SYMBOL, 0.1)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade_db(res.order, SYMBOL, "SELL", res.price, 0, 0, 0.1, 0, "OPEN", "MANUAL")
        log_trade_csv(res.order, SYMBOL, "SELL", res.price, 0, 0, 0.1, 0, "OPEN", "MANUAL")
        bot.send_message(CHAT_ID,
            f"✅ *Manual SELL Filled!*\n\n"
            f"🎫 *Ticket:* #{res.order}\n"
            f"🔹 *Entry:* {res.price}\n"
            f"📦 *Lot:* 0.1\n"
            f"💰 *Balance:* ${get_balance()}\n\n"
            f"_SL/TP will be applied after the 3-min hold._",
            parse_mode="Markdown")
    else:
        err_msg = res.comment if res else "Unknown error"
        bot.send_message(CHAT_ID,
            f"❌ *Manual SELL Failed*\n\n"
            f"⚠️ *Reason:* {err_msg}\n\n"
            f"_Check that MT5 is connected and the market is open._",
            parse_mode="Markdown")

# ── /reset ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if not admin_guard(message):
        return
    reset_daily_state()
    with state_lock:
        shared["paused"]            = False
        shared["daily_loss_hit"]    = False
        shared["fired_gaps"].clear()
        shared["pred_orders"].clear()
        shared["active_tickets"].clear()
        shared["breakeven_tickets"].clear()
        shared["trade_open_times"].clear()
        shared["trade_targets"].clear()
    bot.reply_to(message,
        "🔄 *Full Bot Reset — Complete*\n\n"
        "✅ Daily loss flag cleared\n"
        "✅ Paused flag cleared\n"
        "✅ Fired gaps memory cleared\n"
        "✅ Pending order tracking cleared\n"
        "✅ Active ticket list cleared\n"
        "✅ Daily balance reset to current\n\n"
        f"💰 *Balance:* ${get_balance()}\n\n"
        "⚠️ _Note: This reset does NOT close open MT5 trades.\n"
        "Use /closeall first if you want a full clean slate._",
        parse_mode="Markdown")

# ── /debug ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    if not admin_guard(message):
        return
    bot.reply_to(message, "🔍 *Running diagnostics...* give me a moment.", parse_mode="Markdown")
    try:
        if not ensure_mt5():
            bot.send_message(CHAT_ID,
                "❌ *MT5 Not Connected*\n\n"
                "The bot cannot reach the MetaTrader 5 terminal.\n"
                "_Make sure MT5 is open and logged in._",
                parse_mode="Markdown")
            return
        df = load_mt5(SYMBOL, TIMEFRAME_M5, 100)
        if df is None:
            bot.send_message(CHAT_ID,
                "❌ *MT5 Data Unavailable*\n\n"
                "Connected to MT5 but failed to fetch candle data for XAUUSD.",
                parse_mode="Markdown")
            return

        fresh    = get_fresh_fvgs(df)
        h1       = get_h1_trend()
        price    = get_current_price()
        balance  = get_balance()

        with state_lock:
            paused   = shared["paused"]
            loss_hit = shared["daily_loss_hit"]
            n_pred   = len([v for v in shared["pred_orders"].values() if not v["filled"]])
            n_fired  = len(shared["fired_gaps"])

        status_emoji = "⏸" if paused else ("🚨" if loss_hit else "🟢")
        trend_emoji  = "📈" if h1 == "bullish" else ("📉" if h1 == "bearish" else "➡️")
        news_active  = is_news_window()

        bot.send_message(CHAT_ID,
            f"🔍 *FVG Bot Diagnostics*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_emoji} *Status:* {'PAUSED' if paused else ('DAILY LOSS HIT' if loss_hit else 'ACTIVE')}\n"
            f"💰 *Balance:* ${balance}\n"
            f"💹 *XAUUSD Price:* {round(price, 2)}\n"
            f"{trend_emoji} *H1 Trend:* {h1.upper()}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 *Open Trades:* {get_open_trade_count()} / {MAX_TRADES}\n"
            f"⏳ *Pending Orders:* {n_pred} waiting\n"
            f"🔥 *Gaps Fired:* {n_fired} this session\n"
            f"📡 *Fresh FVGs:* {len(fresh)} (last {FVG_LOOKBACK} candles)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📰 *News Window:* {'🚫 YES — trading paused' if news_active else '✅ Clear'}\n"
            f"⚠️ *Daily Loss Hit:* {'YES 🚨' if loss_hit else 'NO ✅'}\n",
            parse_mode="Markdown")
    except Exception as e:
        bot.send_message(CHAT_ID,
            f"❌ *Diagnostics Error*\n\n`{e}`",
            parse_mode="Markdown")

# ====================================
# HEARTBEAT
# ====================================
def heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            balance = get_balance()
            price   = get_current_price()
            with state_lock:
                paused = shared["paused"]
                n_open = len(shared["active_tickets"])
                n_pred = len([v for v in shared["pred_orders"].values() if not v["filled"]])
            status = "⏸ PAUSED" if paused else "🟢 ACTIVE"
            admin_only(
                f"💓 FVG Bot Heartbeat\n"
                f"Status   : {status}\n"
                f"Balance  : {balance}\n"
                f"Price    : {round(price,2)}\n"
                f"Open     : {n_open} trade(s)\n"
                f"Pending  : {n_pred} predictive order(s)")
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
                overall   = get_stats()
                balance   = get_balance()
                last_sent = now.date()
                if overall:
                    admin_only(
                        f"📅 FVG Bot Daily Summary — {now.strftime('%Y-%m-%d')}\n\n"
                        f"Today's P&L  : {get_today_pnl()}\n"
                        f"Balance      : {balance}\n"
                        f"Total Trades : {overall['total']}\n"
                        f"Win Rate     : {round(overall['win_rate']*100,1)}%\n"
                        f"All-time P&L : {overall['total_pnl']}\n\n"
                        f"APPROACH W/R     : {get_trigger_winrate('APPROACH')}\n"
                        f"LIVE FORMING W/R : {get_trigger_winrate('LIVE_FORMING')}\n"
                        f"STRUCTURE W/R    : {get_trigger_winrate('STRUCTURE_BREAK')}")
                reset_daily_state()
                # Clear fired gaps daily so fresh session starts clean
                with state_lock:
                    shared["fired_gaps"].clear()
                print("Fired gaps cleared for new day.")
            except Exception as e:
                print("Daily summary error:", e)
        time.sleep(60)

# ====================================
# MARKET SCANNER — PREDICTIVE ENGINE
# ====================================
def market_scanner():
    print("FVG Predictive Scanner Started")
    broadcast("⚡ Akshath FVG Predictive Bot Online — XAUUSD M5")

    scan_count = 0

    while True:
        try:
            scan_count += 1
            with state_lock:
                paused   = shared["paused"]
                loss_hit = shared["daily_loss_hit"]

            if paused or check_daily_loss():
                time.sleep(SCAN_INTERVAL)
                continue

            if get_open_trade_count() >= MAX_TRADES:
                time.sleep(SCAN_INTERVAL)
                continue

            if is_news_window():
                time.sleep(SCAN_INTERVAL)
                continue

            df = load_mt5(SYMBOL, TIMEFRAME_M5, 100)
            if df is None:
                time.sleep(10)
                continue

            fresh_fvgs    = get_fresh_fvgs(df)
            fresh_count   = len(fresh_fvgs)
            current_price = get_current_price()
            h1            = get_h1_trend()

            # ── Run all 3 triggers ─────────────────────────────────────
            signals = []

            # Trigger 1: Approach — H1 filter APPLIES
            t1_signals = trigger1_approach(df, fresh_fvgs, current_price)
            for side, entry, sl, tp, gap_id, trig in t1_signals:
                if trend_allows(side) or (fresh_count >= MOMENTUM_FVG_COUNT):
                    signals.append((side, entry, sl, tp, gap_id, trig))
                else:
                    print(f"[Scan #{scan_count}] T1 APPROACH {side} blocked by H1={h1}")

            # Trigger 2: Live forming — H1 filter BYPASSED
            t2_signals = trigger2_live_forming(df)
            signals.extend(t2_signals)

            # Trigger 3: Structure break — H1 filter BYPASSED
            t3_signals = trigger3_structure_break(df)
            signals.extend(t3_signals)

            # ── Place limit orders for each signal ─────────────────────
            for side, entry, sl, tp, gap_id, trigger in signals:
                # Don't exceed MAX_TRADES including pending
                with state_lock:
                    open_count    = get_open_trade_count()
                    pending_count = len([v for v in shared["pred_orders"].values()
                                         if not v["filled"]])
                if open_count + pending_count >= MAX_TRADES:
                    print(f"[Scan #{scan_count}] Max trades+pending reached — skipping {trigger}")
                    break

                ticket = place_limit_order(side, entry, sl, tp, trigger, gap_id)
                if ticket:
                    print(f"[Scan #{scan_count}] ✅ {trigger} {side} @ {entry} → #{ticket}")

            if signals:
                print(f"[Scan #{scan_count}] Triggers fired: {[s[5] for s in signals]} | "
                      f"Fresh FVGs={fresh_count} | H1={h1.upper()}")
            else:
                print(f"[Scan #{scan_count}] No triggers | Fresh FVGs={fresh_count} | "
                      f"H1={h1.upper()} | Price={round(current_price,2)}")

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
        print("MT5 init failed — ensure terminal is open.")
    else:
        print("MT5 connected.")
        mt5.symbol_select(SYMBOL, True)
        info = mt5.symbol_info(SYMBOL)
        if info:
            print(f"\n{'='*50}")
            print(f"SYMBOL INFO FOR {SYMBOL}")
            print(f"  digits         : {info.digits}")
            print(f"  trade_tick_size: {info.trade_tick_size}")
            print(f"  filling_mode   : {info.filling_mode}")
            print(f"  volume_min     : {info.volume_min}")
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick:
                print(f"  ask : {tick.ask}  bid : {tick.bid}")
            print(f"{'='*50}")
        print(f"\nPredictive Config:")
        print(f"  MIN_FVG_GAP       : {MIN_FVG_GAP}")
        print(f"  FVG_LOOKBACK      : {FVG_LOOKBACK} candles")
        print(f"  APPROACH_PROXIMITY: {APPROACH_PROXIMITY} pts")
        print(f"  ENTRY_INSIDE_PCT  : {int(ENTRY_INSIDE_PCT*100)}% from edge")
        print(f"  PENDING_EXPIRY    : {PENDING_EXPIRY_MIN} min")
        print(f"  RR_RATIO          : {RR_RATIO}")
        print(f"  MOMENTUM_THRESHOLD: {MOMENTUM_FVG_COUNT} FVGs\n")

    threading.Thread(target=market_scanner, daemon=True).start()
    threading.Thread(target=trade_monitor,  daemon=True).start()
    threading.Thread(target=heartbeat,      daemon=True).start()
    threading.Thread(target=daily_summary,  daemon=True).start()

    print("FVG Predictive Bot Listening...")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling dropped ({type(e).__name__}): {e} — reconnecting in 15s...")
            time.sleep(15)