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
    return "Forex Trading Bot is active and monitoring HeroFX markets."

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

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

instrument_cache = {}

# Standard Watchlist (Bot will automatically find the .raw versions)
US_GBP_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", 
    "USDCAD", "NZDUSD", "USDCHF", "GBPJPY", 
    "EURGBP", "GBPCAD", "GBPAUD", "GBPCHF"
]

def get_instrument_id(symbol_name):
    """
    Fetches the broker instrument ID. 
    Automatically checks for HeroFX raw spread suffixes (.raw, .r).
    """
    symbol_name = symbol_name.upper()
    possible_names = [symbol_name, f"{symbol_name}.RAW", f"{symbol_name}.R"]
    
    if symbol_name in instrument_cache:
        return instrument_cache[symbol_name]
        
    try:
        instruments = tl_client.get_instruments()
        data = instruments.get('d', instruments) if isinstance(instruments, dict) else instruments
        
        if isinstance(data, list):
            for inst in data:
                if isinstance(inst, dict):
                    inst_name = str(inst.get('name', inst.get('symbol', ''))).upper()
                    if inst_name in possible_names:
                        inst_id = inst.get('id', inst.get('instrumentId'))
                        if inst_id:
                            instrument_cache[symbol_name] = inst_id
                            return inst_id
                            
        elif isinstance(data, dict):
            names = data.get('name', data.get('symbol', []))
            ids = data.get('id', data.get('instrumentId', []))
            if names and ids:
                for i in range(min(len(names), len(ids))):
                    inst_name = str(names[i]).upper()
                    if inst_name in possible_names:
                        inst_id = ids[i]
                        instrument_cache[symbol_name] = inst_id
                        return inst_id
                        
    except Exception as e:
        print(f"Error fetching instrument ID for {symbol_name}: {e}")
        
    return None

# ==========================================
# 3. INDICATOR & MARKET STATE ENGINE
# ==========================================
def fetch_and_calculate_indicators(symbol_id, resolution):
    """Fetches candle data safely and calculates EMAs and RSI."""
    try:
        data = tl_client.get_tabular_data(symbol_id=symbol_id, resolution=resolution)
        if not data:
            return None
            
        df = pd.DataFrame(data)
        df.columns = [str(c).lower() for c in df.columns]
        
        price_col = 'close' if 'close' in df.columns else (df.columns[-1] if len(df.columns) > 0 else None)
        if not price_col:
            return None
            
        close_series = pd.to_numeric(df[price_col], errors='coerce')
        
        # Calculate EMAs and RSI
        df['ema_9'] = ta.trend.EMAIndicator(close=close_series, window=9).ema_indicator()
        df['ema_20'] = ta.trend.EMAIndicator(close=close_series, window=20).ema_indicator()
        df['ema_60'] = ta.trend.EMAIndicator(close=close_series, window=60).ema_indicator()
        df['ema_200'] = ta.trend.EMAIndicator(close=close_series, window=200).ema_indicator()
        df['rsi'] = ta.momentum.RSIIndicator(close=close_series, window=14).rsi()
        
        return df
    except Exception as e:
        print(f"Indicator calculation error: {e}")
        return None

def analyze_market_conditions(df):
    """Determines market trend and re-entry zones based on the 9/20/60/200 EMAs."""
    if df is None or len(df) < 5:
        return "Unknown", 50, "Insufficient Data"
        
    last_row = df.iloc[-1]
    close = last_row.get('close', 0)
    ema9 = last_row.get('ema_9', 0)
    ema20 = last_row.get('ema_20', 0)
    ema60 = last_row.get('ema_60', 0)
    ema200 = last_row.get('ema_200', 0)
    rsi = round(last_row.get('rsi', 50), 1)
    
    # Check Trend
    if ema9 > ema20 > ema60 > ema200 and close > ema9:
        state = "🟢 Bullish"
    elif ema9 < ema20 < ema60 < ema200 and close < ema9:
        state = "🔴 Bearish"
    else:
        state = "🟡 Consolidating"
        
    # Check Zones
    dist_to_20 = abs(close - ema20) / close * 100
    dist_to_60 = abs(close - ema60) / close * 100
    
    if dist_to_20 <= 0.05:
        zone = "🎯 Pullback at 20 EMA (Re-entry)"
    elif dist_to_60 <= 0.08:
        zone = "🎯 Pullback at 60 EMA (Value Zone)"
    else:
        zone = "⚖️ Moving freely"
        
    return state, rsi, zone

