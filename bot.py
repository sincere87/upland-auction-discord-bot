import os
import datetime
import re
import pytz
import sqlite3
from contextlib import closing
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import defaultdict

# =========================
# Load environment variables
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise EnvironmentError("DISCORD_TOKEN must be set in your .env file.")

# =========================
# Constants / Config
# =========================
AUCTION_CHANNEL_IDS = [
    1200435807920591008, 1206719103772139630, 1206719174643023923,
    1213557612826595459, 1394009977378574508, 1394010162108432545,
    1231738039496085535, 1254867397366517841, 1259960721152807005,
    1309896571692912651, 1332469120897122454, 1352104013654528120,
    1377038520061001769
]

# Example role IDs (replace with your actual)
ROLE_BIDDER_ID = 123456789012345678
ROLE_COLLECTOR_ID = 223456789012345678
ROLE_SNIPER_ID   = 323456789012345678

# Regex to find <t:UNIX> timestamps
TIMESTAMP_REGEX = re.compile(r"<t:(\d+)>")

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.messages = True
intents.reactions = True

# Track in-memory (kept for fast reads; DB is the source of truth)
current_auctions = defaultdict(dict)
outbid_watchers = defaultdict(dict)
scheduled_messages = set()

DB_PATH = "auctions.db"
UTC = pytz.UTC


# =========================
# Bot class
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
# DB Utilities
# =========================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS auctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id TEXT NOT NULL UNIQUE,
            message_id TEXT,
            channel_id TEXT,
            end_time_utc TEXT,      -- ISO 8601 UTC string
            status TEXT DEFAULT 'pending', -- pending | active | ended | canceled
            created_at_utc TEXT DEFAULT (datetime('now'))
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            bid_time_utc TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (auction_id) REFERENCES auctions(auction_id)
        );
        """)
        # Helpful indexes
        c.execute("CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bids_time ON bids(bid_time_utc);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status);")
        conn.commit()


def db_execute(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()


def db_query_one(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchone()


def db_query_all(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchall()


# =========================
# Helper functions
# =========================
def iso_utc(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return dt.astimezone(UTC).isoformat()


def parse_amount(text: str) -> int:
    """
    Parse bid amounts inside free-form text.
    Handles "5k", "I'll go 10k", "new bid 7,500", "$12", "12 upx", etc.
    """
    text = text.lower().replace(",", "").replace("upx", "").replace("$", "")
    match = re.search(r"(\d+)(k)?", text)
    if not match:
        raise ValueError(f"Could not parse amount from: {text}")
    amount = int(match.group(1))
    if match.group(2) == "k":
        amount *= 1000
    return amount


def get_auction(auction_id: str):
    return db_query_one("SELECT * FROM auctions WHERE auction_id = ?", (auction_id,))


def upsert_pending_auction(auction_id: str, message_id: str, channel_id: str, end_time_utc: str):
    # Insert if not exists; if exists, keep earliest data but ensure end_time/status updated if missing
    existing = get_auction(auction_id)
    if existing is None:
        db_execute("""
            INSERT INTO auctions (auction_id, message_id, channel_id, end_time_utc, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (auction_id, message_id, channel_id, end_time_utc))
    else:
        # Only update end_time if not set; keep status as is
        if not existing["end_time_utc"]:
            db_execute("UPDATE auctions SET end_time_utc = ? WHERE auction_id = ?",
                       (end_time_utc, auction_id))


def set_auction_active(auction_id: str):
    db_execute("UPDATE auctions SET status = 'active' WHERE auction_id = ?", (auction_id,))


def set_auction_ended(auction_id: str):
    db_execute("UPDATE auctions SET status = 'ended' WHERE auction_id = ?", (auction_id,))


def record_bid(auction_id: str, user_id: int, amount: int, when_utc: datetime.datetime | None = None):
    if when_utc is None:
        when_utc = datetime.datetime.now(UTC)
    db_execute("""
        INSERT INTO bids (auction_id, user_id, amount, bid_time_utc)
        VALUES (?, ?, ?, ?)
    """, (auction_id, str(user_id), amount, iso_utc(when_utc)))


def get_highest_bid_before_end(auction_id: str):
    return db_query_one("""
        SELECT b.user_id, b.amount, b.bid_time_utc
        FROM bids b
        JOIN auctions a ON a.auction_id = b.auction_id
        WHERE b.auction_id = ?
          AND datetime(b.bid_time_utc) <= datetime(a.end_time_utc)
        ORDER BY b.amount DESC, datetime(b.bid_time_utc) ASC
        LIMIT 1
    """, (auction_id,))


