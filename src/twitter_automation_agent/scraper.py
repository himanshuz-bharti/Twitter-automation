from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import Article


class XScraper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def scrape_top_tweets(self, category: str | None = None, limit: int = 5) -> list[Article]:
        """
        Retrieves top tweets from Google using SerpApi, bypassing Playwright/auth blocks.
        If category is omitted, retrieves global trending English tweets.
        """
        api_key = self.settings.serpapi_api_key
        if not api_key:
            print("[SCRAPER] Warning: SERPAPI_API_KEY is not set. Cannot run debate mode scraper.")
            return []

        # Target actual tweet status links for the given category or general trends
        if not category:
            query = "site:x.com/*/status (news OR breaking OR tech OR AI)"
        else:
            query = f"site:x.com/*/status {category}"

        encoded_query = urllib.parse.quote(query)
        url = f"https://serpapi.com/search.json?engine=google&q={encoded_query}&api_key={api_key}"

        print(f"[SCRAPER] Fetching top tweets from SerpApi for query: '{query}'...")
        articles: list[Article] = []

        try:
            response = httpx.get(url, timeout=15.0, trust_env=False)
            response.raise_for_status()
            data = response.json()
            organic_results = data.get("organic_results", [])
            print(f"[SCRAPER] SerpApi returned {len(organic_results)} organic search results.")

            for res in organic_results:
                if len(articles) >= limit:
                    break

                link = res.get("link", "")
                snippet = res.get("snippet", "").strip()
                title = res.get("title", "").strip()

                if not link or not snippet:
                    continue

                # Parse username and tweet ID from link
                match = re.search(r'(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)', link)
                if not match:
                    continue

                username = match.group(1)
                tweet_id = match.group(2)
                resolved_url = f"https://x.com/{username}/status/{tweet_id}"

                # Clean up the snippet
                clean_snippet = snippet
                clean_snippet = re.sub(rf"^(?:[^:]+)\b(?:on X|on Twitter)?:\s*", "", clean_snippet, flags=re.IGNORECASE)

                if not clean_snippet:
                    continue

                # Skip snippets that look like profile bios or follow invites
                lowered_snippet = clean_snippet.lower()
                if any(x in lowered_snippet for x in [
                    "follow me on x", "follow me on twitter", "the latest tweets from",
                    "tweets from @", "posts from @", "follow @", "follow on x", "follow on twitter"
                ]):
                    continue


                articles.append(Article(
                    title=title or clean_snippet[:100],
                    url=resolved_url,
                    source="X/Twitter",
                    publisher=f"@{username}",
                    published_at=datetime.now(UTC),
                    summary=clean_snippet,
                ))

        except Exception as e:
            print(f"[SCRAPER] SerpApi scraping failed: {e}")

        print(f"[SCRAPER] Scraped {len(articles)} fresh viral tweets using SerpApi.")
        return articles
