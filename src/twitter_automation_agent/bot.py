from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from rich.console import Console

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import DraftStyle
from twitter_automation_agent.news import get_trending_topics, get_trending_genres
from twitter_automation_agent.pipeline import Pipeline
from twitter_automation_agent.telegram import TelegramSender

HELP_TEXT = """Commands:
/topic <topic> [count] - send drafts for a topic
/trending [count] - send trending tech drafts
/post <topic> [--posts <num>] [--interval <mins>] - instantly post or schedule multiple tweets
/draft - interactively generate drafts for a topic
/status - check that the bot is alive
/cancel - cancel current conversation
/help - show this message

Examples:
/topic Microsoft 3
/post "AI models"
/post "Nvidia" --posts 3 --interval 60
/trending 3
""".strip()


class ConversationState(Enum):
    IDLE = auto()
    DIALOG = auto()


@dataclass(frozen=True)
class BotCommand:
    name: str
    category: str = "Tech"
    topic: str | None = None
    count: int = 3
    style: DraftStyle | None = None
    include_seen: bool = False
    posts: int = 1
    interval_minutes: float = 90.0
    is_thread: bool = False
    thread_length: int = 4


class TelegramCommandBot:
    def __init__(
        self,
        settings: Settings,
        output_dir: Path = Path("outputs"),
        default_count: int = 3,
        max_count: int = 10,
        poll_timeout: int = 30,
        drop_pending_updates: bool = False,
        console: Console | None = None,
    ) -> None:
        self.settings = settings
        self.output_dir = output_dir
        self.default_count = default_count
        self.max_count = max_count
        self.poll_timeout = poll_timeout
        self.drop_pending_updates = drop_pending_updates
        self.telegram = TelegramSender(settings)
        self.console = console or Console()
        self._busy = False
        
        self._state = ConversationState.IDLE
        self._dialog_slots = {"action": None, "topic": None, "count": None}

    def listen(self) -> None:
        if not self.settings.can_send_to_telegram:
            raise RuntimeError("Telegram credentials are incomplete.")

        self.telegram.delete_webhook(drop_pending_updates=self.drop_pending_updates)
        self.telegram.set_my_commands()
        self.console.print("[bold]Telegram command listener started.[/bold]")
        self.telegram.send_text(
            "Bot listener is online. Tap the Menu button to see commands.",
            chat_id=self.settings.telegram_chat_id,
        )

        offset: int | None = None
        while True:
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=self.poll_timeout)
            except RuntimeError as exc:
                self.console.print(f"[red]Telegram polling failed:[/red] {exc}")
                time.sleep(5)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    self._handle_update(update)
                except Exception as exc:
                    self.console.print(f"[red]Error handling update:[/red] {exc}")

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return

        chat_id = str(chat.get("id"))
        if chat_id != str(self.settings.telegram_chat_id):
            return

        text = str(message.get("text") or "").strip()
        voice = message.get("voice")

        if voice:
            self.telegram.send_text("🎙️ Processing voice command...", chat_id=chat_id)
            try:
                file_id = voice.get("file_id")
                file_path = self.telegram.get_file_path(file_id)
                audio_data = self.telegram.download_file(file_path)
                
                from twitter_automation_agent.llm import LLMClient
                llm = LLMClient(self.settings)
                
                transcription = llm.transcribe_audio_local(audio_data)
                if not transcription:
                    self.telegram.send_text("❌ Failed to transcribe audio locally. Ensure ffmpeg is installed and working.", chat_id=chat_id)
                    return
                    
                self.telegram.send_text(f"🗣️ You said: {transcription}", chat_id=chat_id)
                text = transcription  # Treat the transcription as the text message
                
            except Exception as e:
                self.telegram.send_text(f"Error processing voice: {e}", chat_id=chat_id)
                return

        if not text:
            return

        if text.lower() == "/cancel":
            self._state = ConversationState.IDLE
            self._dialog_slots = {"action": None, "topic": None, "count": None}
            self.telegram.send_text("Conversation cancelled.", chat_id=chat_id)
            return
            
        # Route to commands if starting with /
        if text.startswith("/"):
            try:
                command = self._parse_command(text)
            except ValueError as exc:
                self.telegram.send_text(str(exc), chat_id=chat_id)
                return
                
            if command.name == "help":
                self.telegram.send_text(HELP_TEXT, chat_id=chat_id)
                return
            if command.name == "status":
                self.telegram.send_text("Bot is online.", chat_id=chat_id)
                return
            if command.name == "quit":
                self.telegram.send_text("Shutting down bot...", chat_id=chat_id)
                import sys
                sys.exit(0)
                
            if command.name == "interactive_post":
                self._state = ConversationState.DIALOG
                self._dialog_slots = {"action": "tweet", "topic": None, "count": None}
                self.telegram.send_text("What topic do you want to post about?", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
                
            if command.name == "interactive_draft":
                self._state = ConversationState.DIALOG
                self._dialog_slots = {"action": "tweet", "topic": None, "count": None}
                self.telegram.send_text("What topic do you want to draft a post about?", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
                
            # It's a fully formed command, execute it directly
            self._state = ConversationState.IDLE
            self._dialog_slots = {"action": None, "topic": None, "count": None}
            self._dispatch_command(command, chat_id)
            return

        # Any conversational text/voice goes to Dialog Manager
        self._handle_dialog(text, chat_id)

    def _handle_dialog(self, text: str, chat_id: str) -> None:
        self._state = ConversationState.DIALOG
        self.telegram.send_text("🧠 Understanding context...", chat_id=chat_id)
        
        from twitter_automation_agent.llm import LLMClient
        llm = LLMClient(self.settings)
        
        try:
            result = llm.dialog_manager_step(self._dialog_slots, text)
        except Exception as e:
            self.telegram.send_text(f"Error communicating with LLM: {e}", chat_id=chat_id)
            return
            
        self._dialog_slots = result.get("updated_slots", self._dialog_slots)
        reply = result.get("reply")
        
        if reply:
            self.telegram.send_text(reply, chat_id=chat_id)
            return
            
        action = self._dialog_slots.get("action")
        topic = self._dialog_slots.get("topic")
        count = self._dialog_slots.get("count")
        
        if not action or not topic or (action == "thread" and not count):
            self.telegram.send_text("I need more details (tweet/thread, topic, and count if thread).", chat_id=chat_id)
            return
            
        # All slots are strictly filled!
        
        self._state = ConversationState.IDLE
        self._dialog_slots = {"action": None, "topic": None, "count": None}
        
        self.telegram.send_text(f"Scheduling 1 {action} about '{topic}'...", chat_id=chat_id)
        
        command = BotCommand(
            name="post",
            category="Tech",
            topic=topic,
            count=max(self.default_count, 1),
            include_seen=False,
            posts=1,
            interval_minutes=0,
            is_thread=(action == "thread"),
            thread_length=count or 4
        )
        self._dispatch_command(command, chat_id)



    def _dispatch_command(self, command: BotCommand, chat_id: str) -> None:
        if self._busy:
            self.telegram.send_text("A batch is already running. Please wait for it to finish.", chat_id=chat_id)
            return
            
        self._busy = True
        try:
            self._run_batch(command, chat_id)
        finally:
            self._busy = False

    def _parse_command(self, text: str) -> BotCommand:
        parts = shlex.split(text)
        if not parts:
            raise ValueError(HELP_TEXT)

        raw_name = parts[0].split("@", 1)[0].lower()
        args, count, include_seen, posts, interval = self._parse_options(parts[1:])

        if raw_name in {"/help", "help", "/start", "start"}:
            return BotCommand(name="help")
        if raw_name in {"/status", "status"}:
            return BotCommand(name="status")
        if raw_name in {"/quit", "quit"}:
            return BotCommand(name="quit")
        if raw_name in {"/trending", "trending"}:
            if count is None and args:
                count = self._parse_count(args[0])
            return BotCommand(
                name="batch",
                topic=None,
                count=count or self.default_count,
                include_seen=include_seen,
            )
        if raw_name in {"/topic", "topic"}:
            if not args:
                raise ValueError("Usage: /topic <topic> [count]")
            if count is None and len(args) > 1 and args[-1].isdigit():
                count = self._parse_count(args[-1])
                topic_parts = args[:-1]
            else:
                topic_parts = args
            topic = " ".join(topic_parts).strip()
            if not topic:
                raise ValueError("Usage: /topic <topic> [count]")
            return BotCommand(
                name="batch",
                topic=topic,
                count=count or self.default_count,
                include_seen=include_seen,
            )
        if raw_name in {"/post", "post"}:
            if not args:
                return BotCommand(name="interactive_post")
                
            topic = " ".join(args).strip()
            
            # Route internally based on whether they requested multiple scheduled posts
            internal_command_name = "autopost" if posts > 1 else "post"
            
            return BotCommand(
                name=internal_command_name,
                topic=topic,
                count=count or (self.default_count if posts > 1 else 1),
                include_seen=include_seen,
                posts=posts,
                interval_minutes=interval,
            )

        if raw_name in {"/draft", "draft"}:
            return BotCommand(name="interactive_draft")

        raise ValueError(HELP_TEXT)

    def _parse_options(self, args: list[str]) -> tuple[list[str], int | None, bool, int, float]:
        topic_parts: list[str] = []
        count: int | None = None
        include_seen = False
        posts = 1
        interval = 90.0
        
        index = 0
        while index < len(args):
            arg = args[index]
            lowered = arg.lower()
            if lowered in {"--include-seen", "include-seen"}:
                include_seen = True
            elif lowered in {"--count", "-c"}:
                index += 1
                if index >= len(args):
                    raise ValueError("Usage: --count <number>")
                count = self._parse_count(args[index])
            elif lowered in {"--posts", "-p"}:
                index += 1
                if index >= len(args):
                    raise ValueError("Usage: --posts <number>")
                posts = self._parse_count(args[index])
            elif lowered in {"--interval", "-i"}:
                index += 1
                if index >= len(args):
                    raise ValueError("Usage: --interval <minutes>")
                try:
                    interval = float(args[index])
                except ValueError:
                    raise ValueError("Interval must be a number.")
            else:
                topic_parts.append(arg)
            index += 1
        return topic_parts, count, include_seen, posts, interval
    def _parse_count(self, raw: str) -> int:
        try:
            count = int(raw)
        except ValueError as exc:
            raise ValueError(f"Count must be a number from 1 to {self.max_count}.") from exc
        if count < 1 or count > self.max_count:
            raise ValueError(f"Count must be from 1 to {self.max_count}.")
        return count

    def _run_batch(self, command: BotCommand, chat_id: str) -> None:
        if command.name == "post":
            self._run_post(command, chat_id)
            return
        if command.name == "autopost":
            self._run_autopost(command, chat_id)
            return

        topic_label = command.topic or f"trending {command.category.lower()} news"
        style = command.style or self.settings.default_style
        self.telegram.send_text(
            f"Building {command.count} draft(s) for {topic_label}. This can take a few minutes.",
            chat_id=chat_id,
        )

        try:
            result = Pipeline(self.settings).send_telegram_batch(
                topic=command.topic,
                style=style,
                output_dir=self.output_dir,
                count=command.count,
                skip_history=not command.include_seen,
                dry_run=False,
                chat_id=chat_id,
                category=command.category,
                is_thread=command.is_thread,
                thread_length=command.thread_length,
            )
        except Exception as exc:
            self.telegram.send_text(f"Batch failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"Done. Sent {len(result.drafts)} draft(s) for {topic_label}.",
            chat_id=chat_id,
        )

    def _run_post(self, command: BotCommand, chat_id: str) -> None:
        topic_label = command.topic or f"trending {command.category.lower()} news"
        style = command.style or self.settings.default_style
        format_label = "thread" if command.is_thread else "tweet"
        
        self.telegram.send_text(
            f"Drafting and auto-posting 1 {format_label} for {topic_label} on your PC...",
            chat_id=chat_id,
        )

        try:
            Pipeline(self.settings).run(
                topic=command.topic,
                style=style,
                output_dir=self.output_dir,
                count=1,
                post=True,
                skip_history=not command.include_seen,
                category=command.category,
                is_thread=command.is_thread,
                thread_length=command.thread_length,
            )
        except Exception as exc:
            self.telegram.send_text(f"Post failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Done! Successfully posted the {format_label} to X from your PC.",
            chat_id=chat_id,
        )

    def _run_autopost(self, command: BotCommand, chat_id: str) -> None:
        topic_label = command.topic or f"trending {command.category.lower()} news"
        style = command.style or self.settings.default_style
        
        queue_size = command.count if command.count > command.posts else command.posts
        
        self.telegram.send_text(
            f"Scheduling {command.posts} post(s) for {topic_label}, spacing them out every {command.interval_minutes} minutes. "
            f"The bot will remain busy processing these in the background on your PC.",
            chat_id=chat_id,
        )

        try:
            Pipeline(self.settings).autopost(
                topic=command.topic,
                style=style,
                output_dir=self.output_dir,
                queue_size=queue_size,
                posts=command.posts,
                interval_minutes=command.interval_minutes,
                skip_history=not command.include_seen,
                category=command.category,
                is_thread=command.is_thread,
                thread_length=command.thread_length,
            )
        except Exception as exc:
            self.telegram.send_text(f"Autopost failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Finished! Successfully posted all {command.posts} scheduled tweets to X.",
            chat_id=chat_id,
        )
