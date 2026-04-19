import pandas as pd
import MetaTrader5 as mt5
import time
import threading
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import os

# ====================================
# CONFIGURATION
# ====================================
TELEGRAM_TOKEN = "8699832560:AAEQ9is_E5WOJCfu-lvzQCPPYjcUWZxNRzg"
CHAT_ID = "5667584601"

SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M1
ACCOUNT_SIZE = 51000
RISK_PERCENT = 0.5 / 100
CONTRACT_SIZE = 100

SCAN_INTERVAL = 60
SIGNAL_TIMEOUT = 600

bot = telebot.TeleBot(TELEGRAM_TOKEN)

pending_signal = {}
signal_time = None
last_entry = None

# ✅ NEW
active_trade_ticket = None

# ====================================
# USER PERSISTENCE
# ====================================
def save_user(chat_id):
    chat_id = str(chat_id)
    # Check if user already exists to prevent duplicates
    try:
        with open("users.txt", "r") as f:
            existing_users = f.read().splitlines()
    except FileNotFoundError:
        existing_users = []

    if chat_id not in existing_users:
        with open("users.txt", "a") as f:
            f.write(chat_id + "\n")
        print(f"New user added: {chat_id}")

@bot.message_handler(commands=['start'])
def start_command(message):
    save_user(message.chat.id)
    bot.reply_to(message, "✅ Welcome to Akshath XAU Bot! You are now subscribed to live IFVG signals.")

def broadcast_signal(message_text, reply_markup=None):
    try:
        with open("users.txt", "r") as f:
            user_ids = f.read().splitlines()
        
        for user_id in user_ids:
            try:
                # This sends the signal + the Approve/Deny buttons to EVERYONE
                bot.send_message(user_id, message_text, reply_markup=reply_markup)
            except Exception as e:
                print(f"Error sending to {user_id}: {e}")
                # Optional: If error is 'Bot was blocked by user', remove them from file
    except FileNotFoundError:
        print("No users found in users.txt")

# ====================================
# MT5 DATA
# ====================================
def load_mt5(symbol, timeframe, bars):
    if not mt5.initialize():
        print("MT5 Initialization failed.")
        return None

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)

    if rates is None:
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    return df

# ====================================
# ACCOUNT INFO (NEW)
# ====================================
def get_balance():
    acc = mt5.account_info()
    if acc:
        return round(acc.balance, 2)
    return 0

# ====================================
# FVG DETECTION (same)
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

def detect_ifvg(df, fvgs):
    ifvgs = []
    for gap in fvgs:
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
# SIGNAL LOGIC (same)
# ====================================
def trade_signal(fvgs, ifvgs):
    # Only trade strong IFVGs (ignore regular FVGs)
    if not ifvgs:
        return "WAIT",0,0,0

    gap = ifvgs[-1]
    entry = (gap["low"] + gap["high"]) / 2
    risk_range = abs(gap["high"] - gap["low"])

    if risk_range < 8:
        risk_range = 8

    if "bearish" in gap["type"]:
        return "SELL", entry, entry - (risk_range * 1.5), entry + risk_range
    else:
        return "BUY", entry, entry + (risk_range * 1.5), entry - risk_range

# ====================================
# LOT SIZE (same)
# ====================================
def lot_size(entry, sl):
    if entry == 0 or sl == 0 or entry == sl:
        return 0.0

    stop_distance = abs(entry - sl)
    lot = (ACCOUNT_SIZE * RISK_PERCENT) / (stop_distance * CONTRACT_SIZE)
    lot = max(0.1, min(1.0, lot))
    return round(lot,2)

# ====================================
# EXECUTE TRADE (MODIFIED)
# ====================================
def execute_trade(action, symbol, lot, entry_price, sl, tp):
    global active_trade_ticket

    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None

    spread = tick.ask - tick.bid
    if spread > 2:
        print("Spread too high, skipping trade")
        return None

    if action == "BUY":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if tick.ask > entry_price else mt5.ORDER_TYPE_BUY_STOP
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT if tick.bid < entry_price else mt5.ORDER_TYPE_SELL_STOP

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": entry_price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 101010,
        "comment": "TG Approved",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN
    }

    result = mt5.order_send(request)

    # ✅ store ticket
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        active_trade_ticket = result.order

    return result

