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
from datetime import datetime, timedelta, time as dt_time

logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

# ====================================
# CONFIGURATION
# ====================================
TELEGRAM_TOKEN = "8578687996:AAFzpAL_OwcDi555gXrOl8-86GhRc-p7d-g"
CHAT_ID        = "5667584601"

SYMBOL        = "XAUUSD"
TIMEFRAME_M1  = mt5.TIMEFRAME_M1
TIMEFRAME_M5  = mt5.TIMEFRAME_M5
TIMEFRAME_M15 = mt5.TIMEFRAME_M15
TIMEFRAME_H4  = mt5.TIMEFRAME_H4

RISK_PERCENT   = 0.5 / 100
CONTRACT_SIZE  = 100
RR_RATIO       = 2.0
SCAN_INTERVAL  = 30

MIN_HOLD_SECONDS        = 180
DAILY_LOSS_LIMIT        = 0.02
MAX_TRADES              = 3

# --- UPGRADED PROFICIENCY SETTINGS ---
MIN_IFVG_GAP            = 8.0   # Lowered from 15.0 for realistic M1 gaps
MOMENTUM_IFVG_THRESHOLD = 5     # Lowered from 15 to allow proper momentum detection
NY_SESSION_START        = dt_time(12, 30) # UTC time for NY Session open
NY_SESSION_END          = dt_time(19, 30) # UTC time for NY Session close
LIQUIDITY_LOOKBACK      = 500   # Lookback for Asian/London session highs/lows
# -------------------------------------

BREAKEVEN_PCT      = 0.50
TRAIL_RISK_PCT     = 0.50
WIN_RATE_THRESHOLD = 0.60
HEARTBEAT_INTERVAL = 3600
NEWS_BUFFER_MIN    = 30
NEWS_RETRY_INTERVAL= 300
DAILY_SUMMARY_HOUR = 22

APPROACH_PROXIMITY   = 12.0
ENTRY_INSIDE_PCT     = 0.20
PENDING_EXPIRY_MIN   = 15
STRUCTURE_LOOKBACK   = 30
STRUCTURE_SWING_N    = 5

CSV_FILE   = "ifvg_trade_log.csv"
DB_FILE    = "ifvg_trade_log.db"
USERS_FILE = "users.txt"

NEWS_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]

bot = telebot.TeleBot(TELEGRAM_TOKEN)

