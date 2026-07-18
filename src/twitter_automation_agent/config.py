from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from twitter_automation_agent.models import DraftStyle


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: str = "ollama"
    dialog_llm_provider: str = "openrouter"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    huggingface_api_token: str | None = None
    huggingface_model: str = "mistralai/Mistral-7B-Instruct-v0.3"
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4o-mini"

    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    twitter_handle: str | None = None

    serpapi_api_key: str | None = None
    
    newsdata_api_key: str | None = None
    newsapi_api_key: str | None = None
    mediastack_api_key: str | None = None

    news_lookback_hours: int = Field(default=24, ge=1, le=168)
    max_articles: int = Field(default=80, ge=1, le=200)
    default_style: DraftStyle = DraftStyle.ragebait

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

    @property
    def can_send_to_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()