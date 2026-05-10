# bot.py — FULL PRODUCTION BUILD (POSTGRES + UI + SETTINGS + LOGS)

import discord
from discord.ext import commands
from discord import app_commands
import os
import psycopg2
from datetime import datetime, timedelta

# ----------------------------
# CONFIG
# ----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

BUTTONS = ["Normal", "Hard", "Treacherous", "Kingslayer"]
DEFAULT_EXPIRY = 60

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------
# DB
# ----------------------------

def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        guild_id BIGINT,
        name TEXT,
        expiry_minutes INT,
        PRIMARY KEY (guild_id, name)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        guild_id BIGINT,
        entry TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS panel (
        guild_id BIGINT PRIMARY KEY,
        channel_id BIGINT,
        message_id BIGINT
    )
    """)

    conn.commit()
    conn.close()

# ----------------------------
# TIME
# ----------------------------

def now():
    return datetime.now().strftime("%B %d, %Y %I:%M %p")

# ----------------------------
# SETTINGS
# ----------------------------

def ensure_settings(guild_id):
    conn = db()
    cur = conn.cursor()

    for b in BUTTONS:
        cur.execute("""
        INSERT INTO settings VALUES (%s,%s,%s)
        ON CONFLICT (guild_id, name)
        DO NOTHING
        """, (guild_id, b, DEFAULT_EXPIRY))

    conn.commit()
    conn.close()


def get_expiry(guild_id, name):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT expiry_minutes FROM settings WHERE guild_id=%s AND name=%s",
                (guild_id, name))
    row = cur.fetchone()

    conn.close()
    return row[0] if row else DEFAULT_EXPIRY


def set_expiry(guild_id, name, minutes):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO settings VALUES (%s,%s,%s)
    ON CONFLICT (guild_id, name)
    DO UPDATE SET expiry_minutes = EXCLUDED.expiry_minutes
    """, (guild_id, name, minutes))

    conn.commit()
    conn.close()

# ----------------------------
# STATE
# ----------------------------

def ensure_state(guild_id):
    conn = db()
    cur = conn.cursor()

    for b in BUTTONS:
        cur.execute("""
        INSERT INTO states VALUES (%s,%s,false,NULL,NULL,NULL)
        ON CONFLICT (guild_id, name)
        DO NOTHING
        """, (guild_id, b))

    conn.commit()
    conn.close()


def get_state(guild_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, closed, user_name, timestamp, expires_at
        FROM states WHERE guild_id=%s
    """, (guild_id,))

    rows = cur.fetchall()
    conn.close()

    return {
        r[0]: {
            "closed": r[1],
            "user": r[2],
            "timestamp": r[3],
            "expires_at": r[4]
        }
        for r in rows
    }


def set_state(guild_id, name, closed, user, timestamp, expires_at):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO states VALUES (%s,%s,%s,%s,%s,%s)
    ON CONFLICT (guild_id, name)
    DO UPDATE SET
        closed = EXCLUDED.closed,
        user_name = EXCLUDED.user_name,
        timestamp = EXCLUDED.timestamp,
        expires_at = EXCLUDED.expires_at
    """, (guild_id, name, closed, user, timestamp, expires_at))

    conn.commit()
    conn.close()

# ----------------------------
# LOGS
# ----------------------------

def log(guild_id, text):
    conn = db()
    cur = conn.cursor()

    cur.execute("INSERT INTO logs VALUES (%s,%s)", (guild_id, f"[{now()}] {text}"))

    conn.commit()
    conn.close()

# ----------------------------
# EMBED
# ----------------------------

def build_embed(guild_id):
    state = get_state(guild_id)

    embed = discord.Embed(title="Difficulty Control Panel", color=discord.Color.gold())

    text = ""

    for b in BUTTONS:
        info = state.get(b)

        if info and info["closed"]:
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
        self.add_item(SettingsButton())

# ----------------------------
# BUTTONS
# ----------------------------

