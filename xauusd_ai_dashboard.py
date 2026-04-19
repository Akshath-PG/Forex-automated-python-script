import pandas as pd
import numpy as np
import MetaTrader5 as mt5
import mplfinance as mpf
import streamlit as st
from sklearn.ensemble import RandomForestClassifier

# ====================================
# CONFIG
# ====================================

SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M1
HIGHER_TF = mt5.TIMEFRAME_M15

BARS = 800

ACCOUNT_SIZE = 51000
RISK_PERCENT = 0.5 / 100
PIP_VALUE = 10

# ====================================
# MT5 DATA
# ====================================

def load_mt5(symbol, timeframe, bars):
    # Check if MT5 initialized properly
    if not mt5.initialize():
        st.error(f"MT5 Initialization failed. Make sure MT5 is open! Error code: {mt5.last_error()}")
        st.stop() # Stops the script from crashing further down

    # Pull the data
    rates = mt5.copy_rates_from_pos(
        symbol,
        timeframe,
        0,
        bars
    )

    # Check if data was actually found
    if rates is None:
        st.error(f"Failed to get data for {symbol}. Check if the symbol name is correct for your broker (e.g., XAUUSD, XAUUSD.a, GOLD) and ensure it is in your Market Watch window. Error code: {mt5.last_error()}")
        st.stop()

    # If successful, build the DataFrame
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    return df

# ====================================
# SWING LEVELS
# ====================================

def swing_levels(df):

    lookback = 25

    high = df["high"].rolling(lookback).max().iloc[-1]
    low = df["low"].rolling(lookback).min().iloc[-1]

    return high, low

# ====================================
# FIB OTE
# ====================================

def fib_levels(high, low):

    fib62 = high - (high-low) * 0.62
    fib705 = high - (high-low) * 0.705
    fib79 = high - (high-low) * 0.79

    return fib62, fib705, fib79

# ====================================
# FVG
# ====================================

def detect_fvg(df):

    fvgs = []

    for i in range(2, len(df)):

        c1 = df.iloc[i-2]
        c3 = df.iloc[i]

        if c1.high < c3.low:

            fvgs.append({
                "type":"bullish",
                "low":c1.high,
                "high":c3.low,
                "index":i
            })

        if c1.low > c3.high:

            fvgs.append({
                "type":"bearish",
                "low":c3.high,
                "high":c1.low,
                "index":i
            })

    return fvgs

# ====================================
# IFVG
# ====================================

def detect_ifvg(df, fvgs):

    ifvgs = []

    for gap in fvgs:

        for i in range(gap["index"], len(df)):

            price = df.iloc[i].close

            if gap["type"] == "bullish" and price < gap["low"]:

                ifvgs.append({
                    "type":"bearish_ifvg",
                    "low":gap["low"],
                    "high":gap["high"]
                })

                break

            if gap["type"] == "bearish" and price > gap["high"]:

                ifvgs.append({
                    "type":"bullish_ifvg",
                    "low":gap["low"],
                    "high":gap["high"]
                })

                break

    return ifvgs

# ====================================
# LIQUIDITY SWEEP
# ====================================

def liquidity_sweep(df):

    prev_high = df.high.rolling(12).max().iloc[-2]
    prev_low = df.low.rolling(12).min().iloc[-2]

    current_high = df.high.iloc[-1]
    current_low = df.low.iloc[-1]

    return current_high > prev_high, current_low < prev_low

# ====================================
# TRADE ENGINE
# ====================================

def trade_signal(fvgs, ifvgs):

    if len(ifvgs) > 0:
        gap = ifvgs[-1]
    else:
        gap = fvgs[-1]

    entry = (gap["low"] + gap["high"]) / 2
    risk_range = abs(gap["high"] - gap["low"])

    if "bearish" in gap["type"]:

        side = "SELL"
        tp = entry - risk_range * 3
        sl = entry + risk_range * 1.5

    else:

        side = "BUY"
        tp = entry + risk_range * 3
        sl = entry - risk_range * 1.5

    return side, entry, tp, sl

# ====================================
# POSITION SIZE
# ====================================

def lot_size(entry, sl):

    risk = ACCOUNT_SIZE * RISK_PERCENT

    stop_distance = abs(entry - sl)

    lot = risk / (stop_distance * PIP_VALUE)

    return lot, risk

# ====================================
# FEATURE EXTRACTION
# ====================================

def build_features(df):

    df["return"] = df["close"].pct_change()

    df["volatility"] = df["return"].rolling(10).std()

    df["momentum"] = df["close"] - df["close"].shift(10)

    df.dropna(inplace=True)

    return df

# ====================================
# ML MODEL
# ====================================

def train_model(df):

    df = build_features(df)

    X = df[["return","volatility","momentum"]]

    y = (df["close"].shift(-5) > df["close"]).astype(int)

    model = RandomForestClassifier()

    model.fit(X[:-5], y[:-5])

    return model

# ====================================
# STREAMLIT DASHBOARD
# ====================================

st.title("XAUUSD AI Trading Dashboard")

df = load_mt5(SYMBOL, TIMEFRAME, BARS)

higher = load_mt5(SYMBOL, HIGHER_TF, 200)

swing_high, swing_low = swing_levels(df)

fib62, fib705, fib79 = fib_levels(swing_high, swing_low)

fvgs = detect_fvg(df)

ifvgs = detect_ifvg(df, fvgs)

sweep_high, sweep_low = liquidity_sweep(df)

side, entry, tp, sl = trade_signal(fvgs, ifvgs)

lot, risk = lot_size(entry, sl)

model = train_model(df)

features = build_features(df)

prob = model.predict_proba(
    features[["return","volatility","momentum"]].iloc[-1:]
)[0][1]

st.subheader("Trade Signal")

st.write("Side:", side)
st.write("Entry:", round(entry,2))
st.write("TP:", round(tp,2))
st.write("SL:", round(sl,2))
st.write("Lot:", round(lot,2))
st.write("Risk:", round(risk,2))

st.subheader("AI Probability")

st.write(round(prob*100,2), "% bullish probability")

st.subheader("Market Context")

st.write("Liquidity Sweep High:", sweep_high)
st.write("Liquidity Sweep Low:", sweep_low)

# ====================================
# CHART
# ====================================

addplots = [

    mpf.make_addplot([fib62]*len(df), color="blue"),
    mpf.make_addplot([fib705]*len(df), color="blue"),
    mpf.make_addplot([fib79]*len(df), color="blue"),

    mpf.make_addplot([entry]*len(df), color="green"),
    mpf.make_addplot([tp]*len(df), color="purple"),
    mpf.make_addplot([sl]*len(df), color="red")
]

fig, ax = mpf.plot(
    df,
    type="candle",
    style="charles",
    addplot=addplots,
    returnfig=True
)

st.pyplot(fig)