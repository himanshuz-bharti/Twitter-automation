from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

import httpx
from pydantic import BaseModel

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import DraftItem


class CacheEntry(BaseModel):
    item: DraftItem
    message_id: str | None = None
    prefix: str | None = None
    is_debate: bool = False
    reply: bool = False
    image_message_ids: list[str] = []


_DRAFT_CACHE_FILE = "outputs/active_drafts.json"
_draft_cache: dict[str, CacheEntry] = {}


def _load_draft_cache() -> None:
    global _draft_cache
    if not os.path.exists(_DRAFT_CACHE_FILE):
        return
    try:
        with open(_DRAFT_CACHE_FILE, "r") as f:
            data = json.load(f)
            for k, v in data.items():
                _draft_cache[k] = CacheEntry.model_validate(v)
    except Exception as e:
        print(f"[DEBUG] Failed to load draft cache: {e}")


def _save_draft_cache() -> None:
    try:
        os.makedirs(os.path.dirname(_DRAFT_CACHE_FILE), exist_ok=True)
        serialized = {k: v.model_dump() for k, v in _draft_cache.items()}
        with open(_DRAFT_CACHE_FILE, "w") as f:
            json.dump(serialized, f, indent=2)
    except Exception as e:
        print(f"[DEBUG] Failed to save draft cache: {e}")


def cache_draft(draft_id: str, entry: CacheEntry) -> None:
    _load_draft_cache()
    _draft_cache[draft_id] = entry
    _save_draft_cache()


def get_cached_draft(draft_id: str) -> CacheEntry | None:
    _load_draft_cache()
    return _draft_cache.get(draft_id)


def get_draft_by_message_id(message_id: str) -> tuple[str, CacheEntry] | None:
    _load_draft_cache()
    for draft_id, entry in _draft_cache.items():
        if entry.message_id == str(message_id):
            return draft_id, entry
    return None


def remove_cached_draft(draft_id: str) -> None:
    _load_draft_cache()
    if draft_id in _draft_cache:
        del _draft_cache[draft_id]
        _save_draft_cache()


def update_draft_text(item: DraftItem, new_text: str) -> None:
    if item.draft.is_thread:
        # Split by thread separator "🧵 Next:"
        parts = [p.strip() for p in re.split(r'(?:\n\s*)*🧵\s*Next:\s*(?:\n\s*)*', new_text)]
        if len(parts) > 1:
            item.draft.thread_texts = parts
            item.draft.text = parts[0]
        else:
            item.draft.text = new_text
            item.draft.thread_texts = [new_text]
    else:
        item.draft.text = new_text


def format_draft_message_text(item: DraftItem, prefix: str | None = None) -> str:
    is_debate_action = ("x.com" in str(item.article.url) or "twitter.com" in str(item.article.url)) and str(item.article.url) not in item.draft.text

    if prefix:
        return f"{prefix}\n\n{item.draft.text}"
    elif is_debate_action:
        return f"💬 Link: {item.article.url}\n\n{item.draft.text}"
    elif item.draft.is_thread and item.draft.thread_texts:
        return "\n\n🧵 Next:\n\n".join(item.draft.thread_texts)
    else:
        return item.draft.text



