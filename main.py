import os
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks
import pandas as pd
import ta
from tradelocker import TLAPI

# ==============================================================================
# FLASK WEB SERVER (FOR REPLIT/RENDER HEALTH CHECKS)
# ==============================================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is active and monitoring markets via TradeLocker!"

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

# TradeLocker API Credentials (HeroFX Render variables)
TL_EMAIL = os.getenv("HEROFX_EMAIL")
TL_PASSWORD = os.getenv("HEROFX_PASSWORD")
TL_SERVER = os.getenv("HEROFX_SERVER", "HeroFX-Live") 
TL_ENVIRONMENT = os.getenv("HEROFX_ENV", "https://live.tradelocker.com")

# Bot configuration
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]

TIMEFRAMES = {
    "4h": "4h",
    "1h": "1h",
    "30m": "30m",
    "15m": "15m",
    "1m": "1m"
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize TradeLocker Official API Client
tl_client = None

def get_tl_client():
    global tl_client
    if tl_client is None:
        try:
            tl_client = TLAPI(
                environment=TL_ENVIRONMENT,
                username=TL_EMAIL,
                password=TL_PASSWORD,
                server=TL_SERVER
            )
            print("[SUCCESS] Connected to TradeLocker API successfully.")
        except Exception as e:
            print(f"[CRITICAL] Failed to initialize TradeLocker client: {e}")
            tl_client = None
    return tl_client

# ==============================================================================
# INSTRUMENT RESOLUTION & DEBUG DUMPER
# ==============================================================================
def get_instrument_id(client, symbol):
    """Robustly resolves instrument ID from HeroFX broker specifications."""
    try:
        # 1. Try standard library method
        try:
            iid = client.get_instrument_id_from_symbol_name(symbol)
            if iid:
                return int(iid)
        except Exception:
            pass

        # 2. Pull all instruments and inspect format
        instruments = client.get_all_instruments()
        clean_target = symbol.upper().replace("/", "").replace(".", "")

        # Helper to extract rows/items
        items = []
        if isinstance(instruments, pd.DataFrame):
            items = instruments.to_dict(orient='records')
        elif isinstance(instruments, list):
            items = instruments
        elif isinstance(instruments, dict):
            # Try typical dictionary wrappers
            for k, v in instruments.items():
                if isinstance(v, list):
                    items = v
                    break

        for item in items:
            if isinstance(item, dict):
                name = str(item.get('name', '')).upper()
                clean_name = name.replace("/", "").replace(".", "").replace(" ", "")
                if clean_target == clean_name or clean_target in clean_name:
                    return int(item.get('tradableInstrumentId') or item.get('id'))

    except Exception as e:
        print(f"[ERROR] Instrument resolution failed for {symbol}: {e}")
    
    return None

def debug_print_instruments():
    """Prints all broker instruments to Render console to verify naming convention."""
    client = get_tl_client()
    if not client:
        return
    try:
        instruments = client.get_all_instruments()
        print("=== AVAILABLE BROKER INSTRUMENTS ===")
        if isinstance(instruments, pd.DataFrame):
            for _, row in instruments.iterrows():
                print(f"Name: {row.get('name')} | ID: {row.get('tradableInstrumentId') or row.get('id')}")
        elif isinstance(instruments, list):
            for item in instruments:
                if isinstance(item, dict):
                    print(f"Name: {item.get('name')} | ID: {item.get('tradableInstrumentId') or item.get('id')}")
        print("====================================")
    except Exception as e:
        print(f"[DEBUG] Could not dump instruments: {e}")

# ==============================================================================
# MARKET DATA FETCHING
# ==============================================================================
def fetch_market_data(symbol, timeframe_res, lookback="5D"):
    client = get_tl_client()
    if not client:
        return None

    try:
        instrument_id = get_instrument_id(client, symbol)
        if not instrument_id:
            print(f"[WARNING] Instrument ID not found for symbol: {symbol}")
            return None

        history = client.get_price_history(
            instrument_id=instrument_id,
            resolution=timeframe_res.upper(),
            start_timestamp=0,
            end_timestamp=0,
            lookback_period=lookback
        )
        
        if not history:
            return None

        candles = history if isinstance(history, list) else history.get("candles", [])
        if not candles:
            return None

        df = pd.DataFrame(candles)
        rename_map = {}
        for col in df.columns:
            if col in ['t', 'timestamp']: rename_map[col] = 'timestamp'
            elif col in ['o', 'open']: rename_map[col] = 'open'
            elif col in ['h', 'high']: rename_map[col] = 'high'
            elif col in ['l', 'low']: rename_map[col] = 'low'
            elif col in ['c', 'close']: rename_map[col] = 'close'
            elif col in ['v', 'volume']: rename_map[col] = 'volume'
        
        df = df.rename(columns=rename_map)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df
    except Exception as e:
        print(f"[ERROR] Failed fetching data for {symbol} ({timeframe_res}): {e}")
        return None

# ==============================================================================
# TECHNICAL ANALYSIS & 9/20/60/200 EMA STRATEGY
# ==============================================================================
def calculate_indicators(df):
    if df is None or df.empty or len(df) < 30:
        return None

    df['ema_9'] = ta.trend.ema_indicator(df['close'], window=9)
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema_60'] = ta.trend.ema_indicator(df['close'], window=60)
    
    if len(df) >= 200:
        df['ema_200'] = ta.trend.ema_indicator(df['close'], window=200)
    else:
        df['ema_200'] = ta.trend.ema_indicator(df['close'], window=len(df)-1)

    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    return df

def analyze_signals(symbol, tf_label, df):
    if df is None or len(df) < 2:
        return None

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    bullish_cross = (previous['ema_9'] <= previous['ema_20']) and (latest['ema_9'] > latest['ema_20'])
    bearish_cross = (previous['ema_9'] >= previous['ema_20']) and (latest['ema_9'] < latest['ema_20'])

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
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

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
                    title=f"🚨 EMA Crossover Alert: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
                embed.add_field(name="9 EMA", value=str(signal['ema_9']), inline=True)
                embed.add_field(name="20 EMA", value=str(signal['ema_20']), inline=True)
                embed.set_footer(text="TradeLocker MTF Scanner | 9/20/60/200 EMA System")

                await channel.send(embed=embed)
            await asyncio.sleep(0.3)

@scan_market.before_loop
async def before_scan():
    await bot.wait_until_ready()

# ==============================================================================
# DISCORD COMMANDS
# ==============================================================================
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("🏓 Pong! Bot is active.")

@bot.command(name="radar")
async def radar(ctx):
    await ctx.send("📡 **Running 9/20/60/200 EMA Market Radar...** Analyzing 1H structure.")
    
    embed = discord.Embed(
        title="📡 Market Radar: EMA Stack Snapshot",
        description="Current market structure relative to 9, 20, 60, and 200 EMAs (1H Trend):",
        color=discord.Color.blue()
    )

    for symbol in SYMBOLS:
        df = fetch_market_data(symbol, timeframe_res="1h")
        if df is None:
            embed.add_field(name=f"**{symbol}**", value="⚠️ Data Unavailable", inline=True)
            continue

        df = calculate_indicators(df)
        if df is None or df.empty:
            embed.add_field(name=f"**{symbol}**", value="⚠️ Calculation Error", inline=True)
            continue

        latest = df.iloc[-1]
        
        if latest['ema_9'] > latest['ema_20'] > latest['ema_60'] > latest['ema_200']:
            alignment = "🟢 FULL BULLISH STACK"
        elif latest['ema_9'] < latest['ema_20'] < latest['ema_60'] < latest['ema_200']:
            alignment = "🔴 FULL BEARISH STACK"
        else:
            alignment = "⚡ Mixed Alignment"

        embed.add_field(
            name=f"**{symbol}**",
            value=f"**Structure:** {alignment}\n**Price:** {latest['close']}\n**9 EMA:** {round(latest['ema_9'], 5)}",
            inline=True
        )
        await asyncio.sleep(0.3)

    embed.set_footer(text="TradeLocker Radar | 9/20/60/200 EMA Alignment")
    await ctx.send(embed=embed)

@bot.command(name="scan")
async def manual_scan(ctx):
    await ctx.send("🔍 Running full manual scan across all timeframes...")
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
                    title=f"🚨 Manual Scan Alert: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
                await ctx.send(embed=embed)
            await asyncio.sleep(0.3)

    if signals_found == 0:
        await ctx.send("✅ Manual scan complete. No active signals found.")
    else:
        await ctx.send(f"✅ Manual scan complete. Found {signals_found} active signal(s).")

# ==============================================================================
# BOT EVENTS & STARTUP
# ==============================================================================
@bot.event
async def on_ready():
    print(f"[SUCCESS] Bot connected as {bot.user.name} (ID: {bot.user.id})")
    debug_print_instruments() # Dumps exact broker names to logs
    if not scan_market.is_running():
        scan_market.start()

if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("[CRITICAL] DISCORD_BOT_TOKEN is missing.")
    else:
        bot.run(DISCORD_TOKEN)
