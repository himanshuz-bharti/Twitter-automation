# Twitter Automation Agent

This fully automated pipeline turns trending tech topics into factual, provocative X/Twitter drafts. It leverages local LLMs (via Ollama) to parse the news, draft tweets, and explicitly reason about the best images to fetch from the internet. Finally, it can send drafts to your phone via Telegram, or post them entirely hands-free to X.com using Windows clipboard automation!

## Core Capabilities
- **News Aggregation:** Fetches the latest articles from Google News, Hacker News, The Verge, Ars Technica, and more.
- **Local AI Drafting:** Uses Ollama (e.g. `llama3.2:3b`) to draft the tweet safely and factually.
- **AI Image Routing:** The LLM outputs strict JSON to route queries. It fetches real-world logos/portraits from Wikipedia, and generates abstract conceptual images on-the-fly using Pollinations AI.
- **Telegram Bot Control:** Send commands like `/post [topic]` or `/autopost [topic]` straight from your phone to trigger the agent.
- **Hands-Free X.com Posting:** Automatically opens your default system browser (where you are already logged in to X), writes the tweet, copies the downloaded images directly to your Windows clipboard, and simulates a `Ctrl+V` to paste the media and post!

## 1. Setup Instructions

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

### Install Ollama (Free Local LLM)
Install Ollama from https://ollama.com/download.
Pull a fast, lightweight model (recommended):
```powershell
ollama pull llama3.2:3b
```

Update your `.env`:
```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
DEFAULT_STYLE=ragebait
```

### Telegram Bot Setup
1. Open Telegram and message `@BotFather`. Send `/newbot` and follow the prompts.
2. Copy the bot token into `.env` (`TELEGRAM_BOT_TOKEN=...`).
3. Open a chat with your bot and send `/start`.
4. Open a browser to: `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Copy the `message.chat.id` into `.env` (`TELEGRAM_CHAT_ID=...`).

*(Optional: Run `tweet-agent telegram-check` to verify your credentials work.)*

## 2. Using the Telegram Bot
The best way to interact with the agent is to run it as a background service listening for Telegram commands.

Start the bot listener:
```powershell
tweet-agent bot
```
*(Tip: If you have old hanging commands stuck in Telegram while the bot was offline, start it with `tweet-agent bot --drop-pending-updates` to ignore them and start fresh!)*

Then, open Telegram on your phone and send commands to your bot:
- `/topic <topic> [count]`: Drafts tweets for a topic and sends them to Telegram for review (e.g., `/topic Microsoft 3`).
- `/trending [count]`: Drafts tweets based on trending tech news and sends them to Telegram.
- `/post <topic>`: Drafts a single tweet for the given topic, sends it to Telegram, and immediately automates posting it to X.com on your PC.
- `/post <topic> --posts <num> --interval <mins>`: Drafts a queue of tweets and automatically schedules them to post one-by-one to X.com (e.g., `/post "AI models" --posts 3 --interval 60`).

## 3. Command Line Interface (CLI)

If you prefer using the terminal, you can trigger actions manually:

### Generate and Post Immediately
Draft a single tweet about a topic and automate the browser to post it instantly:
```powershell
tweet-agent run --topic "SpaceX" --count 1 --post
```
*Note: When this runs, it will open your default browser to X.com. **Do not touch your mouse or keyboard for 5 seconds** while it simulates the keystrokes (Ctrl+V) to attach the images and hit post!*

### Schedule an Autopost Queue
Draft a queue of tweets and automatically post them at intervals (e.g., 20 posts, one every 90 minutes):
```powershell
tweet-agent autopost --topic "AI models" --queue-size 20 --posts 20 --interval-minutes 90
```

### Dry Run (Draft Only)
Generate local drafts without opening the browser to post:
```powershell
tweet-agent run --topic "Apple" --count 5
```
Output is written to `outputs/` as JSON, and downloaded images go to `outputs/images/`.

## 4. How Image Fetching Works
When the LLM finishes drafting the tweet, it uses its reasoning to determine exactly what images you need.
1. **Wikipedia Fallback:** The LLM attempts to identify a strictly proper noun (e.g. `"Microsoft"`, `"Satya Nadella"`). The agent queries the Wikipedia API for this exact entity to get guaranteed, accurate, real-world photos.
2. **Pollinations AI:** If Wikipedia fails to find an image, the agent uses a descriptive prompt (e.g., `"A futuristic high-tech abstract server room"`) to generate a royalty-free image on-the-fly using the Pollinations AI generator. 

The agent guarantees exactly **one photo per visual suggestion** to avoid redundant concepts, and attaches the top **two** images to your tweet. After a successful post, it auto-cleans the `outputs/images/` directory to save disk space!