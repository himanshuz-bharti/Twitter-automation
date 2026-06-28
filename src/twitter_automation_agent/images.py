from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import Article

MIN_IMAGE_BYTES = 8_000
MIN_DIMENSION_HINT = 240
HttpUrlAdapter = TypeAdapter(HttpUrl)
STOPWORDS = {
    "about",
    "after",
    "amid",
    "and",
    "are",
    "following",
    "from",
    "has",
    "have",
    "into",
    "its",
    "latest",
    "launches",
    "limits",
    "model",
    "models",
    "new",
    "the",
    "this",
    "under",
    "what",
    "with",
}
GENERIC_IMAGE_TERMS = [
    "technology",
    "tech company",
    "artificial intelligence",
    "semiconductor",
    "smartphone",
    "laptop",
    "software",
    "data center",
    "robotics",
    "cybersecurity",
    "cloud computing",
]


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:80] or "image"


def _looks_like_low_value_image(url: str) -> bool:
    lowered = url.lower()
    host = urlparse(url).netloc.lower()
    low_value_terms = [
        "sprite",
        "favicon",
        "icon",
        "placeholder",
        "avatar",
        "profile_images",
        "default",
    ]
    return host in {"lh3.googleusercontent.com", "encrypted-tbn0.gstatic.com"} or any(
        term in lowered for term in low_value_terms
    )


def _dimension_score(url: str, width: str | None = None, height: str | None = None) -> int:
    score = 0
    for value in [width, height]:
        if value and value.isdigit() and int(value) >= MIN_DIMENSION_HINT:
            score += 2

    for pattern in [r"[?&]w=(\d+)", r"[?&]width=(\d+)", r"-(\d+)x(\d+)\."]:
        match = re.search(pattern, url)
        if not match:
            continue
        dims = [int(group) for group in match.groups() if group.isdigit()]
        if dims and max(dims) >= MIN_DIMENSION_HINT:
            score += 2
    return score


def _valid_http_url(value: str | None) -> HttpUrl | None:
    if not value:
        return None
    try:
        return HttpUrlAdapter.validate_python(value)
    except ValueError:
        return None


def _keywords(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", value.lower())
        if len(token) > 2 and token not in STOPWORDS
    }


