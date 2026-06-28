from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.drafter import TweetDrafter
from twitter_automation_agent.images import ImageFinder
from twitter_automation_agent.models import BatchPipelineResult, DraftItem, DraftStyle
from twitter_automation_agent.news import NewsCollector
from twitter_automation_agent.publisher import XPublisher

HttpUrlAdapter = TypeAdapter(HttpUrl)


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
    ) -> BatchPipelineResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        history = self._load_history(output_dir) if skip_history else {"urls": [], "titles": []}
        target = topic or "trending tech news"

        articles = self.news.collect(
            topic=topic,
            lookback_hours=self.settings.news_lookback_hours,
            limit=max(self.settings.max_articles, count * 4),
        )
        fresh_articles = self._filter_history(articles, history)
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
        self._append_history(output_dir, drafts)
        return result

    def _filter_history(self, articles: list, history: dict[str, list[str]]) -> list:
        seen_urls = set(history.get("urls", []))
        seen_titles = set(history.get("titles", []))
        fresh = []
        for article in articles:
            title_key = _title_fingerprint(article.title)
            if str(article.url) in seen_urls or title_key in seen_titles:
                continue
            fresh.append(article)
        return fresh

    def _history_path(self, output_dir: Path) -> Path:
        return output_dir / "history.json"

    def _load_history(self, output_dir: Path) -> dict[str, list[str]]:
        path = self._history_path(output_dir)
        if not path.exists():
            return {"urls": [], "titles": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"urls": [], "titles": []}

        return {
            "urls": list(data.get("urls", [])),
            "titles": list(data.get("titles", [])),
        }

    def _append_history(self, output_dir: Path, drafts: list[DraftItem]) -> None:
        history = self._load_history(output_dir)
        urls = set(history.get("urls", []))
        titles = set(history.get("titles", []))

        for item in drafts:
            urls.add(str(item.article.url))
            titles.add(_title_fingerprint(item.article.title))

        path = self._history_path(output_dir)
        path.write_text(
            json.dumps(
                {
                    "urls": sorted(urls),
                    "titles": sorted(titles),
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