state_lock = threading.Lock()
shared = {
    "active_tickets":    [],
    "paused":            False,
    "daily_start_bal":   None,
    "daily_loss_hit":    False,
    "breakeven_tickets": set(),
    "trade_open_times":  {},
    "trade_targets":     {},
    "pred_orders":       {},
    "fired_gaps":        set(),
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

def broadcast(text, parse_mode=None):
    for uid in get_all_users():
        try:
            bot.send_message(uid, text, parse_mode=parse_mode)
        except Exception as e:
            print(f"Broadcast error {uid}: {e}")

def admin_only(text, parse_mode=None):
    try:
        bot.send_message(CHAT_ID, text, parse_mode=parse_mode)
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER, symbol TEXT, side TEXT,
            entry REAL, sl REAL, tp REAL, lot REAL,
            pnl REAL, result TEXT, trigger TEXT, timestamp TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE trades ADD COLUMN trigger TEXT DEFAULT 'REACTIVE'")
        print("DB migrated: added trigger column.")
    except Exception:
        pass
    conn.commit()
    conn.close()

def log_trade_db(ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger="REACTIVE"):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""INSERT INTO trades
            (ticket,symbol,side,entry,sl,tp,lot,pnl,result,trigger,timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger,
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
    """
    WIN       = pnl > 0  (full TP or trailing SL with profit locked)
    BREAKEVEN = pnl == 0 (SL moved to entry — counted as WIN for win rate)
    LOSS      = pnl < 0  (genuine stop loss before breakeven)
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
        mt5.shutdown()
        time.sleep(2)
        if not mt5.initialize():
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

def get_server_time():
    """MT5 broker server time — same timezone as pos.time."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick and tick.time > 0:
        return tick.time
    return int(time.time())

# ====================================
# PROFICIENCY FILTERS (NEW)
# ====================================
def is_trading_session():
    now_utc = datetime.utcnow().time()
    return NY_SESSION_START <= now_utc <= NY_SESSION_END

def check_liquidity_sweep(df):
    """Checks if price swept the high/low of the last session"""
    if len(df) < LIQUIDITY_LOOKBACK:
        return False, False
    
    # Establish previous session's range
    window = df.iloc[-LIQUIDITY_LOOKBACK:-15]
    session_high = window['high'].max()
    session_low  = window['low'].min()
    
    # Check if recent 15 candles broke out of that range
    current_high = df.iloc[-15:]['high'].max()
    current_low  = df.iloc[-15:]['low'].min()
    
    swept_high = current_high > session_high
    swept_low  = current_low < session_low
    
    return swept_high, swept_low

# ====================================
# TREND FILTER — M15 + H4
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

def trend_allows(side, ifvg_count=0, swept_high=False, swept_low=False):
    """M15 + H4 must agree. Bypassed by momentum OR liquidity sweeps."""
    m15      = get_trend(TIMEFRAME_M15)
    h4       = get_trend(TIMEFRAME_H4)
    momentum = ifvg_count >= MOMENTUM_IFVG_THRESHOLD
    
    if side == "BUY":
        if swept_low: return True  # Sweep override
        return h4 == "bullish" if momentum else (m15 == "bullish" and h4 == "bullish")
    if side == "SELL":
        if swept_high: return True # Sweep override
        return h4 == "bearish" if momentum else (m15 == "bearish" and h4 == "bearish")
    return False

# ====================================
# NEWS FILTER
# ====================================
_news_cache = []
_news_cache_time = 0
_news_last_attempt = 0
_news_backoff = NEWS_RETRY_INTERVAL

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
                        dt = datetime.strptime(e["date"] + " " + e["time"], "%Y-%m-%d %I:%M%p")
                        events.append(dt)
                    except:
                        pass
            _news_cache = events
            _news_cache_time = now
            _news_backoff = NEWS_RETRY_INTERVAL
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
    if stats and stats["total"] >= 10 and stats["win_rate"] < WIN_RATE_THRESHOLD:
        with state_lock:
            shared["paused"] = True
        admin_only(
            f"⚠️ Win rate {round(stats['win_rate']*100,1)}% below "
            f"{int(WIN_RATE_THRESHOLD*100)}%.\nBot auto-paused. Use /resume.")

# ====================================
# FVG + IFVG DETECTION
# ====================================
def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        c1 = df.iloc[i-2]
        c3 = df.iloc[i]
        if c1.high < c3.low:
            fvgs.append({"type":"bullish","low":c1.high,"high":c3.low,"index":i,
                         "gap_id":f"bull_{round(c1.high,2)}_{round(c3.low,2)}"})
        if c1.low > c3.high:
            fvgs.append({"type":"bearish","low":c3.high,"high":c1.low,"index":i,
                         "gap_id":f"bear_{round(c3.high,2)}_{round(c1.low,2)}"})
    return fvgs

def get_fresh_fvg_count(df, fvgs, lookback=50):
    recent        = [g for g in fvgs if g["index"] >= len(df) - lookback]
    current_price = df.iloc[-1].close
    fresh = []
    for g in recent:
        if g["type"] == "bullish" and current_price < g["low"]:
            continue
        if g["type"] == "bearish" and current_price > g["high"]:
            continue
        fresh.append(g)
    return len(fresh)

def detect_ifvg(df, fvgs):
    """Inverted FVGs — gaps price has closed back through."""
    ifvgs = []
    for gap in fvgs:
        if (gap["high"] - gap["low"]) < MIN_IFVG_GAP:
            continue
        for i in range(gap["index"], len(df)):
            price = df.iloc[i].close
            if gap["type"] == "bullish" and price < gap["low"]:
                ifvgs.append({
                    "type":   "bearish_ifvg",
                    "low":    gap["low"], "high": gap["high"],
                    "gap_id": f"ifvg_bear_{round(gap['low'],2)}_{round(gap['high'],2)}",
                })
                break
            if gap["type"] == "bearish" and price > gap["high"]:
                ifvgs.append({
                    "type":   "bullish_ifvg",
                    "low":    gap["low"], "high": gap["high"],
                    "gap_id": f"ifvg_bull_{round(gap['low'],2)}_{round(gap['high'],2)}",
                })
                break
    return ifvgs

# ====================================
# PRICE HELPERS
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
# PREDICTIVE ENGINE — IFVG TRIGGERS
# ====================================

def trigger1_ifvg_approach(df_m1, fvgs, ifvgs, current_price, ifvg_count, swept_high, swept_low):
    """
    TRIGGER 1 — Price Approaching IFVG Zone
    Detects when price is within APPROACH_PROXIMITY of an existing IFVG.
    """
    signals = []
    for gap in ifvgs:
        gap_id   = gap["gap_id"]
        gap_size = gap["high"] - gap["low"]
        with state_lock:
            if gap_id in shared["fired_gaps"]:
                continue

        if gap["type"] == "bullish_ifvg":
            dist = gap["low"] - current_price
            if 0 < dist <= APPROACH_PROXIMITY:
                near_edge = gap["low"]
                far_edge  = gap["high"]
                entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl    = near_edge - gap_size * 0.3
                tp    = entry + (entry - sl) * RR_RATIO
                if trend_allows("BUY", ifvg_count, swept_high, swept_low):
                    signals.append(("BUY", entry, sl, tp, gap_id, "IFVG_APPROACH"))
                else:
                    print(f"T1 IFVG_APPROACH BUY blocked by trend filter")

        elif gap["type"] == "bearish_ifvg":
            dist = current_price - gap["high"]
            if 0 < dist <= APPROACH_PROXIMITY:
                near_edge = gap["high"]
                far_edge  = gap["low"]
                entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl    = near_edge + gap_size * 0.3
                tp    = entry - (sl - entry) * RR_RATIO
                if trend_allows("SELL", ifvg_count, swept_high, swept_low):
                    signals.append(("SELL", entry, sl, tp, gap_id, "IFVG_APPROACH"))
                else:
                    print(f"T1 IFVG_APPROACH SELL blocked by trend filter")

    return signals


def trigger2_inversion_forming(df_m1, fvgs, ifvg_count, swept_high, swept_low):
    """
    TRIGGER 2 — Live Inversion Forming on M1
    Price is actively crossing back through a FVG gap right now.
    """
    signals = []
    if len(df_m1) < 3:
        return signals

    current_price = df_m1.iloc[-1].close
    prev_price    = df_m1.iloc[-2].close
    h4 = get_trend(TIMEFRAME_H4)

    for gap in fvgs:
        if (gap["high"] - gap["low"]) < MIN_IFVG_GAP:
            continue
        gap_id   = f"inv_{gap['gap_id']}"
        gap_size = gap["high"] - gap["low"]
        with state_lock:
            if gap_id in shared["fired_gaps"]:
                continue

        if gap["type"] == "bullish":
            # Price crossing below gap.low = bearish inversion forming
            if prev_price >= gap["low"] and current_price < gap["low"]:
                if h4 == "bearish" or ifvg_count >= MOMENTUM_IFVG_THRESHOLD or swept_high:
                    near_edge = gap["low"]
                    far_edge  = gap["high"]
                    entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                    sl    = near_edge + gap_size * 0.3
                    tp    = entry - (sl - entry) * RR_RATIO
                    signals.append(("SELL", entry, sl, tp, gap_id, "INVERSION_FORMING"))

        elif gap["type"] == "bearish":
            # Price crossing above gap.high = bullish inversion forming
            if prev_price <= gap["high"] and current_price > gap["high"]:
                if h4 == "bullish" or ifvg_count >= MOMENTUM_IFVG_THRESHOLD or swept_low:
                    near_edge = gap["high"]
                    far_edge  = gap["low"]
                    entry = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                    sl    = near_edge - gap_size * 0.3
                    tp    = entry + (entry - sl) * RR_RATIO
                    signals.append(("BUY", entry, sl, tp, gap_id, "INVERSION_FORMING"))

    return signals


def trigger3_structure_break_m5(ifvg_count, swept_high, swept_low):
    """
    TRIGGER 3 — Structure Break on M5
    """
    signals = []
    df = load_mt5(SYMBOL, TIMEFRAME_M5, STRUCTURE_LOOKBACK + STRUCTURE_SWING_N * 2 + 5)
    if df is None or len(df) < STRUCTURE_LOOKBACK + STRUCTURE_SWING_N * 2:
        return signals

    n             = STRUCTURE_SWING_N
    window        = df.iloc[-(STRUCTURE_LOOKBACK):]
    current_close = df.iloc[-1].close
    current_high  = df.iloc[-1].high
    current_low   = df.iloc[-1].low
    h4 = get_trend(TIMEFRAME_H4)

    for i in range(n, len(window) - n - 2):
        candle = window.iloc[i]

        is_swing_high = all(
            candle.high >= window.iloc[i-j].high and candle.high >= window.iloc[i+j].high
            for j in range(1, n+1)
        )
        is_swing_low = all(
            candle.low <= window.iloc[i-j].low and candle.low <= window.iloc[i+j].low
            for j in range(1, n+1)
        )

        if is_swing_high and current_close > candle.high:
            swing_level = candle.high
            gap_id = f"struct_bull_m5_{round(swing_level,2)}"
            with state_lock:
                fired = gap_id in shared["fired_gaps"]
            if not fired and (h4 == "bullish" or ifvg_count >= MOMENTUM_IFVG_THRESHOLD or swept_low):
                gap_size  = max(current_high - swing_level, MIN_IFVG_GAP)
                near_edge = swing_level
                far_edge  = swing_level - gap_size * 0.5
                entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl        = far_edge - gap_size * 0.1
                tp        = entry + (entry - sl) * RR_RATIO
                signals.append(("BUY", entry, sl, tp, gap_id, "STRUCT_BREAK_M5"))

        if is_swing_low and current_close < candle.low:
            swing_level = candle.low
            gap_id = f"struct_bear_m5_{round(swing_level,2)}"
            with state_lock:
                fired = gap_id in shared["fired_gaps"]
            if not fired and (h4 == "bearish" or ifvg_count >= MOMENTUM_IFVG_THRESHOLD or swept_high):
                gap_size  = max(swing_level - current_low, MIN_IFVG_GAP)
                near_edge = swing_level
                far_edge  = swing_level + gap_size * 0.5
                entry     = near_edge + (far_edge - near_edge) * ENTRY_INSIDE_PCT
                sl        = far_edge + gap_size * 0.1
                tp        = entry - (sl - entry) * RR_RATIO
                signals.append(("SELL", entry, sl, tp, gap_id, "STRUCT_BREAK_M5"))

    return signals

# ====================================
# PLACE PREDICTIVE LIMIT ORDER
# ====================================
def place_limit_order(side, entry, sl, tp, trigger, gap_id):
    if not ensure_mt5():
        return None

    lot     = lot_size(entry, sl)
    filling = get_filling_mode(SYMBOL)
    entry   = normalize_price(SYMBOL, entry)
    sl      = normalize_price(SYMBOL, sl)
    tp      = normalize_price(SYMBOL, tp)

    order_type = mt5.ORDER_TYPE_BUY_LIMIT if side == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

    result = mt5.order_send({
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        entry,
        "sl":           0.0,
        "tp":           0.0,
        "deviation":    20,
        "magic":        101010,
        "comment":      f"PRED_{trigger[:6]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    })

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

        admin_only(
            f"🔮 IFVG PREDICTIVE LIMIT\n\n"
            f"Trigger   : {trigger}\n"
            f"Side      : {side}\n"
            f"Entry     : {entry}\n"
            f"TP        : {tp}\n"
            f"SL        : {sl}\n"
            f"Lot       : {lot}\n\n"
            f"Win Rate  : {get_trigger_winrate(trigger)}\n"
            f"Balance   : {get_balance()}\n"
            f"Expires   : {PENDING_EXPIRY_MIN} min if unfilled\n"
            f"Ticket    : #{result.order}")
        print(f"place_limit_order: ✅ {trigger} {side} @ {entry} Ticket={result.order}")
        return result.order
    else:
        print(f"place_limit_order: ❌ retcode={result.retcode} {result.comment}")
        return None

def cancel_pending_order(ticket):
    result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"cancel_pending_order: ✅ #{ticket}")
        return True
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
    - Widens by 5 pts per attempt (not 0.5) to actually clear the stops level
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

# ====================================
# PREDICTIVE ORDER MONITOR
# ====================================
def monitor_predictive_orders():
    if not ensure_mt5():
        return
    now = time.time()
    with state_lock:
        pred_tickets = dict(shared["pred_orders"])

    for ticket, info in pred_tickets.items():
        if not info["filled"]:
            age = now - info["placed_at"]
            if age > PENDING_EXPIRY_MIN * 60:
                pending_orders = mt5.orders_get(symbol=SYMBOL)
                pending_ids    = {o.ticket for o in pending_orders} if pending_orders else set()
                if ticket in pending_ids:
                    if cancel_pending_order(ticket):
                        admin_only(
                            f"⏱ Predictive order expired\n"
                            f"Ticket  : #{ticket}\nTrigger : {info['trigger']}\n"
                            f"Side    : {info['side']}\nEntry   : {info['entry']}")
                        with state_lock:
                            shared["pred_orders"].pop(ticket, None)
                continue

        if not info["filled"]:
            pending_orders = mt5.orders_get(symbol=SYMBOL)
            pending_ids    = {o.ticket for o in pending_orders} if pending_orders else set()
            if ticket not in pending_ids:
                filled_pos  = None
                real_ticket = None
                deals = mt5.history_deals_get(time.time() - 3600, time.time())
                if deals:
                    for d in reversed(deals):
                        if d.order == ticket:
                            positions = mt5.positions_get(symbol=SYMBOL)
                            if positions:
                                for p in positions:
                                    if p.ticket == d.position_id:
                                        filled_pos  = p
                                        real_ticket = p.ticket
                                        break
                                if not filled_pos:
                                    for p in positions:
                                        if p.magic == 101010 and \
                                           abs(p.price_open - info["entry"]) < 2.0:
                                            filled_pos  = p
                                            real_ticket = p.ticket
                                            break
                            break

                if filled_pos and real_ticket:
                    with state_lock:
                        shared["pred_orders"][ticket]["filled"]     = True
                        shared["pred_orders"][ticket]["real_ticket"] = real_ticket
                        if real_ticket not in shared["active_tickets"]:
                            shared["active_tickets"].append(real_ticket)
                        shared["trade_open_times"][real_ticket] = get_server_time()
                        shared["trade_targets"][real_ticket]    = {"sl": info["sl"], "tp": info["tp"]}
                    log_trade_db(real_ticket, SYMBOL, info["side"], info["entry"],
                                 info["sl"], info["tp"], info["lot"], 0, "OPEN", info["trigger"])
                    log_trade_csv(real_ticket, SYMBOL, info["side"], info["entry"],
                                  info["sl"], info["tp"], info["lot"], 0, "OPEN", info["trigger"])
                    admin_only(
                        f"✅ IFVG ENTRY FILLED\n\n"
                        f"Trigger   : {info['trigger']}\nSide      : {info['side']}\n"
                        f"Entry     : {filled_pos.price_open}\nTP        : {info['tp']}\n"
                        f"SL        : {info['sl']}\nLot       : {info['lot']}\n\n"
                        f"Price now : {round(get_current_price(),2)}\n"
                        f"Win Rate  : {get_trigger_winrate(info['trigger'])}\n"
                        f"Balance   : {get_balance()}\n⏱ SL/TP applied after 3-min hold")
                    print(f"monitor: ✅ Fill order={ticket} → pos={real_ticket}")
                else:
                    print(f"monitor: order #{ticket} gone, no fill — removing.")
                    with state_lock:
                        shared["pred_orders"].pop(ticket, None)

# ====================================
# MANAGE OPEN TRADES
# ====================================
def manage_open_trades():
    if not ensure_mt5():
        return
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return

    now = get_server_time()

    for pos in positions:
        if pos.magic != 101010:
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

        if open_time is None:
            if now - int(pos.time) < 5:
                continue

            print(f"manage_open_trades: untracked #{ticket} — registering.")
            with state_lock:
                shared["trade_open_times"][ticket] = int(pos.time)
                if ticket not in shared["active_tickets"]:
                    shared["active_tickets"].append(ticket)
                if ticket not in shared["trade_targets"]:
                    shared["trade_targets"][ticket] = {
                        "sl": current_sl if current_sl else 0,
                        "tp": tp         if tp         else 0,
                    }
                open_time = int(pos.time)
                targets   = shared["trade_targets"][ticket]
            admin_only(
                f"⚠️ Untracked IFVG position recovered\n"
                f"Ticket : #{ticket}\n"
                f"Side   : {'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'}\n"
                f"Entry  : {open_price}\nSL/TP will be applied if missing.")

        hold_remaining = max(0, MIN_HOLD_SECONDS - (now - open_time))
        if hold_remaining > 0:
            print(f"[Hold] #{ticket} — {round(hold_remaining)}s left")
            continue

        if targets and (current_sl == 0.0 or current_sl is None) and (tp == 0.0 or tp is None):
            sym_info  = mt5.symbol_info(SYMBOL)
            tick_now  = mt5.symbol_info_tick(SYMBOL)
            current_bid = tick_now.bid if tick_now else open_price
            current_ask = tick_now.ask if tick_now else open_price
            broker_min  = (sym_info.trade_stops_level * sym_info.point
                           if sym_info and sym_info.trade_stops_level > 0 else 0.5)
            min_stop_dist = max(broker_min * 3.0, 2.0)

            if targets["sl"] != 0 and targets["tp"] != 0:
                original_risk = abs(targets["sl"] - targets["tp"]) / (1 + RR_RATIO)
            else:
                original_risk = 15.0
            original_risk = max(original_risk, min_stop_dist)

            if pos.type == mt5.ORDER_TYPE_BUY:
                real_sl = normalize_price(SYMBOL, open_price - original_risk)
                real_tp = normalize_price(SYMBOL, open_price + original_risk * RR_RATIO)
                max_valid_sl = current_bid - min_stop_dist
                if real_sl > max_valid_sl:
                    real_sl = normalize_price(SYMBOL, max_valid_sl)
            else:
                real_sl = normalize_price(SYMBOL, open_price + original_risk)
                real_tp = normalize_price(SYMBOL, open_price - original_risk * RR_RATIO)
                min_valid_sl = current_ask + min_stop_dist
                if real_sl < min_valid_sl:
                    real_sl = normalize_price(SYMBOL, min_valid_sl)

            print(f"Applying SL/TP #{ticket} open={open_price} SL={real_sl} TP={real_tp}")
            if apply_sltp(ticket, real_sl, real_tp):
                admin_only(f"⏱ 3-min hold complete — SL/TP applied\nTicket : #{ticket}\nSL : {real_sl}\nTP : {real_tp}")
                with state_lock:
                    shared["trade_targets"].pop(ticket, None)
                current_sl = real_sl
                tp         = real_tp
            else:
                with state_lock:
                    shared["trade_targets"].pop(ticket, None)
                admin_only(
                    f"⚠️ Could not apply SL/TP for #{ticket}\n"
                    f"Please set manually in MT5.\n"
                    f"Entry: {open_price} | Current: {round(current_bid,2)}")
                continue

        if pos.type == mt5.ORDER_TYPE_BUY:
            current_price = tick.bid
            tp_distance   = tp - open_price if tp > 0 else 0
            if tp_distance <= 0 or current_sl <= 0:
                continue

            progress = (current_price - open_price) / tp_distance
            new_sl = current_sl
            locked_msg = ""
            
            if progress >= 0.75:
                proposed_sl = open_price + (tp_distance * 0.50)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit"
            elif progress >= 0.50:
                proposed_sl = open_price + (tp_distance * 0.35)
                if proposed_sl > new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit"

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
            
            if progress >= 0.75:
                proposed_sl = open_price - (tp_distance * 0.50)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "50% profit"
            elif progress >= 0.50:
                proposed_sl = open_price - (tp_distance * 0.35)
                if proposed_sl < new_sl:
                    new_sl = proposed_sl
                    locked_msg = "35% profit"

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
            monitor_predictive_orders()

            if ensure_mt5():
                manage_open_trades()

            if ensure_mt5():
                open_positions = mt5.positions_get(symbol=SYMBOL)
                if open_positions:
                    with state_lock:
                        for p in open_positions:
                            if p.magic == 101010:
                                if p.ticket not in shared["active_tickets"]:
                                    shared["active_tickets"].append(p.ticket)

            with state_lock:
                tickets = list(shared["active_tickets"])

            if tickets and ensure_mt5():
                open_positions = mt5.positions_get(symbol=SYMBOL)
                open_tickets   = {p.ticket for p in open_positions} if open_positions else set()

                for ticket in tickets:
                    if ticket not in open_tickets:
                        deals = mt5.history_deals_get(time.time() - 86400, time.time())
                        pnl   = 0.0
                        if deals:
                            for d in reversed(deals):
                                if d.position_id == ticket and d.entry == 1:
                                    pnl = round(d.profit + d.commission + d.swap, 2)
                                    break

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
                                 if v.get("real_ticket") == ticket), None)
                        trigger = pred_info["trigger"] if pred_info else "REACTIVE"

                        log_trade_db(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result, trigger)
                        log_trade_csv(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result, trigger)

                        if result == "WIN":
                            with state_lock:
                                was_trailed = ticket in shared["breakeven_tickets"]
                            emoji = "🎯"
                            label = "TRAILING SL — PROFIT SECURED 🔒" if was_trailed else "TP HIT ✅"
                        elif result == "BREAKEVEN":
                            emoji = "🔄"
                            label = "BREAKEVEN — SL AT ENTRY"
                        else:
                            emoji = "🛑"
                            label = "SL HIT ❌"

                        pnl_str = f"+${pnl}" if pnl >= 0 else f"-${abs(pnl)}"
                        overall = get_stats()
                        wr_str  = f"{round(overall['win_rate']*100,1)}%" if overall else "N/A"

                        broadcast(
                            f"{emoji} *IFVG {label}*\n\n"
                            f"🎫 *Ticket:* #{ticket}\n"
                            f"📌 *Trigger:* {trigger}\n"
                            f"💰 *PnL:* {pnl_str}\n"
                            f"🏦 *Balance:* ${balance}\n"
                            f"💹 *Price now:* {round(get_current_price(), 2)}\n\n"
                            f"📊 *Overall W/R:* {wr_str}\n"
                            f"📌 *{trigger} W/R:* {get_trigger_winrate(trigger)}",
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

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

def is_admin(message):
    return str(message.chat.id) == str(CHAT_ID)

def admin_guard(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ This command is for the admin only.")
        return False
    return True

@bot.message_handler(commands=["start"])
def start_command(message):
    save_user(message.chat.id)
    name = message.from_user.first_name or "Trader"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📊 Status",  callback_data="status"),
        InlineKeyboardButton("📈 Stats",   callback_data="stats"),
        InlineKeyboardButton("📋 Pending", callback_data="pending"),
        InlineKeyboardButton("🔍 Debug",   callback_data="debug"),
    )
    bot.reply_to(message,
        f"👋 Hey {name}! Welcome to *Akshath IFVG Predictive Bot*\n\n"
        f"📍 *Symbol:* XAUUSD  |  *TF:* M1\n"
        f"🤖 *Mode:* Predictive IFVG — Inverted Fair Value Gaps\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Quick Commands:*\n\n"
        f"📊 /status — open trades & hold timers\n"
        f"📈 /stats — full P\\&L breakdown by trigger\n"
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
        "📖 *IFVG Bot — Command Reference*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Info Commands:*\n"
        "/status — open trades with entry, SL, TP, PnL & hold timer\n"
        "/stats — win rate & PnL per trigger type\n"
        "/pending — all limit orders currently waiting to fill\n"
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

@bot.callback_query_handler(func=lambda call: call.data in ("status","stats","pending","debug"))
def handle_quick_buttons(call):
    bot.answer_callback_query(call.id)
    if call.data == "status":
        cmd_status(call.message)
    elif call.data == "stats":
        cmd_stats(call.message)
    elif call.data == "pending":
        cmd_pending(call.message)
    elif call.data == "debug":
        cmd_debug(call.message)

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
    m15           = get_trend(TIMEFRAME_M15)
    h4            = get_trend(TIMEFRAME_H4)
    open_count    = get_open_trade_count()

    with state_lock:
        paused   = shared["paused"]
        loss_hit = shared["daily_loss_hit"]
        n_pred   = len([v for v in shared["pred_orders"].values() if not v["filled"]])

    status_emoji = "⏸" if paused else ("🚨" if loss_hit else "🟢")
    status_label = "PAUSED" if paused else ("DAILY LOSS HIT" if loss_hit else "ACTIVE")
    m15_emoji    = "📈" if m15 == "bullish" else ("📉" if m15 == "bearish" else "➡️")
    h4_emoji     = "📈" if h4  == "bullish" else ("📉" if h4  == "bearish" else "➡️")

    header = (
        f"📊 *IFVG Bot — Status*\n\n"
        f"{status_emoji} *Status:* {status_label}\n"
        f"💰 *Balance:* ${balance}\n"
        f"💹 *XAUUSD:* {round(current_price, 2)}\n"
        f"{m15_emoji} *M15 Trend:* {m15.upper()}\n"
        f"{h4_emoji} *H4 Trend:* {h4.upper()}\n"
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
        if p.magic != 101010:
            continue
        side      = "BUY 🟢" if p.type == mt5.ORDER_TYPE_BUY else "SELL 🔴"
        profit    = round(p.profit, 2)
        pnl_str   = f"+${profit}" if profit >= 0 else f"-${abs(profit)}"
        pnl_emoji = "✅" if profit >= 0 else "❌"

        with state_lock:
            open_time = shared["trade_open_times"].get(p.ticket)
            pred_info = next((v for v in shared["pred_orders"].values()
                              if v.get("real_ticket") == p.ticket), None)

        hold_left = max(0, MIN_HOLD_SECONDS - (get_server_time() - open_time)) if open_time else 0
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
            "_The bot will place orders when the next IFVG trigger fires._",
            parse_mode="Markdown")
        return

    now   = time.time()
    lines = [f"📋 *Pending IFVG Limit Orders* — {len(unfilled)} waiting\n\n━━━━━━━━━━━━━━━━━━━━\n"]

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
            f"   Win Rate: *{round(s['win_rate']*100, 1)}%* "
            f"({s['wins']}W{be_str} / {s['losses']}L)  |  PnL: *{pnl_str}*"
        )

    today_pnl   = get_today_pnl()
    today_emoji = "✅" if today_pnl >= 0 else "❌"

    bot.reply_to(message,
        f"📈 *IFVG Scalper — Statistics*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Overall* ({overall['total']} trades)\n"
        f"{fmt(overall)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*By Trigger Type:*\n\n"
        f"🎯 *IFVG APPROACH*\n{fmt(get_stats('IFVG_APPROACH'))}\n\n"
        f"⚡ *INVERSION FORMING*\n{fmt(get_stats('INVERSION_FORMING'))}\n\n"
        f"💥 *STRUCT BREAK M5*\n{fmt(get_stats('STRUCT_BREAK_M5'))}\n\n"
        f"🔁 *REACTIVE*\n{fmt(get_stats('REACTIVE'))}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{today_emoji} *Today's P&L:* {today_pnl}\n"
        f"💰 *Balance:* ${get_balance()}",
        parse_mode="Markdown")

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
        if pos.magic != 101010:
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
        if order.magic != 101010:
            continue
        with state_lock:
            gap_id = shared["pred_orders"].get(order.ticket, {}).get("gap_id")
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  order.ticket,
        })
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            cancelled.append(order.ticket)
            with state_lock:
                shared["pred_orders"].pop(order.ticket, None)
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
        "_The bot is now scanning for IFVG setups._",
        parse_mode="Markdown")

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
        df_m1 = load_mt5(SYMBOL, TIMEFRAME_M1, 1000)
        if df_m1 is None:
            bot.send_message(CHAT_ID,
                "❌ *MT5 Data Unavailable*\n\n"
                "Connected to MT5 but failed to fetch M1 candle data for XAUUSD.",
                parse_mode="Markdown")
            return

        fvgs          = detect_fvg(df_m1)
        ifvgs         = detect_ifvg(df_m1, fvgs)
        ifvg_count    = get_fresh_fvg_count(df_m1, fvgs, lookback=50)
        m15           = get_trend(TIMEFRAME_M15)
        h4            = get_trend(TIMEFRAME_H4)
        momentum      = ifvg_count >= MOMENTUM_IFVG_THRESHOLD
        price         = get_current_price()
        balance       = get_balance()
        news_active   = is_news_window()
        swept_high, swept_low = check_liquidity_sweep(df_m1)

        with state_lock:
            paused   = shared["paused"]
            loss_hit = shared["daily_loss_hit"]
            n_pred   = len([v for v in shared["pred_orders"].values() if not v["filled"]])
            n_fired  = len(shared["fired_gaps"])

        status_emoji = "⏸" if paused else ("🚨" if loss_hit else "🟢")
        m15_emoji    = "📈" if m15 == "bullish" else ("📉" if m15 == "bearish" else "➡️")
        h4_emoji     = "📈" if h4  == "bullish" else ("📉" if h4  == "bearish" else "➡️")

        bot.send_message(CHAT_ID,
            f"🔍 *IFVG Bot Diagnostics*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_emoji} *Status:* {'PAUSED' if paused else ('DAILY LOSS HIT' if loss_hit else 'ACTIVE')}\n"
            f"💰 *Balance:* ${balance}\n"
            f"💹 *XAUUSD Price:* {round(price, 2)}\n"
            f"{m15_emoji} *M15 Trend:* {m15.upper()}\n"
            f"{h4_emoji} *H4 Trend:* {h4.upper()}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 *Open Trades:* {get_open_trade_count()} / {MAX_TRADES}\n"
            f"⏳ *Pending Orders:* {n_pred} waiting\n"
            f"🔥 *Gaps Fired:* {n_fired} this session\n"
            f"📡 *Valid IFVGs:* {len(ifvgs)}\n"
            f"📊 *Fresh FVGs:* {ifvg_count} (last 50 M1 candles)\n"
            f"🚀 *Momentum Mode:* {'YES' if momentum else f'NO (need {MOMENTUM_IFVG_THRESHOLD})'}\n"
            f"🧹 *Liquidity Sweep:* H={swept_high} | L={swept_low}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📰 *News Window:* {'🚫 YES — trading paused' if news_active else '✅ Clear'}\n"
            f"⚠️ *Daily Loss Hit:* {'YES 🚨' if loss_hit else 'NO ✅'}\n",
            parse_mode="Markdown")
    except Exception as e:
        bot.send_message(CHAT_ID,
            f"❌ *Diagnostics Error*\n\n`{e}`",
            parse_mode="Markdown")

