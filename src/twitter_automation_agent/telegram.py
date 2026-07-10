from __future__ import annotations

from pathlib import Path

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import DraftItem


class TelegramSender:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self._request(
            "deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
        )

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset

        data = self._request("getUpdates", json=payload).json()
        updates = data.get("result", [])
        return updates if isinstance(updates, list) else []

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

    def send_draft(
        self,
        item: DraftItem,
        index: int | None = None,
        total: int | None = None,
        chat_id: str | None = None,
    ) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")

        message_id = self.send_text(item.draft.text, chat_id=chat_id)
        image_paths = [suggestion.path for suggestion in item.draft.image_suggestions]
        if not image_paths and item.draft.image_paths:
            image_paths = item.draft.image_paths

        total_images = len(image_paths)
        for image_index, image_path in enumerate(image_paths, start=1):
            self._send_photo(
                Path(image_path),
                caption=f"Image {image_index}/{total_images}",
                chat_id=chat_id,
            )

        return message_id

    def send_text(self, text: str, chat_id: str | None = None) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")
        response = self._request(
            "sendMessage",
            json={
                "chat_id": chat_id or self.settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        result = response.json().get("result", {})
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else "sent"

    def _send_photo(self, image_path: Path, caption: str, chat_id: str | None = None) -> str:
        with image_path.open("rb") as image_file:
            response = self._request(
                "sendPhoto",
                data={"chat_id": chat_id or self.settings.telegram_chat_id, "caption": caption},
                files={"photo": (image_path.name, image_file)},
            )
        result = response.json().get("result", {})
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else "sent"

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