# ====================================
# TRADE MONITOR (NEW)
# ====================================
def trade_monitor():
    global active_trade_ticket

    while True:
        try:
            if active_trade_ticket is None:
                time.sleep(5)
                continue

            positions = mt5.positions_get()

            # If no open positions → trade closed
            if not positions:
                deals = mt5.history_deals_get(time.time()-3600, time.time())

                if deals:
                    last = deals[-1]
                    pnl = round(last.profit,2)
                    balance = get_balance()

                    if pnl > 0:
                        broadcast_signal(
                            f"🎯 TP HIT\nPnL: {pnl}\nBalance: {balance}")
                    else:
                        broadcast_signal(
                            f"🛑 SL HIT\nPnL: {pnl}\nBalance: {balance}")

                active_trade_ticket = None

        except Exception as e:
            print("Monitor error:", e)

        time.sleep(5)

# ====================================
# TELEGRAM HANDLERS (same)
# ====================================
@bot.callback_query_handler(func=lambda call: call.data == "approve_trade")
def handle_approval(call):
    global pending_signal

    if not pending_signal:
        bot.answer_callback_query(call.id,"No active signal")
        return

    res = execute_trade(
        pending_signal["side"],
        SYMBOL,
        pending_signal["lot"],
        pending_signal["entry"],
        pending_signal["sl"],
        pending_signal["tp"]
    )

    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        broadcast_signal(f"✅ Trade placed successfully\nTicket: {res.order}")
    else:
        error_code = res.retcode if res else "Unknown"
        broadcast_signal(f"❌ Trade failed\nError: {error_code}")

    pending_signal.clear()

@bot.callback_query_handler(func=lambda call: call.data == "deny_trade")
def handle_deny(call):
    global pending_signal

    pending_signal.clear()

    bot.answer_callback_query(call.id,"Signal denied")
    broadcast_signal("❌ Signal rejected. Bot scanning again.")

# ====================================
# MARKET SCANNER (MINIMAL CHANGE)
# ====================================
def market_scanner():
    global pending_signal
    global signal_time
    global last_entry

    print("Market Scanner Started")
    broadcast_signal("🤖 AI Trading Bot Online & Scanning XAUUSD")

    while True:
        try:
            if pending_signal and (time.time() - signal_time > SIGNAL_TIMEOUT):
                print(f"Signal expired for {pending_signal.get('side')} at {pending_signal.get('entry')}. Clearing pending signal.")
                pending_signal.clear()

            df = load_mt5(SYMBOL, TIMEFRAME, 200)

            if df is None:
                time.sleep(10)
                continue

            fvgs = detect_fvg(df)
            ifvgs = detect_ifvg(df,fvgs)

            side,entry,tp,sl = trade_signal(fvgs,ifvgs)

            if entry == last_entry:
                time.sleep(SCAN_INTERVAL)
                continue

            if side in ["BUY","SELL"]:
                lot = lot_size(entry,sl)

                pending_signal = {
                    "side":side,
                    "entry":round(entry,2),
                    "tp":round(tp,2),
                    "sl":round(sl,2),
                    "lot":lot
                }

                signal_time = time.time()
                last_entry = entry

                markup = InlineKeyboardMarkup()

                approve_btn = InlineKeyboardButton(
                    f"🟢 APPROVE {side} ({lot})",
                    callback_data="approve_trade"
                )

                deny_btn = InlineKeyboardButton(
                    "❌ DENY",
                    callback_data="deny_trade"
                )

                markup.add(approve_btn,deny_btn)

                msg = f"""🚨 XAUUSD SIGNAL

Side: {side}
Entry: {round(entry,2)}
TP: {round(tp,2)}
SL: {round(sl,2)}
Lot: {lot}"""

                broadcast_signal(msg,reply_markup=markup)
                print(f"Signal sent: {side} at {entry}")

        except Exception as e:
            print("Scanner error:",e)

        time.sleep(SCAN_INTERVAL)

# ====================================
# RUN BOT
# ====================================
if __name__ == "__main__":
    if not os.path.exists("users.txt"):
        with open("users.txt", "w") as f:
            pass

    threading.Thread(target=market_scanner, daemon=True).start()
    threading.Thread(target=trade_monitor, daemon=True).start()

    print("Telegram Bot Listening...")

    bot.infinity_polling()