from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.models import Article

HttpUrlAdapter = TypeAdapter(HttpUrl)

GOOGLE_NEWS_SEARCH_FEED = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

DEFAULT_FEEDS = [
    GOOGLE_NEWS_SEARCH_FEED,
    "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "https://www.theverge.com/rss/index.xml",
    "https://techcrunch.com/feed/",
    "https://www.wired.com/feed/rss",
    "https://www.technologyreview.com/feed/",
]

TRENDING_TECH_QUERIES = [
    "technology news when:1d",
    "artificial intelligence news when:1d",
    "startup funding technology when:1d",
    "cybersecurity breach technology when:1d",
    "semiconductor chips technology when:1d",
    "big tech regulation when:1d",
    "consumer technology product launch when:1d",
]

TOPIC_STOPWORDS = {
    "about",
    "after",
    "all",
    "and",
    "are",
    "for",
    "from",
    "latest",
    "new",
    "news",
    "the",
    "this",
    "today",
    "with",
}

TECH_TERMS = {
    "ai",
    "algorithm",
    "app",
    "artificial intelligence",
    "autonomous",
    "chip",
    "cloud",
    "computer",
    "cyber",
    "data",
    "device",
    "digital",
    "gadget",
    "gemini",
    "gpu",
    "hardware",
    "internet",
    "laptop",
    "llm",
    "machine learning",
    "model",
    "phone",
    "platform",
    "privacy",
    "processor",
    "robot",
    "security",
    "semiconductor",
    "software",
    "startup",
    "tech",
    "technology",
}

