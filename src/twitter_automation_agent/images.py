from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.llm import LLMClient
from twitter_automation_agent.models import Article

@dataclass
class ImageTarget:
    wikipedia_entity: str
    pollinations_prompt: str

MIN_IMAGE_BYTES = 8_000
MIN_DIMENSION_HINT = 240
HttpUrlAdapter = TypeAdapter(HttpUrl)

STOPWORDS = {
    "about",
    "after",
    "amid",
    "and",
    "are",
    "as",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "its",
    "latest",
    "new",
    "news",
    "over",
    "report",
    "says",
    "source",
    "the",
    "this",
    "under",
    "what",
    "with",
}

LOW_VALUE_IMAGE_TERMS = [
    "sprite",
    "favicon",
    "icon",
    "placeholder",
    "avatar",
    "profile_images",
    "default",
    "author",
    "byline",
]

GENERIC_BAD_SUBJECTS = {
    "draft",
    "source",
    "photo",
    "image",
    "technology",
    "news",
}


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:80] or "image"


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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


def _valid_http_url(value: str | None) -> HttpUrl | None:
    if not value:
        return None
    try:
        return HttpUrlAdapter.validate_python(value)
    except ValueError:
        return None


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


def _host(value: str) -> str:
    return urlparse(value).netloc.lower().removeprefix("www.")


def _looks_like_low_value_image(url: str) -> bool:
    lowered = url.lower()
    host = _host(url)
    return host in {"lh3.googleusercontent.com", "encrypted-tbn0.gstatic.com"} or any(
        term in lowered for term in LOW_VALUE_IMAGE_TERMS
    )


def _source_terms(article: Article) -> set[str]:
    raw = f"{article.source} {article.publisher or ''}"
    return {token for token in _keywords(raw) if len(token) > 3}


def _captioned_context(article: Article, draft_text: str | None) -> str:
    return " ".join(
        part
        for part in [draft_text or "", article.title, article.summary or ""]
        if part
    )


def _is_bad_subject(subject: str, article: Article) -> bool:
    value = normalize_space(subject.strip(" .,;:-"))
    lowered = value.lower()
    if not value or len(value) < 2 or len(value) > 80:
        return True
    if lowered in GENERIC_BAD_SUBJECTS or lowered in STOPWORDS:
        return True
    if lowered.isdigit():
        return True

    source_terms = _source_terms(article)
    subject_tokens = _keywords(value)
    if subject_tokens and subject_tokens.issubset(source_terms):
        return True
    return False


def _subject_key(subject: str) -> str:
    tokens = _ordered_keywords(subject)
    return " ".join(tokens[:4]) if tokens else subject.lower()


