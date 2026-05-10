# bot.py — FULL RESTORED asyncpg VERSION (FIXED FEATURES)

import discord
from discord.ext import commands, tasks
import asyncpg
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
# TIME (FIXED - UTC SAFE)
# ----------------------------

def now_utc():
    return datetime.now(timezone.utc)

def format_local(ts: datetime, tz="America/Chicago"):
    if ts is None:
        return None
    return ts.astimezone(ZoneInfo(tz)).strftime("%B %d, %Y %I:%M %p")

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
            closed BOOLEAN,
            user_name TEXT,
            timestamp TEXT,
            expires_at TIMESTAMP,
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
# STATE
# ----------------------------

async def get_state(gid):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, closed, user_name, timestamp, expires_at FROM states WHERE guild_id=$1",
            gid
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

async def set_state(gid, name, closed, user, ts, expires):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO states VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (guild_id, name)
        DO UPDATE SET
            closed=EXCLUDED.closed,
            user_name=EXCLUDED.user_name,
            timestamp=EXCLUDED.timestamp,
            expires_at=EXCLUDED.expires_at
        """, gid, name, closed, user, ts, expires)

# ----------------------------
# SETTINGS
# ----------------------------

async def get_expiry(gid, name):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
        SELECT expiry_minutes FROM settings
        WHERE guild_id=$1 AND name=$2
        """, gid, name)

    return row["expiry_minutes"] if row else DEFAULT_EXPIRY

async def set_expiry(gid, name, minutes):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO settings VALUES ($1,$2,$3)
        ON CONFLICT (guild_id, name)
        DO UPDATE SET expiry_minutes=EXCLUDED.expiry_minutes
        """, gid, name, minutes)

# ----------------------------
# LOGS
# ----------------------------

async def log(gid, text):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO logs (guild_id, entry)
        VALUES ($1,$2)
        """, gid, f"[{now_utc().isoformat()}] {text}")

# ----------------------------
# AUTO EXPIRE ENGINE (FIXED MISSING FEATURE)
# ----------------------------

@tasks.loop(seconds=30)
async def expiry_loop():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
        SELECT guild_id, name, expires_at, closed
        FROM states
        WHERE closed = TRUE AND expires_at IS NOT NULL
        """)

        for r in rows:
            if datetime.fromisoformat(str(r["expires_at"])) <= now_utc():
                await conn.execute("""
                UPDATE states
                SET closed=FALSE, user_name=NULL, timestamp=NULL, expires_at=NULL
                WHERE guild_id=$1 AND name=$2
                """, r["guild_id"], r["name"])

                await log(r["guild_id"], f"AUTO-EXPIRED {r['name']}")

# ----------------------------
# EMBED
# ----------------------------

async def build_embed(gid):
    state = await get_state(gid)

    embed = discord.Embed(title="Difficulty Panel", color=discord.Color.gold())

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
# BUTTON VIEW (FIXED COLORS)
# ----------------------------

class ToggleButton(discord.ui.Button):
    def __init__(self, name, closed=False):
        style = discord.ButtonStyle.red if closed else discord.ButtonStyle.green
        super().__init__(label=name, style=style)
        self.name = name

    async def callback(self, interaction):
        gid = interaction.guild.id
        state = await get_state(gid)
        info = state[self.name]

        if not info["closed"]:
            expiry = await get_expiry(gid, self.name)
            expires = now_utc() + timedelta(minutes=expiry)

            await set_state(gid, self.name, True, interaction.user.display_name, now_utc().isoformat(), expires.isoformat())
            await log(gid, f"{interaction.user.display_name} CLOSED {self.name}")

        else:
            if info["user"] != interaction.user.display_name:
                return await interaction.response.send_message("Only locker can reopen.", ephemeral=True)

            await set_state(gid, self.name, False, None, None, None)
            await log(gid, f"{interaction.user.display_name} OPENED {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)

# ----------------------------
# LOG VIEW / CLEAR / DOWNLOAD (FIXED)
# ----------------------------

class LogsView(discord.ui.View):
    @discord.ui.button(label="View Logs", style=discord.ButtonStyle.gray)
    async def view(self, interaction, button):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT entry FROM logs
            WHERE guild_id=$1
            ORDER BY id DESC LIMIT 20
            """, interaction.guild.id)

        text = "\n".join([r["entry"] for r in rows]) or "No logs"
        await interaction.response.send_message(f"```{text}```", ephemeral=True)

    @discord.ui.button(label="Clear Logs", style=discord.ButtonStyle.red)
    async def clear(self, interaction, button):
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM logs WHERE guild_id=$1", interaction.guild.id)

        await interaction.response.send_message("Logs cleared.", ephemeral=True)

    @discord.ui.button(label="Download Logs", style=discord.ButtonStyle.blurple)
    async def download(self, interaction, button):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT entry FROM logs
            WHERE guild_id=$1
            ORDER BY id ASC
            """, interaction.guild.id)

        text = "\n".join([r["entry"] for r in rows]) or "No logs"
        file = discord.File(fp=bytes(text, "utf-8"), filename="logs.txt")

        await interaction.response.send_message(file=file, ephemeral=True)

# ----------------------------
# PANEL UPDATE
# ----------------------------

async def update_panel(guild):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT channel_id, message_id FROM panel WHERE guild_id=$1", guild.id)

    if not row:
        return

    channel = guild.get_channel(row["channel_id"])
    if not channel:
        return

    msg = await channel.fetch_message(row["message_id"])
    await msg.edit(embed=await build_embed(guild.id), view=discord.ui.View())

# ----------------------------
# READY
# ----------------------------

@bot.event
async def on_ready():
    await init_db()
    expiry_loop.start()
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# ----------------------------
# RUN
# ----------------------------

bot.run(TOKEN)