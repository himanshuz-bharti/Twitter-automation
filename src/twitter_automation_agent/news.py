from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from twitter_automation_agent.models import Article

DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
    "https://www.theverge.com/rss/index.xml",
    "https://techcrunch.com/feed/",
    "https://www.wired.com/feed/rss",
    "https://www.technologyreview.com/feed/",
]

PREFERRED_PUBLISHERS = {
    "axios": 5.0,
    "bloomberg": 5.0,
    "reuters": 5.0,
    "the verge": 4.5,
    "wired": 4.0,
    "techcrunch": 4.0,
    "mit technology review": 4.0,
    "the washington post": 4.0,
    "the information": 4.0,
    "financial times": 4.0,
    "the wall street journal": 4.0,
    "associated press": 4.0,
    "ap news": 4.0,
    "the hill": 3.0,
    "rnz": 2.0,
    "moneycontrol": 2.0,
}

LOWER_CONFIDENCE_PUBLISHERS = {
    "msn",
    "bundle",
    "inkorr",
    "sambad",
    "qazinform",
    "the hans india",
}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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
        return title[: -(len(publisher) + 3)].strip()
    return title


def _fingerprint(title: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", title.lower())
    keywords = [token for token in lowered.split() if len(token) > 3]
    return hashlib.sha1(" ".join(keywords[:12]).encode("utf-8")).hexdigest()


def article_relevance_score(article: Article, topic: str) -> float:
    haystack = f"{article.title} {article.summary or ''}".lower()
    topic_terms = [term for term in re.split(r"\W+", topic.lower()) if len(term) > 2]
    score = 0.0

    for term in topic_terms:
        if term in haystack:
            score += 5

    spicy_terms = [
        "launch",
        "released",
        "ban",
        "blocked",
        "lawsuit",
        "leak",
        "security",
        "ai",
        "openai",
        "nvidia",
        "apple",
        "google",
        "microsoft",
        "meta",
        "chip",
        "model",
        "startup",
    ]
    score += sum(1 for term in spicy_terms if term in haystack)

    if article.image_url:
        score += 1.5

    publisher = (article.publisher or article.source).lower()
    for name, weight in PREFERRED_PUBLISHERS.items():
        if name in publisher:
            score += weight
            break

    if any(name in publisher for name in LOWER_CONFIDENCE_PUBLISHERS):
        score -= 2.0

    if article.published_at:
        age_hours = max(
            0.0,
            (datetime.now(UTC) - article.published_at.astimezone(UTC)).total_seconds() / 3600,
        )
        score += max(0.0, 12.0 - age_hours) / 3

    return score


class NewsCollector:
    def __init__(self, feeds: list[str] | None = None, timeout: float = 20.0) -> None:
        self.feeds = feeds or DEFAULT_FEEDS
        self.timeout = timeout

    def collect(self, topic: str, lookback_hours: int, limit: int) -> list[Article]:
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        articles: list[Article] = []

        for feed_url in self._feed_urls(topic):
            articles.extend(self._collect_feed(feed_url, topic, cutoff))

        deduped = self._dedupe(articles)
        for article in deduped:
            article.score = article_relevance_score(article, topic)

        deduped.sort(key=lambda item: item.score, reverse=True)
        return deduped[:limit]

    def _feed_urls(self, topic: str) -> list[str]:
        query = quote_plus(f"{topic} technology when:1d")
        return [feed.format(query=query) for feed in self.feeds]

    def _collect_feed(self, feed_url: str, topic: str, cutoff: datetime) -> list[Article]:
        try:
            response = httpx.get(
                feed_url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []

        parsed = feedparser.parse(response.text)
        feed_title = normalize_text(parsed.feed.get("title", "")) or _source_from_url(feed_url)
        articles: list[Article] = []

        for entry in parsed.entries:
            raw_title = normalize_text(entry.get("title", ""))
            url = entry.get("link")
            if not raw_title or not url:
                continue

            publisher = _publisher_from_title(raw_title)
            title = _clean_title(raw_title)
            published_at = _entry_datetime(entry)
            if published_at and published_at < cutoff:
                continue

            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ")
            article = Article(
                title=title,
                url=url,
                source=publisher or feed_title,
                publisher=publisher,
                published_at=published_at,
                summary=normalize_text(summary)[:500] or None,
                image_url=_entry_image(entry),
            )
            if article_relevance_score(article, topic) > 0:
                articles.append(article)

        return articles

    def _dedupe(self, articles: list[Article]) -> list[Article]:
        seen: set[str] = set()
        deduped: list[Article] = []
        for article in articles:
            key = _fingerprint(article.title)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(article)
        return deduped
