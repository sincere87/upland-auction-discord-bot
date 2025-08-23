# Fix indentation in the multi-line SQL strings by ensuring they are within a proper triple-quoted Python string.
optimized_code = r"""import os
import re
import sqlite3
import datetime as dt
from contextlib import closing
from collections import defaultdict

import pytz
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================
# Config & Constants
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise EnvironmentError("DISCORD_TOKEN must be set in your .env file.")

DB_PATH = "auctions.db"
UTC = pytz.UTC
TIMESTAMP_REGEX = re.compile(r"<t:(\d+)>")
AUCTION_CHANNEL_IDS = [
    1200435807920591008, 1206719103772139630, 1206719174643023923,
    1213557612826595459, 1394009977378574508, 1394010162108432545,
    1231738039496085535, 1254867397366517841, 1259960721152807005,
    1309896571692912651, 1332469120897122454, 1352104013654528120,
    1377038520061001769
]
CONFIRM_EMOJI_ID = "1365117493919744122"  # adjust if you change your emoji
ROLE_BIDDER_ID = 1315016261293576345
ROLE_COLLECTOR_ID = 1314988994580320266
ROLE_SNIPER_ID = 1315017025764196483

# =========================
# Discord Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.messages = True
intents.reactions = True

# In-memory helpers
outbid_watchers = defaultdict(dict)   # {auction_id: {user_id: True}}
scheduled_messages = set()            # message_ids with alerts scheduled

# =========================
# Database Helpers
# =========================
def db_exec(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(query, params)
        conn.commit()

def db_one(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()

def db_all(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

def init_db():
    db_exec(\"\"\"\
CREATE TABLE IF NOT EXISTS auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id TEXT NOT NULL UNIQUE,
    message_id TEXT,
    channel_id TEXT,
    end_time_utc TEXT,
    status TEXT DEFAULT 'pending',
    created_at_utc TEXT DEFAULT (datetime('now'))
);
\"\"\")
    db_exec(\"\"\"\
CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    bid_time_utc TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (auction_id) REFERENCES auctions(auction_id)
);
\"\"\")
    db_exec(\"CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);\")
    db_exec(\"CREATE INDEX IF NOT EXISTS idx_bids_time ON bids(bid_time_utc);\")
    db_exec(\"CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status);\")

# =========================
# Utilities
# =========================
def iso_utc(dt_obj: dt.datetime) -> str:
    if dt_obj.tzinfo is None:
        dt_obj = pytz.UTC.localize(dt_obj)
    return dt_obj.astimezone(pytz.UTC).isoformat()

def parse_amount(text: str) -> int:
    text = text.lower().replace(\",\", \"\").replace(\"upx\", \"\").replace(\"$\", \"\")
    m = re.search(r\"(\\d+)(k)?\", text)
    if not m:
        raise ValueError(\"no amount\")
    n = int(m.group(1))
    return n * 1000 if m.group(2) == \"k\" else n

def get_auction(auction_id: str):
    return db_one(\"SELECT * FROM auctions WHERE auction_id = ?\", (auction_id,))

def upsert_pending(auction_id: str, message_id: str, channel_id: str, end_time_utc: str):
    existing = get_auction(auction_id)
    if not existing:
        db_exec(
            \"INSERT INTO auctions (auction_id, message_id, channel_id, end_time_utc, status) VALUES (?, ?, ?, ?, 'pending')\",
            (auction_id, message_id, channel_id, end_time_utc),
        )
    elif not existing[\"end_time_utc\"]:
        db_exec(\"UPDATE auctions SET end_time_utc=? WHERE auction_id=?\", (end_time_utc, auction_id))

def set_status(auction_id: str, status: str):
    db_exec(\"UPDATE auctions SET status=? WHERE auction_id=?\", (status, auction_id))

def record_bid(auction_id: str, user_id: int, amount: int, when: dt.datetime | None = None):
    when = when or dt.datetime.now(pytz.UTC)
    db_exec(
        \"INSERT INTO bids (auction_id, user_id, amount, bid_time_utc) VALUES (?, ?, ?, ?)\",
        (auction_id, str(user_id), amount, iso_utc(when)),
    )

def best_bid_now(auction_id: str):
    return db_one(
        \"SELECT user_id, amount, bid_time_utc FROM bids WHERE auction_id=? ORDER BY amount DESC, datetime(bid_time_utc) ASC LIMIT 1\",
        (auction_id,),
    )

def best_bid_before_end(auction_id: str):
    return db_one(
        \"\"\"\
SELECT b.user_id, b.amount, b.bid_time_utc
FROM bids b JOIN auctions a ON a.auction_id=b.auction_id
WHERE b.auction_id=? AND datetime(b.bid_time_utc) <= datetime(a.end_time_utc)
ORDER BY b.amount DESC, datetime(b.bid_time_utc) ASC LIMIT 1
\"\"\",
        (auction_id,),
    )

# =========================
# Auction Manager (runtime cache)
# =========================
class AuctionManager:
    \"\"\"Caches active auctions per channel for fast lookups and consistent IDs.\"\"\"
    def __init__(self):
        self.active_by_channel: dict[str, str] = {}

    def activate(self, channel_id: str | int, auction_id: str | int):
        self.active_by_channel[str(channel_id)] = str(auction_id)

    def deactivate(self, channel_id: str | int):
        self.active_by_channel.pop(str(channel_id), None)

    def get_active_for_channel(self, channel_id: str | int) -> str | None:
        a = self.active_by_channel.get(str(channel_id))
        if a:
            return a
        row = db_one(
            \"SELECT auction_id FROM auctions WHERE channel_id=? AND status='active' ORDER BY datetime(created_at_utc) DESC LIMIT 1\",
            (str(channel_id),),
        )
        if row:
            self.activate(channel_id, row[\"auction_id\"])
            return row[\"auction_id\"]
        return None

auction_mgr = AuctionManager()

# =========================
# Bot
# =========================
class AuctionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self.scheduler = AsyncIOScheduler()
        self.reminders = {}

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()

bot = AuctionBot()

# =========================
# Alerts
# =========================
ROLE_BIDDER_ID = 1315016261293576345
ROLE_COLLECTOR_ID = 1314988994580320266
ROLE_SNIPER_ID = 1315017025764196483

async def send_halfway_alert(channel_id, message_id):
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return
    bidder_role = channel.guild.get_role(ROLE_BIDDER_ID)
    collector_role = channel.guild.get_role(ROLE_COLLECTOR_ID)
    msg = await channel.fetch_message(int(message_id))
    await channel.send(f\"⏳ {bidder_role.mention if bidder_role else ''} {collector_role.mention if collector_role else ''} — This auction is at **halftime**!\\n{msg.jump_url}\")

async def send_one_hour_alert(channel_id, message_id):
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return
    sniper_role = channel.guild.get_role(ROLE_SNIPER_ID)
    msg = await channel.fetch_message(int(message_id))
    await channel.send(f\"🎯 {sniper_role.mention if sniper_role else ''} — **1 hour remaining**! Final bids incoming!\\n{msg.jump_url}\")

# =========================
# Core Actions
# =========================
async def confirm_bid(bidder: discord.Member, amount: int, auction_id: str, channel: discord.TextChannel | None = None, interaction: discord.Interaction | None = None):
    auction = get_auction(auction_id)
    if not auction:
        text = f\"⚠️ Auction `{auction_id}` is not registered. Use `/track_auction {auction_id}` to activate.\"
        if interaction and not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True)
        elif channel:
            await channel.send(text)
        return

    current = best_bid_now(auction_id)
    if current and amount <= current[\"amount\"]:
        msg = f\"⚠️ Bid must be higher than the current bid ({current['amount']:,}).\"
        if interaction and not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        elif channel:
            await channel.send(msg)
        return

    record_bid(auction_id, bidder.id, amount)
    prev_bidder_id = current[\"user_id\"] if current else None

    if prev_bidder_id and int(prev_bidder_id) in outbid_watchers[auction_id]:
        try:
            prev_user = await bot.fetch_user(int(prev_bidder_id))
            await prev_user.send(f\"You’ve been outbid in auction `{auction_id}`.\\nNew high bid: {amount:,} by {bidder.display_name}.\")
        except discord.Forbidden:
            pass
        outbid_watchers[auction_id].pop(int(prev_bidder_id), None)

    ack = f\"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`.\"
    if interaction and not interaction.response.is_done():
        await interaction.response.send_message(ack, ephemeral=True)
    elif channel:
        await channel.send(ack)

# =========================
# Commands
# =========================
@bot.tree.command(name=\"notify_outbid\", description=\"DM you if you're outbid on an auction.\")
@app_commands.describe(auction_id=\"Auction ID to watch (the listing message ID).\"
)
async def notify_outbid_cmd(interaction: discord.Interaction, auction_id: str):
    outbid_watchers[auction_id][interaction.user.id] = True
    await interaction.response.send_message(f\"🔔 You'll be DMed if you're outbid in `{auction_id}`.\", ephemeral=True)

@bot.tree.command(name=\"cb\", description=\"Confirm a bid on the active auction in this channel.\")
@app_commands.describe(bidder=\"User placing the bid\", amount=\"Bid amount\", auction_id=\"Auction ID (optional).\"
)
async def cb_cmd(interaction: discord.Interaction, bidder: discord.Member, amount: int, auction_id: str | None = None):
    auction_id = auction_id or auction_mgr.get_active_for_channel(interaction.channel_id)
    if not auction_id:
        await interaction.response.send_message(\"⚠️ No active auction found for this channel. Use `/track_auction <message_id>` first.\", ephemeral=True)
        return
    await confirm_bid(bidder, amount, auction_id, interaction=interaction)

@bot.tree.command(name=\"set_reminder\", description=\"DM reminder for an auction\")
async def set_reminder_cmd(interaction: discord.Interaction, auction_id: str, hours: int = 0, minutes: int = 0):
    if hours == 0 and minutes == 0:
        await interaction.response.send_message(\"⏳ Provide at least hours or minutes.\", ephemeral=True)
        return
    run_time = dt.datetime.utcnow() + dt.timedelta(hours=hours, minutes=minutes)
    job_id = f\"{interaction.user.id}_{auction_id}_{hours}h{minutes}m_{dt.datetime.utcnow().timestamp()}\"
    bot.reminders[job_id] = {\"auction_id\": auction_id, \"user_id\": interaction.user.id}
    bot.scheduler.add_job(send_reminder_dm, trigger=\"date\", run_date=run_time, args=[interaction.user.id, auction_id], id=job_id)
    await interaction.response.send_message(f\"✅ Reminder set for `{auction_id}` in {hours}h {minutes}m.\", ephemeral=True)

async def send_reminder_dm(user_id, auction_id):
    user = await bot.fetch_user(user_id)
    if user:
        await user.send(f\"⏰ Reminder: Auction '{auction_id}' is coming to a close soon!\")

@bot.tree.command(name=\"track_auction\", description=\"Activate a detected auction by message_id\")
@app_commands.describe(message_id=\"The message ID of the auction post (<t:UNIX> inside).\"
)
async def track_cmd(interaction: discord.Interaction, message_id: str):
    target_msg = None
    for ch_id in AUCTION_CHANNEL_IDS:
        ch = interaction.client.get_channel(ch_id)
        if not ch:
            continue
        try:
            m = await ch.fetch_message(int(message_id))
            if m:
                target_msg = m
                break
        except Exception:
            continue
    if not target_msg:
        await interaction.response.send_message(\"❌ Message not found in auction channels.\", ephemeral=True)
        return

    match = TIMESTAMP_REGEX.search(target_msg.content)
    if not match:
        await interaction.response.send_message(\"❌ No `<t:UNIX>` timestamp found in that message.\", ephemeral=True)
        return

    unix_time = int(match.group(1))
    end_time = dt.datetime.fromtimestamp(unix_time, tz=pytz.UTC)

    upsert_pending(str(target_msg.id), str(target_msg.id), str(target_msg.channel.id), iso_utc(end_time))
    set_status(str(target_msg.id), \"active\")
    auction_mgr.activate(target_msg.channel.id, target_msg.id)

    now = dt.datetime.now(pytz.UTC)
    if end_time > now + dt.timedelta(hours=1):
        scheduled_messages.add(target_msg.id)
        half_when = now + (end_time - now) / 2
        one_hour_when = end_time - dt.timedelta(hours=1)
        bot.scheduler.add_job(send_halfway_alert, \"date\", run_date=half_when, args=[target_msg.channel.id, target_msg.id])
        bot.scheduler.add_job(send_one_hour_alert, \"date\", run_date=one_hour_when, args=[target_msg.channel.id, target_msg.id])

    await interaction.response.send_message(
        f\"✅ Auction activated.\\n• Auction ID: `{target_msg.id}`\\n• Ends: <t:{unix_time}:F> (<t:{unix_time}:R>)\",
        ephemeral=True
    )

@bot.tree.command(name=\"final_bid\", description=\"Get the last valid bid at/before auction end\")
@app_commands.describe(auction_id=\"Auction ID (listing message_id).\"
)
async def final_bid_cmd(interaction: discord.Interaction, auction_id: str):
    a = get_auction(auction_id)
    if not a:
        await interaction.response.send_message(f\"❌ Auction `{auction_id}` not found.\", ephemeral=True)
        return
    row = best_bid_before_end(auction_id)
    if not row:
        await interaction.response.send_message(f\"No valid bids found for `{auction_id}`.\", ephemeral=True)
        return
    jump = f\"https://discord.com/channels/{interaction.guild_id}/{a['channel_id']}/{a['message_id']}\" if a[\"channel_id\"] and a[\"message_id\"] else \"\"
    await interaction.response.send_message(f\"🏁 Final bid for `{auction_id}`: **{row['amount']:,}** by <@{row['user_id']}> (at {row['bid_time_utc']} UTC)\\n{jump}\", ephemeral=True)

@bot.tree.command(name=\"auction_info\", description=\"Show stored info for an auction\")
@app_commands.describe(auction_id=\"Auction ID (message_id)\"
)
async def auction_info_cmd(interaction: discord.Interaction, auction_id: str):
    a = get_auction(auction_id)
    if not a:
        await interaction.response.send_message(\"❌ Not found.\", ephemeral=True); return
    jump = f\"https://discord.com/channels/{interaction.guild_id}/{a['channel_id']}/{a['message_id']}\" if a[\"channel_id\"] and a[\"message_id\"] else \"\"
    await interaction.response.send_message(
        f\"🗂 **Auction** `{auction_id}`\\n• Status: `{a['status']}`\\n• Ends (UTC): `{a['end_time_utc']}`\\n{jump}\",
        ephemeral=True
    )

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    init_db()
    print(f\"✅ Logged in as {bot.user} (ID: {bot.user.id})\")

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot or message.channel.id not in AUCTION_CHANNEL_IDS or message.id in scheduled_messages:
        return

    m = TIMESTAMP_REGEX.search(message.content)
    if not m:
        return

    unix_time = int(m.group(1))
    end_time = dt.datetime.fromtimestamp(unix_time, tz=pytz.UTC)
    upsert_pending(str(message.id), str(message.id), str(message.channel.id), iso_utc(end_time))

    await message.channel.send(f\"🛎 Potential auction detected for message `{message.id}` (ends <t:{unix_time}:R>). Confirm with `/track_auction {message.id}`.\")

    now = dt.datetime.now(pytz.UTC)
    if end_time <= now + dt.timedelta(hours=1):
        return
    half_when = now + (end_time - now) / 2
    one_hour_when = end_time - dt.timedelta(hours=1)
    scheduled_messages.add(message.id)
    bot.scheduler.add_job(send_halfway_alert, \"date\", run_date=half_when, args=[message.channel.id, message.id])
    bot.scheduler.add_job(send_one_hour_alert, \"date\", run_date=one_hour_when, args=[message.channel.id, message.id])

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(getattr(payload.emoji, \"id\", None)) != CONFIRM_EMOJI_ID:
        return
    if payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    channel = guild.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    bidder = message.author
    try:
        amount = parse_amount(message.content)
    except Exception:
        await channel.send(f\"⚠️ Couldn’t detect a valid bid in {bidder.mention}’s message.\")
        return

    auction_id = auction_mgr.get_active_for_channel(channel.id)
    if not auction_id:
        await channel.send(\"⚠️ No active auction found for this channel. Please use `/track_auction <message_id>` first.\")
        return

    await confirm_bid(bidder, amount, str(auction_id), channel=channel)

# =========================
# Run
# =========================
if __name__ == '__main__':
    from keep_alive import keep_alive
    init_db()
    keep_alive()
    bot.run(TOKEN)
"""