def get_current_highest_now(auction_id: str):
    return db_query_one("""
        SELECT user_id, amount, bid_time_utc
        FROM bids
        WHERE auction_id = ?
        ORDER BY amount DESC, datetime(bid_time_utc) ASC
        LIMIT 1
    """, (auction_id,))


async def confirm_bid(
    bidder: discord.Member,
    amount: int,
    auction_id: str,
    channel: discord.TextChannel = None,
    interaction: discord.Interaction = None
):
    # Validate auction existence
    auction = get_auction(auction_id)
    if auction is None:
        # Allow on-the-fly auctions using channel.id as auction_id, but warn
        if channel:
            await channel.send(f"⚠️ Auction `{auction_id}` is not registered. Use `/track_auction {auction_id}` to activate.")
        if interaction:
            await interaction.response.send_message(
                f"⚠️ Auction `{auction_id}` is not registered. Use `/track_auction {auction_id}` to activate.",
                ephemeral=True
            )
        return

    # Enforce higher-than-current bid
    current = get_current_highest_now(auction_id)
    if current and amount <= current["amount"]:
        msg = f"⚠️ Bid must be higher than the current bid ({current['amount']:,})."
        if interaction:
            await interaction.response.send_message(msg, ephemeral=True)
        elif channel:
            await channel.send(msg)
        return

    # Persist bid
    record_bid(auction_id, bidder.id, amount)

    # Update in-memory snapshot (optional)
    prev_bidder_id = current["user_id"] if current else None
    current_auctions[auction_id] = {"highest_bidder": bidder, "amount": amount}

    # Notify outbid watchers
    if prev_bidder_id and int(prev_bidder_id) in outbid_watchers[auction_id]:
        try:
            prev_user = await bot.fetch_user(int(prev_bidder_id))
            await prev_user.send(
                f"You’ve been outbid in auction `{auction_id}`.\nNew high bid: {amount:,} by {bidder.display_name}."
            )
        except discord.Forbidden:
            print(f"⚠️ Couldn't DM user {prev_bidder_id}")
        del outbid_watchers[auction_id][int(prev_bidder_id)]

    # Acknowledge
    if interaction:
        await interaction.response.send_message(
            f"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`.",
            ephemeral=True
        )
    elif channel:
        await channel.send(f"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`.")


# =========================
# Slash Commands
# =========================

@bot.tree.command(name="notify_outbid", description="Get notified via DM if you're outbid.")
@app_commands.describe(auction_id="The ID of the auction to watch.")
async def notify_outbid(interaction: discord.Interaction, auction_id: str):
    user_id = interaction.user.id
    outbid_watchers[auction_id][user_id] = True
    await interaction.response.send_message(
        f"You'll be notified via DM if you're outbid in auction `{auction_id}`.",
        ephemeral=True
    )


@bot.tree.command(name="cb", description="Confirm a bid and notify any outbid watchers.")
@app_commands.describe(bidder="User placing the bid", amount="Bid amount", auction_id="Auction ID (channel id or custom)")
async def cb(interaction: discord.Interaction, bidder: discord.Member, amount: int, auction_id: str):
    await confirm_bid(bidder, amount, auction_id, interaction=interaction)


@bot.tree.command(name="set_reminder", description="Set a DM reminder for an auction listing")
async def set_reminder(
    interaction: discord.Interaction,
    auction_id: str,
    hours: int = 0,
    minutes: int = 0
):
    user_id = interaction.user.id

    if hours == 0 and minutes == 0:
        await interaction.response.send_message(
            "⏳ Please provide at least hours or minutes for the reminder.",
            ephemeral=True
        )
        return

    total_delay = datetime.timedelta(hours=hours, minutes=minutes)
    run_time = datetime.datetime.utcnow() + total_delay

    job_id = f"{user_id}_{auction_id}_{hours}h{minutes}m_{datetime.datetime.utcnow().timestamp()}"
    bot.reminders[job_id] = {"auction_id": auction_id, "user_id": user_id}

    bot.scheduler.add_job(
        send_reminder_dm,
        trigger='date',
        run_date=run_time,
        args=[user_id, auction_id],
        id=job_id
    )

    await interaction.response.send_message(
        f"✅ Reminder set for auction '{auction_id}' in {hours}h {minutes}m. You will receive a DM.",
        ephemeral=True
    )


