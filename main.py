import os
import asyncio
import discord
from discord.ext import commands, tasks
import pandas as pd
import pandas_ta as ta
import requests

# ==========================================
# CONFIGURATION & DISCORD SETUP
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN_BOT")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))  # Set your target channel ID

# TradeLocker API Credentials
TL_EMAIL = os.getenv("TRADELOCKER_EMAIL")
TL_PASSWORD = os.getenv("TRADELOCKER_PASSWORD")
TL_SERVER = os.getenv("TRADELOCKER_SERVER")
TL_BASE_URL = "https://live.tradelocker.com/api/v2"  # Update to demo URL if using Demo account

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]
TIMEFRAMES = ["4h", "1h", "30m", "15m", "1m"]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Global Auth Token Storage
tl_access_token = None


# ==========================================
# TRADELOCKER API AUTHENTICATION & DATA
# ==========================================
def get_tradelocker_token():
    global tl_access_token
    url = f"{TL_BASE_URL}/auth/jwt/token"
    payload = {
        "email": TL_EMAIL,
        "password": TL_PASSWORD,
        "server": TL_SERVER
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            tl_access_token = response.json().get("accessToken")
            return tl_access_token
        else:
            print(f"Auth Failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Auth Exception: {e}")
        return None

def fetch_klines(symbol, timeframe, limit=100):
    global tl_access_token
    if not tl_access_token:
        get_tradelocker_token()

    headers = {"Authorization": f"Bearer {tl_access_token}"}
    # Map timeframe strings to API resolution formats if necessary
    url = f"{TL_BASE_URL}/market/bar?symbol={symbol}&resolution={timeframe}&limit={limit}"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 401:  # Token expired, retry once
            get_tradelocker_token()
            headers = {"Authorization": f"Bearer {tl_access_token}"}
            response = requests.get(url, headers=headers)
            
        if response.status_code == 200:
            data = response.json()
            # Convert response bars into pandas DataFrame
            df = pd.DataFrame(data.get("bars", []))
            if not df.empty:
                df['close'] = df['c'].astype(float)
                df['high'] = df['h'].astype(float)
                df['low'] = df['l'].astype(float)
                df['open'] = df['o'].astype(float)
                return df
    except Exception as e:
        print(f"Error fetching data for {symbol} ({timeframe}): {e}")
    
    return pd.DataFrame()


# ==========================================
# TECHNICAL ANALYSIS ENGINE
# ==========================================
def analyze_pair(symbol):
    data = {}
    for tf in TIMEFRAMES:
        data[tf] = fetch_klines(symbol, tf)

    # 1. 4H Trend Pattern & Stack
    df_4h = data.get("4h")
    pattern_4h = "CONSOLIDATION PATTERN ⏳"
    stack_4h = "NEUTRAL"
    if not df_4h.empty and len(df_4h) >= 20:
        ema9 = ta.ema(df_4h['close'], length=9)
        ema20 = ta.ema(df_4h['close'], length=20)
        if ema9.iloc[-1] > ema20.iloc[-1]:
            stack_4h = "BULLISH BIAS (WEAK STACK) 🐂"
        elif ema9.iloc[-1] < ema20.iloc[-1]:
            stack_4h = "BEARISH BIAS (WEAK STACK) 🐻"

    # 2. 1H EMA Stack
    df_1h = data.get("1h")
    stack_1h = "NEUTRAL"
    if not df_1h.empty and len(df_1h) >= 20:
        ema9 = ta.ema(df_1h['close'], length=9)
        ema20 = ta.ema(df_1h['close'], length=20)
        if ema9.iloc[-1] > ema20.iloc[-1]:
            stack_1h = "BULLISH EXPANSION 🟢"
        elif ema9.iloc[-1] < ema20.iloc[-1]:
            stack_1h = "BEARISH EXPANSION 🔴"

    # 3. 30M Setup
    df_30m = data.get("30m")
    setup_30m = "CONSOLIDATING ⏳"
    if not df_30m.empty and len(df_30m) >= 20:
        ema9 = ta.ema(df_30m['close'], length=9)
        ema20 = ta.ema(df_30m['close'], length=20)
        close_30m = df_30m['close'].iloc[-1]
        dist_9 = abs(close_30m - ema9.iloc[-1]) / close_30m
        if dist_9 > 0.003:  # Price extended beyond 0.3% of EMA
            setup_30m = "EXTENDED FROM EMAS ⚡"
        elif ema9.iloc[-1] > ema20.iloc[-1]:
            setup_30m = "BULLISH RETEST 📈"
        elif ema9.iloc[-1] < ema20.iloc[-1]:
            setup_30m = "BEARISH RETEST 📉"

    # 4. 15M Setup
    df_15m = data.get("15m")
    setup_15m = "CONSOLIDATING ⏳"
    if not df_15m.empty and len(df_15m) >= 20:
        ema9 = ta.ema(df_15m['close'], length=9)
        ema20 = ta.ema(df_15m['close'], length=20)
        close_15m = df_15m['close'].iloc[-1]
        dist_9 = abs(close_15m - ema9.iloc[-1]) / close_15m
        if dist_9 > 0.0025:
            setup_15m = "EXTENDED FROM EMAS ⚡"
        elif ema9.iloc[-1] > ema20.iloc[-1]:
            setup_15m = "PULLBACK TO EMA 🟢"
        else:
            setup_15m = "PULLBACK TO EMA 🔴"

    # 5. 1M Trigger Check
    df_1m = data.get("1m")
    trigger_1m = "NO 1M TRIGGER ⚪"
    current_price = 0.0
    if not df_1m.empty and len(df_1m) >= 20:
        current_price = df_1m['close'].iloc[-1]
        ema9_1m = ta.ema(df_1m['close'], length=9)
        ema20_1m = ta.ema(df_1m['close'], length=20)
        # Check cross on the latest completed 1M candle
        if ema9_1m.iloc[-2] < ema20_1m.iloc[-2] and ema9_1m.iloc[-1] > ema20_1m.iloc[-1]:
            trigger_1m = "BULLISH CROSSOVER 🟢"
        elif ema9_1m.iloc[-2] > ema20_1m.iloc[-2] and ema9_1m.iloc[-1] < ema20_1m.iloc[-1]:
            trigger_1m = "BEARISH CROSSOVER 🔴"

    return {
        "symbol": symbol,
        "price": current_price,
        "pattern_4h": pattern_4h,
        "stack_4h": stack_4h,
        "stack_1h": stack_1h,
        "setup_30m": setup_30m,
        "setup_15m": setup_15m,
        "trigger_1m": trigger_1m
    }


# ==========================================
# DISCORD BOT COMMANDS & SCANNER LOOP
# ==========================================
def build_scan_output():
    message = ""
    for symbol in SYMBOLS:
        res = analyze_pair(symbol)
        message += f"🔹 **{res['symbol']}**\n"
        message += f"• **4H Pattern:** {res['pattern_4h']}\n"
        message += f"• **4H EMA Stack:** {res['stack_4h']}\n"
        message += f"• **1H EMA Stack:** {res['stack_1h']}\n"
        message += f"• **30M Setup:** {res['setup_30m']}\n"
        message += f"• **15M Setup:** {res['setup_15m']}\n"
        message += f"• **1M Trigger:** {res['trigger_1m']}\n"
        message += f"• **Price:** {res['price']:.5f} if 'JPY' not in symbol else f\"{res['price']:.3f}\"\n\n"
    
    message += "Use `!scan` for execution signals or `!scalp` for micro-entries"
    return message

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    get_tradelocker_token()

@bot.command(name="scan")
async def scan_command(ctx):
    """Manual command to trigger a scan on demand."""
    async with ctx.typing():
        report = build_scan_output()
        await ctx.send(report)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
