import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import openai
import os
import datetime

# Load environment variables securely
TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN or not OPENAI_API_KEY:
    raise EnvironmentError("DISCORD_TOKEN and OPENAI_API_KEY must be set in your .env file.")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)
scheduler = AsyncIOScheduler()
openai.api_key = OPENAI_API_KEY

AUCTION_CHANNEL_IDS = [
    1200435807920591008, 1206719103772139630, 1206719174643023923,
    1213557612826595459, 1394009977378574508, 1394010162108432545,
    1231738039496085535, 1254867397366517841, 1259960721152807005,
    1309896571692912651, 1332469120897122454, 1352104013654528120,
    1377038520061001769
]  # Fill with channel IDs to monitor

reminders = {}

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    scheduler.start()

@bot.command(name="set_reminder")
async def set_reminder(ctx, asset: str, minutes: int):
    user_id = ctx.author.id
    job_id = f"{user_id}_{asset}_{minutes}_{datetime.datetime.utcnow().timestamp()}"
    reminders[job_id] = (asset, ctx.channel.id)
    run_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    scheduler.add_job(
        send_reminder,
        trigger='date',
        run_date=run_time,
        args=[ctx.channel.id, asset, ctx.author.mention],
        id=job_id
    )
    await ctx.send(f"Reminder set for '{asset}' in {minutes} minutes.")

async def send_reminder(channel_id, asset, mention):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(f"{mention}, auction for '{asset}' is ending soon!")

@bot.event
async def on_message(message):
    if message.channel.id in AUCTION_CHANNEL_IDS and not message.author.bot:
        auction_info = parse_auction_message(message.content)
        if auction_info:
            ai_response = await get_ai_summary(auction_info)
            await message.channel.send(ai_response)
    await bot.process_commands(message)

def parse_auction_message(content):
    # Simple example: you can improve with regex or ML for Upland formats
    if "auction" in content.lower() and "asset" in content.lower():
        # Extract details here
        return {"content": content}
    return None

async def get_ai_summary(auction_info):
    prompt = f"Summarize and validate this upland NFT auction post: {auction_info['content']}"
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    return response['choices'][0]['message']['content']

if __name__ == "__main__":
    bot.run(TOKEN)