# ====================================
# HEARTBEAT + DAILY SUMMARY
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
                f"💓 IFVG Bot Heartbeat\n"
                f"Status   : {status}\nBalance  : {balance}\n"
                f"Price    : {round(price,2)}\nOpen     : {n_open} trade(s)\n"
                f"Pending  : {n_pred} predictive order(s)")
        except Exception as e:
            print("Heartbeat error:", e)

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
                        f"📅 IFVG Bot Daily Summary — {now.strftime('%Y-%m-%d')}\n\n"
                        f"Today's P&L  : {get_today_pnl()}\nBalance      : {balance}\n"
                        f"Total Trades : {overall['total']}\n"
                        f"Win Rate     : {round(overall['win_rate']*100,1)}%\n"
                        f"All-time P&L : {overall['total_pnl']}\n\n"
                        f"IFVG_APPROACH    : {get_trigger_winrate('IFVG_APPROACH')}\n"
                        f"INVERSION_FORMING: {get_trigger_winrate('INVERSION_FORMING')}\n"
                        f"STRUCT_BREAK_M5  : {get_trigger_winrate('STRUCT_BREAK_M5')}")
                reset_daily_state()
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
    print("IFVG Predictive Scanner Started")
    broadcast("⚡ Akshath IFVG Predictive Bot Online — XAUUSD M1", parse_mode="Markdown")
    scan_count = 0

    while True:
        try:
            scan_count += 1
            with state_lock:
                paused = shared["paused"]

            if paused or check_daily_loss() or not is_trading_session():
                time.sleep(SCAN_INTERVAL)
                continue
            if get_open_trade_count() >= MAX_TRADES:
                time.sleep(SCAN_INTERVAL)
                continue
            if is_news_window():
                time.sleep(SCAN_INTERVAL)
                continue

            df_m1 = load_mt5(SYMBOL, TIMEFRAME_M1, 1000)
            if df_m1 is None:
                time.sleep(10)
                continue

            fvgs          = detect_fvg(df_m1)
            ifvgs         = detect_ifvg(df_m1, fvgs)
            ifvg_count    = get_fresh_fvg_count(df_m1, fvgs, lookback=50)
            current_price = get_current_price()
            m15           = get_trend(TIMEFRAME_M15)
            h4            = get_trend(TIMEFRAME_H4)
            momentum      = ifvg_count >= MOMENTUM_IFVG_THRESHOLD
            
            swept_high, swept_low = check_liquidity_sweep(df_m1)

            signals = []

            t1 = trigger1_ifvg_approach(df_m1, fvgs, ifvgs, current_price, ifvg_count, swept_high, swept_low)
            signals.extend(t1)

            t2 = trigger2_inversion_forming(df_m1, fvgs, ifvg_count, swept_high, swept_low)
            signals.extend(t2)

            t3 = trigger3_structure_break_m5(ifvg_count, swept_high, swept_low)
            signals.extend(t3)

            for side, entry, sl, tp, gap_id, trigger in signals:
                with state_lock:
                    open_count    = get_open_trade_count()
                    pending_count = len([v for v in shared["pred_orders"].values()
                                         if not v["filled"]])
                if open_count + pending_count >= MAX_TRADES:
                    print(f"[Scan #{scan_count}] Max trades+pending — skipping {trigger}")
                    break
                ticket = place_limit_order(side, entry, sl, tp, trigger, gap_id)
                if ticket:
                    print(f"[Scan #{scan_count}] ✅ {trigger} {side} @ {entry} → #{ticket}")

            if signals:
                print(f"[Scan #{scan_count}] Triggers: {[s[5] for s in signals]} | "
                      f"IFVGs={len(ifvgs)} FreshFVGs={ifvg_count} | "
                      f"M15={m15.upper()} H4={h4.upper()}")
            else:
                print(f"[Scan #{scan_count}] No triggers | IFVGs={len(ifvgs)} "
                      f"FreshFVGs={ifvg_count} | Sweeps(H/L): {swept_high}/{swept_low} | "
                      f"M15={m15.upper()} H4={h4.upper()} {'MOMENTUM 🚀' if momentum else ''} | "
                      f"Price={round(current_price,2)}")

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
        print(f"\nIFVG Predictive Config:")
        print(f"  MIN_IFVG_GAP         : {MIN_IFVG_GAP}")
        print(f"  MOMENTUM_THRESHOLD   : {MOMENTUM_IFVG_THRESHOLD} fresh FVGs")
        print(f"  APPROACH_PROXIMITY   : {APPROACH_PROXIMITY} pts")
        print(f"  ENTRY_INSIDE_PCT     : {int(ENTRY_INSIDE_PCT*100)}% from edge")
        print(f"  PENDING_EXPIRY       : {PENDING_EXPIRY_MIN} min")
        print(f"  RR_RATIO             : {RR_RATIO}")
        print(f"  STRUCTURE_LOOKBACK   : {STRUCTURE_LOOKBACK} M5 candles\n")

    threading.Thread(target=market_scanner, daemon=True).start()
    threading.Thread(target=trade_monitor,  daemon=True).start()
    threading.Thread(target=heartbeat,      daemon=True).start()
    threading.Thread(target=daily_summary,  daemon=True).start()

    print("IFVG Predictive Bot Listening...")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling dropped ({type(e).__name__}): {e} — reconnecting in 15s...")
            time.sleep(15)