class TelegramSender:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.bot_username: str | None = None

    def get_bot_username(self) -> str:
        if self.bot_username:
            return self.bot_username
        try:
            bot = self._request("getMe").json().get("result", {})
            self.bot_username = bot.get("username")
            return self.bot_username
        except Exception:
            return "bot"

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self._request(
            "deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
        )

    def set_my_commands(self) -> None:
        if not self.settings.can_send_to_telegram:
            return
        commands = [
            {"command": "post", "description": "Schedule & post tweets to X"},
            {"command": "draft", "description": "Generate draft ideas (no posting)"},
            {"command": "debate", "description": "Scrape viral tweets & draft counter-arguments"},
            {"command": "reply", "description": "Scrape viral tweets & reply with counter-arguments"},
            {"command": "quote", "description": "Scrape viral tweets & quote with counter-arguments"},
            {"command": "mix", "description": "Scrape viral tweets & post both direct reply and quote-tweet"},
            {"command": "status", "description": "Check if the bot is alive"},
            {"command": "cancel", "description": "Cancel the current questionnaire"},
            {"command": "quit", "description": "Shut down the bot completely"},
            {"command": "help", "description": "Show help message"}
        ]
        self._request("setMyCommands", json={"commands": commands})

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
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
        self.bot_username = bot_username
        chat = self._request("getChat", json={"chat_id": self.settings.telegram_chat_id}).json().get(
            "result",
            {},
        )
        chat_label = chat.get("title") or chat.get("username") or chat.get("first_name")
        chat_id = str(chat.get("id")) if chat.get("id") is not None else chat_label
        return bot_username, chat_id

    def get_file_path(self, file_id: str) -> str:
        """Get the file path from Telegram using the file_id."""
        response = self._request("getFile", json={"file_id": file_id})
        data = response.json()
        result = data.get("result", {})
        file_path = result.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram API did not return a file_path.")
        return file_path

    def download_file(self, file_path: str) -> bytes:
        """Download the file from Telegram."""
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Telegram bot token is missing.")
            
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        try:
            response = httpx.get(url, timeout=self.timeout, trust_env=False)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to download file from Telegram: {exc}") from exc

    def send_draft(
        self,
        item: DraftItem,
        index: int | None = None,
        total: int | None = None,
        chat_id: str | None = None,
        prefix: str | None = None,
        interactive: bool = False,
        is_debate: bool = False,
        reply: bool = False,
    ) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")

        reply_markup = None
        draft_id = None
        if interactive:
            draft_id = f"dr_{uuid.uuid4().hex[:8]}"
            entry = CacheEntry(
                item=item,
                prefix=prefix,
                is_debate=is_debate,
                reply=reply,
            )
            cache_draft(draft_id, entry)

            import urllib.parse
            if item.draft.is_thread and item.draft.thread_texts:
                draft_content = "\n\n🧵 Next:\n\n".join(item.draft.thread_texts)
            else:
                draft_content = item.draft.text
            prefilled_text = f"{draft_content}\n\n(Draft ID: {draft_id})"
            encoded_text = urllib.parse.quote(prefilled_text)
            bot_username = self.get_bot_username()

            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 Post to X", "callback_data": f"post_{draft_id}"},
                        {"text": "✍️ Edit Text", "url": f"https://t.me/{bot_username}?text={encoded_text}"},
                        {"text": "❌ Discard", "callback_data": f"discard_{draft_id}"}
                    ]
                ]
            }

        full_text = format_draft_message_text(item, prefix)
        message_id = self.send_text(full_text, chat_id=chat_id, reply_markup=reply_markup)

        image_paths = [suggestion.path for suggestion in item.draft.image_suggestions]
        if not image_paths and item.draft.image_paths:
            image_paths = item.draft.image_paths

        sent_image_ids = []
        total_images = len(image_paths)
        for image_index, image_path in enumerate(image_paths, start=1):
            try:
                img_msg_id = self._send_photo(
                    Path(image_path),
                    caption=f"Image {image_index}/{total_images}",
                    chat_id=chat_id,
                )
                if img_msg_id and img_msg_id != "sent":
                    sent_image_ids.append(img_msg_id)
            except Exception as e:
                print(f"[DEBUG] Failed to send photo {image_index} to Telegram: {e}")

        if interactive and draft_id:
            entry = get_cached_draft(draft_id)
            if entry:
                if message_id and message_id != "sent":
                    entry.message_id = message_id
                entry.image_message_ids = sent_image_ids
                cache_draft(draft_id, entry)

        return message_id

    def send_text(
        self,
        text: str,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are not fully configured.")
            
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            
        response = self._request("sendMessage", json=payload)
        result = response.json().get("result", {})
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else "sent"

    def answer_callback_query(self, callback_query_id: str, text: str | None = None, show_alert: bool = False) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        self._request("answerCallbackQuery", json=payload)

    def edit_message_text(
        self,
        text: str,
        message_id: str,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "message_id": int(message_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}  # removes inline keyboard
        self._request("editMessageText", json=payload)

    def delete_message(self, message_id: str, chat_id: str | None = None) -> None:
        try:
            self._request(
                "deleteMessage",
                json={
                    "chat_id": chat_id or self.settings.telegram_chat_id,
                    "message_id": int(message_id),
                },
            )
        except Exception as e:
            print(f"[DEBUG] Failed to delete message {message_id}: {e}")

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
