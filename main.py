import sys
import types

# ==============================================================================
# PRE-IMPORT HOOK FOR TRADELOCKER TYPE-CHECK BYPASS
# ==============================================================================
class TradelockerPatcher(types.ModuleType):
    pass

# We hook directly into Python's module loading system to neutralize the strict typing check
# before the library can even initialize its internal dictionaries.
import importlib.abc
import importlib.machinery

class TradelockerFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'tradelocker.tradelocker_api':
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Load the original module normally
        real_mod_name = 'tradelocker.tradelocker_api'
        mod = importlib.import_module('_tradelocker_api_real')
        for attr in dir(mod):
            setattr(module, attr, getattr(mod, attr))
        
        # Override _apply_typing to permanently absorb the 'status' field error
        if hasattr(module, '_apply_typing'):
            orig_typing = module._apply_typing
            def patched_apply_typing(df, types_dict, *args, **kwargs):
                if isinstance(types_dict, dict) and 'status' not in types_dict:
                    types_dict['status'] = object
                try:
                    return orig_typing(df, types_dict, *args, **kwargs)
                except Exception:
                    return df
            module._apply_typing = patched_apply_typing

# Register the finder
import tradelocker
sys.modules['_tradelocker_api_real'] = tradelocker.tradelocker_api
sys.meta_path.insert(0, TradelockerFinder())

# ==============================================================================
# STANDARD IMPORTS & BOT SETUP
# ==============================================================================
import os
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks
import pandas as pd
import ta
from tradelocker import TLAPI

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

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

TL_EMAIL = os.getenv("HEROFX_EMAIL")
TL_PASSWORD = os.getenv("HEROFX_PASSWORD")
TL_SERVER = os.getenv("HEROFX_SERVER", "HeroFX-Live") 
TL_ENVIRONMENT = os.getenv("HEROFX_ENV", "https://live.tradelocker.com")

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]

