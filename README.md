# Twitter Automation Agent

This is an MVP pipeline for turning current tech news into factual, provocative X/Twitter drafts with a relevant image candidate.

The default development flow is dry-run first:

1. Collect recent tech stories from RSS feeds.
2. Rank and deduplicate articles.
3. Draft a tweet with a local/open model through Ollama, or Hugging Face as an optional provider.
4. Resolve Google News links and scrape publisher/article metadata for a relevant image.
5. Save a local draft bundle.
6. Post to X with `--post` after the generated tweet and image look correct.

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

Run:

```powershell
tweet-agent run --style ragebait --count 20
```

If Ollama is not running, the app uses a deterministic fallback template instead of crashing.

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

The agent will not fabricate claims, impersonate people, or generate targeted harassment. If a claim is not supported by the collected article metadata, the drafter is instructed to leave it out and a validator can force a fallback.

## Image Fetching

The image finder now tries, in order:

1. Resolve the news URL to the publisher article.
2. Scrape publisher `og:image` and `twitter:image`.
3. Scan article `<img>` tags and rank likely article images.
4. Use SerpAPI Google Images fallback if `SERPAPI_API_KEY` is configured.
5. Use the RSS image only as a last resort.

This is still scraping, so it depends on the publisher's HTML and robots/access behavior.

## Preview Sources

```powershell
tweet-agent sources --limit 10
```

## Dry Run

```powershell
tweet-agent run --style ragebait --count 20
```

Output is written to `outputs/` as JSON. Downloaded images go to `outputs/images/`.
The agent also writes `outputs/history.json` and skips exact same article URLs/titles on later runs.

You can still bias the search when needed:

```powershell
tweet-agent run --topic "AI chips" --style ragebait --count 20
```

To allow previously generated articles again:

```powershell
tweet-agent run --style ragebait --count 20 --include-seen
```

## How To Get X/Twitter API Keys

1. Go to the X Developer Portal:

```text
https://developer.x.com/
```

2. Sign in with the X account that will own the app.
3. Apply for developer access if your account does not already have it.
4. Create a Project and App.
5. In the app settings, enable User authentication.
6. Set app permissions to `Read and write`. Use `Read and write and Direct message` only if you truly need DMs.
7. Configure OAuth 1.0a user context. This project uses OAuth 1.0a because media upload still commonly relies on the v1.1 media endpoint.
8. Add a callback URL if the portal requires one. For local/manual scripts you can use something like:

```text
http://localhost:3000/callback
```

9. Go to Keys and tokens.
10. Copy or generate:

- API Key
- API Key Secret
- Access Token
- Access Token Secret

11. Put them in `.env`:

```dotenv
X_API_KEY=your_api_key
X_API_SECRET=your_api_key_secret
X_ACCESS_TOKEN=your_access_token
X_ACCESS_TOKEN_SECRET=your_access_token_secret
```

12. Confirm the app permissions are `Read and write`. If you changed permissions after creating tokens, regenerate the access token and secret.

## Post To X

Always test with dry-run first:

```powershell
tweet-agent run --style ragebait --count 20
```

Then post:

```powershell
tweet-agent run --style ragebait --count 20 --post
```

Notes:

- X API access level and pricing changes over time.
- Free/basic tiers may have strict posting limits or may not include every endpoint.
- If image upload fails, verify your app has OAuth 1.0a credentials and write permission.
- Keep credentials out of git. `.env` is ignored by this repo.