PREFERRED_PUBLISHERS = {
    "axios": 5.0,
    "bloomberg": 5.0,
    "reuters": 5.0,
    "associated press": 4.5,
    "ap news": 4.5,
    "bbc": 4.0,
    "cnbc": 4.0,
    "financial times": 4.0,
    "the guardian": 4.0,
    "the hill": 3.0,
    "the new york times": 4.0,
    "the wall street journal": 4.0,
    "the washington post": 4.0,
    "the verge": 4.5,
    "wired": 4.0,
    "techcrunch": 4.0,
    "mit technology review": 4.0,
    "the information": 4.0,
    "variety": 3.5,
    "the hollywood reporter": 3.5,
    "people": 3.0,
    "entertainment weekly": 3.0,
    "deadline": 3.0,
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


def _google_when_filter(lookback_hours: int) -> str:
    days = max(1, min(7, (lookback_hours + 23) // 24))
    return f"when:{days}d"


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


def _topic_terms(topic: str | None) -> list[str]:
    if not topic:
        return []

    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9]+", topic):
        lowered = token.lower()
        if lowered in TOPIC_STOPWORDS or len(lowered) < 2:
            continue
        terms.append(lowered)
    return _dedupe_values(terms)


def _has_token(haystack: str, token: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack) is not None


def _topic_queries(topic: str, lookback_hours: int) -> list[str]:
    clean_topic = normalize_text(topic)
    when_filter = _google_when_filter(lookback_hours)
    queries = [
        f"{clean_topic} {when_filter}",
        f"{clean_topic} news {when_filter}",
        f"latest {clean_topic} {when_filter}",
    ]
    if " " in clean_topic:
        queries.insert(1, f'"{clean_topic}" {when_filter}')
    return _dedupe_values(queries)


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


def article_relevance_score(article: Article, topic: str | None, cluster_size: int = 1) -> float:
    haystack = _story_haystack(article)
    score = 0.0

    if topic:
        normalized_topic = normalize_text(topic).lower()
        topic_terms = _topic_terms(topic)
        matched_topic_terms = sum(1 for term in topic_terms if _has_token(haystack, term))
        score += matched_topic_terms * 4

        if topic_terms and matched_topic_terms == 0:
            score -= 12
        elif topic_terms and matched_topic_terms == len(topic_terms):
            score += 12
        elif len(topic_terms) > 1:
            score -= 2

        if normalized_topic and normalized_topic in haystack:
            score += 10

    engagement_terms = [
        "accused",
        "backlash",
        "ban",
        "blocked",
        "breaking",
        "controversy",
        "crackdown",
        "crisis",
        "deal",
        "exclusive",
        "fine",
        "investigation",
        "lawsuit",
        "leak",
        "launch",
        "probe",
        "recall",
        "release",
        "released",
        "restrict",
        "scandal",
        "strike",
        "unveil",
        "warn",
        "warning",
    ]
    if not topic:
        engagement_terms.extend(
            [
                "ai",
                "artificial intelligence",
                "antitrust",
                "chip",
                "cyber",
                "funding",
                "model",
                "privacy",
                "regulation",
                "security",
                "semiconductor",
                "startup",
            ]
        )
    score += sum(1 for term in engagement_terms if term in haystack)
    score += min(cluster_size, 5) * 2

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


def is_technology_article(article: Article, topic: str | None = None) -> bool:
    if topic:
        return is_topic_article(article, topic)
    haystack = _story_haystack(article)
    return any(term in haystack for term in TECH_TERMS)


def is_topic_article(article: Article, topic: str) -> bool:
    story = _story_haystack(article)
    source = _source_haystack(article)
    normalized_topic = normalize_text(topic).lower()
    topic_terms = _topic_terms(topic)
    if not topic_terms:
        return True
    if normalized_topic and normalized_topic in story:
        return True
    if any(_has_token(story, term) for term in topic_terms):
        return True
    return sum(1 for term in topic_terms if _has_token(source, term)) >= max(2, len(topic_terms))

class NewsCollector:
    def __init__(self, feeds: list[str] | None = None, timeout: float = 20.0) -> None:
        self.feeds = feeds or DEFAULT_FEEDS
        self.timeout = timeout

    def collect(self, topic: str | None, lookback_hours: int, limit: int) -> list[Article]:
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        articles: list[Article] = []

        for feed_url in self._feed_urls(topic, lookback_hours):
            articles.extend(self._collect_feed(feed_url, topic, cutoff))

        deduped, cluster_sizes = self._dedupe(articles)
        for article in deduped:
            article.score = article_relevance_score(
                article,
                topic,
                cluster_size=cluster_sizes.get(_fingerprint(article.title), 1),
            )

        deduped.sort(key=lambda item: item.score, reverse=True)
        return deduped[:limit]

    def _feed_urls(self, topic: str | None, lookback_hours: int) -> list[str]:
        if topic:
            return [
                GOOGLE_NEWS_SEARCH_FEED.format(query=quote_plus(query))
                for query in _topic_queries(topic, lookback_hours)
            ]

        queries = TRENDING_TECH_QUERIES
        urls: list[str] = []
        for feed in self.feeds:
            if "{query}" not in feed:
                urls.append(feed)
                continue
            for query in queries:
                urls.append(feed.format(query=quote_plus(query)))
        return urls

    def _collect_feed(self, feed_url: str, topic: str | None, cutoff: datetime) -> list[Article]:
        try:
            response = httpx.get(
                feed_url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
                trust_env=False,
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

            feed_publisher, publisher_href = _entry_source(entry)
            publisher = feed_publisher or _publisher_from_title(raw_title)
            title = _clean_title(raw_title)
            published_at = _entry_datetime(entry)
            if published_at and published_at < cutoff:
                continue

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
            if is_technology_article(article, topic) and article_relevance_score(article, topic) > 0:
                articles.append(article)

        return articles

    def _dedupe(self, articles: list[Article]) -> tuple[list[Article], dict[str, int]]:
        seen: set[str] = set()
        cluster_sizes: dict[str, int] = {}
        deduped: list[Article] = []
        for article in articles:
            key = _fingerprint(article.title)
            cluster_sizes[key] = cluster_sizes.get(key, 0) + 1
            if key in seen:
                continue
            seen.add(key)
            deduped.append(article)
        return deduped, cluster_sizes
