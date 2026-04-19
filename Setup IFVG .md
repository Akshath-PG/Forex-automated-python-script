# 🤖 Akshath's IFVG Telegram Algo Bot - Setup & Run Guide

This guide covers everything you need to start the algorithmic trading bot from scratch, connect it to MetaTrader 5, and control it via Telegram.

### Step 1: Prepare MetaTrader 5 (Crucial First Step)
Before Python can do anything, MT5 needs to be ready and listening.
1. Open your **MetaTrader 5** desktop application.
2. Ensure you are logged into your prop firm account (e.g., Goat Funded).
3. Open a chart for your specific tradable Gold symbol (e.g., `XAUUSD.c` or whatever symbol variant your broker uses for live execution).
4. **Turn on Algo Trading:** Look at the top toolbar in MT5. Click the **Algo Trading** button so it turns **GREEN**. *(If this is red, the bot will fail with Error 10027).*

### Step 2: Install Python Requirements
You need to make sure your computer has the correct libraries installed so Python can talk to MT5 and Telegram.
1. Open your **PowerShell** or VS Code Terminal.
2. Copy and paste this exact command and hit Enter:
```powershell
pip install pandas MetaTrader5 pyTelegramBotAPI requests
```
*(If it says "Requirement already satisfied" for all of them, you are good to go).*

### Step 3: Clear Ghost Processes
Sometimes old versions of the script get stuck running invisibly in the background, which causes a `409 Conflict` error in Telegram. Always clear the slate before a new session.
1. In your terminal, paste this and hit Enter:
```powershell
taskkill /F /IM python.exe
```
*(If it says "SUCCESS", it killed a ghost. If it says "not found", you were already clear).*

### Step 4: Start the Telegram Bot Connection
The bot needs to register you as a user in its database before it starts scanning.
1. Open the **Telegram app** on your phone or desktop.
2. Search for your bot's username (the one you created with BotFather).
3. Open the chat and hit the **Start** button at the bottom (or type `/start` and hit send).
4. *Note: Because of the multi-user script, this action automatically creates a `users.txt` file on your computer and saves your Chat ID inside it.*

### Step 5: Run the Master Python Script
Now it is time to turn the engine on. 
1. In your PowerShell/VS Code terminal, run the script by pointing Python to your specific file path. Press Enter:
```powershell
python C:/Users/aksha/OneDrive/Documents/Trading/fvg_scalper_bot.py
```
2. **Watch the Terminal:** You should see:
   * `MT5 connected.`
   * `Market Scanner Started`
   * `Telegram Bot Listening...`

### Step 6: Verify and Command
Once the script is running, check your phone.
1. You should instantly receive a Telegram message saying: **"🤖 Akshath XAU Bot Online & Scanning XAUUSD"**
2. Test the connection by sending the bot a command. Type **`/status`** in the Telegram chat. 
3. The bot should instantly reply with your live account balance, Prop PnL, and whether it is actively scanning or paused.

### 🎮 How to operate the bot daily:
* Leave the VS Code terminal open and running in the background. Do not close the window, or the bot will die.
* When the bot finds a valid IFVG setup that aligns with the M15/H4 trend and is safe from high-impact news, it will text you.
* Tap **🟢 APPROVE** to instantly execute the trade on MT5.
* Tap **❌ DENY** to reject it and force the bot to keep scanning.
* At 10:00 PM every night, the bot will automatically text you a Daily Summary of your wins, losses, and total PnL.