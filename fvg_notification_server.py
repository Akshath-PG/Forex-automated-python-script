"""
FVG Hybrid Bot — Python Notification Server
Receives HTTP events from MQL5 EA and sends Telegram messages.
Also handles /status, /stats, /pending, /pause, /resume commands.
Run this alongside MT5. It does NOT connect to MT5 directly.
"""

import sqlite3
import csv
import os
import time
import threading
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from json import loads, dumps

import telebot
import MetaTrader5 as mt5

logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

# ====================================
# CONFIGURATION
# ====================================
TELEGRAM_TOKEN = "8792398021:AAGKffX2hKEkswP-I2WyYfR01ofUHQqS74Y"
CHAT_ID        = "5667584601"
SERVER_PORT    = 5000

SYMBOL              = "XAUUSD"
HEARTBEAT_INTERVAL  = 3600
DAILY_SUMMARY_HOUR  = 22
WIN_RATE_THRESHOLD  = 0.60

CSV_FILE   = "fvg_trade_log.csv"
DB_FILE    = "fvg_trade_log.db"
USERS_FILE = "fvg_users.txt"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ====================================
# SHARED STATE (read-only mirror of EA)
# ====================================
state_lock   = threading.Lock()
server_state = {
    "paused":          False,
    "pending_orders":  {},   # ticket → info dict from EA
    "open_trades":     {},   # ticket → info dict
    "last_event_time": None,
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

def broadcast(text):
    for uid in get_all_users():
        try:
            bot.send_message(uid, text)
        except Exception as e:
            print(f"Broadcast error {uid}: {e}")

def admin_only(text, reply_markup=None):
    try:
        if reply_markup:
            bot.send_message(CHAT_ID, text, reply_markup=reply_markup)
        else:
            bot.send_message(CHAT_ID, text)
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
    conn.commit()
    conn.close()

def log_trade(ticket, symbol, side, entry, sl, tp, lot, pnl, result, trigger):
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

    try:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["ticket","symbol","side","entry","sl","tp",
                                 "lot","pnl","result","trigger","timestamp"])
            writer.writerow([ticket, symbol, side, entry, sl, tp, lot, pnl,
                             result, trigger,
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except Exception as e:
        print("CSV log error:", e)

def get_stats(trigger=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if trigger:
            c.execute("SELECT result, pnl FROM trades WHERE result IN ('WIN','LOSS') AND trigger=?",
                      (trigger,))
        else:
            c.execute("SELECT result, pnl FROM trades WHERE result IN ('WIN','LOSS')")
        rows = c.fetchall()
        conn.close()
        if not rows:
            return None
        wins      = [r for r in rows if r[0] == "WIN"]
        total     = len(rows)
        win_rate  = len(wins) / total
        total_pnl = sum(r[1] for r in rows)
        best      = max(rows, key=lambda r: r[1])
        worst     = min(rows, key=lambda r: r[1])
        return {
            "total":     total,
            "wins":      len(wins),
            "losses":    total - len(wins),
            "win_rate":  win_rate,
            "total_pnl": round(total_pnl, 2),
            "best":      round(best[1], 2),
            "worst":     round(worst[1], 2),
        }
    except Exception as e:
        print("Stats error:", e)
        return None

def get_trigger_winrate(trigger):
    stats = get_stats(trigger=trigger)
    if not stats or stats["total"] < 3:
        return "N/A (< 3 trades)"
    return f"{round(stats['win_rate']*100,1)}% ({stats['wins']}/{stats['total']})"

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
# MT5 HELPERS (read-only — just for /status)
# ====================================
def get_balance():
    try:
        if mt5.terminal_info():
            acc = mt5.account_info()
            return round(acc.balance, 2) if acc else 0
    except:
        pass
    return 0

def get_current_price():
    try:
        if mt5.terminal_info():
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick:
                return round((tick.bid + tick.ask) / 2, 2)
    except:
        pass
    return 0

# ====================================
# EVENT HANDLERS
# Receive events from MQL5 and format Telegram messages
# ====================================
def handle_event(data: dict):
    event_type = data.get("type", "UNKNOWN")
    with state_lock:
        server_state["last_event_time"] = datetime.now()

    print(f"Event received: {event_type} | {data}")

    # ── Bot started ──────────────────────────────────────────────────
    if event_type == "BOT_STARTED":
        broadcast(f"🤖 FVG Predictive EA Online\n{data.get('message','')}")

    elif event_type == "BOT_STOPPED":
        admin_only(f"⚠️ FVG EA Stopped\n{data.get('message','')}")

    # ── Predictive order placed ──────────────────────────────────────
    elif event_type == "ORDER_PLACED":
        trigger  = data.get("trigger", "")
        side     = data.get("side", "")
        entry    = data.get("entry", 0)
        sl       = data.get("sl", 0)
        tp       = data.get("tp", 0)
        lot      = data.get("lot", 0)
        ticket   = data.get("ticket", "")
        balance  = data.get("balance", 0)
        wr       = get_trigger_winrate(trigger)

        with state_lock:
            server_state["pending_orders"][str(ticket)] = {
                "trigger": trigger, "side": side,
                "entry": entry, "sl": sl, "tp": tp,
                "lot": lot, "placed_at": time.time()
            }

        admin_only(
            f"🔮 PREDICTIVE LIMIT PLACED\n\n"
            f"Trigger   : {trigger}\n"
            f"Side      : {side}\n"
            f"Entry     : {entry}\n"
            f"TP        : {tp}\n"
            f"SL        : {sl}\n"
            f"Lot       : {lot}\n\n"
            f"Win Rate  : {wr}\n"
            f"Balance   : {balance}\n"
            f"Expires   : 15 min if unfilled\n"
            f"Ticket    : #{ticket}")

    # ── Order filled / trade entered ─────────────────────────────────
    elif event_type == "TRADE_FILLED":
        trigger = data.get("trigger", "")
        side    = data.get("side", "")
        entry   = data.get("entry", 0)
        sl      = data.get("sl", 0)
        tp      = data.get("tp", 0)
        lot     = data.get("lot", 0)
        ticket  = data.get("ticket", "")
        balance = data.get("balance", 0)
        price   = get_current_price()
        wr      = get_trigger_winrate(trigger)

        with state_lock:
            server_state["open_trades"][str(ticket)] = {
                "trigger": trigger, "side": side,
                "entry": entry, "sl": sl, "tp": tp, "lot": lot
            }
            server_state["pending_orders"].pop(str(ticket), None)

        log_trade(ticket, SYMBOL, side, entry, sl, tp, lot, 0, "OPEN", trigger)

        admin_only(
            f"✅ PREDICTIVE ENTRY FILLED\n\n"
            f"Trigger   : {trigger}\n"
            f"Side      : {side}\n"
            f"Entry     : {entry}\n"
            f"TP        : {tp}\n"
            f"SL        : {sl}\n"
            f"Lot       : {lot}\n\n"
            f"Price now : {price}\n"
            f"Win Rate  : {wr}\n"
            f"Balance   : {balance}\n"
            f"⏱ SL/TP applied after 3-min hold")

    # ── SL/TP applied after hold ─────────────────────────────────────
    elif event_type == "SLTP_APPLIED":
        ticket = data.get("ticket", "")
        sl     = data.get("sl", 0)
        tp     = data.get("tp", 0)
        admin_only(
            f"⏱ 3-min hold complete\n"
            f"Ticket : #{ticket}\n"
            f"SL     : {sl}\n"
            f"TP     : {tp}")

    # ── Breakeven triggered ──────────────────────────────────────────
    elif event_type == "BREAKEVEN":
        ticket  = data.get("ticket", "")
        sl      = data.get("sl", 0)
        trigger = data.get("trigger", "")
        side    = data.get("side", "")
        admin_only(f"🔒 {side} #{ticket}: SL moved to breakeven ({sl})")

    # ── Trailing stop moved ──────────────────────────────────────────
    elif event_type == "TRAIL_MOVED":
        ticket  = data.get("ticket", "")
        new_sl  = data.get("new_sl", 0)
        # Silent — just log, no Telegram spam on every trail move
        print(f"Trail moved: #{ticket} new SL={new_sl}")

    # ── TP hit ───────────────────────────────────────────────────────
    elif event_type == "TP_HIT":
        _handle_close(data, won=True)

    # ── SL hit ───────────────────────────────────────────────────────
    elif event_type == "SL_HIT":
        _handle_close(data, won=False)

    # ── Order expired ─────────────────────────────────────────────────
    elif event_type == "ORDER_EXPIRED":
        ticket  = data.get("ticket", "")
        trigger = data.get("trigger", "")
        side    = data.get("side", "")
        entry   = data.get("entry", 0)
        with state_lock:
            server_state["pending_orders"].pop(str(ticket), None)
        admin_only(
            f"⏱ Predictive order expired\n"
            f"Ticket  : #{ticket}\n"
            f"Trigger : {trigger}\n"
            f"Side    : {side}\n"
            f"Entry   : {entry}")

    # ── Daily loss hit ────────────────────────────────────────────────
    elif event_type == "DAILY_LOSS_HIT":
        loss_pct = data.get("loss_pct", 0)
        balance  = data.get("balance", 0)
        admin_only(
            f"🚨 DAILY LOSS LIMIT HIT\n"
            f"Loss    : {loss_pct}%\n"
            f"Balance : {balance}\n"
            f"EA has stopped trading for today.")

def _handle_close(data: dict, won: bool):
    ticket  = data.get("ticket", "")
    trigger = data.get("trigger", "UNKNOWN")
    pnl     = data.get("pnl", 0)
    balance = data.get("balance", 0)
    price   = data.get("price", get_current_price())
    result  = "WIN" if won else "LOSS"
    emoji   = "🎯" if won else "🛑"
    label   = "TP HIT" if won else "SL HIT"

    log_trade(ticket, SYMBOL, "CLOSED", 0, 0, 0, 0, pnl, result, trigger)

    with state_lock:
        server_state["open_trades"].pop(str(ticket), None)

    overall = get_stats()
    wr_str  = f"{round(overall['win_rate']*100,1)}%" if overall else "N/A"
    trig_wr = get_trigger_winrate(trigger)

    broadcast(
        f"{emoji} FVG {label}\n\n"
        f"Ticket        : #{ticket}\n"
        f"Trigger       : {trigger}\n"
        f"PnL           : {pnl}\n"
        f"Balance       : {balance}\n"
        f"Price now     : {price}\n\n"
        f"Overall W/R   : {wr_str}\n"
        f"{trigger} W/R : {trig_wr}")

    # Auto-pause if win rate drops below threshold
    if overall and overall["total"] >= 10:
        if overall["win_rate"] < WIN_RATE_THRESHOLD:
            admin_only(
                f"⚠️ Win rate {round(overall['win_rate']*100,1)}% below "
                f"{int(WIN_RATE_THRESHOLD*100)}%\n"
                f"Consider pausing the EA manually.")

# ====================================
# HTTP SERVER — receives events from MQL5
# ====================================
class EventHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default HTTP logs

    def do_POST(self):
        if self.path != "/event":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            data   = loads(body.decode("utf-8"))

            # Check pause state
            with state_lock:
                paused = server_state["paused"]
            if paused and data.get("type") not in (
                "BOT_STOPPED", "DAILY_LOSS_HIT", "TP_HIT", "SL_HIT"
            ):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"paused"}')
                return

            threading.Thread(target=handle_event, args=(data,), daemon=True).start()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception as e:
            print(f"HTTP handler error: {e}")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        # Health check endpoint
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"running"}')
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server = HTTPServer(("localhost", SERVER_PORT), EventHandler)
    print(f"HTTP event server listening on localhost:{SERVER_PORT}")
    server.serve_forever()

# ====================================
# TELEGRAM COMMANDS
# ====================================
@bot.message_handler(commands=["start"])
def start_command(message):
    save_user(message.chat.id)
    bot.reply_to(message,
        "✅ Welcome to Akshath FVG Predictive Bot!\n"
        "Subscribed to live M5 FVG signals on XAUUSD.\n\n"
        "Commands:\n"
        "/status  — open trades + balance\n"
        "/stats   — P&L breakdown by trigger\n"
        "/pending — predictive orders waiting to fill\n"
        "/pause   — pause EA notifications (admin)\n"
        "/resume  — resume (admin)")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    balance = get_balance()
    price   = get_current_price()
    with state_lock:
        paused     = server_state["paused"]
        open_t     = dict(server_state["open_trades"])
        last_event = server_state["last_event_time"]

    status_str = "⏸ PAUSED" if paused else "🟢 ACTIVE"
    last_ev_str = last_event.strftime("%H:%M:%S") if last_event else "never"

    if not open_t:
        bot.reply_to(message,
            f"📊 FVG Bot — {status_str}\n"
            f"No open trades\n"
            f"Balance   : {balance}\n"
            f"Price     : {price}\n"
            f"Last event: {last_ev_str}")
        return

    lines = [f"📊 FVG Bot — {status_str} | Balance: {balance} | Price: {price}\n"]
    for ticket, info in open_t.items():
        side    = info.get("side", "?")
        trigger = info.get("trigger", "?")
        entry   = info.get("entry", 0)
        sl      = info.get("sl", 0)
        tp      = info.get("tp", 0)
        lines.append(
            f"{'🟢' if side=='BUY' else '🔴'} {side} #{ticket} [{trigger}]\n"
            f"  Entry: {entry}  SL: {sl}  TP: {tp}\n")
    lines.append(f"\nLast EA event: {last_ev_str}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["pending"])
def cmd_pending(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    with state_lock:
        pending = dict(server_state["pending_orders"])
    if not pending:
        bot.reply_to(message, "📋 No predictive orders pending.")
        return
    now   = time.time()
    lines = ["📋 Predictive Orders Pending\n"]
    for ticket, info in pending.items():
        age_min    = round((now - info.get("placed_at", now)) / 60, 1)
        expires_in = max(0, 15 - age_min)
        lines.append(
            f"#{ticket} — {info['trigger']}\n"
            f"  {info['side']} @ {info['entry']}\n"
            f"  TP: {info['tp']}  SL: {info['sl']}\n"
            f"  Age: {age_min}m | Expires: {round(expires_in,1)}m\n")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    overall = get_stats()
    if not overall:
        bot.reply_to(message, "📈 No closed trades yet.")
        return

    def fmt(s):
        if not s:
            return "No trades yet"
        return (f"{round(s['win_rate']*100,1)}% "
                f"({s['wins']}W/{s['losses']}L)  PnL: {s['total_pnl']}")

    bot.reply_to(message,
        f"📈 FVG Predictive Statistics\n\n"
        f"Overall         : {fmt(overall)}\n\n"
        f"By Trigger:\n"
        f"APPROACH        : {fmt(get_stats('APPROACH'))}\n"
        f"LIVE FORMING    : {fmt(get_stats('LIVE_FORMING'))}\n"
        f"STRUCTURE BREAK : {fmt(get_stats('STRUCTURE_BREAK'))}\n\n"
        f"Today's P&L  : {get_today_pnl()}\n"
        f"Balance      : {get_balance()}")

@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    with state_lock:
        server_state["paused"] = True
    bot.reply_to(message,
        "⏸ Notification server paused.\n"
        "Note: EA in MT5 continues trading — pause it manually in MT5 if needed.")

@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Admin only.")
        return
    with state_lock:
        server_state["paused"] = False
    bot.reply_to(message, "▶️ Notification server resumed.")

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
                paused     = server_state["paused"]
                n_open     = len(server_state["open_trades"])
                n_pending  = len(server_state["pending_orders"])
                last_event = server_state["last_event_time"]

            status     = "⏸ PAUSED" if paused else "🟢 ACTIVE"
            last_ev_str = last_event.strftime("%H:%M:%S") if last_event else "never"

            admin_only(
                f"💓 FVG Bot Heartbeat\n"
                f"Status   : {status}\n"
                f"Balance  : {balance}\n"
                f"Price    : {price}\n"
                f"Open     : {n_open} trade(s)\n"
                f"Pending  : {n_pending} order(s)\n"
                f"Last EA  : {last_ev_str}")
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
                        f"Today's P&L     : {get_today_pnl()}\n"
                        f"Balance         : {balance}\n"
                        f"Total Trades    : {overall['total']}\n"
                        f"Win Rate        : {round(overall['win_rate']*100,1)}%\n"
                        f"All-time P&L    : {overall['total_pnl']}\n\n"
                        f"APPROACH W/R    : {get_trigger_winrate('APPROACH')}\n"
                        f"LIVE FORMING    : {get_trigger_winrate('LIVE_FORMING')}\n"
                        f"STRUCTURE BREAK : {get_trigger_winrate('STRUCTURE_BREAK')}")
            except Exception as e:
                print("Daily summary error:", e)
        time.sleep(60)

# ====================================
# MAIN
# ====================================
if __name__ == "__main__":
    init_db()

    # Connect MT5 for read-only balance/price queries
    if mt5.initialize():
        print("MT5 connected (read-only for balance/price).")
    else:
        print("MT5 not connected — balance/price in /status will show 0.")
        print("This is OK — EA handles all trading independently.")

    # Start HTTP server in background thread
    threading.Thread(target=run_server, daemon=True).start()

    # Start background threads
    threading.Thread(target=heartbeat,     daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()

    print(f"Python notification server ready on localhost:{SERVER_PORT}")
    print("Waiting for events from MT5 EA...")
    print("Telegram bot listening...")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling dropped ({type(e).__name__}): {e} — reconnecting in 15s...")
            time.sleep(15)
