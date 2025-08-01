import os
import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise EnvironmentError("DISCORD_TOKEN must be set in your .env file.")

intents = discord.Intents.default()
intents.message_content = True

class AuctionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self.scheduler = AsyncIOScheduler()
        self.reminders = {}

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()

bot = AuctionBot()

AUCTION_CHANNEL_IDS = [
    1200435807920591008, 1206719103772139630, 1206719174643023923,
    1213557612826595459, 1394009977378574508, 1394010162108432545,
    1231738039496085535, 1254867397366517841, 1259960721152807005,
    1309896571692912651, 1332469120897122454, 1352104013654528120,
    1377038520061001769
]

bot.watched_auctions = {}

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
    # Optionally, you can process or summarize the tracked messages here

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    # Track messages for watched auctions
    for auction_id, auction in bot.watched_auctions.items():
        if auction["active"] and message.channel.id == auction["channel_id"]:
            auction["messages"].append({
                "author": str(message.author),
                "content": message.content,
                "timestamp": str(message.created_at)
            })
    # Optionally, detect auction posts in monitored channels
    if message.channel.id in AUCTION_CHANNEL_IDS and not message.author.bot:
        if "auction" in message.content.lower() and "asset" in message.content.lower():
            await message.channel.send("Auction post detected!")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')

if __name__ == "__main__":
    bot.run(TOKEN)