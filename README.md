# Twitter Automation Agent

This is an MVP pipeline for turning current tech news into factual, punchy X/Twitter post drafts with a relevant image candidate.

The pipeline is intentionally dry-run first:

1. Collect recent tech stories from RSS feeds.
2. Rank and deduplicate articles.
3. Draft a tweet with source-grounded constraints.
4. Extract a relevant image from the source article metadata, with optional SerpAPI fallback.
5. Save a local draft bundle.
6. Post to X only when `--post` is explicitly used and X API credentials are configured.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

Edit `.env` with the API keys you want to use.

## Dry Run

```powershell
tweet-agent run --topic "AI chips" --style spicy
```

Output is written to `outputs/` as JSON. The tweet text is also printed in the terminal.

## Post To X

Posting requires X API credentials in `.env`.

```powershell
tweet-agent run --topic "AI chips" --style spicy --post
```

## Styles

- `neutral`: restrained news summary
- `sharp`: direct, high-contrast framing
- `spicy`: punchy and debate-friendly, but still factual

This project does not fabricate claims, impersonate people, or create targeted harassment. If a claim is not supported by the collected sources, the drafter is instructed to leave it out.

## Notes

- RSS collection works without paid news APIs.
- OpenAI drafting requires `OPENAI_API_KEY`; otherwise the agent uses a deterministic fallback template.
- Article image extraction depends on each publisher exposing `og:image` metadata.
- SerpAPI image fallback is optional and only used when `SERPAPI_API_KEY` is set.