async def send_reminder_dm(user_id, auction_id):
    user = await bot.fetch_user(user_id)
    if user:
        await user.send(f"Reminder: Auction '{auction_id}' is coming to a close soon!")


# -------- Hybrid tracking commands --------

@bot.tree.command(name="track_auction", description="Confirm & activate a detected auction by message_id")
@app_commands.describe(message_id="The message ID of the auction post (the one that has <t:UNIX>)")
async def track_auction(interaction: discord.Interaction, message_id: str):
    # Use message_id also as auction_id for simplicity (you can switch to channel.id if you prefer)
    # Fetch the message to extract channel and end time if needed
    try:
        # Search all known channels (only auction channels for efficiency)
        target_msg = None
        for ch_id in AUCTION_CHANNEL_IDS:
            ch = interaction.client.get_channel(ch_id)
            if ch:
                try:
                    m = await ch.fetch_message(int(message_id))
                    if m:
                        target_msg = m
                        break
                except Exception:
                    continue
        if not target_msg:
            await interaction.response.send_message("❌ Message not found in auction channels.", ephemeral=True)
            return

        # Find <t:UNIX>
        match = TIMESTAMP_REGEX.search(target_msg.content)
        if not match:
            await interaction.response.send_message("❌ No `<t:UNIX>` timestamp found in that message.", ephemeral=True)
            return

        unix_time = int(match.group(1))
        end_time = datetime.datetime.fromtimestamp(unix_time, tz=UTC)

        # Upsert then activate
        upsert_pending_auction(
            auction_id=str(target_msg.id),
            message_id=str(target_msg.id),
            channel_id=str(target_msg.channel.id),
            end_time_utc=iso_utc(end_time)
        )
        set_auction_active(str(target_msg.id))

        # Schedule alerts (same logic as your on_message)
        now = datetime.datetime.now(UTC)
        if end_time > now + datetime.timedelta(hours=1):
            time_remaining = end_time - now
            halfway_time = now + time_remaining / 2
            one_hour_before = end_time - datetime.timedelta(hours=1)

            scheduled_messages.add(target_msg.id)
            bot.scheduler.add_job(send_halfway_alert, "date", run_date=halfway_time,
                                  args=[target_msg.channel.id, target_msg.id])
            bot.scheduler.add_job(send_one_hour_alert, "date", run_date=one_hour_before,
                                  args=[target_msg.channel.id, target_msg.id])

        await interaction.response.send_message(
            f"✅ Auction registered & activated.\n• Auction ID: `{target_msg.id}`\n• Ends: <t:{unix_time}:F> (<t:{unix_time}:R>)",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to track auction: {e}", ephemeral=True)


@bot.tree.command(name="final_bid", description="Get the final (valid) bid at or before auction end time")
@app_commands.describe(auction_id="Auction ID (message_id used at registration)")
async def final_bid(interaction: discord.Interaction, auction_id: str):
    auction = get_auction(auction_id)
    if not auction:
        await interaction.response.send_message(f"❌ Auction `{auction_id}` not found.", ephemeral=True)
        return

    row = get_highest_bid_before_end(auction_id)
    if row:
        user_id, amount, bid_time = row["user_id"], row["amount"], row["bid_time_utc"]
        # Jump URL if we know channel/message
        jump = ""
        if auction["channel_id"] and auction["message_id"]:
            jump = f"https://discord.com/channels/{interaction.guild_id}/{auction['channel_id']}/{auction['message_id']}"
        await interaction.response.send_message(
            f"🏁 **Final bid** for `{auction_id}`: **{amount:,}** by <@{user_id}> (at {bid_time} UTC)\n{jump}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"No valid bids found for auction `{auction_id}`.", ephemeral=True)


@bot.tree.command(name="auction_info", description="Show stored info for an auction")
@app_commands.describe(auction_id="Auction ID (message_id)")
async def auction_info(interaction: discord.Interaction, auction_id: str):
    a = get_auction(auction_id)
    if not a:
        await interaction.response.send_message("❌ Not found.", ephemeral=True)
        return
    jump = ""
    if a["channel_id"] and a["message_id"]:
        jump = f"https://discord.com/channels/{interaction.guild_id}/{a['channel_id']}/{a['message_id']}"
    await interaction.response.send_message(
        f"🗂 **Auction** `{auction_id}`\n"
        f"• Status: `{a['status']}`\n"
        f"• Ends (UTC): `{a['end_time_utc']}`\n"
        f"{jump}",
        ephemeral=True
    )


# =========================
# Auction Alerts (reused)
# =========================
async def send_halfway_alert(channel_id, message_id):
    channel = bot.get_channel(channel_id)
    bidder_role = channel.guild.get_role(1315016261293576345)
    collector_role = channel.guild.get_role(1314988994580320266)
    original_message = await channel.fetch_message(message_id)
    await channel.send(
        f"⏳ {bidder_role.mention} {collector_role.mention} — This auction is at **halftime**!\n"
        f"{original_message.jump_url}"
    )


async def send_one_hour_alert(channel_id, message_id):
    channel = bot.get_channel(channel_id)
    sniper_role = channel.guild.get_role(1315017025764196483)
    original_message = await channel.fetch_message(message_id)
    await channel.send(
        f"🎯 {sniper_role.mention} — **1 hour remaining**! Final bids incoming!\n"
        f"{original_message.jump_url}"
    )


# =========================
# Events
# =========================
@bot.event
async def on_ready():
    init_db()
    print(f'✅ Logged in as {bot.user} (ID: {bot.user.id})')


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    # Only monitor auction channels and human posts
    if message.channel.id not in AUCTION_CHANNEL_IDS or message.author.bot:
        return
    if message.id in scheduled_messages:
        return

    # Detect <t:UNIX> and register as pending
    match = TIMESTAMP_REGEX.search(message.content)
    if not match:
        return

    unix_time = int(match.group(1))
    end_time = datetime.datetime.fromtimestamp(unix_time, tz=UTC)
    now = datetime.datetime.now(UTC)

    # Upsert to DB as pending; auction_id == message.id for simplicity
    upsert_pending_auction(
        auction_id=str(message.id),
        message_id=str(message.id),
        channel_id=str(message.channel.id),
        end_time_utc=iso_utc(end_time)
    )

    # Prompt to confirm
    await message.channel.send(
        f"🛎 Potential auction detected for message `{message.id}` (ends <t:{unix_time}:R>). "
        f"Confirm with `/track_auction {message.id}`."
    )

    # If far enough away, schedule alerts now (will be fine if /track_auction re-schedules)
    if end_time <= now + datetime.timedelta(hours=1):
        return

    time_remaining = end_time - now
    halfway_time = now + time_remaining / 2
    one_hour_before = end_time - datetime.timedelta(hours=1)

    scheduled_messages.add(message.id)
    print(f"[Scheduled] Halftime alert for message {message.id} at {halfway_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[Scheduled] 1-hour alert for message {message.id} at {one_hour_before.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    bot.scheduler.add_job(send_halfway_alert, "date", run_date=halfway_time, args=[message.channel.id, message.id])
    bot.scheduler.add_job(send_one_hour_alert, "date", run_date=one_hour_before, args=[message.channel.id, message.id])


# =========================
# Reaction Event: Confirm Bids
# =========================
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Replace with your confirm emoji ID
    # If you're using a unicode emoji, payload.emoji.id will be None; adjust check accordingly.
    if str(getattr(payload.emoji, "id", None)) != "1365117493919744122":
        return
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    channel = guild.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    # Bidder = the author of the bid message
    bidder = message.author

    try:
        amount = parse_amount(message.content)
    except Exception:
        await channel.send(f"⚠️ Couldn’t detect a valid bid in {bidder.mention}’s message.")
        return

    # Use channel.id as auction_id if you prefer channel-scoped auctions; here we use message's parent post id if tracked.
    # For consistency with /track_auction and on_message, we'll use the ORIGINAL LISTING message id as auction_id.
    # In reply chains, you may want to map replies → root listing; here we fallback to channel.id if unknown.
    auction_id = None

    # Try to detect the root listing id from the DB by channel and "active pending latest" heuristic
    # Simplify: if this message itself is the listing (has <t:UNIX>), use that; else default to channel.id
    m_match = TIMESTAMP_REGEX.search(message.content)
    if m_match:
        auction_id = str(message.id)
    else:
        # Use channel id as bucket by default; admin can always /track_auction <message_id> to be precise
        auction_id = str(channel.id)

    await confirm_bid(bidder, amount, auction_id, channel=channel)


# =========================
# Keep-alive & Run
# =========================
if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    bot.run(TOKEN)
