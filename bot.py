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
# TIME HELPERS
# ----------------------------

def now_utc():
    return datetime.now(timezone.utc)

def to_utc(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def fmt(ts):
    if not ts:
        return None
    ts = to_utc(ts)
    return ts.strftime("%B %d, %Y %I:%M %p")

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
# LOG SYSTEM
# ----------------------------

async def log(gid, text):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO logs (guild_id, entry)
        VALUES ($1,$2)
        """, gid, f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}] {text}")

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
# AUTO EXPIRY
# ----------------------------

@tasks.loop(seconds=15)
async def expiry_loop():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
        SELECT guild_id, name, expires_at
        FROM states
        WHERE closed = TRUE AND expires_at IS NOT NULL
        """)

        now = now_utc()

        for r in rows:
            expires = to_utc(r["expires_at"])

            if expires and expires <= now:
                await conn.execute("""
                UPDATE states
                SET closed = FALSE,
                    user_name = NULL,
                    timestamp = NULL,
                    expires_at = NULL
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
            text += f"🔴 {b} — {info['user']} — {fmt(info['timestamp'])}\n"
        else:
            text += f"🟢 {b} — OPEN\n"

    embed.add_field(name="Status", value=text, inline=False)
    return embed

# ----------------------------
# BUTTON UI (FIXED COLOR LOGIC)
# ----------------------------

class ToggleButton(discord.ui.Button):
    def __init__(self, name, is_closed):
        style = discord.ButtonStyle.red if is_closed else discord.ButtonStyle.green
        super().__init__(label=name, style=style)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = await get_state(gid)
        info = state[self.name]

        if not info["closed"]:
            expiry = now_utc() + timedelta(minutes=DEFAULT_EXPIRY)

            await set_state(
                gid,
                self.name,
                True,
                interaction.user.display_name,
                now_utc().isoformat(),
                expiry.isoformat()
            )

            await log(gid, f"{interaction.user.display_name} CLOSED {self.name}")

        else:
            if info["user"] != interaction.user.display_name:
                return await interaction.response.send_message("Only locker can reopen.", ephemeral=True)

            await set_state(gid, self.name, False, None, None, None)
            await log(gid, f"{interaction.user.display_name} OPENED {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)

# ----------------------------
# VIEW (STATE AWARE)
# ----------------------------

class PanelView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=None)

        for b in BUTTONS:
            self.add_item(ToggleButton(b, state[b]["closed"]))

# ----------------------------
# PANEL UPDATE
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
# SLASH COMMAND
# ----------------------------

@bot.tree.command(name="setup_panel", description="Creates the difficulty panel")
async def setup_panel(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel

    state = await get_state(guild.id)

    msg = await channel.send(
        embed=await build_embed(guild.id),
        view=PanelView(state)
    )

    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO panel (guild_id, channel_id, message_id)
        VALUES ($1,$2,$3)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id=$2, message_id=$3
        """, guild.id, channel.id, msg.id)

    await interaction.response.send_message("Panel created.", ephemeral=True)

# ----------------------------
# LOG UI COMMANDS
# ----------------------------

@bot.tree.command(name="logs", description="View recent logs (20)")
async def logs(interaction: discord.Interaction):
    entries = await fetch_logs(interaction.guild.id, 20)

    if not entries:
        return await interaction.response.send_message("No logs.", ephemeral=True)

    embed = discord.Embed(title="📜 Recent Logs", color=discord.Color.blurple())

    for e in reversed(entries):
        embed.add_field(name="\u200b", value=e, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="all_logs", description="View full logs")
async def all_logs(interaction: discord.Interaction):
    entries = await fetch_all_logs(interaction.guild.id)

    if not entries:
        return await interaction.response.send_message("No logs.", ephemeral=True)

    content = "\n".join(entries)
    chunks = [content[i:i+3500] for i in range(0, len(content), 3500)]

    embed = discord.Embed(title="📜 Full Logs", color=discord.Color.dark_grey())

    for i, chunk in enumerate(chunks[:3]):
        embed.add_field(name=f"Page {i+1}", value=f"```{chunk}```", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clear_logs", description="Clear logs")
async def clear_logs(interaction: discord.Interaction):
    await clear_logs_db(interaction.guild.id)
    await interaction.response.send_message("Logs cleared.", ephemeral=True)


@bot.tree.command(name="download_logs", description="Download logs file")
async def download_logs(interaction: discord.Interaction):
    entries = await fetch_all_logs(interaction.guild.id)

    if not entries:
        return await interaction.response.send_message("No logs.", ephemeral=True)

    content = "\n".join(entries)

    file = discord.File(io.BytesIO(content.encode("utf-8")), filename="logs.txt")

    await interaction.response.send_message(file=file, ephemeral=True)

# ----------------------------
# STARTUP
# ----------------------------

@bot.event
async def setup_hook():
    await init_db()
    await bot.tree.sync()

    if not expiry_loop.is_running():
        expiry_loop.start()

    print(f"Synced {len(bot.tree.get_commands())} commands")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ----------------------------
# RUN
# ----------------------------

bot.run(TOKEN)