def _ordered_keywords(value: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for token in re.split(r"[^a-z0-9]+", value.lower()):
        if len(token) <= 2 or token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


class ImageFinder:
    def __init__(self, settings: Settings, timeout: float = 20.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def find(self, article: Article) -> str | None:
        search_query = self._image_query(article)
        candidate_urls = self._article_page_candidates(article)

        for page_url in candidate_urls:
            image_url = self._extract_best_article_image(page_url, article)
            if image_url:
                return image_url

        if self.settings.serpapi_api_key:
            serp_image = self._serpapi_image(search_query)
            if serp_image:
                return serp_image

        scraped_image = self._duckduckgo_image(search_query, _keywords(search_query))
        if scraped_image:
            return scraped_image

        if article.image_url and not _looks_like_low_value_image(str(article.image_url)):
            return str(article.image_url)

        return None

    def download(self, image_url: str, output_dir: Path) -> Path | None:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = httpx.get(
                image_url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type and not content_type.startswith("image/"):
            return None
        if len(response.content) < MIN_IMAGE_BYTES:
            return None

        suffix = mimetypes.guess_extension(content_type) if content_type else None
        if not suffix:
            suffix = Path(urlparse(image_url).path).suffix or ".jpg"

        path = output_dir / f"{safe_filename(urlparse(image_url).netloc)}{suffix}"
        path.write_bytes(response.content)
        return path

    def _article_page_candidates(self, article: Article) -> list[str]:
        candidates: list[str] = []
        for raw_url in [article.resolved_url, article.url, article.publisher_url]:
            if raw_url and str(raw_url) not in candidates:
                candidates.append(str(raw_url))

        resolved = self._resolve_article_url(str(article.url))
        if resolved and resolved not in candidates:
            article.resolved_url = _valid_http_url(resolved)
            candidates.insert(0, resolved)

        return candidates

    def _resolve_article_url(self, url: str) -> str | None:
        try:
            response = httpx.get(
                url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        final_url = str(response.url)
        if "news.google.com" not in urlparse(final_url).netloc:
            return final_url

        soup = BeautifulSoup(response.text, "html.parser")
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            href = str(canonical["href"])
            if "news.google.com" not in urlparse(href).netloc:
                return href

        for anchor in soup.find_all("a", href=True):
            href = urljoin(final_url, str(anchor["href"]))
            host = urlparse(href).netloc.lower()
            if host and "news.google.com" not in host and "google.com" not in host:
                return href
        return None

    def _extract_best_article_image(self, article_url: str, article: Article) -> str | None:
        try:
            response = httpx.get(
                article_url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        candidates: list[tuple[int, str]] = []
        article_keywords = _keywords(f"{article.title} {article.source} {article.publisher or ''}")

        for selector in [
            ("property", "og:image"),
            ("property", "og:image:url"),
            ("name", "twitter:image"),
            ("property", "twitter:image"),
        ]:
            tag = soup.find("meta", attrs={selector[0]: selector[1]})
            if tag and tag.get("content"):
                url = urljoin(article_url, str(tag["content"]))
                content_score = self._content_match_score(url, "", article_keywords)
                candidates.append((12 + _dimension_score(url) + content_score, url))

        for image in soup.find_all("img"):
            raw_src = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            if not raw_src:
                srcset = image.get("srcset") or image.get("data-srcset")
                raw_src = self._largest_srcset_url(str(srcset)) if srcset else None
            if not raw_src:
                continue

            url = urljoin(article_url, str(raw_src))
            score = _dimension_score(url, image.get("width"), image.get("height"))
            alt = str(image.get("alt") or "").lower()
            score += self._content_match_score(url, alt, article_keywords)
            if image.find_parent("article"):
                score += 3
            candidates.append((score, url))

        ranked = sorted(candidates, key=lambda item: item[0], reverse=True)
        for score, url in ranked:
            if score >= 4 and self._is_usable_image_url(url):
                return url
        return None

    def _content_match_score(self, image_url: str, alt: str, article_keywords: set[str]) -> int:
        haystack = f"{image_url} {alt}".lower()
        score = 0
        for keyword in article_keywords:
            if keyword in haystack:
                score += 3

        return score

    def _image_query(self, article: Article) -> str:
        keyword_text = f"{article.title} {article.summary or ''} {article.source}"
        keywords = _ordered_keywords(keyword_text)
        priority_terms = keywords[:6]
        if not priority_terms:
            return "technology news"

        generic_hint = next(
            (term for term in GENERIC_IMAGE_TERMS if any(part in keyword_text.lower() for part in term.split())),
            "technology news",
        )
        return " ".join([*priority_terms, generic_hint, "news image"])

    def _largest_srcset_url(self, srcset: str) -> str | None:
        best_url: str | None = None
        best_width = 0
        for candidate in srcset.split(","):
            parts = candidate.strip().split()
            if not parts:
                continue
            width = 0
            if len(parts) > 1 and parts[1].endswith("w"):
                width = int(parts[1][:-1]) if parts[1][:-1].isdigit() else 0
            if best_url is None or width > best_width:
                best_url = parts[0]
                best_width = width
        return best_url

    def _is_usable_image_url(self, image_url: str) -> bool:
        if _looks_like_low_value_image(image_url):
            return False
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        suffix = Path(parsed.path).suffix.lower()
        return suffix in {"", ".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def _serpapi_image(self, query: str) -> str | None:
        try:
            response = httpx.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google_images",
                    "q": query,
                    "api_key": self.settings.serpapi_api_key,
                    "safe": "active",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = response.json()
        for item in data.get("images_results", []):
            original = item.get("original")
            if original and self._is_usable_image_url(original):
                return original
        return None

    def _duckduckgo_image(self, query: str, keywords: set[str]) -> str | None:
        try:
            response = httpx.get(
                "https://duckduckgo.com/",
                params={"q": query},
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        match = re.search(r"vqd=['\"]([^'\"]+)['\"]", response.text)
        if not match:
            return None

        try:
            image_response = httpx.get(
                "https://duckduckgo.com/i.js",
                params={
                    "q": query,
                    "vqd": match.group(1),
                    "o": "json",
                    "l": "us-en",
                    "p": "1",
                },
                timeout=self.timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1",
                    "Referer": str(response.url),
                },
            )
            image_response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = image_response.json()
        for item in data.get("results", []):
            image = item.get("image")
            width = str(item.get("width") or "")
            height = str(item.get("height") or "")
            title = str(item.get("title") or "")
            if not image or not self._is_usable_image_url(image):
                continue
            if _dimension_score(image, width, height) > 0:
                return image
        return None
