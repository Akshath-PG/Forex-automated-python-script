import pandas as pd
import numpy as np
import mplfinance as mpf
import MetaTrader5 as mt5
import requests
from datetime import datetime

# ============================================
# CONFIG
# ============================================

SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M1
BARS = 800

ACCOUNT_SIZE = 51000
RISK_PERCENT = 0.5 / 100
PIP_VALUE = 10

# Telegram (optional)
TELEGRAM_ENABLED = False
BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"


# ============================================
# TELEGRAM ALERT
# ============================================

def send_telegram(msg):

    if not TELEGRAM_ENABLED:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    requests.post(url,data={
        "chat_id":CHAT_ID,
        "text":msg
    })


# ============================================
# MT5 DATA
# ============================================

def load_mt5():

    mt5.initialize()

    rates = mt5.copy_rates_from_pos(
        SYMBOL,
        TIMEFRAME,
        0,
        BARS
    )

    df = pd.DataFrame(rates)

    df["time"] = pd.to_datetime(df["time"],unit="s")

    df.set_index("time",inplace=True)

    return df


# ============================================
# SESSION DETECTION
# ============================================

def detect_session(hour):

    if 0 <= hour < 8:
        return "Asia"

    if 8 <= hour < 13:
        return "London"

    return "NewYork"


# ============================================
# SWING DETECTION
# ============================================

def swing_levels(df):

    lookback = 25

    swing_high = df["high"].rolling(lookback).max().iloc[-1]
    swing_low = df["low"].rolling(lookback).min().iloc[-1]

    return swing_high,swing_low


# ============================================
# FIBONACCI OTE
# ============================================

def fib_levels(high,low):

    fib62 = high - (high-low)*0.62
    fib705 = high - (high-low)*0.705
    fib79 = high - (high-low)*0.79

    return fib62,fib705,fib79


# ============================================
# FVG DETECTION
# ============================================

def detect_fvg(df):

    fvgs = []

    for i in range(2,len(df)):

        c1 = df.iloc[i-2]
        c3 = df.iloc[i]

        if c1["high"] < c3["low"]:

            fvgs.append({
                "type":"bullish",
                "low":c1["high"],
                "high":c3["low"],
                "index":i
            })

        if c1["low"] > c3["high"]:

            fvgs.append({
                "type":"bearish",
                "low":c3["high"],
                "high":c1["low"],
                "index":i
            })

    return fvgs


# ============================================
# IFVG DETECTION
# ============================================

def detect_ifvg(df,fvgs):

    ifvgs = []

    for gap in fvgs:

        gap_low = gap["low"]
        gap_high = gap["high"]

        for i in range(gap["index"],len(df)):

            price = df.iloc[i]["close"]

            if gap["type"] == "bullish" and price < gap_low:

                ifvgs.append({
                    "type":"bearish_ifvg",
                    "low":gap_low,
                    "high":gap_high
                })

                break

            if gap["type"] == "bearish" and price > gap_high:

                ifvgs.append({
                    "type":"bullish_ifvg",
                    "low":gap_low,
                    "high":gap_high
                })

                break

    return ifvgs


# ============================================
# LIQUIDITY SWEEP
# ============================================

def liquidity_sweep(df):

    recent_high = df["high"].rolling(12).max().iloc[-2]
    recent_low = df["low"].rolling(12).min().iloc[-2]

    current_high = df["high"].iloc[-1]
    current_low = df["low"].iloc[-1]

    sweep_high = current_high > recent_high
    sweep_low = current_low < recent_low

    return sweep_high,sweep_low


# ============================================
# TRADE ENGINE
# ============================================

def trade_signal(df,fvgs,ifvgs):

    if len(ifvgs) > 0:
        gap = ifvgs[-1]
    else:
        gap = fvgs[-1]

    entry = (gap["low"] + gap["high"]) / 2

    risk_range = abs(gap["high"] - gap["low"])

    if "bearish" in gap["type"]:

        side = "SELL"
        tp = entry - risk_range*3
        sl = entry + risk_range*1.5

    else:

        side = "BUY"
        tp = entry + risk_range*3
        sl = entry - risk_range*1.5

    return side,entry,tp,sl


# ============================================
# POSITION SIZING
# ============================================

def lot_size(entry,sl):

    risk_amount = ACCOUNT_SIZE * RISK_PERCENT

    stop_distance = abs(entry-sl)

    lot = risk_amount / (stop_distance * PIP_VALUE)

    return lot,risk_amount


# ============================================
# BACKTEST
# ============================================

def backtest(df,side,tp,sl):

    wins=0
    losses=0

    for i in range(len(df)-40):

        future=df.iloc[i:i+40]

        if side=="BUY":

            if future["high"].max()>=tp:
                wins+=1

            elif future["low"].min()<=sl:
                losses+=1

        else:

            if future["low"].min()<=tp:
                wins+=1

            elif future["high"].max()>=sl:
                losses+=1

    if wins+losses==0:
        return 0

    return wins/(wins+losses)


# ============================================
# MAIN
# ============================================

df = load_mt5()

df["session"]=df.index.hour.map(detect_session)

swing_high,swing_low = swing_levels(df)

fib62,fib705,fib79 = fib_levels(swing_high,swing_low)

fvgs = detect_fvg(df)

ifvgs = detect_ifvg(df,fvgs)

sweep_high,sweep_low = liquidity_sweep(df)

side,entry,tp,sl = trade_signal(df,fvgs,ifvgs)

lot,risk = lot_size(entry,sl)

winrate = backtest(df,side,tp,sl)


# ============================================
# OUTPUT
# ============================================

print("\n==========================")
print("XAUUSD TRADE SIGNAL")
print("==========================")

print("Side :",side)
print("Entry:",round(entry,2))
print("TP   :",round(tp,2))
print("SL   :",round(sl,2))
print("Lot  :",round(lot,2))
print("Risk :",round(risk,2),"USD")

print("\nMarket Context")
print("Session:",df["session"].iloc[-1])
print("Liquidity Sweep High:",sweep_high)
print("Liquidity Sweep Low :",sweep_low)

print("\nBacktest Winrate:",round(winrate*100,2),"%")

print("==========================\n")

msg = f"""
XAUUSD SIGNAL

{side}

Entry {round(entry,2)}
TP {round(tp,2)}
SL {round(sl,2)}
Lot {round(lot,2)}
"""

send_telegram(msg)


# ============================================
# VISUALIZATION
# ============================================

addplots=[

mpf.make_addplot([fib62]*len(df),color="blue"),
mpf.make_addplot([fib705]*len(df),color="blue"),
mpf.make_addplot([fib79]*len(df),color="blue"),

mpf.make_addplot([entry]*len(df),color="green"),
mpf.make_addplot([tp]*len(df),color="purple"),
mpf.make_addplot([sl]*len(df),color="red")

]

mpf.plot(

df,
type="candle",
style="charles",
title="XAUUSD ICT Engine",
addplot=addplots,
volume=False,
figsize=(14,7)

)