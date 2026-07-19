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

    def scrape_single_tweet(self, tweet_url: str) -> Article | None:
        """
        Retrieves details of a single tweet using SerpApi search, falling back to Playwright.
        """
        api_key = self.settings.serpapi_api_key
        if not api_key:
            print("[SCRAPER] Warning: SERPAPI_API_KEY is not set. Cannot run debate mode scraper.")
            return None

        # Clean / resolve standard status URL
        match = re.search(r'(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)', tweet_url)
        if not match:
            print(f"[SCRAPER] Invalid tweet URL: {tweet_url}")
            return None

        username = match.group(1)
        tweet_id = match.group(2)
        resolved_url = f"https://x.com/{username}/status/{tweet_id}"

        # 1. Try SerpApi Google Search first
        query = f'"{username}/status/{tweet_id}"'
        encoded_query = urllib.parse.quote(query)
        url = f"https://serpapi.com/search.json?engine=google&q={encoded_query}&api_key={api_key}"

        print(f"[SCRAPER] Fetching single tweet details from SerpApi for URL: {resolved_url}...")
        try:
            response = httpx.get(url, timeout=15.0, trust_env=False)
            response.raise_for_status()
            data = response.json()
            organic_results = data.get("organic_results", [])

            for res in organic_results:
                link = res.get("link", "")
                snippet = res.get("snippet", "").strip()
                title = res.get("title", "").strip()

                # Make sure it's a match for this tweet ID
                if tweet_id in link:
                    clean_snippet = snippet
                    clean_snippet = re.sub(rf"^(?:[^:]+)\b(?:on X|on Twitter)?:\s*", "", clean_snippet, flags=re.IGNORECASE)
                    summary = clean_snippet or title
                    
                    if summary:
                        summary_clean = summary.replace(".", "").replace("…", "").strip()
                        if len(summary_clean) > 5:
                            return Article(
                                title=title or summary[:100],
                                url=resolved_url,
                                source="X/Twitter",
                                publisher=f"@{username}",
                                published_at=datetime.now(UTC),
                                summary=summary,
                            )
        except Exception as e:
            print(f"[SCRAPER] SerpApi single tweet fetch failed: {e}")

        # 2. Fallback to direct Playwright scraping
        playwright_text = self._scrape_via_playwright(resolved_url, username)
        if playwright_text:
            print("[SCRAPER] Successfully resolved tweet text using live Playwright scraping fallback.")
            return Article(
                title=f"Tweet by @{username}",
                url=resolved_url,
                source="X/Twitter",
                publisher=f"@{username}",
                published_at=datetime.now(UTC),
                summary=playwright_text,
            )

        # 3. Last resort fallback
        print("[SCRAPER] Warning: Could not scrape live tweet. Using fallback summary.")
        return Article(
            title=f"Tweet by @{username}",
            url=resolved_url,
            source="X/Twitter",
            publisher=f"@{username}",
            published_at=datetime.now(UTC),
            summary=f"A tweet by @{username} at URL {resolved_url}.",
        )

    def _scrape_via_playwright(self, tweet_url: str, username: str) -> str | None:
        """
        Launches Playwright headless browser to fetch the actual tweet text directly from X.com.
        """
        print(f"[SCRAPER] Attempting to scrape live tweet text via Playwright fallback...")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                try:
                    page.goto(tweet_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_selector('article', timeout=15000)
                    text = page.eval_on_selector('article', "el => el.innerText")
                    
                    # Search for the handle in the text
                    handle_pattern = re.compile(rf'@{re.escape(username)}', re.IGNORECASE)
                    handle_match = handle_pattern.search(text)
                    if handle_match:
                        start_pos = handle_match.end()
                        content_part = text[start_pos:]
                        
                        # Clean up initial prefix words
                        content_part = re.sub(r'^(?:Article|Follow|·|Post|Replying to)*', '', content_part, flags=re.IGNORECASE).strip()
                        
                        # Truncate at timestamp
                        footer_match = re.search(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm||·)', content_part)
                        if footer_match:
                            tweet_content = content_part[:footer_match.start()].strip()
                        else:
                            views_match = re.search(r'\b(?:\d+(?:\.\d+)?[KMB]?)\s*(?:Views|Reposts|Likes|Bookmarks)', content_part)
                            if views_match:
                                tweet_content = content_part[:views_match.start()].strip()
                            else:
                                tweet_content = content_part
                        
                        return tweet_content
                    else:
                        return text
                finally:
                    browser.close()
        except Exception as e:
            print(f"[SCRAPER] Playwright live scraping failed: {e}")
        return None
