from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.llm import LLMClient
from twitter_automation_agent.models import Article
HttpUrlAdapter = TypeAdapter(HttpUrl)

try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False

BING_NEWS_SEARCH_FEED = "https://www.bing.com/news/search?q={query}&format=rss"
GOOGLE_NEWS_SEARCH_FEED = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

def get_trending_topics(settings: Settings, category: str, limit: int = 4) -> list[str]:
    headlines = []
    if HAS_DDGS:
        try:
            results = DDGS().news(keywords=f"{category} breaking news", max_results=15)
            headlines = [r.get("title") for r in results if r.get("title")]
        except Exception as e:
            print(f"[DEBUG] DDGS trending failed: {e}")
        
    if not headlines:
        try:
            feed_url = BING_NEWS_SEARCH_FEED.format(query=quote_plus(f"{category} news"))
            response = httpx.get(
                feed_url,
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.text)
            headlines = [entry.get("title", "").strip() for entry in parsed.entries[:25] if entry.get("title")]
        except Exception as e:
            print(f"[DEBUG] Bing RSS trending fallback failed: {e}")

    if not headlines:
        try:
            feed_url = GOOGLE_NEWS_SEARCH_FEED.format(query=quote_plus(f"{category} news"))
            response = httpx.get(
                feed_url,
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.text)
            headlines = [entry.get("title", "").strip() for entry in parsed.entries[:25] if entry.get("title")]
        except Exception as e:
            print(f"[DEBUG] Google News RSS trending fallback failed: {e}")
            
    if not headlines and settings.newsdata_api_key:
        try:
            response = httpx.get(
                "https://newsdata.io/api/1/news",
                params={"apikey": settings.newsdata_api_key, "q": category, "language": "en"},
                timeout=10.0
            )
            if response.status_code == 200:
                headlines = [r.get("title") for r in response.json().get("results", []) if r.get("title")]
        except Exception:
            pass
            
    if not headlines and settings.newsapi_api_key:
        try:
            response = httpx.get(
                "https://newsapi.org/v2/everything",
                params={"apiKey": settings.newsapi_api_key, "q": category, "language": "en", "sortBy": "publishedAt"},
                timeout=10.0
            )
            if response.status_code == 200:
                headlines = [r.get("title") for r in response.json().get("articles", []) if r.get("title")]
        except Exception:
            pass
            
    if not headlines and settings.mediastack_api_key:
        try:
            response = httpx.get(
                "http://api.mediastack.com/v1/news",
                params={"access_key": settings.mediastack_api_key, "keywords": category, "languages": "en"},
                timeout=10.0
            )
            if response.status_code == 200:
                headlines = [r.get("title") for r in response.json().get("data", []) if r.get("title")]
        except Exception:
            pass
            
    if not headlines:
        return ["Trending"]
        
    llm = LLMClient(settings, timeout=30.0)
    prompt = f"""Analyze these recent news headlines for the category '{category}'.
Extract the top {limit} trending entities, topics, or subjects being discussed. Keep them short (1-3 words).
Return a JSON array of strings ONLY.

Headlines:
""" + "\\n".join(f"- {h}" for h in headlines) + f"""

Example JSON:
["Topic 1", "Topic 2", "Topic 3", "Topic 4"]
"""
    try:
        raw = llm.generate(prompt, json_format=True, temperature=0.3, max_tokens=150)
        import json
        topics = json.loads(raw)
        extracted = []
        if isinstance(topics, dict):
            for k, v in topics.items():
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, str):
                            extracted.append(item)
                        elif isinstance(item, dict) and "topic" in item:
                            extracted.append(item["topic"])
        elif isinstance(topics, list):
            for item in topics:
                if isinstance(item, str):
                    extracted.append(item)
                elif isinstance(item, dict) and "topic" in item:
                    extracted.append(item["topic"])
        
        if extracted:
            return [str(t) for t in extracted][:limit]
    except Exception as e:
        print(f"[DEBUG] Failed to extract topics via LLM: {e}")
        
    return ["Trending"]









