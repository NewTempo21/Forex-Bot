import os
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks
import pandas as pd
import ta
import requests

# ==============================================================================
# FLASK WEB SERVER (FOR RENDER HEALTH CHECKS)
# ==============================================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and monitoring 9/20/60/200 EMA markets!"

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ==============================================================================
# CONFIGURATION & DISCORD SETUP
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# TradeLocker API Credentials
TL_EMAIL = os.getenv("TRADELOCKER_EMAIL")
TL_PASSWORD = os.getenv("TRADELOCKER_PASSWORD")
TL_SERVER = os.getenv("TRADELOCKER_SERVER")
TL_BASE_URL = "https://live.tradelocker.com/api/v2"

# Bot configuration (Six standard pairs)
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]

# Multi-Timeframe mapping for TradeLocker API resolutions
TIMEFRAMES = {
    "4h": "4h",
    "1h": "1h",
    "30m": "30m",
    "15m": "15m",
    "1m": "1m"
}

intents = discord.Intents.default()
intents.message_content = True  # Required for processing '!' commands
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

def fetch_market_data(symbol, timeframe_res, limit=250):
    """Fetches historical price data for a given symbol and resolution."""
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
        "resolution": timeframe_res,
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
        print(f"[ERROR] Failed to fetch candles for {symbol} ({timeframe_res}): {e}")
        return None

# ==============================================================================
# TECHNICAL ANALYSIS & 9/20/60/200 EMA STRATEGY
# ==============================================================================
def calculate_indicators(df):
    """Calculates 9, 20, 60, and 200 EMAs along with RSI using pure-Python 'ta' library."""
    if df is None or df.empty or len(df) < 200:
        return None

    # Calculate 9, 20, 60, and 200 EMAs
    df['ema_9'] = ta.trend.ema_indicator(df['close'], window=9)
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema_60'] = ta.trend.ema_indicator(df['close'], window=60)
    df['ema_200'] = ta.trend.ema_indicator(df['close'], window=200)

    # RSI (14 period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)

    return df

def analyze_signals(symbol, tf_label, df):
    """Evaluates 9/20 EMA crossovers filtered by 60/200 EMA alignment."""
    if df is None or len(df) < 2:
        return None

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    # Check for recent 9 EMA crossing above 20 EMA (Bullish Cross)
    bullish_cross = (previous['ema_9'] <= previous['ema_20']) and (latest['ema_9'] > latest['ema_20'])
    
    # Check for recent 9 EMA crossing below 20 EMA (Bearish Cross)
    bearish_cross = (previous['ema_9'] >= previous['ema_20']) and (latest['ema_9'] < latest['ema_20'])

    # Trend structure checks using 60 and 200 EMAs
    if bullish_cross:
        return {
            "symbol": symbol,
            "tf": tf_label,
            "type": "BUY 🟢",
            "close": latest['close'],
            "ema_9": round(latest['ema_9'], 5),
            "ema_20": round(latest['ema_20'], 5),
            "ema_60": round(latest['ema_60'], 5) if pd.notnull(latest['ema_60']) else "N/A",
            "ema_200": round(latest['ema_200'], 5) if pd.notnull(latest['ema_200']) else "N/A",
            "rsi": round(latest['rsi'], 2) if pd.notnull(latest['rsi']) else "N/A"
        }
    elif bearish_cross:
        return {
            "symbol": symbol,
            "tf": tf_label,
            "type": "SELL 🔴",
            "close": latest['close'],
            "ema_9": round(latest['ema_9'], 5),
            "ema_20": round(latest['ema_20'], 5),
            "ema_60": round(latest['ema_60'], 5) if pd.notnull(latest['ema_60']) else "N/A",
            "ema_200": round(latest['ema_200'], 5) if pd.notnull(latest['ema_200']) else "N/A",
            "rsi": round(latest['rsi'], 2) if pd.notnull(latest['rsi']) else "N/A"
        }

    return None

