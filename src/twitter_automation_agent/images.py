from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import Article


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:80] or "image"


class ImageFinder:
    def __init__(self, settings: Settings, timeout: float = 20.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def find(self, article: Article) -> str | None:
        if article.image_url:
            return str(article.image_url)

        og_image = self._extract_og_image(str(article.url))
        if og_image:
            return og_image

        if self.settings.serpapi_api_key:
            return self._serpapi_image(article)

        return None

    def download(self, image_url: str, output_dir: Path) -> Path | None:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = httpx.get(
                image_url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type and not content_type.startswith("image/"):
            return None

        suffix = mimetypes.guess_extension(content_type) if content_type else None
        if not suffix:
            suffix = Path(urlparse(image_url).path).suffix or ".jpg"

        path = output_dir / f"{safe_filename(urlparse(image_url).netloc)}{suffix}"
        path.write_bytes(response.content)
        return path

    def _extract_og_image(self, article_url: str) -> str | None:
        try:
            response = httpx.get(
                article_url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "TwitterAutomationAgent/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        for selector in [
            ("property", "og:image"),
            ("name", "twitter:image"),
            ("property", "twitter:image"),
        ]:
            tag = soup.find("meta", attrs={selector[0]: selector[1]})
            if tag and tag.get("content"):
                return str(tag["content"])
        return None

    def _serpapi_image(self, article: Article) -> str | None:
        query = f"{article.title} {article.source}"
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
            if original:
                return original
        return None
