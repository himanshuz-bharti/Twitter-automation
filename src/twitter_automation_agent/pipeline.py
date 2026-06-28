from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.drafter import TweetDrafter
from twitter_automation_agent.images import ImageFinder
from twitter_automation_agent.models import DraftStyle, PipelineResult
from twitter_automation_agent.news import NewsCollector
from twitter_automation_agent.publisher import XPublisher

HttpUrlAdapter = TypeAdapter(HttpUrl)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.news = NewsCollector()
        self.drafter = TweetDrafter(settings)
        self.images = ImageFinder(settings)
        self.publisher = XPublisher(settings)

    def run(
        self,
        topic: str,
        style: DraftStyle,
        output_dir: Path,
        post: bool = False,
    ) -> PipelineResult:
        articles = self.news.collect(
            topic=topic,
            lookback_hours=self.settings.news_lookback_hours,
            limit=self.settings.max_articles,
        )
        if not articles:
            raise RuntimeError(f"No recent articles found for topic: {topic}")

        selected = articles[0]
        draft = self.drafter.draft(selected, style)
        image_url = self.images.find(selected)
        if image_url:
            draft.image_url = HttpUrlAdapter.validate_python(image_url)
            image_path = self.images.download(image_url, output_dir / "images")
            if image_path:
                draft.image_path = str(image_path)

        result = PipelineResult(
            topic=topic,
            generated_at=datetime.now(UTC),
            selected_article=selected,
            candidates=articles,
            draft=draft,
        )

        if post:
            result.post_id = self.publisher.post(draft.text, draft.image_path)
            result.posted = True

        self._write_result(result, output_dir)
        return result

    def _write_result(self, result: PipelineResult, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = result.generated_at.strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"draft-{timestamp}.json"
        path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return path
