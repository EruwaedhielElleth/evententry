# bot.py — Railway SAFE asyncpg version

import discord
from discord.ext import commands
import asyncpg
import os
from datetime import datetime, timedelta

# ----------------------------
# CONFIG
# ----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BUTTONS = ["Normal", "Hard", "Treacherous", "Kingslayer"]
DEFAULT_EXPIRY = 60

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

pool = None

# ----------------------------
# TIME
# ----------------------------

def now():
    return datetime.now().strftime("%B %d, %Y %I:%M %p")

# ----------------------------
# DB INIT
# ----------------------------

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS states (
            guild_id BIGINT,
            name TEXT,
            closed BOOLEAN DEFAULT FALSE,
            user_name TEXT,
            timestamp TEXT,
            expires_at TEXT,
            PRIMARY KEY (guild_id, name)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id BIGINT,
            name TEXT,
            expiry_minutes INT,
            PRIMARY KEY (guild_id, name)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            entry TEXT
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS panel (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT,
            message_id BIGINT
        )
        """)

# ----------------------------
# STATE HELPERS
# ----------------------------

async def ensure_state(guild_id):
    async with pool.acquire() as conn:
        for b in BUTTONS:
            await conn.execute("""
            INSERT INTO states (guild_id, name, closed, user_name, timestamp, expires_at)
            VALUES ($1,$2,false,NULL,NULL,NULL)
            ON CONFLICT DO NOTHING
            """, guild_id, b)

async def get_state(guild_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, closed, user_name, timestamp, expires_at FROM states WHERE guild_id=$1",
            guild_id
        )

    state = {}
    for r in rows:
        state[r["name"]] = {
            "closed": r["closed"],
            "user": r["user_name"],
            "timestamp": r["timestamp"],
            "expires": r["expires_at"]
        }

    for b in BUTTONS:
        state.setdefault(b, {"closed": False, "user": None, "timestamp": None, "expires": None})

    return state

async def set_state(guild_id, name, closed, user, timestamp, expires):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO states (guild_id, name, closed, user_name, timestamp, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (guild_id, name)
        DO UPDATE SET
            closed=EXCLUDED.closed,
            user_name=EXCLUDED.user_name,
            timestamp=EXCLUDED.timestamp,
            expires_at=EXCLUDED.expires_at
        """, guild_id, name, closed, user, timestamp, expires)

# ----------------------------
# SETTINGS
# ----------------------------

async def get_expiry(guild_id, name):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
        SELECT expiry_minutes FROM settings
        WHERE guild_id=$1 AND name=$2
        """, guild_id, name)

    return row["expiry_minutes"] if row else DEFAULT_EXPIRY

async def set_expiry(guild_id, name, minutes):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO settings (guild_id, name, expiry_minutes)
        VALUES ($1,$2,$3)
        ON CONFLICT (guild_id, name)
        DO UPDATE SET expiry_minutes=EXCLUDED.expiry_minutes
        """, guild_id, name, minutes)

# ----------------------------
# LOGS
# ----------------------------

async def log(guild_id, text):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO logs (guild_id, entry)
        VALUES ($1,$2)
        """, guild_id, f"[{now()}] {text}")

# ----------------------------
# EMBED
# ----------------------------

async def build_embed(guild_id):
    state = await get_state(guild_id)

    embed = discord.Embed(
        title="Difficulty Control Panel",
        color=discord.Color.gold()
    )

    text = ""

    for b in BUTTONS:
        info = state[b]
        if info["closed"]:
            text += f"🔴 {b} — {info['user']} — {info['timestamp']}\n"
        else:
            text += f"🟢 {b} — OPEN\n"

    embed.add_field(name="Status", value=text, inline=False)
    return embed

# ----------------------------
# VIEW
# ----------------------------

class ControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for b in BUTTONS:
            self.add_item(ToggleButton(b))
            self.add_item(OverrideButton(b))
        self.add_item(ViewLogs())

# ----------------------------
# BUTTONS
# ----------------------------

class ToggleButton(discord.ui.Button):
    def __init__(self, name):
        super().__init__(label=name, style=discord.ButtonStyle.green)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = await get_state(gid)
        info = state[self.name]

        if not info["closed"]:
            expiry = await get_expiry(gid, self.name)
            expires = (datetime.now() + timedelta(minutes=expiry)).strftime("%Y-%m-%d %H:%M:%S")

            await set_state(gid, self.name, True, interaction.user.display_name, now(), expires)
            await log(gid, f"{interaction.user.display_name} CLOSED {self.name}")

        else:
            if info["user"] != interaction.user.display_name:
                return await interaction.response.send_message("Only locker can reopen.", ephemeral=True)

            await set_state(gid, self.name, False, None, None, None)
            await log(gid, f"{interaction.user.display_name} OPENED {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)

class OverrideButton(discord.ui.Button):
    def __init__(self, name):
        super().__init__(label=f"Override {name}", style=discord.ButtonStyle.blurple)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        await set_state(interaction.guild.id, self.name, False, None, None, None)
        await log(interaction.guild.id, f"{interaction.user.display_name} OVERRIDDEN {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)

class ViewLogs(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Logs", style=discord.ButtonStyle.gray)

    async def callback(self, interaction: discord.Interaction):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT entry FROM logs
            WHERE guild_id=$1
            ORDER BY id DESC LIMIT 20
            """, interaction.guild.id)

        text = "\n".join([r["entry"] for r in rows]) or "No logs"

        await interaction.response.send_message(f"```{text}```", ephemeral=True)

# ----------------------------
# PANEL
# ----------------------------

async def update_panel(guild):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_id, message_id FROM panel WHERE guild_id=$1",
            guild.id
        )

    if not row:
        return

    channel = guild.get_channel(row["channel_id"])
    if not channel:
        return

    try:
        msg = await channel.fetch_message(row["message_id"])
        await msg.edit(embed=await build_embed(guild.id), view=ControlView())
    except:
        msg = await channel.send(embed=await build_embed(guild.id), view=ControlView())

        async with pool.acquire() as conn:
            await conn.execute("""
            INSERT INTO panel (guild_id, channel_id, message_id)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id)
            DO UPDATE SET channel_id=$2, message_id=$3
            """, guild.id, channel.id, msg.id)

# ----------------------------
# SETUP COMMAND
# ----------------------------

@bot.tree.command(name="setup_panel")
async def setup_panel(interaction: discord.Interaction):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO panel (guild_id, channel_id, message_id)
        VALUES ($1,$2,NULL)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id=$2
        """, interaction.guild.id, interaction.channel.id)

    await ensure_state(interaction.guild.id)
    await update_panel(interaction.guild)

    await interaction.response.send_message("Panel created.", ephemeral=True)

# ----------------------------
# READY
# ----------------------------

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# ----------------------------
# RUN
# ----------------------------

bot.run(TOKEN)