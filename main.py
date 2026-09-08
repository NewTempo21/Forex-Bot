import os
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
import pandas as pd
import ta
from tradelocker import TLAPI

# ==========================================
# 1. FLASK KEEP-ALIVE SERVER (FOR RENDER)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Forex Trading Bot is active and monitoring markets."

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ==========================================
# 2. ENVIRONMENT & CONFIGURATION
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

TL_EMAIL = os.getenv("HEROFX_EMAIL")
TL_PASSWORD = os.getenv("HEROFX_PASSWORD")
TL_SERVER = os.getenv("HEROFX_SERVER", "HeroFX-Live")
TL_ENVIRONMENT = os.getenv("HEROFX_ENV", "https://live.tradelocker.com")

# Initialize TradeLocker Client
tl_client = TLAPI(
    environment=TL_ENVIRONMENT,
    username=TL_EMAIL,
    password=TL_PASSWORD,
    server=TL_SERVER
)

# Initialize Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

instrument_cache = {}

def get_instrument_id(symbol_name):
    """Dynamically fetches or caches the broker instrument ID for a given symbol."""
    symbol_name = symbol_name.upper()
    if symbol_name in instrument_cache:
        return instrument_cache[symbol_name]
    try:
        instruments = tl_client.get_instruments()
        inst_list = instruments.get('d', []) if isinstance(instruments, dict) else instruments
        for inst in inst_list:
            name = inst.get('name') or inst.get('symbol')
            if name == symbol_name:
                inst_id = inst.get('id') or inst.get('instrumentId')
                instrument_cache[symbol_name] = inst_id
                return inst_id
    except Exception as e:
        print(f"Error fetching instrument ID for {symbol_name}: {e}")
    return None

# ==========================================
# 3. INDICATOR & MARKET STATE ENGINE
# ==========================================
def fetch_and_calculate_indicators(symbol_id, resolution):
    """Safely fetches candle data, normalizes columns, and calculates 9, 20, 60, 200 EMAs + RSI."""
    try:
        data = tl_client.get_tabular_data(symbol_id=symbol_id, resolution=resolution)
        if isinstance(data, pd.DataFrame):
            df = data.copy()
        elif isinstance(data, dict):
            df = pd.DataFrame(data)
        else:
            df = pd.DataFrame(data)
            
        df.columns = [str(c).lower() for c in df.columns]
        price_col = 'close' if 'close' in df.columns else (df.columns[-1] if len(df.columns) > 0 else None)
        if not price_col:
            return None
            
        close_series = pd.to_numeric(df[price_col], errors='coerce')
        
        # Calculate EMAs (9, 20, 60, 200) and RSI
        df['ema_9'] = ta.trend.EMAIndicator(close=close_series, window=9).ema_indicator()
        df['ema_20'] = ta.trend.EMAIndicator(close=close_series, window=20).ema_indicator()
        df['ema_60'] = ta.trend.EMAIndicator(close=close_series, window=60).ema_indicator()
        df['ema_200'] = ta.trend.EMAIndicator(close=close_series, window=200).ema_indicator()
        df['rsi'] = ta.momentum.RSIIndicator(close=close_series, window=14).rsi()
        
        return df
    except Exception as e:
        print(f"Indicator calculation error for ID {symbol_id} at {resolution}: {e}")
        return None

def analyze_market_conditions(df):
    """Analyzes trend state, RSI, and re-entry zones for a single timeframe chart."""
    if df is None or len(df) < 5:
        return "Unknown", 50, "Insufficient Data"
        
    last_row = df.iloc[-1]
    close = last_row.get('close', 0)
    ema9 = last_row.get('ema_9', 0)
    ema20 = last_row.get('ema_20', 0)
    ema60 = last_row.get('ema_60', 0)
    ema200 = last_row.get('ema_200', 0)
    rsi = round(last_row.get('rsi', 50), 1)
    
    # Trend State
    if ema9 > ema20 > ema60 > ema200 and close > ema9:
        state = "🟢 Bullish Trend"
    elif ema9 < ema20 < ema60 < ema200 and close < ema9:
        state = "🔴 Bearish Trend"
    else:
        state = "🟡 Consolidating / Ranging"
        
    # Re-entry Zone Check
    dist_to_20 = abs(close - ema20) / close * 100
    dist_to_60 = abs(close - ema60) / close * 100
    
    if dist_to_20 <= 0.05:
        zone = "🎯 Pullback at 20 EMA (Active Re-entry Zone)"
    elif dist_to_60 <= 0.08:
        zone = "🎯 Pullback at 60 EMA (Deep Value Zone)"
    else:
        zone = "⚖️ Price moving freely between EMAs"
        
    return state, rsi, zone

# ==========================================
# 4. DISCORD COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}. Trading engine operational.")

@bot.command(name="scan")
async def scan(ctx, symbol: str = "EURUSD", timeframe: str = "15m"):
    """
    Scans a symbol on a specific timeframe.
    Usage: !scan EURUSD 15m  or  !scan GBPUSD 4h
    """
    await ctx.invoke(bot.get_command('chart'), symbol=symbol, timeframe=timeframe)

@bot.command(name="chart")
async def chart(ctx, symbol: str = "EURUSD", timeframe: str = "15m"):
    """
    Independently analyzes ANY single chart on demand.
    Usage: !chart EURUSD 4h  OR  !chart GBPUSD 15m
    Supported timeframes: 4h, 1h, 30m, 15m, 1m
    """
    tf_map = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240"
    }
    
    tf_clean = timeframe.lower()
    if tf_clean not in tf_map:
        await ctx.send(f"❌ Invalid timeframe `{timeframe}`. Use: `4h`, `1h`, `30m`, `15m`, or `1m`.")
        return
        
    resolution = tf_map[tf_clean]
    symbol_upper = symbol.upper()
    
    await ctx.send(f"🔍 Analyzing **{symbol_upper}** on the **{tf_clean.upper()}** chart...")
    
    symbol_id = get_instrument_id(symbol_upper)
    if not symbol_id:
        await ctx.send(f"❌ Could not find instrument ID for `{symbol_upper}`.")
        return
        
    df = fetch_and_calculate_indicators(symbol_id, resolution=resolution)
    if df is None:
        await ctx.send(f"⚠️ Failed to fetch data for {symbol_upper} at {tf_clean.upper()}.")
        return
        
    state, rsi, zone = analyze_market_conditions(df)
    
    report = [
        f"📊 **CHART ANALYSIS: {symbol_upper} ({tf_clean.upper()})**",
        f"• **Market State:** {state}",
        f"• **RSI (14):** `{rsi}`",
        f"• **Zone Status:** {zone}",
        f"• **Framework:** 9 / 20 / 60 / 200 EMA Reactions"
    ]
    
    await ctx.send("\n".join(report))

@bot.command(name="scalp")
async def scalp(ctx, symbol: str = "EURUSD"):
    """Quick 1m scalping trigger check."""
    await ctx.invoke(bot.get_command('chart'), symbol=symbol, timeframe="1m")

@bot.command(name="radar")
async def radar(ctx):
    """Overview command."""
    await ctx.send("📡 Use **`!scan [symbol] [timeframe]`** or **`!chart [symbol] [timeframe]`** to inspect any individual chart (e.g., `!scan EURUSD 15m` or `!chart GBPUSD 4h`).")

# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
    else:
        keep_alive()
        bot.run(DISCORD_TOKEN)
