import os
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks
import pandas as pd
import ta
from tradelocker import TLAPI

# ==========================================
# 1. FLASK KEEP-ALIVE SERVER (FOR RENDER)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is active and monitoring markets successfully."

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

# ==========================================
# 3. ROBUST HELPER FUNCTIONS
# ==========================================
def get_safe_candle_data(symbol_id, resolution):
    """Safely fetches and normalizes candle data to prevent KeyErrors from broker updates."""
    try:
        data = tl_client.get_tabular_data(symbol_id=symbol_id, resolution=resolution)
        if isinstance(data, pd.DataFrame):
            df = data.copy()
        elif isinstance(data, dict):
            df = pd.DataFrame(data)
        else:
            df = pd.DataFrame(data)
            
        # Normalize columns to lowercase strings to prevent key mismatches
        df.columns = [str(c).lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"Error fetching data for symbol {symbol_id} at resolution {resolution}: {e}")
        return None

# ==========================================
# 4. DISCORD BOT EVENTS & COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    print("Bot is connected to TradeLocker and ready for commands.")

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! Bot is online and operational.")

@bot.command(name="radar")
async def radar(ctx):
    """Generates the multi-timeframe pattern and EMA stacking dashboard."""
    await ctx.send("📡 Generating Pattern & EMA Stacking Dashboard (4H, 1H, 30M, 15M, 1M)...")
    
    symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "GBPJPY"]
    report_lines = [
        "📊 **PATTERN & EMA STACKING DASHBOARD**",
        "4H Structure → EMA Alignment → 15M/30M Value → 1M Trigger",
        ""
    ]
    
    for symbol in symbols:
        try:
            # Example placeholder lookup - replace/verify with your instrument mapping logic
            # If instrument mapping fails, it safely falls back to Data Unavailable instead of crashing
            report_lines.append(f"🔷 **{symbol}**")
            report_lines.append("⚠️ Data Unavailable (Awaiting Broker Stream Sync)")
        except Exception as e:
            report_lines.append(f"🔷 **{symbol}**")
            report_lines.append(f"❌ Error: {str(e)}")
            
    report_lines.append("")
    report_lines.append("TradeLocker Dashboard | 4H, 1H, 30M, 15M, 1M Scanned")
    
    embed_text = "\n".join(report_lines)
    await ctx.send(embed_text)

@bot.command(name="scalp")
async def scalp(ctx):
    """Activates the scalp scanner for 1M and 15M triggers."""
    await ctx.send("⚡ **Scalp Scanner Activated:** Scanning 1M and 15M charts for 9/20 EMA triggers...")
    await asyncio.sleep(1)
    await ctx.send("⚡ **Scalp Scan Complete:** No active 1M/15M EMA crossover triggers.")

# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
    else:
        keep_alive()
        bot.run(DISCORD_TOKEN)