# ==========================================
# 4. DISCORD COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}. Ready to trade.")

@bot.command(name="scan")
async def scan(ctx, timeframe: str = "30m"):
    """
    Scans all US and GBP pairs on a chosen timeframe.
    Defaults to 30m if no timeframe is typed.
    """
    tf_map = {
        "1m": "1",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240"
    }
    
    tf_clean = timeframe.lower()
    if tf_clean not in tf_map:
        await ctx.send(f"❌ Invalid timeframe. Please use `4h`, `1h`, `30m`, `15m`, or `1m`.")
        return
        
    resolution = tf_map[tf_clean]
    await ctx.send(f"📡 **Scanning US & GBP Pairs ({tf_clean.upper()})**... Please stand by.")
    
    report = [f"📊 **MARKET SCAN ({tf_clean.upper()})**", ""]
    
    for symbol in US_GBP_PAIRS:
        symbol_id = get_instrument_id(symbol)
        if not symbol_id:
            report.append(f"🔷 **{symbol}**: ⚠️ ID Not Found on HeroFX")
            continue
            
        df = fetch_and_calculate_indicators(symbol_id, resolution=resolution)
        if df is None:
            report.append(f"🔷 **{symbol}**: ⚠️ Data Fetch Failed")
            continue
            
        state, rsi, zone = analyze_market_conditions(df)
        report.append(f"🔷 **{symbol}** | RSI: `{rsi}`")
        report.append(f"   • State: **{state}**")
        report.append(f"   • Zone: *{zone}*")
        report.append("")
        
    # Send report in chunks to bypass Discord message limits
    message_chunk = ""
    for line in report:
        if len(message_chunk) + len(line) + 1 > 1900:
            await ctx.send(message_chunk)
            message_chunk = line + "\n"
        else:
            message_chunk += line + "\n"
    if message_chunk:
        await ctx.send(message_chunk)

@bot.command(name="chart")
async def chart(ctx, symbol: str = "EURUSD", timeframe: str = "30m"):
    """Analyzes a specific chart on demand."""
    tf_map = {"1m": "1", "15m": "15", "30m": "30", "1h": "60", "4h": "240"}
    tf_clean = timeframe.lower()
    
    if tf_clean not in tf_map:
        await ctx.send("❌ Invalid timeframe.")
        return
        
    resolution = tf_map[tf_clean]
    symbol_upper = symbol.upper()
    
    await ctx.send(f"🔍 Analyzing **{symbol_upper} ({tf_clean.upper()})**...")
    
    symbol_id = get_instrument_id(symbol_upper)
    if not symbol_id:
        await ctx.send(f"❌ Could not find {symbol_upper} on HeroFX.")
        return
        
    df = fetch_and_calculate_indicators(symbol_id, resolution=resolution)
    if df is None:
        await ctx.send(f"⚠️ Failed to fetch data.")
        return
        
    state, rsi, zone = analyze_market_conditions(df)
    
    report = [
        f"📊 **CHART: {symbol_upper} ({tf_clean.upper()})**",
        f"• **Market State:** {state}",
        f"• **RSI (14):** `{rsi}`",
        f"• **Zone Status:** {zone}"
    ]
    await ctx.send("\n".join(report))

# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
    else:
        keep_alive()
        bot.run(DISCORD_TOKEN)
