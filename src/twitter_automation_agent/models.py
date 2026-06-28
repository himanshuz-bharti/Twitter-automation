from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class DraftStyle(StrEnum):
    neutral = "neutral"
    sharp = "sharp"
    spicy = "spicy"


class Article(BaseModel):
    title: str
    url: HttpUrl
    source: str
    publisher: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    image_url: HttpUrl | None = None
    score: float = 0.0


class TweetDraft(BaseModel):
    text: str = Field(..., max_length=280)
    style: DraftStyle
    article: Article
    image_url: HttpUrl | None = None
    image_path: str | None = None
    rationale: str | None = None


class PipelineResult(BaseModel):
    topic: str
    generated_at: datetime
    selected_article: Article
    candidates: list[Article]
    draft: TweetDraft
    posted: bool = False
    post_id: str | None = None
