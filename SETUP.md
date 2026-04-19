# FVG Hybrid Bot — Setup Guide

## What you have
- `FVG_Predictive_EA.mq5` — runs inside MT5, handles ALL trading
- `fvg_notification_server.py` — runs on your PC, handles ALL Telegram messages

---

## Step 1 — Install the MQL5 EA

1. Open MT5
2. Press `Ctrl+Shift+D` to open the Data Folder
3. Navigate to `MQL5\Experts\`
4. Copy `FVG_Predictive_EA.mq5` into that folder
5. In MT5, go to Navigator panel → Expert Advisors → right-click → Refresh
6. You should see `FVG_Predictive_EA` appear

---

## Step 2 — Allow WebRequests in MT5

The EA needs to POST to Python via HTTP. You must whitelist the URL:

1. MT5 → Tools → Options → Expert Advisors tab
2. Check ✅ "Allow WebRequest for listed URL"
3. Add: `http://localhost:5000`
4. Click OK

Without this, the EA will run but Telegram notifications won't work.

---

## Step 3 — Attach EA to chart

1. Open XAUUSD M5 chart
2. Drag `FVG_Predictive_EA` from Navigator onto the chart
3. In the settings dialog, verify:
   - Symbol: XAUUSD
   - PythonURL: http://localhost:5000/event
   - Magic Number: 303030
   - All other params as desired
4. Make sure ✅ "Allow live trading" is checked
5. Click OK
6. You should see a smiley face 🙂 in the top-right of the chart (EA running)

---

## Step 4 — Run Python notification server

```bash
pip install pytelegrambotapi requests MetaTrader5
python fvg_notification_server.py
```

You should see:
```
MT5 connected (read-only for balance/price).
HTTP event server listening on localhost:5000
Python notification server ready on localhost:5000
Telegram bot listening...
```

---

## Step 5 — Test the connection

Send `/status` to your Telegram bot. You should get a response.

When the EA fires its first predictive order, you'll get a Telegram message like:
```
🔮 PREDICTIVE LIMIT PLACED
Trigger : LIVE_FORMING
Side    : SELL
Entry   : 4872.59
...
```

---

## Important notes

- **Both must run simultaneously** — EA in MT5, Python on your PC
- If Python is not running, the EA still trades — you just won't get Telegram notifications
- The EA uses Magic Number 303030 — don't use this for manual trades
- The 3-minute hold rule is enforced by the EA itself (SL/TP not set until 3 min after entry)
- `/pause` in Telegram pauses notifications only — to stop the EA trading, disable it in MT5

---

## Tunable parameters (in EA settings dialog)

| Parameter | Default | Description |
|---|---|---|
| InpMinFVGGap | 12.0 | Minimum gap size in points |
| InpFVGLookback | 20 | Candles to scan for fresh FVGs |
| InpApproachProx | 10.0 | Points from zone to trigger approach |
| InpEntryInsidePct | 0.20 | Entry 20% inside gap from edge |
| InpPendingExpiryMin | 15 | Cancel unfilled orders after N minutes |
| InpRRRatio | 2.0 | Risk:Reward ratio |
| InpRiskPercent | 0.5 | Risk % per trade |
| InpMaxTrades | 3 | Max simultaneous trades |
| InpDailyLossLimit | 2.0 | Stop trading if down 2% on the day |
| InpMomentumFVGCount | 3 | Fresh FVGs needed to override H1 filter |
| InpMinHoldSeconds | 180 | 3-minute funded account hold rule |

---

## File locations after setup

```
MT5 Data Folder/
  MQL5/
    Experts/
      FVG_Predictive_EA.mq5

Your Trading Folder/
  fvg_notification_server.py
  fvg_trade_log.db
  fvg_trade_log.csv
  fvg_users.txt
```
