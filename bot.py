# bot.py (SCAFFOLD)
# Discord Toggle Bot - Clean rebuild

import discord
from discord.ext import commands
import json
import os
from datetime import datetime

# ----------------------------
# CONFIG - FILL THESE IN
# ----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")  # set in Koyeb or env
CHANNEL_ID = 123456789012345678      # <-- REPLACE THIS

# ----------------------------
# SETUP
# ----------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

STATE_FILE = "state.json"
LOG_FILE = "activity_log.txt"

BUTTONS = ["Normal", "Hard", "Treacherous", "Kingslayer"]

panel_message = None

# ----------------------------
# HELPERS
# ----------------------------

def now():
    return datetime.now().strftime("%B %d, %Y %I:%M %p")


def load_state():
    if not os.path.exists(STATE_FILE):
        state = {b: {"closed": False, "user": None, "timestamp": None} for b in BUTTONS}
        save_state(state)
        return state

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)


def log(text):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now()}] {text}\n")

# ----------------------------
# EMBED
# ----------------------------

def build_embed():
    state = load_state()

    embed = discord.Embed(title="Difficulty Panel", color=discord.Color.gold())

    status = ""
    for b in BUTTONS:
        info = state[b]
        if info["closed"]:
            status += f"🔴 {b} — Closed by {info['user']} — {info['timestamp']}\n"
        else:
            status += f"🟢 {b} — OPEN\n"

    embed.add_field(name="Status", value=status, inline=False)

    return embed

# ----------------------------
# VIEW (PLACEHOLDER)
# ----------------------------

class ControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Buttons will be added later

# ----------------------------
# UPDATE PANEL
# ----------------------------

async def update_panel():
    global panel_message

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found")
        return

    embed = build_embed()

    if panel_message:
        await panel_message.edit(embed=embed, view=ControlView())
    else:
        panel_message = await channel.send(embed=embed, view=ControlView())

# ----------------------------
# READY
# ----------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await update_panel()

# ----------------------------
# RUN BOT
# ----------------------------

bot.run(TOKEN)