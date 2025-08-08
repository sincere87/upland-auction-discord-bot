import os
import datetime
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

# Intents
intents = discord.Intents.default()
intents.message_content = True

# Track auctions and watchers
current_auctions = defaultdict(dict)
outbid_watchers = defaultdict(dict)

# Bot class
class AuctionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self.scheduler = AsyncIOScheduler()
        self.reminders = {}
        self.watched_auctions = {}

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()

bot = AuctionBot()

# Slash Commands
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
    prev = current_auctions[auction_id].get("highest_bidder")
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

    await interaction.response.send_message(
        f"✅ {bidder.display_name} confirmed at {amount:,} for `{auction_id}`.",
        ephemeral=True
    )

@bot.tree.command(name="set_reminder", description="Set a DM reminder for an auction listing")
async def set_reminder(interaction: discord.Interaction, auction_id: str, minutes: int):
    user_id = interaction.user.id
    job_id = f"{user_id}_{auction_id}_{minutes}_{datetime.datetime.utcnow().timestamp()}"
    run_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    bot.reminders[job_id] = {"auction_id": auction_id, "user_id": user_id}
    bot.scheduler.add_job(
        send_reminder_dm,
        trigger='date',
        run_date=run_time,
        args=[user_id, auction_id],
        id=job_id
    )
    await interaction.response.send_message(
        f"Reminder set for auction '{auction_id}' in {minutes} minutes. You will receive a DM.",
        ephemeral=True
    )

async def send_reminder_dm(user_id, auction_id):
    user = await bot.fetch_user(user_id)
    if user:
        await user.send(f"Reminder: Auction '{auction_id}' is coming to a close soon!")

@bot.tree.command(name="watch_auction", description="Start monitoring an auction in this channel")
async def watch_auction(interaction: discord.Interaction, auction_id: str):
    channel_id = interaction.channel_id
    user_id = interaction.user.id
    if channel_id not in AUCTION_CHANNEL_IDS:
        await interaction.response.send_message(
            "This channel is not monitored for auctions.", ephemeral=True
        )
        return
    bot.watched_auctions[auction_id] = {
        "channel_id": channel_id,
        "user_id": user_id,
        "active": True,
        "messages": []
    }
    await interaction.response.send_message(
        f"Bot is now watching auction '{auction_id}' in this channel.", ephemeral=True
    )

@bot.tree.command(name="conclude_auction", description="Stop monitoring an auction and show summary")
async def conclude_auction(interaction: discord.Interaction, auction_id: str):
    auction = bot.watched_auctions.get(auction_id)
    if not auction or not auction["active"]:
        await interaction.response.send_message(
            f"Auction '{auction_id}' is not being watched or already concluded.", ephemeral=True
        )
        return
    auction["active"] = False
    msg_count = len(auction["messages"])
    await interaction.response.send_message(
        f"Auction '{auction_id}' concluded. {msg_count} messages tracked during auction.",
        ephemeral=True
    )

# Events
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')

@bot.event
async def on_message(message):
    await bot.process_commands(message)

    for auction_id, auction in bot.watched_auctions.items():
        if auction["active"] and message.channel.id == auction["channel_id"]:
            auction["messages"].append({
                "author": str(message.author),
                "content": message.content,
                "timestamp": str(message.created_at)
            })

    if message.channel.id in AUCTION_CHANNEL_IDS and not message.author.bot:
        if "auction" in message.content.lower() and "asset" in message.content.lower():
            await message.channel.send("Auction post detected!")

# Keep-alive
if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    bot.run(TOKEN)