class ToggleButton(discord.ui.Button):
    def __init__(self, name):
        super().__init__(label=name, style=discord.ButtonStyle.green)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = get_state(gid)
        info = state[self.name]
        user = interaction.user.display_name

        ensure_settings(gid)
        minutes = get_expiry(gid, self.name)

        if not info["closed"]:
            expires = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

            set_state(gid, self.name, True, user, now(), expires)
            log(gid, f"{user} CLOSED {self.name}")

        else:
            if info["user"] != user:
                await interaction.response.send_message("Only locker can reopen.", ephemeral=True)
                return

            set_state(gid, self.name, False, None, None, None)
            log(gid, f"{user} OPENED {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)


class OverrideButton(discord.ui.Button):
    def __init__(self, name):
        super().__init__(label=f"Override {name}", style=discord.ButtonStyle.blurple)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = get_state(gid)

        if not state[self.name]["closed"]:
            return await interaction.response.send_message("Already open.", ephemeral=True)

        set_state(gid, self.name, False, None, None, None)
        log(gid, f"{interaction.user.display_name} OVERRIDDEN {self.name}")

        await interaction.response.defer()
        await update_panel(interaction.guild)

# ----------------------------
# LOG VIEW
# ----------------------------

class ViewLogs(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Logs", style=discord.ButtonStyle.gray)

    async def callback(self, interaction: discord.Interaction):
        conn = db()
        cur = conn.cursor()

        cur.execute("""
        SELECT entry FROM logs
        WHERE guild_id=%s
        ORDER BY rowid DESC LIMIT 50
        """, (interaction.guild.id,))

        logs = cur.fetchall()
        conn.close()

        text = "\n".join([l[0] for l in logs]) or "No logs"

        await interaction.response.send_message(f"```{text}```", ephemeral=True)

# ----------------------------
# SETTINGS UI
# ----------------------------

class SettingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Settings", style=discord.ButtonStyle.gray)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=SettingsView(), ephemeral=True)


class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(SettingsSelect())


class SettingsSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=b) for b in BUTTONS]
        super().__init__(placeholder="Choose difficulty", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SettingsModal(self.values[0]))


class SettingsModal(discord.ui.Modal, title="Set Expiry (minutes)"):
    minutes = discord.ui.TextInput(label="Minutes")

    def __init__(self, name):
        super().__init__()
        self.name = name

    async def on_submit(self, interaction: discord.Interaction):
        set_expiry(interaction.guild.id, self.name, int(self.minutes.value))
        log(interaction.guild.id, f"SET {self.name} = {self.minutes.value}m")

        await interaction.response.send_message("Updated.", ephemeral=True)

# ----------------------------
# PANEL SYSTEM
# ----------------------------

async def update_panel(guild):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT channel_id, message_id FROM panel WHERE guild_id=%s",
                (guild.id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return

    channel = guild.get_channel(row[0])
    if not channel:
        return

    try:
        msg = await channel.fetch_message(row[1])
        await msg.edit(embed=build_embed(guild.id), view=ControlView())
    except:
        msg = await channel.send(embed=build_embed(guild.id), view=ControlView())

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO panel VALUES (%s,%s,%s)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id=EXCLUDED.channel_id, message_id=EXCLUDED.message_id
        """, (guild.id, channel.id, msg.id))
        conn.commit()
        conn.close()

# ----------------------------
# SETUP COMMAND
# ----------------------------

@bot.tree.command(name="setup_panel")
async def setup_panel(interaction: discord.Interaction):
    channel = interaction.channel

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO panel VALUES (%s,%s,NULL)
    ON CONFLICT (guild_id)
    DO UPDATE SET channel_id=EXCLUDED.channel_id
    """, (interaction.guild.id, channel.id))
    conn.commit()
    conn.close()

    ensure_state(interaction.guild.id)
    ensure_settings(interaction.guild.id)

    await update_panel(interaction.guild)

    await interaction.response.send_message("Panel created.", ephemeral=True)

# ----------------------------
# READY
# ----------------------------

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()

    print(f"Logged in as {bot.user}")

# ----------------------------
# RUN
# ----------------------------

bot.run(TOKEN)