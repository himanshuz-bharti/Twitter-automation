from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.drafter import TweetDrafter
from twitter_automation_agent.images import ImageFinder
from twitter_automation_agent.models import BatchPipelineResult, DraftItem, DraftStyle
from twitter_automation_agent.news import NewsCollector
from twitter_automation_agent.publisher import XPublisher

HttpUrlAdapter = TypeAdapter(HttpUrl)
HistoryScope = Literal["drafted", "posted"]


def _title_fingerprint(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.news = NewsCollector()
        self.drafter = TweetDrafter(settings)
        self.images = ImageFinder(settings)
        self.publisher = XPublisher(settings)

    def run(
        self,
        topic: str | None,
        style: DraftStyle,
        output_dir: Path,
        count: int = 20,
        post: bool = False,
        skip_history: bool = True,
        history_scope: HistoryScope = "drafted",
        record_history: bool = True,
    ) -> BatchPipelineResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        history = self._load_history(output_dir) if skip_history else self._empty_history()
        target = topic or "trending tech news"

        articles = self.news.collect(
            topic=topic,
            lookback_hours=self.settings.news_lookback_hours,
            limit=max(self.settings.max_articles, count * 4),
        )
        fresh_articles = self._filter_history(articles, history, history_scope)
        selected_articles = fresh_articles[:count]

        if not selected_articles:
            raise RuntimeError(
                f"No fresh recent articles found for: {target}. "
                "Use --include-seen to allow articles already generated before."
            )

        drafts: list[DraftItem] = []
        for article in selected_articles:
            draft = self.drafter.draft(article, style)
            image_url = self.images.find(article)
            if image_url:
                draft.image_url = HttpUrlAdapter.validate_python(image_url)
                image_path = self.images.download(image_url, output_dir / "images")
                if image_path:
                    draft.image_path = str(image_path)

            item = DraftItem(article=article, draft=draft)
            if post:
                item.post_id = self.publisher.post(draft.text, draft.image_path)
                item.posted = True
            drafts.append(item)

        result = BatchPipelineResult(
            topic=target,
            generated_at=datetime.now(UTC),
            candidates=articles,
            drafts=drafts,
        )

        self._write_result(result, output_dir)
        if record_history:
            self._append_history(output_dir, drafts, "posted" if post else "drafted")
        return result

    def autopost(
        self,
        topic: str | None,
        style: DraftStyle,
        output_dir: Path,
        queue_size: int = 20,
        posts: int = 20,
        interval_minutes: float = 90.0,
        skip_history: bool = True,
        dry_run: bool = False,
    ) -> BatchPipelineResult:
        result = self.run(
            topic=topic,
            style=style,
            output_dir=output_dir,
            count=queue_size,
            post=False,
            skip_history=skip_history,
            history_scope="posted",
            record_history=False,
        )

        posted_count = 0
        attempted_items: list[DraftItem] = []
        for item in result.drafts:
            if posted_count >= posts:
                break

            if not item.draft.image_path:
                continue

            attempted_items.append(item)
            if dry_run:
                item.posted = False
                item.post_id = "dry-run"
            else:
                item.post_id = self.publisher.post(item.draft.text, item.draft.image_path)
                item.posted = True
                self._append_history(output_dir, [item], "posted")

            posted_count += 1
            if posted_count < posts:
                time.sleep(interval_minutes * 60)

        result.drafts = attempted_items
        if posted_count < posts:
            target = topic or "trending tech news"
            raise RuntimeError(
                f"Only {posted_count} image-backed draft(s) were available for {target}; "
                f"requested {posts}. Try a larger --queue-size or run again later."
            )

        self._write_result(result, output_dir)
        return result

    def _filter_history(
        self,
        articles: list,
        history: dict[str, list[str]],
        scope: HistoryScope,
    ) -> list:
        seen_urls = set(history.get(f"{scope}_urls", []))
        seen_titles = set(history.get(f"{scope}_titles", []))
        fresh = []
        for article in articles:
            title_key = _title_fingerprint(article.title)
            if str(article.url) in seen_urls or title_key in seen_titles:
                continue
            fresh.append(article)
        return fresh

    def _history_path(self, output_dir: Path) -> Path:
        return output_dir / "history.json"

    def _empty_history(self) -> dict[str, list[str]]:
        return {
            "drafted_urls": [],
            "drafted_titles": [],
            "posted_urls": [],
            "posted_titles": [],
        }

    def _load_history(self, output_dir: Path) -> dict[str, list[str]]:
        path = self._history_path(output_dir)
        if not path.exists():
            return self._empty_history()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_history()

        legacy_urls = list(data.get("urls", []))
        legacy_titles = list(data.get("titles", []))

        return {
            "drafted_urls": sorted(set(legacy_urls) | set(data.get("drafted_urls", []))),
            "drafted_titles": sorted(set(legacy_titles) | set(data.get("drafted_titles", []))),
            "posted_urls": list(data.get("posted_urls", [])),
            "posted_titles": list(data.get("posted_titles", [])),
        }

    def _append_history(self, output_dir: Path, drafts: list[DraftItem], scope: HistoryScope) -> None:
        history = self._load_history(output_dir)
        urls = set(history.get(f"{scope}_urls", []))
        titles = set(history.get(f"{scope}_titles", []))

        for item in drafts:
            urls.add(str(item.article.url))
            titles.add(_title_fingerprint(item.article.title))

        history[f"{scope}_urls"] = sorted(urls)
        history[f"{scope}_titles"] = sorted(titles)

        path = self._history_path(output_dir)
        path.write_text(
            json.dumps(
                {
                    "drafted_urls": sorted(set(history.get("drafted_urls", []))),
                    "drafted_titles": sorted(set(history.get("drafted_titles", []))),
                    "posted_urls": sorted(set(history.get("posted_urls", []))),
                    "posted_titles": sorted(set(history.get("posted_titles", []))),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_result(self, result: BatchPipelineResult, output_dir: Path) -> Path:
        timestamp = result.generated_at.strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"draft-batch-{timestamp}.json"
        path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return path
