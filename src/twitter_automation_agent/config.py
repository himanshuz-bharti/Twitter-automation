from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from twitter_automation_agent.models import DraftStyle


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None

    serpapi_api_key: str | None = None

    news_lookback_hours: int = Field(default=24, ge=1, le=168)
    max_articles: int = Field(default=20, ge=1, le=100)
    default_style: DraftStyle = DraftStyle.spicy

    @property
    def can_post_to_x(self) -> bool:
        return all(
            [
                self.x_api_key,
                self.x_api_secret,
                self.x_access_token,
                self.x_access_token_secret,
            ]
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