class ImageFinder:
    def __init__(self, settings: Settings, timeout: float = 180.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.llm = LLMClient(settings, timeout)
        self._ddg_disabled = False

    def find(self, article: Article, draft_text: str | None = None) -> str | None:
        candidates = self.find_candidates(article, draft_text=draft_text, limit=1)
        return candidates[0] if candidates else None

    def find_candidates(
        self,
        article: Article,
        draft_text: str | None = None,
        limit: int = 3,
    ) -> list[str]:
        self._ensure_resolved_article_url(article)
        targets = self._visual_subjects(article, draft_text)

        candidates: list[str] = []
        subject_keywords: set[str] = set()
        for target in targets:
            subject_keywords.update(_keywords(target.wikipedia_entity))
            subject_keywords.update(_keywords(target.pollinations_prompt))

        for target in targets:
            if len(candidates) >= limit:
                break
                
            bucket: list[str] = []
            query = target.wikipedia_entity
            
            if self.settings.serpapi_api_key:
                bucket.extend(self._serpapi_images(query, article, subject_keywords, limit=1))
            if not bucket:
                bucket.extend(self._duckduckgo_images(query, article, subject_keywords, limit=1))
            if not bucket:
                bucket.extend(self._wikipedia_images(query, limit=1))
            if not bucket:
                bucket.extend(self._wikimedia_commons_images(query, limit=1))
                
            if bucket:
                candidates.append(bucket[0])

        if article.image_url:
            candidates.insert(0, str(article.image_url))

        # ONLY turn to Pollinations if NO web images were found for any target
        if not candidates and targets:
            pollinations_url = self._pollinations_images(targets[0].pollinations_prompt)
            candidates.append(pollinations_url)

        return candidates

    def download(self, image_url: str, output_dir: Path) -> Path | None:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = httpx.get(
                image_url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
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

        import uuid
        image_hash = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:10]
        unique_id = uuid.uuid4().hex[:6]
        path = output_dir / f"{safe_filename(urlparse(image_url).netloc)}-{image_hash}-{unique_id}{suffix}"
        path.write_bytes(response.content)
        return path

    def _visual_subjects(self, article: Article, draft_text: str | None) -> list[ImageTarget]:
        targets = self._llm_visual_subjects(article, draft_text)
        
        print(f"\n[DEBUG] [Image Criteria Extraction]")
        if targets:
            for i, target in enumerate(targets, start=1):
                print(f"  [{i}] Wikipedia Entity: '{target.wikipedia_entity}' | Pollinations: '{target.pollinations_prompt}'")
        else:
            print(f"  -> Extracted Entities: (None found)")
            
        return targets

    def _llm_visual_subjects(self, article: Article, draft_text: str | None) -> list[ImageTarget]:
        provider = self.settings.llm_provider.lower().strip()
        if provider in {"none", "fallback", "template"}:
            return []
            
        prompt = self._visual_subject_prompt(article, draft_text)
        raw = self.llm.generate(prompt, json_format=True, temperature=0.35, max_tokens=800)
        return self._parse_visual_subjects(raw)

    def _visual_subject_prompt(self, article: Article, draft_text: str | None) -> str:
        return f"""You are an expert at image search criteria generation for a news tweet.
Return JSON ONLY in exactly this format:
{{
  "image_targets": [
    {{
      "reasoning": "...",
      "wikipedia_entity": "Exact proper noun",
      "pollinations_prompt": "Rich descriptive scene"
    }}
  ]
}}

Goal: Provide 1 to 3 HIGHLY RELEVANT visual targets for this tweet.
For each target, provide:
1. wikipedia_entity: A STRICT, widely-known proper noun (e.g. "Microsoft", "Satya Nadella", "Xbox", "Flag of the United States"). DO NOT append words like "logo", "building", or "photo". This must match an exact Wikipedia article title.
2. pollinations_prompt: A highly descriptive visual scene for an AI generator.

Rules:
- STRONGLY PRIORITIZE real-world entities (names of specific people, companies, places, or physical products mentioned in the article) over abstract concepts.
- ONLY output targets that are CENTRAL to the article. If the article is only about ONE central subject, return ONLY ONE target. 
- DO NOT hallucinate names. DO NOT pad the list with generic terms (like "Revenue growth" or "IT services") just to have more targets. Quality over quantity!
- Do not include news publisher names.

Example Execution 1:
Draft tweet: "Mark Zuckerberg just announced major updates for Instagram."
Output:
{{
  "image_targets": [
    {{"reasoning": "We need the exact company to find the logo", "wikipedia_entity": "Instagram", "pollinations_prompt": "A glowing high-tech Instagram app logo"}},
    {{"reasoning": "The CEO is mentioned", "wikipedia_entity": "Mark Zuckerberg", "pollinations_prompt": "Portrait illustration of Mark Zuckerberg speaking at a futuristic conference"}},
    {{"reasoning": "Abstract representation of the product", "wikipedia_entity": "Social media", "pollinations_prompt": "Abstract futuristic social media interface screen"}}
  ]
}}

Now execute for the following:
Draft tweet: {draft_text or "none"}
Article title: {article.title}
Article summary: {article.summary or "none"}
Publisher/source to avoid: {article.source}
"""
    def _parse_visual_subjects(self, raw: str | None) -> list[ImageTarget]:
        if not raw:
            return []
        text = raw.strip()
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
            
        targets: list[ImageTarget] = []
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                items = data.get("image_targets", [])
                for item in items:
                    if isinstance(item, dict):
                        w_ent = item.get("wikipedia_entity", "")
                        p_prompt = item.get("pollinations_prompt", "")
                        if w_ent and p_prompt:
                            targets.append(ImageTarget(
                                wikipedia_entity=normalize_space(str(w_ent).strip(" .,;:-")),
                                pollinations_prompt=normalize_space(str(p_prompt).strip(" .,;:-"))
                            ))
        except json.JSONDecodeError:
            pass

        return targets[:5]

    def _fallback_subjects(self, article: Article, draft_text: str | None) -> list[str]:
        context = _captioned_context(article, draft_text).replace("U.S.", "US").replace("U.K.", "UK")
        subjects: list[str] = []
        phrase_pattern = r"\b(?:[A-Z][A-Za-z0-9+.-]{1,}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z0-9+.-]{1,}|[A-Z]{2,})){0,3}\b"
        for match in re.finditer(phrase_pattern, context):
            phrase = normalize_space(match.group(0).strip(" .,;:!?-"))
            if not _is_bad_subject(phrase, article):
                subjects.append(phrase)

        for keyword in _ordered_keywords(context)[:8]:
            if keyword not in _source_terms(article):
                subjects.append(keyword)

        return self._dedupe_subjects(subjects)

    def _dedupe_subjects(self, subjects: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for subject in subjects:
            value = normalize_space(subject)
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped



    def _round_robin_candidates(self, buckets: list[list[str]], limit: int) -> list[str]:
        candidates: list[str] = []
        max_len = max((len(bucket) for bucket in buckets), default=0)
        for index in range(max_len):
            for bucket in buckets:
                if index < len(bucket):
                    candidates.append(bucket[index])
                    if len(candidates) >= limit:
                        return candidates
        return candidates

    def _dedupe_urls(self, candidates: list[str], limit: int) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for url in candidates:
            if url in seen or not self._is_usable_image_url(url):
                continue
            seen.add(url)
            deduped.append(url)
            if len(deduped) >= limit:
                break
        return deduped

    def _dedupe_queries(self, queries: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for query in queries:
            clean = normalize_space(query)
            key = clean.lower()
            if not clean or key in seen:
                continue
            seen.add(key)
            deduped.append(clean)
        return deduped

    def _ensure_resolved_article_url(self, article: Article) -> None:
        if article.resolved_url:
            return
        resolved = self._resolve_article_url(str(article.url))
        if resolved:
            article.resolved_url = _valid_http_url(resolved)

    def _resolve_article_url(self, url: str) -> str | None:
        try:
            response = httpx.get(
                url,
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
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



    def _content_match_score(self, image_url: str, descriptor: str, keywords: set[str]) -> int:
        haystack = f"{image_url} {descriptor}".lower()
        score = 0
        for keyword in keywords:
            if keyword in haystack:
                score += 3 if len(keyword) > 3 else 1
        return score

    def _is_source_only_image(
        self,
        image_url: str,
        descriptor: str,
        article: Article,
        subject_keywords: set[str],
    ) -> bool:
        haystack = f"{_host(image_url)} {urlparse(image_url).path} {descriptor}".lower()
        source_terms = _source_terms(article)
        has_source = any(term in haystack for term in source_terms)
        has_subject = any(keyword in haystack for keyword in subject_keywords if len(keyword) > 2)
        if has_source and not has_subject:
            return True
        return has_source and "logo" in haystack and not has_subject

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

    def _serpapi_images(
        self,
        query: str,
        article: Article,
        subject_keywords: set[str],
        limit: int,
    ) -> list[str]:
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
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []

        data = response.json()
        ranked: list[tuple[int, str]] = []
        for item in data.get("images_results", []):
            original = item.get("original")
            if not original or not self._is_usable_image_url(original):
                continue
            descriptor = f"{item.get('title') or ''} {item.get('source') or ''}"
            if self._is_source_only_image(original, descriptor, article, subject_keywords):
                continue
            score = self._content_match_score(original, descriptor, subject_keywords)
            ranked.append((score, original))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in ranked[:limit]]

    def _duckduckgo_images(
        self,
        query: str,
        article: Article,
        subject_keywords: set[str],
        limit: int,
    ) -> list[str]:
        if getattr(self, "_ddg_disabled", False):
            return []

        try:
            response = httpx.get(
                "https://duckduckgo.com/",
                params={"q": query},
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            self._ddg_disabled = True
            return []

        match = re.search(r"vqd=['\"]([^'\"]+)['\"]", response.text)
        if not match:
            return []

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
                trust_env=False,
            )
            image_response.raise_for_status()
        except httpx.HTTPError:
            self._ddg_disabled = True
            return []

        ranked: list[tuple[int, str]] = []
        data = image_response.json()
        for item in data.get("results", []):
            image = item.get("image")
            if not image or not self._is_usable_image_url(image):
                continue

            width = str(item.get("width") or "")
            height = str(item.get("height") or "")
            descriptor = f"{item.get('title') or ''} {item.get('source') or ''} {item.get('url') or ''}"
            if self._is_source_only_image(image, descriptor, article, subject_keywords):
                continue

            score = _dimension_score(image, width, height)
            score += self._content_match_score(image, descriptor, subject_keywords)
            if score <= 0:
                score = 1
            ranked.append((score, image))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in ranked[:limit]]

    def _wikipedia_images(self, query: str, limit: int) -> list[str]:
        try:
            search_resp = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "utf8": "",
                    "format": "json",
                    "srlimit": 1
                },
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            search_resp.raise_for_status()
            
            search_results = search_resp.json().get("query", {}).get("search", [])
            if not search_results:
                return []
                
            title = search_results[0].get("title")
            if not title:
                return []
                
            img_resp = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "pageimages",
                    "format": "json",
                    "piprop": "thumbnail|original",
                    "pithumbsize": 1000,
                    "titles": title
                },
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            img_resp.raise_for_status()
            
            pages = img_resp.json().get("query", {}).get("pages", {})
            for page_info in pages.values():
                original = page_info.get("original", {}).get("source")
                if original and self._is_usable_image_url(original):
                    return [original]
                    
                thumb = page_info.get("thumbnail", {}).get("source")
                if thumb and self._is_usable_image_url(thumb):
                    return [thumb]
                    
            return []
        except Exception:
            return []

    def _pollinations_images(self, query: str) -> str:
        # Appending a generic style hint helps the generator create better images
        prompt = f"{query}, high quality photorealistic"
        encoded = quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?nologo=true"

    def _wikimedia_commons_images(self, query: str, limit: int) -> list[str]:
        try:
            search_resp = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srnamespace": 6,  # File namespace
                    "utf8": "",
                    "format": "json",
                    "srlimit": limit
                },
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            search_resp.raise_for_status()
            
            search_results = search_resp.json().get("query", {}).get("search", [])
            if not search_results:
                return []
                
            titles = "|".join([res.get("title") for res in search_results if res.get("title")])
            if not titles:
                return []
                
            img_resp = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": titles,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": 1000,
                    "format": "json",
                },
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 TwitterAutomationAgent/0.1"},
                trust_env=False,
            )
            img_resp.raise_for_status()
            
            urls = []
            pages = img_resp.json().get("query", {}).get("pages", {})
            for page_info in pages.values():
                imageinfo = page_info.get("imageinfo", [])
                if imageinfo:
                    source = imageinfo[0].get("url")
                    if source and self._is_usable_image_url(source):
                        urls.append(source)
                    else:
                        thumb = imageinfo[0].get("thumburl")
                        if thumb and self._is_usable_image_url(thumb):
                            urls.append(thumb)
                            
                    if len(urls) >= limit:
                            break
            return urls
        except Exception:
            return []