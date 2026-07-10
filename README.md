# Twitter Automation Agent

This pipeline turns current news topics into factual, provocative X/Twitter drafts with relevant image suggestions. Because X returned `402 Payment Required` for `/2/tweets`, the recommended flow is now manual posting:

1. Collect recent stories from Google News topic search, or trending tech feeds when no topic is supplied.
2. Rank and deduplicate articles.
3. Draft a tweet with a local/open model through Ollama, or Hugging Face as an optional provider.
4. Resolve article links and fetch a relevant image.
5. Send the tweet draft plus image to Telegram.
6. You manually post the text and image on X.

The X posting code still exists if you later add paid X API credits.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

## Free Local Tweet Generation With Ollama

Install Ollama from:

```text
https://ollama.com/download
```

Pull a free local model:

```powershell
ollama pull llama3.2:3b
```

Keep Ollama running, then set `.env`:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
DEFAULT_STYLE=ragebait
```

Good model options:

- `llama3.2:3b`: recommended lightweight default for this project
- `qwen2.5:3b`: similar size, also good for instruction following
- `mistral:7b`: bigger, fast, stronger hooks if your machine can run it
- `llama3.1:8b`: bigger general-purpose alternative

If Ollama is not running, the app uses a deterministic fallback template instead of crashing.

## Telegram Setup

1. Open Telegram and message `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token into `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:your_bot_token_here
```

4. Open a chat with your bot and send `/start`.
5. Get your chat id by opening this URL in a browser, replacing `<TOKEN>` with your bot token:

```text
https://api.telegram.org/bot<TOKEN>/getUpdates
```

6. In the JSON response, copy `message.chat.id` into `.env`:

```dotenv
TELEGRAM_CHAT_ID=123456789
```

7. Verify Telegram delivery:

```powershell
tweet-agent telegram-check
```

For a group chat, add the bot to the group, send a message in the group, then use the group `chat.id` from `getUpdates`. Group ids are often negative numbers.

## Generate Local Drafts

```powershell
tweet-agent run --style ragebait --count 20
```

Output is written to `outputs/` as JSON. Downloaded images go to `outputs/images/`.

Preview ranked sources without drafting:

```powershell
tweet-agent sources --limit 10
```

You can search any current news topic when needed:

```powershell
tweet-agent run --topic "AI models" --style ragebait --count 20

tweet-agent run --topic "Bollywood gossip" --style ragebait --count 10
```

## Send Drafts To Telegram

Each run builds and sends a fresh batch immediately. The default is 10 image-backed tweet drafts.

Test without sending anything:

```powershell
tweet-agent telegram --topic "AI models" --count 10 --dry-run
```

Send 10 real drafts plus images to Telegram:

```powershell
tweet-agent telegram --topic "AI models" --count 10
```

For trending tech news, omit `--topic`:

```powershell
tweet-agent telegram --count 10
```

## Telegram Command Arguments

- `--count`: number of image-backed drafts to send immediately. Defaults to `10`.
- `--topic`: specific news topic to search, such as `AI models`, `Bollywood gossip`, `elections`, or `football transfers`. Omit it for trending tech news.
- `--style`: draft style. Defaults to `DEFAULT_STYLE` from `.env`.
- `--include-seen`: allows articles already sent through Telegram history.
- `--dry-run`: builds the batch and image paths but does not send to Telegram.

Images are compulsory for Telegram delivery. Drafts without a downloaded image are skipped.

## History Behavior

The agent writes `outputs/history.json`.

- `tweet-agent run` records drafted articles.
- `tweet-agent telegram` records only articles actually sent to Telegram.
- `tweet-agent autopost` records only articles actually posted to X.

A Telegram dry run does not mark articles as sent, so you can dry-run first and then send the same top draft for real.

## Optional X Posting

Test X workflow

```powershell
tweet-agent x-check
```

Then post one item:

```powershell
tweet-agent autopost --queue-size 20 --posts 1 --topic "AI models" --interval-minutes 0
```

Schedule 20 X posts, one every 90 minutes:

```powershell
tweet-agent autopost --queue-size 20 --posts 20 --interval-minutes 90
```

## Optional Hugging Face Provider

Some Hugging Face hosted inference usage may require a token.

```dotenv
LLM_PROVIDER=huggingface
HUGGINGFACE_API_TOKEN=your_token
HUGGINGFACE_MODEL=mistralai/Mistral-7B-Instruct-v0.3
```

## Styles

- `neutral`: restrained news summary
- `sharp`: direct, skeptical framing
- `spicy`: punchy, high-stakes framing
- `ragebait`: maximum hook and controversy framing, still source-grounded

The agent should not fabricate claims, impersonate people, or generate targeted harassment. If a claim is not supported by the collected article metadata, the drafter is instructed to leave it out and a validator can force a fallback.

## Image Fetching

The image finder tries, in order:

1. Generate an original, abstract tech image prompt via the LLM.
2. Render a high-quality AI image on-the-fly using the Pollinations AI generator API.
3. Fallback to Wikipedia API for deterministic, real-world logos/portraits.
4. Fallback to Wikimedia Commons API for supplementary file search.
5. Use SerpAPI/DuckDuckGo Google Images fallback if configured.

The script selects the top **two** distinct images and copies them to the Windows clipboard simultaneously via a PowerShell `FileDropList`, attaching both to your tweet instantly!

Keep credentials out of git. `.env` is ignored by this repo.