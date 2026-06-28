from __future__ import annotations

from pathlib import Path

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import DraftItem


class TelegramSender:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def verify_credentials(self) -> tuple[str | None, str | None]:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")

        bot = self._request("getMe").json().get("result", {})
        bot_username = bot.get("username")
        chat = self._request("getChat", json={"chat_id": self.settings.telegram_chat_id}).json().get(
            "result",
            {},
        )
        chat_label = chat.get("title") or chat.get("username") or chat.get("first_name")
        chat_id = str(chat.get("id")) if chat.get("id") is not None else chat_label
        return bot_username, chat_id

    def send_draft(self, item: DraftItem, index: int | None = None, total: int | None = None) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")
        if not item.draft.image_path:
            raise RuntimeError("Telegram delivery requires a downloaded image for every draft.")

        label = f"Draft {index}/{total}" if index and total else "Tweet draft"
        message_id = self.send_text(self._draft_message(item, label))
        image_paths = [suggestion.path for suggestion in item.draft.image_suggestions]
        if not image_paths and item.draft.image_path:
            image_paths = [item.draft.image_path]

        total_images = min(len(image_paths), 5)
        for image_index, image_path in enumerate(image_paths[:5], start=1):
            image_id = self._send_photo(
                Path(image_path),
                caption=f"{label} - Image {image_index}/{total_images}",
            )
            if image_index == 1:
                message_id = image_id

        return message_id

    def send_text(self, text: str) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")
        response = self._request(
            "sendMessage",
            json={
                "chat_id": self.settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        result = response.json().get("result", {})
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else "sent"

    def _send_photo(self, image_path: Path, caption: str) -> str:
        with image_path.open("rb") as image_file:
            response = self._request(
                "sendPhoto",
                data={"chat_id": self.settings.telegram_chat_id, "caption": caption},
                files={"photo": (image_path.name, image_file)},
            )
        result = response.json().get("result", {})
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else "sent"

    def _draft_message(self, item: DraftItem, label: str) -> str:
        source_url = str(item.article.resolved_url or item.article.url)
        image_count = len(item.draft.image_suggestions) or (1 if item.draft.image_path else 0)
        parts = [
            label,
            "",
            item.draft.text,
            "",
            f"Images: {min(image_count, 5)} suggestions attached below",
            f"Source: {item.article.source}",
            source_url,
        ]
        return self._fit_message("\n".join(parts), item, label)

    def _fit_message(self, text: str, item: DraftItem, label: str) -> str:
        if len(text) <= 4096:
            return text
        source_url = str(item.article.resolved_url or item.article.url)
        suffix = f"\n\nImages: 5 suggestions attached below\nSource: {item.article.source}\n{source_url}"
        budget = 4096 - len(label) - len("\n\n") - len(suffix) - 3
        shortened_text = item.draft.text[: max(40, budget)].rsplit(" ", 1)[0].rstrip()
        return f"{label}\n\n{shortened_text}...{suffix}"

    def _request(self, method: str, **kwargs: object) -> httpx.Response:
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Telegram bot token is missing.")

        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/{method}",
                timeout=self.timeout,
                trust_env=False,
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._telegram_error(exc.response)
            raise RuntimeError(f"Telegram API rejected {method}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram API request failed for {method}: {exc}") from exc

        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API rejected {method}: {data.get('description', 'unknown error')}")
        return response

    def _telegram_error(self, response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text[:300]
        return str(data.get("description") or data)[:300]