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
CONTRACT_SIZE = 100 

# ====================================
# MT5 DATA
# ====================================

def load_mt5(symbol, timeframe, bars):
    if not mt5.initialize():
        st.error(f"MT5 Initialization failed. Make sure MT5 is open! Error code: {mt5.last_error()}")
        st.stop()

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)

    if rates is None:
        st.error(f"Failed to get data for {symbol}. Check if the symbol name is correct. Error code: {mt5.last_error()}")
        st.stop()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    return df

# ====================================
# SWING LEVELS & FIB OTE
# ====================================

def swing_levels(df):
    lookback = 25
    high = df["high"].rolling(lookback).max().iloc[-1]
    low = df["low"].rolling(lookback).min().iloc[-1]
    return high, low

def fib_levels(high, low):
    fib62 = high - (high-low) * 0.62
    fib705 = high - (high-low) * 0.705
    fib79 = high - (high-low) * 0.79
    return fib62, fib705, fib79

# ====================================
# FVG & IFVG DETECTION
# ====================================

def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        c1 = df.iloc[i-2]
        c3 = df.iloc[i]
        if c1.high < c3.low:
            fvgs.append({"type": "bullish", "low": c1.high, "high": c3.low, "index": i})
        if c1.low > c3.high:
            fvgs.append({"type": "bearish", "low": c3.high, "high": c1.low, "index": i})
    return fvgs

def detect_ifvg(df, fvgs):
    ifvgs = []
    for gap in fvgs:
        for i in range(gap["index"], len(df)):
            price = df.iloc[i].close
            if gap["type"] == "bullish" and price < gap["low"]:
                ifvgs.append({"type": "bearish_ifvg", "low": gap["low"], "high": gap["high"]})
                break
            if gap["type"] == "bearish" and price > gap["high"]:
                ifvgs.append({"type": "bullish_ifvg", "low": gap["low"], "high": gap["high"]})
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
# TRADE SIGNAL & RISK MANAGEMENT
# ====================================

def trade_signal(fvgs, ifvgs):
    if not ifvgs and not fvgs:
        return "WAIT", 0.0, 0.0, 0.0

    if len(ifvgs) > 0:
        gap = ifvgs[-1]
    else:
        gap = fvgs[-1]

    entry = (gap["low"] + gap["high"]) / 2
    risk_range = abs(gap["high"] - gap["low"])

    # Force the trade to be at least an $8.00 move so it stays open > 3 mins
    if risk_range < 8.00:
        risk_range = 8.00 

    if "bearish" in gap["type"]:
        side = "SELL"
        sl = entry + risk_range             
        tp = entry - (risk_range * 1.5)     
    else:
        side = "BUY"
        sl = entry - risk_range             
        tp = entry + (risk_range * 1.5)     

    return side, entry, tp, sl

def lot_size(entry, sl):
    if entry == 0 or sl == 0 or entry == sl:
        return 0.0, 0.0
        
    risk = ACCOUNT_SIZE * RISK_PERCENT
    stop_distance = abs(entry - sl)
    
    lot = risk / (stop_distance * CONTRACT_SIZE)
    
    # Clamp lot size between 0.1 and 1.0 for prop firm compliance
    if lot < 0.1:
        lot = 0.1
    elif lot > 1.0:
        lot = 1.0
        
    lot = round(lot, 2)
    actual_risk = lot * stop_distance * CONTRACT_SIZE
    
    return lot, actual_risk

# ====================================
# MACHINE LEARNING
# ====================================

def build_features(df):
    df_ml = df.copy()
    df_ml["return"] = df_ml.close.pct_change()
    df_ml["volatility"] = df_ml["return"].rolling(10).std()
    df_ml["momentum"] = df_ml.close - df_ml.close.shift(10)
    df_ml.dropna(inplace=True)
    return df_ml

def train_model(df):
    df_ml = build_features(df)
    X = df_ml[["return", "volatility", "momentum"]]
    y = (df_ml.close.shift(-5) > df_ml.close).astype(int)
    model = RandomForestClassifier()
    model.fit(X[:-5], y[:-5])
    return model

# ====================================
# TRADE EXECUTION (PENDING ORDERS)
# ====================================

def execute_trade(action, symbol, lot, entry_price, sl, tp):
    lot = round(float(lot), 2)
    entry_price = round(float(entry_price), 2)
    
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None

    if action == "BUY":
        if tick.ask > entry_price:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            order_type = mt5.ORDER_TYPE_BUY_STOP
    elif action == "SELL":
        if tick.bid < entry_price:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        else:
            order_type = mt5.ORDER_TYPE_SELL_STOP
    else:
        return None

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": entry_price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 20,          
        "magic": 101010,          
        "comment": "AI Approved",
        "type_time": mt5.ORDER_TIME_GTC, 
    }

    result = mt5.order_send(request)
    return result

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

if not features.empty:
    prob = model.predict_proba(
        features[["return", "volatility", "momentum"]].iloc[-1:]
    )[0][1]
else:
    prob = 0.5

# ---- UI LAYOUT ----
col1, col2 = st.columns(2)

with col1:
    st.subheader("Trade Signal")
    st.write(f"**Side:** {side}")
    st.write(f"**Entry:** {round(entry, 2)}")
    st.write(f"**TP:** {round(tp, 2)}")
    st.write(f"**SL:** {round(sl, 2)}")
    st.write(f"**Lot:** {round(lot, 2)}")
    st.write(f"**Risk:** ${round(risk, 2)}")

    if side in ["BUY", "SELL"] and lot > 0:
        st.write("---")
        if st.button(f"APPROVE {side} AT {round(entry, 2)}", type="primary"):
            with st.spinner('Placing pending order in MT5...'):
                result = execute_trade(side, SYMBOL, lot, entry, sl, tp)
                
                if result is None:
                    st.error("Execution failed. MT5 not connected or symbol missing.")
                elif result.retcode != mt5.TRADE_RETCODE_DONE:
                    st.error(f"Order Failed! Error Code: {result.retcode}. Make sure 'Algo Trading' is enabled.")
                else:
                    st.success(f"Pending Order Placed! MT5 will enter trade when price hits {round(entry,2)}")

with col2:
    st.subheader("AI Probability")
    st.write(f"{round(prob * 100, 2)}% bullish probability")

    st.subheader("Market Context")
    st.write(f"**Liquidity Sweep High:** {sweep_high}")
    st.write(f"**Liquidity Sweep Low:** {sweep_low}")

st.write("---")

# ====================================
# CHART
# ====================================

if side != "WAIT":
    addplots = [
        mpf.make_addplot([fib62]*len(df), color="blue", alpha=0.3),
        mpf.make_addplot([fib705]*len(df), color="blue", alpha=0.3),
        mpf.make_addplot([fib79]*len(df), color="blue", alpha=0.3),
        mpf.make_addplot([entry]*len(df), color="green", width=2),
        mpf.make_addplot([tp]*len(df), color="purple", width=2),
        mpf.make_addplot([sl]*len(df), color="red", width=2)
    ]
else:
    addplots = [
        mpf.make_addplot([fib62]*len(df), color="blue", alpha=0.3),
        mpf.make_addplot([fib705]*len(df), color="blue", alpha=0.3),
        mpf.make_addplot([fib79]*len(df), color="blue", alpha=0.3),
    ]

fig, ax = mpf.plot(
    df,
    type="candle",
    style="charles",
    addplot=addplots,
    returnfig=True,
    figsize=(12, 6),
    warn_too_much_data=1000
)

st.pyplot(fig)