def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def _dedupe_values(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = normalize_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped

def _spaced_camel_case(value: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return normalize_text(spaced)

def _topic_variants(topic: str | None) -> list[str]:
    if not topic:
        return []

    clean_topic = normalize_text(topic)
    compact_topic = re.sub(r"\s+", "", clean_topic)
    variants = [clean_topic]
    if compact_topic and compact_topic != clean_topic:
        variants.append(compact_topic)

    spaced_camel = _spaced_camel_case(clean_topic)
    if spaced_camel != clean_topic:
        variants.append(spaced_camel)

    spaced_compact = _spaced_camel_case(compact_topic)
    if spaced_compact and spaced_compact != compact_topic:
        variants.append(spaced_compact)

    return _dedupe_values(variants)

def _topic_terms(topic: str | None) -> list[str]:
    if not topic:
        return []

    terms: list[str] = []
    for variant in _topic_variants(topic):
        for token in re.findall(r"[A-Za-z0-9]+", variant):
            lowered = token.lower()
            if len(lowered) < 3:
                continue
            terms.append(lowered)
    return _dedupe_values(terms)

def _has_token(haystack: str, token: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack) is not None

def _entry_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return None

def _entry_image(entry: feedparser.FeedParserDict) -> str | None:
    media_content = entry.get("media_content") or []
    for item in media_content:
        url = item.get("url")
        if url:
            return url

    media_thumbnail = entry.get("media_thumbnail") or []
    for item in media_thumbnail:
        url = item.get("url")
        if url:
            return url

    links = entry.get("links") or []
    for item in links:
        if str(item.get("type", "")).startswith("image/") and item.get("href"):
            return item["href"]
    return None

def _entry_source(entry: feedparser.FeedParserDict) -> tuple[str | None, str | None]:
    source = entry.get("source") or {}
    title = source.get("title") if isinstance(source, dict) else None
    href = source.get("href") if isinstance(source, dict) else None
    return title, href

def _source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or "unknown"

def _publisher_from_title(title: str) -> str | None:
    if " - " not in title:
        return None
    publisher = title.rsplit(" - ", 1)[-1].strip()
    return publisher or None

def _clean_title(title: str) -> str:
    publisher = _publisher_from_title(title)
    if publisher:
        title = title[: -(len(publisher) + 3)].strip()
    if " | " in title:
        parts = [part.strip() for part in title.split(" | ") if part.strip()]
        if len(parts) > 1 and len(parts[-1].split()) <= 4:
            title = " | ".join(parts[:-1])
    return title

def _strip_trailing_publisher(text: str, publisher: str | None) -> str:
    if not publisher:
        return text.strip()

    previous = None
    clean = text.strip()
    while clean and clean != previous:
        previous = clean
        clean = re.sub(
            rf"(?:\s*[-|]\s*)?{re.escape(publisher)}$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip(" -:|")
    return clean

def _clean_summary(summary: str, title: str, publisher: str | None) -> str | None:
    text = normalize_text(summary)
    if not text:
        return None

    text = _strip_trailing_publisher(text, publisher)
    clean_title = normalize_text(title)
    if clean_title and text.lower().startswith(clean_title.lower()):
        text = text[len(clean_title) :].strip(" -:|")
    text = _strip_trailing_publisher(text, publisher)

    if not text or text.lower() == clean_title.lower():
        return None
    if len(text.split()) <= 2:
        return None
    return text[:500]

def _story_haystack(article: Article) -> str:
    return f"{article.title} {article.summary or ''}".lower()

def _source_haystack(article: Article) -> str:
    return f"{article.source} {article.publisher or ''}".lower()

def _valid_http_url(value: str | None) -> HttpUrl | None:
    if not value:
        return None
    try:
        return HttpUrlAdapter.validate_python(value)
    except ValueError:
        return None

def _fingerprint(title: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", title.lower())
    keywords = [token for token in lowered.split() if len(token) > 3]
    return hashlib.sha1(" ".join(keywords[:12]).encode("utf-8")).hexdigest()

def _is_similar(a1: Article, a2: Article) -> bool:
    def get_words(a: Article) -> set[str]:
        text = f"{a.title} {a.summary or ''}".lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return {w for w in text.split() if len(w) > 4}
        
    set1 = get_words(a1)
    set2 = get_words(a2)
    if not set1 or not set2:
        return False
        
    intersection = set1.intersection(set2)
    union = set1.union(set2)
    overlap = len(intersection) / len(union) if union else 0
    
    return overlap > 0.35 or (len(intersection) >= 4 and overlap > 0.20)

def article_relevance_score(article: Article, topic: str | None, cluster_size: int = 1, lookback_hours: int = 24) -> float:
    haystack = _story_haystack(article)
    score = 0.0

    if topic:
        topic_variants = [variant.lower() for variant in _topic_variants(topic)]
        if any(variant and variant in haystack for variant in topic_variants):
            score += 15.0

    score += min(cluster_size, 5) * 3.0

    if article.image_url:
        score += 2.0

    if article.published_at:
        age_hours = max(
            0.0,
            (datetime.now(UTC) - article.published_at.astimezone(UTC)).total_seconds() / 3600,
        )
        score += max(0.0, 15.0 - age_hours) / 2.0
        # Soft hierarchy: massively prioritize articles within the lookback window
        if age_hours <= lookback_hours:
            score += 50.0
        # Slight penalty for very old articles to keep them at the bottom
        elif age_hours > lookback_hours * 2:
            score -= (age_hours / 24.0)

    return score

def is_category_article(article: Article, topic: str | None = None, category: str = "Tech") -> bool:
    return True


def get_trending_genres(settings: Settings, limit: int = 6) -> list[str]:
    headlines = []
    if HAS_DDGS:
        try:
            results = DDGS().news(keywords="latest top news", max_results=20)
            headlines = [r.get("title") for r in results if r.get("title")]
        except Exception as e:
            print(f"[DEBUG] DDGS genres failed: {e}")
        
    if not headlines:
        try:
            feed_url = BING_NEWS_SEARCH_FEED.format(query=quote_plus("latest top news"))
            response = httpx.get(
                feed_url,
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.text)
            headlines = [entry.get("title", "").strip() for entry in parsed.entries[:25] if entry.get("title")]
        except Exception:
            pass

    if not headlines:
        return ["Tech", "Finance", "Politics", "Entertainment", "Sports", "World"]

    llm = LLMClient(settings, timeout=180.0)
    prompt = f"""Analyze these recent global news headlines.
Cluster them into exactly {limit} broad, distinct news genres or categories that are trending right now. 
Keep the genre names strictly 1 or 2 words max (e.g., "Technology", "Politics", "Crypto", "Entertainment", "Sports").
Return a JSON array of strings ONLY.

Headlines:
{chr(10).join(f"- {h}" for h in headlines)}

Example JSON:
["Technology", "Politics", "Finance", "Sports", "Entertainment", "World News"]
"""

    raw = llm.generate(prompt, json_format=True, temperature=0.3, max_tokens=200)
    if not raw:
        return ["Tech", "Finance", "Politics", "Entertainment", "Sports", "World"]
        
    import json
    text = raw.strip()
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            genres = [item.strip() for item in data if item.strip()]
            return genres[:limit] if genres else ["Tech", "Finance", "Politics", "Entertainment", "Sports", "World"]
    except Exception:
        pass
        
    return ["Tech", "Finance", "Politics", "Entertainment", "Sports", "World"]

class NewsCollector:
    def __init__(self, settings: Settings, timeout: float = 180.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.llm = LLMClient(settings, timeout)

    
    def _collect_newsdata(self, query: str, limit: int) -> list[Article]:
        if not self.settings.newsdata_api_key:
            return []
        try:
            response = httpx.get(
                "https://newsdata.io/api/1/news",
                params={"apikey": self.settings.newsdata_api_key, "q": query, "language": "en"},
                timeout=self.timeout
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            articles = []
            for r in results[:limit]:
                if not r.get("title") or not r.get("link"):
                    continue
                from dateutil.parser import parse
                pub_date = parse(r.get("pubDate")).astimezone(UTC) if r.get("pubDate") else datetime.now(UTC)
                articles.append(Article(
                    title=_clean_title(r.get("title")),
                    url=r.get("link"),
                    source=r.get("source_id", "NewsData"),
                    publisher=_publisher_from_title(r.get("title")) or r.get("source_id"),
                    publisher_url=None,
                    published_at=pub_date,
                    summary=r.get("description", "") or r.get("content", ""),
                    image_url=_valid_http_url(r.get("image_url")),
                ))
            return articles
        except Exception as e:
            print(f"[DEBUG] NewsData API fallback failed: {e}")
            return []

    def _collect_newsapi(self, query: str, limit: int) -> list[Article]:
        if not self.settings.newsapi_api_key:
            return []
        try:
            response = httpx.get(
                "https://newsapi.org/v2/everything",
                params={"apiKey": self.settings.newsapi_api_key, "q": query, "language": "en", "sortBy": "publishedAt"},
                timeout=self.timeout
            )
            response.raise_for_status()
            results = response.json().get("articles", [])
            articles = []
            for r in results[:limit]:
                if not r.get("title") or not r.get("url"):
                    continue
                from dateutil.parser import parse
                pub_date = parse(r.get("publishedAt")).astimezone(UTC) if r.get("publishedAt") else datetime.now(UTC)
                articles.append(Article(
                    title=_clean_title(r.get("title")),
                    url=r.get("url"),
                    source=r.get("source", {}).get("name", "NewsAPI"),
                    publisher=_publisher_from_title(r.get("title")) or r.get("source", {}).get("name"),
                    publisher_url=None,
                    published_at=pub_date,
                    summary=r.get("description", "") or r.get("content", ""),
                    image_url=_valid_http_url(r.get("urlToImage")),
                ))
            return articles
        except Exception as e:
            print(f"[DEBUG] NewsAPI fallback failed: {e}")
            return []

    def _collect_mediastack(self, query: str, limit: int) -> list[Article]:
        if not self.settings.mediastack_api_key:
            return []
        try:
            response = httpx.get(
                "http://api.mediastack.com/v1/news",
                params={"access_key": self.settings.mediastack_api_key, "keywords": query, "languages": "en", "limit": limit},
                timeout=self.timeout
            )
            response.raise_for_status()
            results = response.json().get("data", [])
            articles = []
            for r in results[:limit]:
                if not r.get("title") or not r.get("url"):
                    continue
                from dateutil.parser import parse
                pub_date = parse(r.get("published_at")).astimezone(UTC) if r.get("published_at") else datetime.now(UTC)
                articles.append(Article(
                    title=_clean_title(r.get("title")),
                    url=r.get("url"),
                    source=r.get("source", "Mediastack"),
                    publisher=_publisher_from_title(r.get("title")) or r.get("source"),
                    publisher_url=None,
                    published_at=pub_date,
                    summary=r.get("description", ""),
                    image_url=_valid_http_url(r.get("image")),
                ))
            return articles
        except Exception as e:
            print(f"[DEBUG] Mediastack fallback failed: {e}")
            return []


    def collect_from_apis(self, topic: str | None, lookback_hours: int, limit: int, category: str = "Tech") -> list[Article]:
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        if topic and category and topic.lower() != category.lower():
            query = f"{topic} {category} news"
        else:
            query = f"{topic} news" if topic else f"{category} breaking news"
        
        api_articles = self._collect_newsdata(query, limit)
        if not api_articles:
            api_articles = self._collect_newsapi(query, limit)
        if not api_articles:
            api_articles = self._collect_mediastack(query, limit)
            
        valid = []
        cluster_sizes = {}
        for article in api_articles:
            if is_category_article(article, topic, category):
                valid.append(article)
                cluster_sizes[_fingerprint(article.title)] = 1
                
        for article in valid:
            article.score = article_relevance_score(
                article,
                topic,
                cluster_size=cluster_sizes.get(_fingerprint(article.title), 1),
                lookback_hours=lookback_hours,
            )
        valid.sort(key=lambda item: item.score, reverse=True)
        return valid[:limit]

    def collect(self, topic: str | None, lookback_hours: int, limit: int, category: str = "Tech") -> list[Article]:
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        articles: list[Article] = []

        if HAS_DDGS:
            print("[DEBUG] Searching DuckDuckGo News...")
            articles.extend(self._collect_ddgs(topic, lookback_hours, category))

        if not articles:
            print("[DEBUG] Searching Google/Bing RSS Feeds as fallback...")
            for feed_url in self._feed_urls(topic, lookback_hours, category):
                articles.extend(self._collect_feed(feed_url, topic, cutoff, category))

        deduped, cluster_sizes = self._dedupe(articles)
        for article in deduped:
            article.score = article_relevance_score(
                article,
                topic,
                cluster_size=cluster_sizes.get(_fingerprint(article.title), 1),
                lookback_hours=lookback_hours,
            )



        deduped.sort(
key=lambda item: item.score, reverse=True)
        return deduped[:limit]

    def _collect_ddgs(self, topic: str | None, lookback_hours: int, category: str) -> list[Article]:
        if topic and category and topic.lower() != category.lower():
            subject = f"{topic} {category}"
        else:
            subject = topic if topic else category
        queries = self._llm_search_queries(subject)
        if not queries:
            queries = [f"{category} news today"]
            
        articles = []
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        
        for q in queries:
            try:
                results = DDGS().news(keywords=q, max_results=15)
                for r in results:
                    title = r.get("title")
                    url = r.get("url")
                    body = r.get("body", "")
                    date_str = r.get("date")
                    source = r.get("source", "Unknown")
                    
                    if not title or not url:
                        continue
                        
                    try:
                        from dateutil.parser import parse
                        pub_date = parse(date_str).astimezone(UTC)
                    except Exception:
                        pub_date = datetime.now(UTC)
                        
                    article = Article(
                        title=_clean_title(title),
                        url=url,
                        source=source,
                        publisher=_publisher_from_title(title) or source,
                        publisher_url=None,
                        published_at=pub_date,
                        summary=body,
                        image_url=_valid_http_url(r.get("image")),
                    )
                    articles.append(article)
            except Exception as e:
                print(f"[DEBUG] DDGS search failed for '{q}': {e}")
                
        return articles

    def _llm_search_queries(self, subject: str) -> list[str]:
        prompt = f"""You are an expert news researcher. Generate 3 diverse Bing News search queries to find BREAKING news for the topic/category '{subject}'.
Return JSON ONLY in exactly this format:
{{
  "queries": [
    "first search query",
    "second search query",
    "third search query"
  ]
}}

Rules:
- Queries MUST be SHORT (2-4 words).
- If '{subject}' is a broad category, generate queries for its most popular CURRENT subcategories (e.g. for Entertainment use "Hollywood", "Bollywood", or "Netflix").
- DO NOT search for specific old events (like "Oscars" or "Game of Thrones"). Keep it broad (e.g., "Hollywood celebrity news", "Netflix trending movies").
- Queries MUST include "news today", "breaking", or "latest".

Example Execution for category 'Entertainment':
{{
  "queries": [
    "Hollywood news today",
    "Bollywood breaking updates",
    "latest Netflix series"
  ]
}}
"""
        try:
            raw = self.llm.generate(prompt, json_format=True, temperature=0.7, max_tokens=100)
            if not raw:
                return []
            import json
            data = json.loads(raw)
            return [str(q) for q in data.get("queries", [])][:3]
        except Exception as e:
            print(f"[DEBUG] Failed to generate LLM search queries: {e}")
            return []

    def _feed_urls(self, topic: str | None, lookback_hours: int, category: str) -> list[str]:
        if topic and category and topic.lower() != category.lower():
            subject = f"{topic} {category}"
            fallback_query = f"{topic} {category} news today"
        else:
            subject = topic if topic else category
            fallback_query = f"{topic} news today" if topic else f"{category} updates today"
        queries = self._llm_search_queries(subject)
        
        if not queries or len(queries) < 2:
            if topic and category and topic.lower() != category.lower():
                queries = [f"{topic} {category} news today", f"latest {topic} {category} updates", f"breaking {topic} {category} news"]
            elif topic:
                queries = [f"{topic} news today", f"latest {topic} updates", f"breaking {topic} news"]
            else:
                queries = [f"{category} updates today", f"breaking {category} news", f"latest {category} news today"]
        elif fallback_query not in queries:
            queries.append(fallback_query)
            
        processed_queries = []
        for q in queries:
            if not any(word in q.lower() for word in ["today", "latest", "breaking", "news"]):
                q += " news today"
            processed_queries.append(q)
            
        urls = [GOOGLE_NEWS_SEARCH_FEED.format(query=quote_plus(query)) for query in processed_queries]
        urls.extend([BING_NEWS_SEARCH_FEED.format(query=quote_plus(query)) for query in processed_queries])
        return urls

    def _collect_feed(self, feed_url: str, topic: str | None, cutoff: datetime, category: str) -> list[Article]:
        import time
        response = None
        for attempt in range(3):
            try:
                response = httpx.get(
                    feed_url,
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "TwitterAutomationAgent/0.1"},
                    trust_env=False,
                )
                response.raise_for_status()
                break
            except httpx.HTTPError as e:
                print(f"[DEBUG] Network error on attempt {attempt + 1} for {feed_url}: {e}")
                if attempt == 2:
                    return []
                time.sleep(2)
                
        if not response:
            return []

        parsed = feedparser.parse(response.text)
        feed_title = normalize_text(parsed.feed.get("title", "")) or _source_from_url(feed_url)
        articles: list[Article] = []

        for entry in parsed.entries:
            raw_title = normalize_text(entry.get("title", ""))
            url = entry.get("link")
            if not raw_title or not url:
                continue

            feed_publisher, publisher_href = _entry_source(entry)
            publisher = feed_publisher or _publisher_from_title(raw_title)
            title = _clean_title(raw_title)
            published_at = _entry_datetime(entry)

            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ")
            cleaned_summary = _clean_summary(summary, title, publisher)
            article = Article(
                title=title,
                url=url,
                source=publisher or feed_title,
                publisher=publisher,
                publisher_url=_valid_http_url(publisher_href),
                published_at=published_at,
                summary=cleaned_summary,
                image_url=_valid_http_url(_entry_image(entry)),
            )
            if is_category_article(article, topic, category):
                articles.append(article)

        return articles

    def _dedupe(self, articles: list[Article]) -> tuple[list[Article], dict[str, int]]:
        cluster_sizes: dict[str, int] = {}
        deduped: list[Article] = []
        for article in articles:
            key = _fingerprint(article.title)
            
            is_duplicate = False
            for d in deduped:
                if _is_similar(article, d):
                    is_duplicate = True
                    cluster_key = _fingerprint(d.title)
                    cluster_sizes[cluster_key] = cluster_sizes.get(cluster_key, 1) + 1
                    break
                    
            if not is_duplicate:
                deduped.append(article)
                cluster_sizes[key] = 1
                
        return deduped, cluster_sizes

    def enrich_article(self, article: Article) -> bool:
        if article.summary and len(article.summary) > 250:
            return True
            
        try:
            url_to_fetch = str(article.resolved_url or article.url)
            response = httpx.get(
                url_to_fetch,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            response.raise_for_status()
            
            # Google News RSS returns a proxy page. We must extract the real URL and follow it.
            if "news.google.com/rss/articles/" in url_to_fetch:
                import re
                match = re.search(r'<a[^>]+href="([^"]+)"', response.text)
                if match:
                    real_url = match.group(1)
                    if real_url.startswith("http"):
                        response = httpx.get(
                            real_url,
                            timeout=self.timeout,
                            follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                            trust_env=False,
                        )
                        response.raise_for_status()
            
            soup = BeautifulSoup(response.text, "html.parser")
            
            meta_desc = soup.find("meta", property="og:description")
            if not meta_desc:
                meta_desc = soup.find("meta", attrs={"name": "description"})
            
            meta_content = meta_desc.get("content", "").strip() if meta_desc else ""
            
            paragraphs = soup.find_all("p")
            text_blocks = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40]
            para_content = " ".join(text_blocks[:6]).strip()
            
            best_summary = para_content if len(para_content) > len(meta_content) else meta_content
            
            if best_summary and len(best_summary) > 60:
                article.summary = best_summary[:1500] + ("..." if len(best_summary) > 1500 else "")
                article.resolved_url = _valid_http_url(str(response.url))
        except Exception as e:
            print(f"[DEBUG] Enrichment failed: {e}")
            
        return bool(article.summary and len(article.summary) >= 100)
