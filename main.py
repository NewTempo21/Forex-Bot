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

# TradeLocker API Credentials
TL_EMAIL = os.getenv("TRADELOCKER_EMAIL")
TL_PASSWORD = os.getenv("TRADELOCKER_PASSWORD")
TL_SERVER = os.getenv("TRADELOCKER_SERVER") 
TL_ENVIRONMENT = os.getenv("TRADELOCKER_ENV", "https://live.tradelocker.com")

# Bot configuration
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]

# Timeframe mapping matching TradeLocker resolutions
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
# MARKET DATA FETCHING (FLEXIBLE INSTRUMENT MATCHING)
# ==============================================================================
def fetch_market_data(symbol, timeframe_res, lookback="5D"):
    """Fetches historical candle data with dynamic broker instrument ID matching."""
    client = get_tl_client()
    if not client:
        return None

    try:
        instrument_id = None
        # Try direct lookup first
        try:
            instrument_id = client.get_instrument_id_from_symbol_name(symbol)
        except Exception:
            pass
            
        # Flexible fallback search through all available broker instruments
        if not instrument_id:
            instruments = client.get_all_instruments()
            clean_target = symbol.upper().replace("/", "").replace(".", "")
            
            if isinstance(instruments, pd.DataFrame):
                for _, row in instruments.iterrows():
                    name = str(row.get('name', '')).upper().replace("/", "").replace(".", "")
                    if clean_target in name:
                        instrument_id = row.get('tradableInstrumentId') or row.get('id')
                        break
            elif isinstance(instruments, dict):
                names = instruments.get('name', [])
                ids = instruments.get('tradableInstrumentId', []) or instruments.get('id', [])
                for n, i in zip(names, ids):
                    if clean_target in str(n).upper().replace("/", "").replace(".", ""):
                        instrument_id = i
                        break

        if not instrument_id:
            print(f"[WARNING] Could not find matching instrument ID for {symbol}")
            return None

        history = client.get_price_history(
            instrument_id=int(instrument_id),
            resolution=timeframe_res,
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
    """Calculates 9, 20, 60, and 200 EMAs along with RSI."""
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
    """Evaluates 9/20 EMA crossovers."""
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

    print("[INFO] Starting automated 9/20 EMA multi-timeframe scan...")

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
                embed.add_field(name="60 EMA", value=str(signal['ema_60']), inline=True)
                embed.add_field(name="200 EMA", value=str(signal['ema_200']), inline=True)
                embed.add_field(name="RSI (14)", value=str(signal['rsi']), inline=True)
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

@bot.command(name="status")
async def status(ctx):
    is_running = scan_market.is_running()
    status_msg = "🟢 Active" if is_running else "🔴 Stopped"
    await ctx.send(f"**Scanner Status:** {status_msg}\n**Pairs:** {', '.join(SYMBOLS)}")

@bot.command(name="radar")
async def radar(ctx):
    """Manual market radar scanning all pairs for EMA stack structure and trend bias."""
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
            await asyncio.sleep(0.3)

    if scalp_signals == 0:
        await ctx.send("⚡ **Scalp Scan Complete:** No active 1M/15M EMA crossover triggers.")
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
                    title=f"🚨 Manual Scan Alert: {signal['symbol']} ({signal['tf'].upper()})",
                    color=color
                )
                embed.add_field(name="Signal Type", value=signal['type'], inline=True)
                embed.add_field(name="Timeframe", value=signal['tf'].upper(), inline=True)
                embed.add_field(name="Close Price", value=str(signal['close']), inline=True)
                embed.add_field(name="9/20 EMAs", value=f"{signal['ema_9']} / {signal['ema_20']}", inline=True)
                embed.set_footer(text="TradeLocker MTF Scanner | 9/20/60/200 EMA System")

                await ctx.send(embed=embed)
            await asyncio.sleep(0.3)

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
