# Akshath-PG's Forex Automated Python Script

Welcome to the **Forex Automated Python Script** repository. This project contains a suite of automated and semi-automated trading bots specifically engineered for trading **Gold (XAUUSD)** on **MetaTrader 5 (MT5)**. The core trading algorithms rely on detecting **Fair Value Gaps (FVG)** and **Inverted Fair Value Gaps (IFVG)** to identify high-probability entry points.

## 🚀 Key Features

* **FVG & IFVG Detection**: Advanced algorithms written in Python and MQL5 to spot algorithmic footprints on multiple timeframes.
* **Hybrid Execution**: Trade purely from Python via the `MetaTrader5` library or use the integrated MQL5 Expert Advisor.
* **Telegram Integration**: Remote control, live trade approval (APPROVE/DENY buttons), push notifications, and daily summaries straight to your phone.
* **Risk Management Enforcement**: Hard-coded safety triggers such as Daily Loss Limits, max simultaneous trades, trailing stops, and breakeven adjustment.
* **High-Impact News Filter**: Real-time checking of the Forex Factory calendar to avoid deploying trades during volatile economic events.
* **Trend Filtering**: Higher timeframe (M15 & H4) EMA trend alignment required for entries, with momentum override capabilities.

---

## 📂 Repository Structure

### 1. The FVG Predictive Hybrid Bot
A professional-grade implementation splitting the workload between MetaTrader 5 and Python.
* **`FVG_Predictive_EA.mq5`**: The MQL5 Expert Advisor. Sits on your XAUUSD chart, handles all gap predictions, price action monitoring, and direct trade execution.
* **`fvg_notification_server.py`**: A local Python HTTP server that receives webhooks from the MT5 EA and pushes notifications to your Telegram Bot.
* **Setup Guide**: See [SETUP.md](./SETUP.md) for installation and run instructions.

### 2. The Python Scalper Bots
Pure Python scripts connecting directly to the MT5 terminal to calculate gaps, run logic, and execute trades based on Telegram inputs.
* **`Predictive_Ifvg_bot.py`**: Specializes in *Inverted* Fair Value Gaps. Looks for setups where price re-tests a broken gap.
* **`Predictive_fvg_scalper_bot.py`** / **`fvg_scalper_bot.py`**: Scans for standard FVG pullbacks.
* **`telegram_algo_IFVG.py`** / **`Telegram_FVG_Trail.py`**: Support scripts handling custom trailing stop logic and Telegram communications.
* **Setup Guide**: See [Setup IFVG .md](./Setup%20IFVG%20.md) for installation and run instructions.

### 3. Analytics & Logging
* **SQLite & CSV Logs**: All trades are tracked in local databases (e.g., `ifvg_trade_log.db`, `fvg_trade_log.csv`) allowing for complex tracking of win-rates by setup type (e.g., Reactive vs. Predictive).
* **`xauusd_ai_dashboard.py`**: A dashboard script for analyzing past trades.

---

## 🛠️ Prerequisites

1. **MetaTrader 5**: Installed and logged into your broker or prop firm account.
2. **Python 3.9+**: Required to run the scanner and Telegram server.
3. **Telegram Account**: To create your bot through `BotFather` and receive updates.

**Required Python Libraries:**
```bash
pip install pandas MetaTrader5 pyTelegramBotAPI requests
```

---

## ⚠️ Important Configuration

1. **Enable Algo Trading**: The "Algo Trading" button in MT5 must be GREEN.
2. **WebRequests**: For the Hybrid Bot, you MUST enable WebRequests in MT5 to `http://localhost:5000` (`Tools -> Options -> Expert Advisors`).
3. **Environment setup**: The Python bots require your specific Telegram token and Chat ID configuration at the top of the scripts (e.g. `TELEGRAM_TOKEN` and `CHAT_ID`).

## 📊 How It Works (Telegram Workflow)

1. The script runs in the background, monitoring live ticks heavily analyzing `XAUUSD`.
2. When an FVG/IFVG aligns with M15/H4 trends and no high-impact news is looming, the script fires a Telegram alert.
3. The Telegram alert will display the entry criteria, risk amount, risk-to-reward ratio, and a prompt.
4. You press **🟢 APPROVE** or **❌ DENY** right from your phone.
5. If approved, the bot manages the stop-loss, take-profit, trailing risk, and logs the final PnL.

---

### Disclaimer
*This suite of tools is developed for automated, proprietary execution. Be sure to backtest the logic thoroughly and test forward in a demo account before risking real capital.*
