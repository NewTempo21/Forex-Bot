import os
import asyncio
import discord
from discord.ext import commands, tasks
import pandas as pd
import ta
import requests

# ==============================================================================
# CONFIGURATION & DISCORD SETUP
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN_BOT")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# TradeLocker API Credentials
TL_EMAIL = os.getenv("TRADELOCKER_EMAIL")
TL_PASSWORD = os.getenv("TRADELOCKER_PASSWORD")
TL_SERVER = os.getenv("TRADELOCKER_SERVER")
TL_BASE_URL = "https://live.tradelocker.com/api/v2"

# Bot configuration
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
TIMEFRAME = "1h"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================================================================
# TRADELOCKER API FUNCTIONS
# ==============================================================================
def get_tradelocker_token():
    """Authenticates with TradeLocker and returns an access token."""
    url = f"{TL_BASE_URL}/auth/jwt/token"
    payload = {
        "email": TL_EMAIL,
        "password": TL_PASSWORD,
        "server": TL_SERVER
    }
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("accessToken")
    except Exception as e:
        print(f"[ERROR] Failed to authenticate with TradeLocker: {e}")
        return None

def fetch_market_data(symbol, timeframe="1h", limit=100):
    """Fetches historical price data for a given symbol from TradeLocker."""
    token = get_tradelocker_token()
    if not token:
        return None

    url = f"{TL_BASE_URL}/market/candles"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "symbol": symbol,
        "resolution": timeframe,
        "limit": limit
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        candles = response.json().get("candles", [])
        
        if not candles:
            return None

        # Convert candle array into a structured Pandas DataFrame
        df = pd.DataFrame(candles)
        # Expected TradeLocker schema columns: ['t', 'o', 'h', 'l', 'c', 'v']
        df = df.rename(columns={
            't': 'timestamp',
            'o': 'open',
            'h': 'high',
            'l': 'low',
            'c': 'close',
            'v': 'volume'
        })
        
        # Ensure numeric types
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        return df
    except Exception as e:
        print(f"[ERROR] Failed to fetch candles for {symbol}: {e}")
        return None

# ==============================================================================
# TECHNICAL ANALYSIS & INDICATORS
# ==============================================================================
def calculate_indicators(df):
    """Calculates RSI, EMAs, and MACD using the pure-Python 'ta' library."""
    if df is None or df.empty or len(df) < 30:
        return None

    # RSI (14 period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)

    # Exponential Moving Averages
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema_50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['ema_200'] = ta.trend.ema_indicator(df['close'], window=200)

    # MACD
    macd_object = ta.trend.MACD(df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['macd'] = macd_object.macd()
    df['macd_signal'] = macd_object.macd_signal()
    df['macd_diff'] = macd_object.macd_diff()

    return df

def analyze_signals(symbol, df):
    """Evaluates strategy logic for oversold/overbought conditions."""
    if df is None or len(df) < 2:
        return None

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    # Example Strategy Logic: RSI Oversold/Overbought Crosses
    buy_signal = (previous['rsi'] < 30) and (latest['rsi'] >= 30)
    sell_signal = (previous['rsi'] > 70) and (latest['rsi'] <= 70)

    if buy_signal:
        return {
            "symbol": symbol,
            "type": "BUY",
            "rsi": round(latest['rsi'], 2),
            "close": latest['close'],
            "ema_200": round(latest['ema_200'], 5) if pd.notnull(latest['ema_200']) else "N/A"
        }
    elif sell_signal:
        return {
            "symbol": symbol,
            "type": "SELL",
            "rsi": round(latest['rsi'], 2),
            "close": latest['close'],
            "ema_200": round(latest['ema_200'], 5) if pd.notnull(latest['ema_200']) else "N/A"
        }

    return None

# ==============================================================================
# DISCORD BACKGROUND SCANNER LOOP
# ==============================================================================
@tasks.loop(minutes=15)
async def scan_market():
    """Background task running every 15 minutes to scan pairs and push alerts."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print(f"[WARNING] Discord channel ID {DISCORD_CHANNEL_ID} not found.")
        return

    print("[INFO] Starting market scanner run...")

    for symbol in SYMBOLS:
        df = fetch_market_data(symbol, timeframe=TIMEFRAME)
        if df is None:
            continue

        df = calculate_indicators(df)
        signal = analyze_signals(symbol, df)

        if signal:
            color = discord.Color.green() if signal['type'] == "BUY" else discord.Color.red()
            embed = discord.Embed(
                title=f"🚨 Forex Signal Alert: {signal['symbol']} ({signal['type']})",
                color=color
            )
            embed.add_field(name="Signal Type", value=signal['type'], inline=True)
            embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
            embed.add_field(name="RSI (14)", value=str(signal['rsi']), inline=True)
            embed.add_field(name="200 EMA", value=str(signal['ema_200']), inline=True)
            embed.set_footer(text=f"Timeframe: {TIMEFRAME} | Powered by TradeLocker")

            await channel.send(embed=embed)
            print(f"[ALERT] Sent {signal['type']} signal for {symbol} to Discord.")

        # Rate limiting pause between pair queries
        await asyncio.sleep(2)

@scan_market.before_loop
async def before_scan():
    """Wait until the Discord bot is fully logged in before starting scanner loop."""
    await bot.wait_until_ready()

# ==============================================================================
# BOT EVENTS & STARTUP
# ==============================================================================
@bot.event
async def on_ready():
    print(f"[SUCCESS] Bot connected as {bot.user.name} (ID: {bot.user.id})")
    if not scan_market.is_running():
        scan_market.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("[CRITICAL] DISCORD_TOKEN_BOT is missing from Environment Variables.")
    else:
        bot.run(DISCORD_TOKEN)
