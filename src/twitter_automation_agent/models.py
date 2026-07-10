from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class DraftStyle(StrEnum):
    neutral = "neutral"
    sharp = "sharp"
    spicy = "spicy"
    ragebait = "ragebait"


class Article(BaseModel):
    title: str
    url: HttpUrl
    source: str
    publisher: str | None = None
    publisher_url: HttpUrl | None = None
    resolved_url: HttpUrl | None = None
    published_at: datetime | None = None
    summary: str | None = None
    image_url: HttpUrl | None = None
    score: float = 0.0


class ImageSuggestion(BaseModel):
    url: HttpUrl
    path: str


class TweetDraft(BaseModel):
    text: str = Field(..., max_length=280)
    style: DraftStyle
    article: Article
    image_url: HttpUrl | None = None
    image_paths: list[str] = Field(default_factory=list)
    image_suggestions: list[ImageSuggestion] = Field(default_factory=list)
    rationale: str | None = None


class DraftItem(BaseModel):
    article: Article
    draft: TweetDraft
    posted: bool = False
    post_id: str | None = None


class BatchPipelineResult(BaseModel):
    topic: str
    generated_at: datetime
    candidates: list[Article]
    drafts: list[DraftItem]