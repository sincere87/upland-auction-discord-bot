# upland-auction-discord-bot

A Discord bot for monitoring Upland NFT auctions, setting reminders, and generating AI-powered auction summaries.

## Features

- Monitors specified Discord channels for auction posts
- Allows users to set reminders for auction assets
- Summarizes and validates auction posts using OpenAI GPT-4

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/upland-auction-discord-bot.git
   cd upland-auction-discord-bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create a `.env` file in the project root:**
   ```
   DISCORD_TOKEN=your_discord_bot_token
   OPENAI_API_KEY=your_openai_api_key
   ```

4. **Run the bot:**
   ```bash
   python bot.py
   ```

## Usage

- Use `/set_reminder <asset> <minutes>` in any monitored channel to set a reminder.
- The bot will automatically summarize auction posts in the specified channels.

## Configuration

- Update `AUCTION_CHANNEL_IDS` in `bot.py` with the IDs of channels you want to monitor.

## Requirements

- Python 3.8+
- Discord bot token
- OpenAI API key

##