import discord
from discord.ext import commands, tasks
import asyncpg
import os
import io
from datetime import datetime, timedelta, timezone

# ----------------------------
# CONFIG
# ----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BUTTONS = ["Normal", "Hard", "Treacherous", "Kingslayer"]
DEFAULT_EXPIRY = 60

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

pool = None

# ----------------------------
# TIME
# ----------------------------

def now_utc():
    return datetime.now(timezone.utc)

def fmt(ts):
    if not ts:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.strftime("%Y-%m-%d %H:%M UTC")

# ----------------------------
# DB
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
            expires_at TEXT,
            PRIMARY KEY (guild_id, name)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS panel (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT,
            message_id BIGINT
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            entry TEXT
        )
        """)

# ----------------------------
# LOGS
# ----------------------------

async def log(gid, text):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO logs (guild_id, entry) VALUES ($1,$2)",
            gid,
            f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {text}"
        )

async def fetch_logs(gid, limit=20):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
        SELECT entry FROM logs
        WHERE guild_id=$1
        ORDER BY id DESC
        LIMIT $2
        """, gid, limit)
    return [r["entry"] for r in rows]

async def fetch_all_logs(gid):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
        SELECT entry FROM logs
        WHERE guild_id=$1
        ORDER BY id ASC
        """, gid)
    return [r["entry"] for r in rows]

async def clear_logs_db(gid):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM logs WHERE guild_id=$1", gid)

# ----------------------------
# STATE
# ----------------------------

async def get_state(gid):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, closed, user_name, timestamp, expires_at FROM states WHERE guild_id=$1",
            gid
        )

    state = {b: {"closed": False, "user": None, "timestamp": None} for b in BUTTONS}

    for r in rows:
        state[r["name"]] = {
            "closed": r["closed"],
            "user": r["user_name"],
            "timestamp": r["timestamp"]
        }

    return state

async def set_state(gid, name, closed, user, ts, expires):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO states VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (guild_id, name)
        DO UPDATE SET closed=$3, user_name=$4, timestamp=$5, expires_at=$6
        """, gid, name, closed, user, ts, expires)

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
            text += f"🔴 {b} — {info['user']} — {fmt(info['timestamp'])}\n"
        else:
            text += f"🟢 {b} — OPEN\n"

    logs = await fetch_logs(gid, 10)
    log_text = "\n".join(reversed(logs)) if logs else "No logs yet."

    embed.add_field(name="Status", value=text, inline=False)
    embed.add_field(name="Recent Logs", value=f"```{log_text}```", inline=False)

    return embed

# ----------------------------
# VIEW (LIVE REFRESH)
# ----------------------------

class PanelView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=None)

        for b in BUTTONS:
            self.add_item(ToggleButton(b, state[b]["closed"]))

        self.add_item(ViewLogs())
        self.add_item(ClearLogs())
        self.add_item(DownloadLogs())

# ----------------------------
# BUTTON
# ----------------------------

class ToggleButton(discord.ui.Button):
    def __init__(self, name, closed):
        super().__init__(
            label=name,
            style=discord.ButtonStyle.red if closed else discord.ButtonStyle.green
        )
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()  # instant response fix

        gid = interaction.guild.id
        state = await get_state(gid)
        info = state[self.name]

        if not info["closed"]:
            await set_state(
                gid,
                self.name,
                True,
                interaction.user.display_name,
                now_utc().isoformat(),
                None
            )
            await log(gid, f"{interaction.user.display_name} CLOSED {self.name}")

        else:
            if info["user"] != interaction.user.display_name:
                return await interaction.followup.send("Only locker can reopen.", ephemeral=True)

            await set_state(gid, self.name, False, None, None, None)
            await log(gid, f"{interaction.user.display_name} OPENED {self.name}")

        await update_panel(interaction.guild)

# ----------------------------
# LOG BUTTONS
# ----------------------------

class ViewLogs(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View Logs", style=discord.ButtonStyle.gray)

    async def callback(self, interaction):
        logs = await fetch_all_logs(interaction.guild.id)
        await interaction.response.send_message("```" + "\n".join(logs[-1000:]) + "```", ephemeral=True)

class ClearLogs(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear Logs", style=discord.ButtonStyle.red)

    async def callback(self, interaction):
        await clear_logs_db(interaction.guild.id)
        await log(interaction.guild.id, f"{interaction.user.display_name} CLEARED LOGS")
        await interaction.response.send_message("Cleared.", ephemeral=True)

class DownloadLogs(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Download Logs", style=discord.ButtonStyle.blurple)

    async def callback(self, interaction):
        logs = await fetch_all_logs(interaction.guild.id)
        file = discord.File(io.BytesIO("\n".join(logs).encode()), "logs.txt")
        await interaction.response.send_message(file=file, ephemeral=True)

# ----------------------------
# PANEL SYNC (REAL-TIME CORE)
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

    state = await get_state(guild.id)

    msg = await channel.fetch_message(row["message_id"])
    await msg.edit(embed=await build_embed(guild.id), view=PanelView(state))

# ----------------------------
# COMMAND
# ----------------------------

@bot.tree.command(name="setup_panel")
async def setup_panel(interaction: discord.Interaction):
    state = await get_state(interaction.guild.id)

    msg = await interaction.channel.send(
        embed=await build_embed(interaction.guild.id),
        view=PanelView(state)
    )

    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO panel VALUES ($1,$2,$3)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id=$2, message_id=$3
        """, interaction.guild.id, interaction.channel.id, msg.id)

    await interaction.response.send_message("Panel created.", ephemeral=True)

# ----------------------------
# STARTUP
# ----------------------------

@bot.event
async def setup_hook():
    await init_db()
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)