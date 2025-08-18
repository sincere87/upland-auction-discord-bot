import os 
import datetime
import re
import pytz
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import defaultdict

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise EnvironmentError("DISCORD_TOKEN must be set in your .env file.")

# Constants
AUCTION_CHANNEL_IDS = [
    1200435807920591008, 1206719103772139630, 1206719174643023923,
    1213557612826595459, 1394009977378574508, 1394010162108432545,
    1231738039496085535, 1254867397366517841, 1259960721152807005,
    1309896571692912651, 1332469120897122454, 1352104013654528120,
    1377038520061001769
]

# Role IDs (replace with actual ones from your server)
ROLE_BIDDER_ID = 123456789012345678
ROLE_COLLECTOR_ID = 223456789012345678
ROLE_SNIPER_ID = 323456789012345678

# Regex to find <t:UNIX> timestamps
TIMESTAMP_REGEX = re.compile(r"<t:(\d+)>")

# Intents
intents = discord.Intents.default()
intents.message_content = True

# Track auctions and watchers
current_auctions = defaultdict(dict)
outbid_watchers = defaultdict(dict)
scheduled_messages = set()

# Bot class
class AuctionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self.scheduler = AsyncIOScheduler()
        self.reminders = {}

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()

bot = AuctionBot()

# ------------------------
# Helper functions
# ------------------------

async def confirm_bid(bidder: discord.Member, amount: int, auction_id: str, channel: discord.TextChannel = None, interaction: discord.Interaction = None):
    prev = current_auctions.get(auction_id, {}).get("highest_bidder")
    current_auctions[auction_id] = {"highest_bidder": bidder, "amount": amount}

    if prev and prev.id in outbid_watchers[auction_id]:
        try:
            await prev.send(
                f"You’ve been outbid in auction `{auction_id}`.\n"
                f"New high bid: {amount:,} by {bidder.display_name}."
            )
        except discord.Forbidden:
            print(f"⚠️ Couldn't DM {prev.display_name}")
        del outbid_watchers[auction_id][prev.id]

    if interaction:
        await interaction.response.send_message(
            f"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`.",
            ephemeral=True
        )
    elif channel:
        await channel.send(
            f"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`."
        )


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

# ------------------------
# Slash Commands
# ------------------------

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
@app_commands.describe(bidder="User placing the bid", amount="Bid amount", auction_id="Auction ID")
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

# ------------------------
# Auction Alerts
# ------------------------

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

# ------------------------
# Events
# ------------------------

@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user} (ID: {bot.user.id})')

@bot.event
async def on_message(message):
    await bot.process_commands(message)

    if message.channel.id not in AUCTION_CHANNEL_IDS or message.author.bot:
        return
    if message.id in scheduled_messages:
        return

    match = TIMESTAMP_REGEX.search(message.content)
    if not match:
        return

    unix_time = int(match.group(1))
    end_time = datetime.datetime.fromtimestamp(unix_time, tz=pytz.UTC)
    now = datetime.datetime.now(pytz.UTC)

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

# ------------------------
# Reaction Event: Confirm Bids
# ------------------------

@bot.event
async def on_raw_reaction_add(payload):
    # Replace with your confirm emoji ID
    if str(payload.emoji.id) != "1365117493919744122":
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

    auction_id = str(channel.id)
    await confirm_bid(bidder, amount, auction_id, channel=channel)

# ------------------------
# Keep-alive
# ------------------------

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    bot.run(TOKEN)