# ==============================================================================
# DISCORD BACKGROUND SCANNER LOOP
# ==============================================================================
@tasks.loop(minutes=3)
async def scan_market():
    """Background task scanning all 6 pairs across 4H, 1H, 30M, 15M, and 1M charts."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print(f"[WARNING] Discord channel ID {DISCORD_CHANNEL_ID} not found.")
        return

    print("[INFO] Starting 9/20/60/200 EMA multi-timeframe scan...")

    for symbol in SYMBOLS:
        for tf_label, tf_res in TIMEFRAMES.items():
            df = fetch_market_data(symbol, timeframe_res=tf_res)
            if df is None:
                continue

            df = calculate_indicators(df)
            signal = analyze_signals(symbol, tf_label, df)

            if signal:
                color = discord.Color.green() if "BUY" in signal['type'] else discord.Color.red()
                embed = discord.Embed(
                    title=f"🚨 9/20 EMA Cross Alert: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
                embed.add_field(name="9 EMA", value=str(signal['ema_9']), inline=True)
                embed.add_field(name="20 EMA", value=str(signal['ema_20']), inline=True)
                embed.add_field(name="60 EMA", value=str(signal['ema_60']), inline=True)
                embed.add_field(name="200 EMA", value=str(signal['ema_200']), inline=True)
                embed.add_field(name="RSI (14)", value=str(signal['rsi']), inline=True)
                embed.set_footer(text="TradeLocker MTF Scanner | 9/20/60/200 EMA System")

                await channel.send(embed=embed)
                print(f"[ALERT] Sent {signal['type']} signal for {symbol} ({signal['tf'].upper()}) to Discord.")

            await asyncio.sleep(0.4)

@scan_market.before_loop
async def before_scan():
    """Wait until the Discord bot is fully logged in before starting scanner loop."""
    await bot.wait_until_ready()

# ==============================================================================
# DISCORD INTERACTIVE COMMANDS
# ==============================================================================
@bot.command(name="ping")
async def ping(ctx):
    """Simple ping command to verify bot responsiveness."""
    await ctx.send("🏓 Pong! Bot is active and monitoring EMA setups.")

@bot.command(name="status")
async def status(ctx):
    """Checks the scanner loop status and monitored pairs."""
    is_running = scan_market.is_running()
    status_msg = "🟢 Active" if is_running else "🔴 Stopped"
    await ctx.send(f"**MTF Scanner Status:** {status_msg}\n**Pairs:** {', '.join(SYMBOLS)}\n**Strategy:** 9/20/60/200 EMA Crossover")

@bot.command(name="radar")
async def radar(ctx):
    """Manual market radar scanning all 6 pairs across all timeframes for EMA trends."""
    await ctx.send("📡 **Running 9/20/60/200 EMA Market Radar...** Scanning all timeframes.")
    
    embed = discord.Embed(
        title="📡 Market Radar: EMA Stack Snapshot",
        description="Current market structure relative to the 9, 20, 60, and 200 EMAs (1H Trend):",
        color=discord.Color.blue()
    )

    for symbol in SYMBOLS:
        df = fetch_market_data(symbol, timeframe_res="1h")
        if df is None:
            embed.add_field(name=f"**{symbol}**", value="⚠️ Data Unavailable", inline=True)
            continue

        df = calculate_indicators(df)
        if df is None or df.empty:
            continue

        latest = df.iloc[-1]
        
        # Check overall EMA stack order
        if latest['ema_9'] > latest['ema_20'] > latest['ema_60'] > latest['ema_200']:
            alignment = "🟢 FULL BULLISH STACK"
        elif latest['ema_9'] < latest['ema_20'] < latest['ema_60'] < latest['ema_200']:
            alignment = "🔴 FULL BEARISH STACK"
        elif latest['ema_9'] > latest['ema_20']:
            alignment = "🟢 9/20 Bullish Cross"
        else:
            alignment = "🔴 9/20 Bearish Cross"

        embed.add_field(
            name=f"**{symbol}**",
            value=f"**Structure:** {alignment}\n**Price:** {latest['close']}\n**9 EMA:** {round(latest['ema_9'], 5)}\n**200 EMA:** {round(latest['ema_200'], 5)}",
            inline=True
        )
        await asyncio.sleep(0.3)

    embed.set_footer(text="TradeLocker Radar | 9/20/60/200 EMA Alignment")
    await ctx.send(embed=embed)

@bot.command(name="scalp")
async def scalp(ctx):
    """Fast execution scan focusing on 1M and 15M EMA crossovers."""
    await ctx.send("⚡ **Scalp Scanner Activated:** Scanning 1M and 15M charts for 9/20 EMA triggers...")
    
    scalp_timeframes = {"15m": "15m", "1m": "1m"}
    scalp_signals = 0

    for symbol in SYMBOLS:
        for tf_label, tf_res in scalp_timeframes.items():
            df = fetch_market_data(symbol, timeframe_res=tf_res)
            if df is None:
                continue

            df = calculate_indicators(df)
            signal = analyze_signals(symbol, tf_label, df)

            if signal:
                scalp_signals += 1
                color = discord.Color.green() if "BUY" in signal['type'] else discord.Color.red()
                embed = discord.Embed(
                    title=f"⚡ SCALP EMA ALERT: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Price", value=str(signal['close']), inline=True)
                embed.add_field(name="9 EMA", value=str(signal['ema_9']), inline=True)
                embed.add_field(name="20 EMA", value=str(signal['ema_20']), inline=True)
                embed.set_footer(text="Fast Execution Scalp Trigger")

                await ctx.send(embed=embed)
            await asyncio.sleep(0.4)

    if scalp_signals == 0:
        await ctx.send("⚡ **Scalp Scan Complete:** No 1M/15M EMA crossover triggers active right now.")
    else:
        await ctx.send(f"⚡ **Scalp Scan Complete:** Found {scalp_signals} active setup(s).")

@bot.command(name="scan")
async def manual_scan(ctx):
    """Triggers an immediate manual scan across all 5 timeframes."""
    await ctx.send("🔍 Running full manual scan across 4H, 1H, 30M, 15M, and 1M charts...")
    signals_found = 0

    for symbol in SYMBOLS:
        for tf_label, tf_res in TIMEFRAMES.items():
            df = fetch_market_data(symbol, timeframe_res=tf_res)
            if df is None:
                continue

            df = calculate_indicators(df)
            signal = analyze_signals(symbol, tf_label, df)

            if signal:
                signals_found += 1
                color = discord.Color.green() if "BUY" in signal['type'] else discord.Color.red()
                embed = discord.Embed(
                    title=f"🚨 Forex Signal Alert: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
                embed.add_field(name="9 EMA", value=str(signal['ema_9']), inline=True)
                embed.add_field(name="20 EMA", value=str(signal['ema_20']), inline=True)
                embed.add_field(name="60 EMA", value=str(signal['ema_60']), inline=True)
                embed.add_field(name="200 EMA", value=str(signal['ema_200']), inline=True)
                embed.set_footer(text="TradeLocker MTF Scanner | 9/20/60/200 EMA System")

                await ctx.send(embed=embed)
            await asyncio.sleep(0.4)

    if signals_found == 0:
        await ctx.send("✅ Manual scan complete. No active EMA crossover signals found at this time.")
    else:
        await ctx.send(f"✅ Manual scan complete. Found {signals_found} active signal(s).")

# ==============================================================================
# BOT EVENTS & STARTUP
# ==============================================================================
@bot.event
async def on_ready():
    print(f"[SUCCESS] Bot connected as {bot.user.name} (ID: {bot.user.id})")
    if not scan_market.is_running():
        scan_market.start()

if __name__ == "__main__":
    keep_alive()
    
    if not DISCORD_TOKEN:
        print("[CRITICAL] DISCORD_BOT_TOKEN is missing from Environment Variables.")
    else:
        bot.run(DISCORD_TOKEN)
