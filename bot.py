import discord
from discord.ext import commands, tasks
import asyncpg
import os
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
# LOGS (basic safe version)
# ----------------------------

async def log(gid, text):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO logs (guild_id, entry)
        VALUES ($1,$2)
        """, gid, f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}] {text}")

# ----------------------------
# AUTO EXPIRY LOOP
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
            expires = r["expires_at"]
            if not expires:
                continue

            expires = to_utc(expires)

            if expires <= now:
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
# UI BUTTONS
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
            expiry = now_utc() + timedelta(minutes=DEFAULT_EXPIRY)

            await set_state(
                gid,
                self.name,
                True,
                interaction.user.display_name,
                now_utc().isoformat(),
                expiry.isoformat()
            )

        else:
            if info["user"] != interaction.user.display_name:
                return await interaction.response.send_message(
                    "Only the user who closed it can reopen.",
                    ephemeral=True
                )

            await set_state(gid, self.name, False, None, None, None)

        await interaction.response.defer()
        await update_panel(interaction.guild)

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for b in BUTTONS:
            self.add_item(ToggleButton(b))

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

    msg = await channel.fetch_message(row["message_id"])
    await msg.edit(embed=await build_embed(guild.id), view=PanelView())

# ----------------------------
# SLASH COMMAND
# ----------------------------

@bot.tree.command(name="setup_panel", description="Creates the difficulty panel")
async def setup_panel(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel

    embed = await build_embed(guild.id)
    view = PanelView()

    msg = await channel.send(embed=embed, view=view)

    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO panel (guild_id, channel_id, message_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id=$2, message_id=$3
        """, guild.id, channel.id, msg.id)

    await interaction.response.send_message("Panel created.", ephemeral=True)

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