TIMEFRAMES = {
    "4h": "240",
    "1h": "60",
    "30m": "30",
    "15m": "15",
    "1m": "1"
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

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

def get_instrument_id(client, symbol):
    try:
        try:
            iid = client.get_instrument_id_from_symbol_name(symbol)
            if iid:
                return int(iid)
        except Exception:
            pass

        instruments = client.get_all_instruments()
        clean_target = symbol.upper().replace("/", "").replace(".", "")

        items = []
        if isinstance(instruments, pd.DataFrame):
            items = instruments.to_dict(orient='records')
        elif isinstance(instruments, list):
            items = instruments
        elif isinstance(instruments, dict):
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

def fetch_market_data(symbol, timeframe_res):
    client = get_tl_client()
    if not client:
        return None

    lookback = "5D"
    if timeframe_res == "240":
        lookback = "60D"
    elif timeframe_res == "60":
        lookback = "30D"
    elif timeframe_res == "30":
        lookback = "15D"
    elif timeframe_res == "15":
        lookback = "10D"
    elif timeframe_res == "1":
        lookback = "3D"

    try:
        instrument_id = get_instrument_id(client, symbol)
        if not instrument_id:
            return None

        history = client.get_price_history(
            instrument_id=instrument_id,
            resolution=str(timeframe_res),
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
            "symbol": symbol, "tf": tf_label, "type": "BUY 🟢",
            "close": latest['close'], "ema_9": round(latest['ema_9'], 5), "ema_20": round(latest['ema_20'], 5)
        }
    elif bearish_cross:
        return {
            "symbol": symbol, "tf": tf_label, "type": "SELL 🔴",
            "close": latest['close'], "ema_9": round(latest['ema_9'], 5), "ema_20": round(latest['ema_20'], 5)
        }
    return None

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
                embed.set_footer(text="TradeLocker MTF Scanner | 9/20/60/200 EMA System")

                await channel.send(embed=embed)
            await asyncio.sleep(0.3)

@scan_market.before_loop
async def before_scan():
    await bot.wait_until_ready()

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("🏓 Pong! Bot is active.")

@bot.command(name="diag")
async def diag(ctx, symbol: str = "EURUSD"):
    await ctx.send(f"🔍 **Running diagnostics for {symbol}...**")
    client = get_tl_client()
    inst_id = get_instrument_id(client, symbol)
    if not inst_id:
        return await ctx.send(f"❌ Failed to find Instrument ID for {symbol}.")
    
    await ctx.send(f"✅ Found Instrument ID: `{inst_id}`")
    try:
        history = client.get_price_history(
            instrument_id=inst_id, resolution="60",
            start_timestamp=0, end_timestamp=0, lookback_period="5D"
        )
        if not history:
            return await ctx.send("❌ API connected, but returned empty history (None).")
        candles = history if isinstance(history, list) else history.get("candles", [])
        if not candles:
            return await ctx.send(f"❌ API returned data, but no candles: `{history}`")
        await ctx.send(f"✅ Success! Fetched {len(candles)} candles.")
    except Exception as e:
        await ctx.send(f"❌ API Error fetching history: `{e}`")

@bot.command(name="radar")
async def radar(ctx):
    await ctx.send("📡 **Generating Pattern & EMA Stacking Dashboard (4H, 1H, 30M, 15M, 1M)...**")
    
    embed = discord.Embed(
        title="📈 PATTERN & EMA STACKING DASHBOARD",
        description="4H Structure → EMA Alignment → 15M/30M Value → 1M Trigger",
        color=discord.Color.dark_embed()
    )

    for symbol in SYMBOLS:
        df_4h = calculate_indicators(fetch_market_data(symbol, timeframe_res=TIMEFRAMES["4h"]))
        df_1h = calculate_indicators(fetch_market_data(symbol, timeframe_res=TIMEFRAMES["1h"]))
        df_30m = calculate_indicators(fetch_market_data(symbol, timeframe_res=TIMEFRAMES["30m"]))
        df_15m = calculate_indicators(fetch_market_data(symbol, timeframe_res=TIMEFRAMES["15m"]))
        df_1m = calculate_indicators(fetch_market_data(symbol, timeframe_res=TIMEFRAMES["1m"]))

        if df_1h is None or df_1h.empty:
            embed.add_field(name=f"🔹 {symbol}", value="⚠️ Data Unavailable", inline=False)
            continue

        if df_4h is not None and len(df_4h) >= 2:
            latest_4h = df_4h.iloc[-1]
            prev_4h = df_4h.iloc[-2]
            if latest_4h['close'] > prev_4h['close']:
                h4_pattern = "📈 HIGHER HIGHS & HIGHER LOWS"
                h4_stack = "BULLISH BIAS (WEAK STACK) 🐂" if latest_4h['ema_9'] < latest_4h['ema_20'] else "BULLISH EXPANSION 🟢"
            else:
                h4_pattern = "📉 LOWER HIGHS & LOWER LOWS"
                h4_stack = "BEARISH BIAS (WEAK STACK) 🐻" if latest_4h['ema_9'] > latest_4h['ema_20'] else "BEARISH EXPANSION 🔴"
        else:
            h4_pattern = "⚖️ RANGE BOUND / CONSOLIDATION"
            h4_stack = "⚪ NEUTRAL STACK"

        latest_1h = df_1h.iloc[-1]
        if latest_1h['ema_9'] > latest_1h['ema_20']:
            h1_stack = "BULLISH EXPANSION 🟢"
        else:
            h1_stack = "BEARISH EXPANSION 🔴"

        m30_stack = "⚡ 30M BULLISH ALIGNMENT"
        if df_30m is not None and not df_30m.empty:
            latest_30m = df_30m.iloc[-1]
            if latest_30m['ema_9'] > latest_30m['ema_20']:
                m30_stack = "🟢 30M BULLISH BIAS"
            else:
                m30_stack = "🔴 30M BEARISH BIAS"

        m15_setup = "⚡ EXTENDED FROM EMAS"
        if df_15m is not None and not df_15m.empty:
            latest_15m = df_15m.iloc[-1]
            diff_from_20 = abs(latest_15m['close'] - latest_15m['ema_20'])
            if diff_from_20 < (latest_15m['close'] * 0.001):
                m15_setup = "👀 AT 15M 20 EMA (PRIME RE-ENTRY)"

        m1_trigger = "⚪ NO 1M TRIGGER"
        if df_1m is not None and len(df_1m) >= 2:
            sig_1m = analyze_signals(symbol, "1m", df_1m)
            if sig_1m:
                m1_trigger = f"🚨 {sig_1m['type']} CROSS TRIGGER"

        current_price = latest_1h['close']

        dashboard_text = (
            f"• **4H Pattern:** {h4_pattern}\n"
            f"• **4H EMA Stack:** {h4_stack}\n"
            f"• **1H EMA Stack:** {h1_stack}\n"
            f"• **30M Stack:** {m30_stack}\n"
            f"• **15M Setup:** {m15_setup}\n"
            f"• **1M Trigger:** {m1_trigger}\n"
            f"• **Price:** `{current_price}`"
        )

        embed.add_field(name=f"🔷 **{symbol}**", value=dashboard_text, inline=False)
        await asyncio.sleep(0.3)

    embed.set_footer(text="TradeLocker Dashboard | 4H, 1H, 30M, 15M, 1M Scanned")
    await ctx.send(embed=embed)

@bot.command(name="scalp")
async def scalp(ctx):
    await ctx.send("⚡ **Scalp Scanner Activated:** Scanning 1M and 15M charts for 9/20 EMA triggers...")
    scalp_timeframes = {"15m": TIMEFRAMES["15m"], "1m": TIMEFRAMES["1m"]}
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
                embed.set_footer(text="Fast Execution Scalp Trigger")

                await ctx.send(embed=embed)
            await asyncio.sleep(0.3)

    if scalp_signals == 0:
        await ctx.send("⚡ **Scalp Scan Complete:** No active 1M/15M EMA crossover triggers.")
    else:
        await ctx.send(f"⚡ **Scalp Scan Complete:** Found {scalp_signals} active setup(s).")

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

@bot.event
async def on_ready():
    print(f"[SUCCESS] Bot connected as {bot.user.name} (ID: {bot.user.id})")
    debug_print_instruments()
    if not scan_market.is_running():
        scan_market.start()

if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("[CRITICAL] DISCORD_BOT_TOKEN is missing.")
    else:
        bot.run(DISCORD_TOKEN)
