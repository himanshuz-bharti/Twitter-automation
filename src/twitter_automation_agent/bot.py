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
/debate - interactively scrape viral tweets & draft counter-arguments
/reply - interactively scrape viral tweets & post direct replies to X
/quote - interactively scrape viral tweets & post quote-tweets to X
/mix - interactively scrape viral tweets & post both direct reply AND quote-tweet to X
/post <topic> [--posts <num>] [--interval <mins>] - instantly post or schedule multiple tweets
/draft - interactively generate drafts for a topic
/status - check that the bot is alive
/cancel - cancel current conversation
/help - show this message

Examples:
/debate
/reply
/quote
/mix
/topic Microsoft 3
/post "AI models"
/post "Nvidia" --posts 3 --interval 60""".strip()


class ConversationState(Enum):
    IDLE = auto()
    AWAITING_POST_LANGUAGE = auto()
    AWAITING_POST_CUSTOM_LANGUAGE = auto()
    AWAITING_POST_FORMAT = auto()
    AWAITING_POST_THREAD_LENGTH = auto()
    AWAITING_POST_CATEGORY = auto()
    AWAITING_POST_TOPIC = auto()
    AWAITING_POST_COUNT = auto()
    AWAITING_INTERVAL = auto()
    AWAITING_DRAFT_LANGUAGE = auto()
    AWAITING_DRAFT_CUSTOM_LANGUAGE = auto()
    AWAITING_DRAFT_FORMAT = auto()
    AWAITING_DRAFT_THREAD_LENGTH = auto()
    AWAITING_DRAFT_CATEGORY = auto()
    AWAITING_DRAFT_TOPIC = auto()
    AWAITING_DRAFT_COUNT = auto()
    AWAITING_DEBATE_LANGUAGE = auto()
    AWAITING_DEBATE_CUSTOM_LANGUAGE = auto()
    AWAITING_DEBATE_TOPIC = auto()
    AWAITING_DEBATE_COUNT = auto()
    AWAITING_DEBATE_STANCE = auto()
    AWAITING_DEBATE_MODE = auto()
    AWAITING_DEBATE_LINK = auto()
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
    stance: str = "contradict"
    target_url: str | None = None
    language: str = "English"


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
        self._pending_category: str | None = None
        self._pending_topic: str | None = None
        self._pending_posts: int | None = None
        self._pending_format: str | None = None
        self._pending_thread_length: int | None = None
        self._pending_command: str | None = None
        self._pending_debate_topic: str | None = None
        self._pending_debate_count: int | None = None
        self._pending_debate_target_url: str | None = None
        self._pending_language: str | None = None
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
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback_query(callback_query)
            return

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

        # CHECK IF THIS MESSAGE CONTAINS A DRAFT ID FOR EDITING!
        match = re.search(r'\(Draft ID:\s*(dr_[a-z0-9]+)\)', text)
        if match:
            draft_id = match.group(1)
            from twitter_automation_agent.telegram import (
                get_cached_draft,
                remove_cached_draft,
                update_draft_text,
            )
            entry = get_cached_draft(draft_id)
            if entry:
                clean_text = text.replace(match.group(0), "").strip()
                update_draft_text(entry.item, clean_text)
                
                # Delete old draft message and old images
                if entry.message_id:
                    self.telegram.delete_message(entry.message_id, chat_id=chat_id)
                for img_id in entry.image_message_ids:
                    self.telegram.delete_message(img_id, chat_id=chat_id)
                
                # Try to delete the user's edit message as well
                try:
                    self.telegram.delete_message(str(message.get("message_id")), chat_id=chat_id)
                except Exception:
                    pass
                
                # Remove the old cached entry
                remove_cached_draft(draft_id)
                
                # Resend the updated draft as a new interactive message
                try:
                    self.telegram.send_draft(
                        item=entry.item,
                        chat_id=chat_id,
                        prefix=entry.prefix,
                        interactive=True,
                        is_debate=entry.is_debate,
                        reply=entry.reply
                    )
                    self.telegram.send_text(
                        "✏️ Draft updated successfully!",
                        chat_id=chat_id
                    )
                except Exception as e:
                    self.telegram.send_text(f"⚠️ Failed to send updated draft: {e}", chat_id=chat_id)
                return

        # CHECK IF THIS MESSAGE IS A REPLY TO A DRAFT MESSAGE FOR EDITING!
        reply_to = message.get("reply_to_message")
        if reply_to:
            replied_msg_id = str(reply_to.get("message_id"))
            from twitter_automation_agent.telegram import (
                get_draft_by_message_id,
                remove_cached_draft,
                update_draft_text,
            )
            found = get_draft_by_message_id(replied_msg_id)
            if found:
                old_draft_id, entry = found
                update_draft_text(entry.item, text)
                
                # Delete old draft message and old images
                self.telegram.delete_message(replied_msg_id, chat_id=chat_id)
                for img_id in entry.image_message_ids:
                    self.telegram.delete_message(img_id, chat_id=chat_id)
                
                # Try to delete the user's reply message as well
                try:
                    self.telegram.delete_message(str(message.get("message_id")), chat_id=chat_id)
                except Exception:
                    pass
                
                # Remove the old cached entry
                remove_cached_draft(old_draft_id)
                
                # Resend the updated draft as a new interactive message
                try:
                    self.telegram.send_draft(
                        item=entry.item,
                        chat_id=chat_id,
                        prefix=entry.prefix,
                        interactive=True,
                        is_debate=entry.is_debate,
                        reply=entry.reply
                    )
                    self.telegram.send_text(
                        "✏️ Draft updated successfully!",
                        chat_id=chat_id
                    )
                except Exception as e:
                    self.telegram.send_text(f"⚠️ Failed to send updated draft: {e}", chat_id=chat_id)
                return

        if text.lower() in {"/cancel", "cancel"}:
            self._state = ConversationState.IDLE
            self._pending_topic = None
            self._pending_posts = None
            self._pending_format = None
            self._pending_thread_length = None
            self._pending_command = None
            self._pending_debate_topic = None
            self._pending_language = None
            self._dialog_slots = {"action": None, "topic": None, "count": None}
            self.telegram.send_text("Conversation cancelled.", chat_id=chat_id)
            return

        if self._state not in {ConversationState.IDLE, ConversationState.DIALOG}:
            self._handle_stateful_message(text, chat_id)
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
                
            if command.name in {"interactive_post", "interactive_draft"}:
                if command.name == "interactive_post":
                    self._ask_language(chat_id, ConversationState.AWAITING_POST_LANGUAGE)
                    return
                if command.name == "interactive_draft":
                    self._ask_language(chat_id, ConversationState.AWAITING_DRAFT_LANGUAGE)
                    return

            if command.name in {"debate", "reply", "quote", "mix"}:
                self._pending_command = command.name
                self._ask_language(chat_id, ConversationState.AWAITING_DEBATE_LANGUAGE)
                return
                    
            if self._busy:
                self.telegram.send_text(
                    "A batch is already running. Try again after it finishes.",
                    chat_id=chat_id,
                )
                return

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
        args, count, include_seen, posts, interval, language = self._parse_options(parts[1:])

        if raw_name in {"/help", "help", "/start", "start"}:
            return BotCommand(name="help")
        if raw_name in {"/status", "status"}:
            return BotCommand(name="status")
        if raw_name in {"/quit", "quit"}:
            return BotCommand(name="quit")
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
                language=language,
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
                language=language,
            )

        if raw_name in {"/debate", "debate"}:
            category = None
            parsed_count = self.default_count
            if args:
                if args[-1].isdigit():
                    parsed_count = self._parse_count(args[-1])
                    category_parts = args[:-1]
                else:
                    category_parts = args
                if category_parts:
                    category = " ".join(category_parts).strip() or None
            return BotCommand(
                name="debate",
                category=category,
                count=parsed_count,
                include_seen=include_seen,
                language=language,
            )


        if raw_name in {"/reply", "reply"}:
            category = None
            parsed_count = self.default_count
            if args:
                if args[-1].isdigit():
                    parsed_count = self._parse_count(args[-1])
                    category_parts = args[:-1]
                else:
                    category_parts = args
                if category_parts:
                    category = " ".join(category_parts).strip() or None
            return BotCommand(
                name="reply",
                category=category,
                count=parsed_count,
                include_seen=include_seen,
                language=language,
            )

        if raw_name in {"/quote", "quote"}:
            category = None
            parsed_count = self.default_count
            if args:
                if args[-1].isdigit():
                    parsed_count = self._parse_count(args[-1])
                    category_parts = args[:-1]
                else:
                    category_parts = args
                if category_parts:
                    category = " ".join(category_parts).strip() or None
            return BotCommand(
                name="quote",
                category=category,
                count=parsed_count,
                include_seen=include_seen,
                language=language,
            )

        if raw_name in {"/mix", "mix", "/both", "both"}:
            category = None
            parsed_count = self.default_count
            if args:
                if args[-1].isdigit():
                    parsed_count = self._parse_count(args[-1])
                    category_parts = args[:-1]
                else:
                    category_parts = args
                if category_parts:
                    category = " ".join(category_parts).strip() or None
            return BotCommand(
                name="mix",
                category=category,
                count=parsed_count,
                include_seen=include_seen,
                language=language,
            )

        if raw_name in {"/draft", "draft"}:
            return BotCommand(name="interactive_draft")

        raise ValueError(HELP_TEXT)

    def _parse_options(self, args: list[str]) -> tuple[list[str], int | None, bool, int, float, str]:
        topic_parts: list[str] = []
        count: int | None = None
        include_seen = False
        posts = 1
        interval = 90.0
        language = self.settings.default_language
        
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
            elif lowered in {"--language", "-l", "--lang"}:
                index += 1
                if index >= len(args):
                    raise ValueError("Usage: --language <language>")
                language = args[index].strip()
            else:
                topic_parts.append(arg)
            index += 1
        return topic_parts, count, include_seen, posts, interval, language
    def _parse_count(self, raw: str) -> int:
        try:
            count = int(raw)
        except ValueError as exc:
            raise ValueError(f"Count must be a number from 1 to {self.max_count}.") from exc
        if count < 1 or count > self.max_count:
            raise ValueError(f"Count must be from 1 to {self.max_count}.")
        return count

    def _run_batch(self, command: BotCommand, chat_id: str) -> None:
        if command.name == "debate":
            self._run_debate(command, chat_id)
            return
        if command.name == "reply":
            self._run_reply(command, chat_id)
            return
        if command.name == "quote":
            self._run_quote(command, chat_id)
            return
        if command.name == "mix":
            self._run_mix(command, chat_id)
            return
        if command.name == "post":
            self._run_post(command, chat_id)
            return
        if command.name == "autopost":
            self._run_autopost(command, chat_id)
            return


        topic_label = self._get_topic_label(command.topic, command.category)
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
                language=command.language,
            )
        except Exception as exc:
            self.telegram.send_text(f"Batch failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"Done. Sent {len(result.drafts)} draft(s) for {topic_label}.",
            chat_id=chat_id,
        )

    def _run_post(self, command: BotCommand, chat_id: str) -> None:
        topic_label = self._get_topic_label(command.topic, command.category)
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
                language=command.language,
            )
        except Exception as exc:
            self.telegram.send_text(f"Post failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Done! Successfully posted the {format_label} to X from your PC.",
            chat_id=chat_id,
        )

    def _run_autopost(self, command: BotCommand, chat_id: str) -> None:
        topic_label = self._get_topic_label(command.topic, command.category)
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
                language=command.language,
            )
        except Exception as exc:
            self.telegram.send_text(f"Autopost failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Finished! Successfully posted all {command.posts} scheduled tweets to X.",
            chat_id=chat_id,
        )

    def _run_debate(self, command: BotCommand, chat_id: str) -> None:
        style = command.style or self.settings.default_style
        category_label = f"Tweet Link" if command.target_url else (command.category or "Global Trends")
        self.telegram.send_text(
            f"Scraping targeted tweet/trends in '{category_label}' and drafting counter-arguments...",
            chat_id=chat_id,
        )
        try:
            result = Pipeline(self.settings).run_debate(
                category=command.category,
                style=style,
                output_dir=self.output_dir,
                count=command.count,
                post=False,
                skip_history=not command.include_seen,
                stance=command.stance,
                target_url=command.target_url,
                language=command.language,
            )
            stance_label = "Support" if command.stance == "support" else "Contradict"
            for item in result.drafts:
                self.telegram.send_draft(
                    item,
                    chat_id=chat_id,
                    prefix=f"💬 Draft Quote Tweet ({stance_label}) for: {item.article.url}",
                    interactive=True,
                    is_debate=True,
                    reply=False,
                )
        except Exception as exc:
            self.telegram.send_text(f"Debate failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Done! Sent {len(result.drafts)} debate draft(s) ({stance_label.lower()}) for '{category_label}'.",
            chat_id=chat_id,
        )


    def _run_reply(self, command: BotCommand, chat_id: str) -> None:
        style = command.style or self.settings.default_style
        category_label = f"Tweet Link" if command.target_url else (command.category or "Global Trends")
        self.telegram.send_text(
            f"Scraping targeted tweet/trends in '{category_label}' and preparing to post direct replies...",
            chat_id=chat_id,
        )
        try:
            result = Pipeline(self.settings).run_debate(
                category=command.category,
                style=style,
                output_dir=self.output_dir,
                count=command.count,
                post=True,
                skip_history=not command.include_seen,
                reply=True,
                stance=command.stance,
                target_url=command.target_url,
                language=command.language,
            )
            stance_label = "Support" if command.stance == "support" else "Contradict"
            for item in result.drafts:
                self.telegram.send_draft(item, chat_id=chat_id, prefix=f"💬 Reply ({stance_label}) to: {item.article.url}")
        except Exception as exc:
            self.telegram.send_text(f"Reply failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Finished! Successfully posted {len(result.drafts)} reply/replies ({stance_label.lower()}) to X for '{category_label}'.",
            chat_id=chat_id,
        )

    def _run_quote(self, command: BotCommand, chat_id: str) -> None:
        style = command.style or self.settings.default_style
        category_label = f"Tweet Link" if command.target_url else (command.category or "Global Trends")
        self.telegram.send_text(
            f"Scraping targeted tweet/trends in '{category_label}' and preparing to post quote-tweets...",
            chat_id=chat_id,
        )
        try:
            result = Pipeline(self.settings).run_debate(
                category=command.category,
                style=style,
                output_dir=self.output_dir,
                count=command.count,
                post=True,
                skip_history=not command.include_seen,
                reply=False,
                stance=command.stance,
                target_url=command.target_url,
                language=command.language,
            )
            stance_label = "Support" if command.stance == "support" else "Contradict"
            for item in result.drafts:
                self.telegram.send_draft(item, chat_id=chat_id, prefix=f"💬 Quote ({stance_label}) of: {item.article.url}")
        except Exception as exc:
            self.telegram.send_text(f"Quote failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Finished! Successfully posted {len(result.drafts)} quote-tweet(s) ({stance_label.lower()}) to X for '{category_label}'.",
            chat_id=chat_id,
        )

    def _run_mix(self, command: BotCommand, chat_id: str) -> None:
        style = command.style or self.settings.default_style
        category_label = f"Tweet Link" if command.target_url else (command.category or "Global Trends")
        self.telegram.send_text(
            f"Scraping targeted tweet/trends in '{category_label}' and preparing to post direct replies AND quote-tweets...",
            chat_id=chat_id,
        )
        try:
            result = Pipeline(self.settings).run_debate(
                category=command.category,
                style=style,
                output_dir=self.output_dir,
                count=command.count,
                post=True,
                skip_history=not command.include_seen,
                reply=False,
                mix=True,
                stance=command.stance,
                target_url=command.target_url,
                language=command.language,
            )
            stance_label = "Support" if command.stance == "support" else "Contradict"
            for item in result.drafts:
                self.telegram.send_draft(item, chat_id=chat_id, prefix=f"💬 Reply & Quote ({stance_label}) for: {item.article.url}")
        except Exception as exc:
            self.telegram.send_text(f"Mix failed: {exc}", chat_id=chat_id)
            return

        self.telegram.send_text(
            f"✅ Finished! Successfully posted {len(result.drafts)} reply-and-quote pairs ({stance_label.lower()}) to X for '{category_label}'.",
            chat_id=chat_id,
        )



    def _handle_stateful_message(self, text: str, chat_id: str) -> None:
        def parse_category(val: str) -> str:
            return val.strip().title()

        def get_topic_keyboard(cat: str) -> dict:
            topics = get_trending_topics(self.settings, cat, limit=4)
            # Arrange in a 2x2 grid if there are 4 items
            grid = []
            if len(topics) >= 4:
                grid = [[{"text": topics[0]}, {"text": topics[1]}], [{"text": topics[2]}, {"text": topics[3]}]]
            elif len(topics) >= 2:
                grid = [[{"text": topics[0]}, {"text": topics[1]}]] + ([[{"text": t}] for t in topics[2:]])
            else:
                grid = [[{"text": t} for t in topics]]
            
            return {
                "keyboard": grid,
                "resize_keyboard": True,
                "one_time_keyboard": True
            }

        if self._state == ConversationState.AWAITING_POST_LANGUAGE:
            val = text.strip()
            if val.lower() == "other":
                self._state = ConversationState.AWAITING_POST_CUSTOM_LANGUAGE
                self.telegram.send_text("Please type the language name (e.g. Hindi, Spanish):", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            self._pending_language = val
            self._state = ConversationState.AWAITING_POST_FORMAT
            keyboard = {
                "keyboard": [[{"text": "Post"}], [{"text": "Thread"}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text("Do you want to create a single Post or a Thread?", chat_id=chat_id, reply_markup=keyboard)
            return

        if self._state == ConversationState.AWAITING_POST_CUSTOM_LANGUAGE:
            self._pending_language = text.strip()
            self._state = ConversationState.AWAITING_POST_FORMAT
            keyboard = {
                "keyboard": [[{"text": "Post"}], [{"text": "Thread"}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text("Do you want to create a single Post or a Thread?", chat_id=chat_id, reply_markup=keyboard)
            return

        if self._state == ConversationState.AWAITING_DRAFT_LANGUAGE:
            val = text.strip()
            if val.lower() == "other":
                self._state = ConversationState.AWAITING_DRAFT_CUSTOM_LANGUAGE
                self.telegram.send_text("Please type the language name (e.g. Hindi, Spanish):", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            self._pending_language = val
            self._state = ConversationState.AWAITING_DRAFT_FORMAT
            keyboard = {
                "keyboard": [[{"text": "Post"}], [{"text": "Thread"}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text("Do you want to create a single Post or a Thread?", chat_id=chat_id, reply_markup=keyboard)
            return

        if self._state == ConversationState.AWAITING_DRAFT_CUSTOM_LANGUAGE:
            self._pending_language = text.strip()
            self._state = ConversationState.AWAITING_DRAFT_FORMAT
            keyboard = {
                "keyboard": [[{"text": "Post"}], [{"text": "Thread"}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text("Do you want to create a single Post or a Thread?", chat_id=chat_id, reply_markup=keyboard)
            return

        if self._state == ConversationState.AWAITING_DEBATE_LANGUAGE:
            val = text.strip()
            if val.lower() == "other":
                self._state = ConversationState.AWAITING_DEBATE_CUSTOM_LANGUAGE
                self.telegram.send_text("Please type the language name (e.g. Hindi, Spanish):", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            self._pending_language = val
            self._state = ConversationState.AWAITING_DEBATE_MODE
            keyboard = {
                "keyboard": [
                    [{"text": "Topic"}],
                    [{"text": "Tweet Link"}]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text(
                "🔍 Would you like to debate/reply/mix by Topic or target a specific Tweet Link?",
                chat_id=chat_id,
                reply_markup=keyboard
            )
            return

        if self._state == ConversationState.AWAITING_DEBATE_CUSTOM_LANGUAGE:
            self._pending_language = text.strip()
            self._state = ConversationState.AWAITING_DEBATE_MODE
            keyboard = {
                "keyboard": [
                    [{"text": "Topic"}],
                    [{"text": "Tweet Link"}]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text(
                "🔍 Would you like to debate/reply/mix by Topic or target a specific Tweet Link?",
                chat_id=chat_id,
                reply_markup=keyboard
            )
            return

        if self._state == ConversationState.AWAITING_DEBATE_MODE:
            val = text.strip().lower()
            if val in {"topic", "by topic"}:
                self._state = ConversationState.AWAITING_DEBATE_TOPIC
                keyboard = {
                    "keyboard": [
                        [{"text": "Skip / Global Trends"}],
                        [{"text": "AI"}, {"text": "Crypto"}],
                        [{"text": "Tech"}, {"text": "Finance"}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                self.telegram.send_text(
                    "💬 Enter a topic/category to search (e.g. AI, Crypto, Tech), or tap 'Skip / Global Trends' to search general trends:",
                    chat_id=chat_id,
                    reply_markup=keyboard
                )
            elif val in {"tweet link", "link", "post link"}:
                self._state = ConversationState.AWAITING_DEBATE_LINK
                self.telegram.send_text(
                    "🔗 Please send/paste the link of the X/Twitter post you want to target (e.g. https://x.com/username/status/123456):",
                    chat_id=chat_id,
                    reply_markup={"remove_keyboard": True}
                )
            else:
                self.telegram.send_text("Please choose either 'Topic' or 'Tweet Link'.", chat_id=chat_id)
            return

        if self._state == ConversationState.AWAITING_DEBATE_LINK:
            val = text.strip()
            # Simple regex verification for X status URL
            match = re.search(r'(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)', val)
            if not match:
                self.telegram.send_text(
                    "⚠️ Invalid link format. Please make sure the URL contains 'x.com/username/status/123456'. Try again:",
                    chat_id=chat_id
                )
                return
            
            self._pending_debate_target_url = val
            self._pending_debate_count = 1
            self._state = ConversationState.AWAITING_DEBATE_STANCE
            
            keyboard = {
                "keyboard": [
                    [{"text": "Support"}],
                    [{"text": "Contradict"}]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text(
                "⚖️ Would you like to Support or Contradict this tweet?",
                chat_id=chat_id,
                reply_markup=keyboard
            )
            return

        if self._state == ConversationState.AWAITING_DEBATE_TOPIC:
            val = text.strip()
            if val.lower() in {"skip / global trends", "skip", "none", "all", "global"}:
                self._pending_debate_topic = None
            else:
                self._pending_debate_topic = val
            
            self._state = ConversationState.AWAITING_DEBATE_COUNT
            self.telegram.send_text(
                "🔢 How many counter-argument drafts/posts do you want to generate? (Send a number between 1 and 10):",
                chat_id=chat_id,
                reply_markup={"remove_keyboard": True}
            )
            return

        if self._state == ConversationState.AWAITING_DEBATE_COUNT:
            try:
                count = int(text.strip())
                if count < 1 or count > 10:
                    raise ValueError()
            except ValueError:
                self.telegram.send_text("Please send a valid positive number between 1 and 10 for the count.", chat_id=chat_id)
                return
            
            self._pending_debate_count = count
            self._state = ConversationState.AWAITING_DEBATE_STANCE
            keyboard = {
                "keyboard": [
                    [{"text": "Support"}],
                    [{"text": "Contradict"}]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            self.telegram.send_text(
                "⚖️ Would you like to Support or Contradict the tweets?",
                chat_id=chat_id,
                reply_markup=keyboard
            )
            return

        if self._state == ConversationState.AWAITING_DEBATE_STANCE:
            val = text.strip().lower()
            if val not in {"support", "contradict"}:
                self.telegram.send_text("Please select either 'Support' or 'Contradict'.", chat_id=chat_id)
                return
            
            self._state = ConversationState.IDLE
            command_name = self._pending_command or "debate"
            
            command = BotCommand(
                name=command_name,
                category=self._pending_debate_topic,
                count=self._pending_debate_count or 1,
                include_seen=False,
                stance=val,
                target_url=self._pending_debate_target_url,
                language=self._pending_language or self.settings.default_language
            )
            
            # Reset pending state
            self._pending_command = None
            self._pending_debate_topic = None
            self._pending_debate_count = None
            self._pending_debate_target_url = None
            self._pending_language = None
            
            self._dispatch_command(command, chat_id)
            return

        if self._state in {ConversationState.AWAITING_POST_FORMAT, ConversationState.AWAITING_DRAFT_FORMAT}:
            format_choice = text.strip().lower()
            if format_choice not in {"post", "thread"}:
                self.telegram.send_text("Invalid choice. Please select Post or Thread.", chat_id=chat_id)
                return
                
            self._pending_format = format_choice
            
            if format_choice == "thread":
                self._state = (
                    ConversationState.AWAITING_POST_THREAD_LENGTH
                    if self._state == ConversationState.AWAITING_POST_FORMAT
                    else ConversationState.AWAITING_DRAFT_THREAD_LENGTH
                )
                self.telegram.send_text("How many tweets should be in this thread? (Max 4)", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            
            self._state = (
                ConversationState.AWAITING_POST_CATEGORY
                if self._state == ConversationState.AWAITING_POST_FORMAT
                else ConversationState.AWAITING_DRAFT_CATEGORY
            )
            self._ask_category(chat_id)
            return

        if self._state in {ConversationState.AWAITING_POST_THREAD_LENGTH, ConversationState.AWAITING_DRAFT_THREAD_LENGTH}:
            try:
                length = int(text.strip())
                if length < 1 or length > 4:
                    raise ValueError()
            except ValueError:
                self.telegram.send_text("Invalid number. Please enter a number between 1 and 4.", chat_id=chat_id)
                return
                
            self._pending_thread_length = length
            self._state = (
                ConversationState.AWAITING_POST_CATEGORY
                if self._state == ConversationState.AWAITING_POST_THREAD_LENGTH
                else ConversationState.AWAITING_DRAFT_CATEGORY
            )
            self._ask_category(chat_id)
            return

        if self._state == ConversationState.AWAITING_POST_CATEGORY:
            category = parse_category(text)
            if not category:
                self.telegram.send_text("Invalid category. Please select from the keyboard.", chat_id=chat_id)
                return
            if category.lower() == "other":
                self.telegram.send_text("Please type the genre/category you want to use:", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            self._pending_category = category
            self._state = ConversationState.AWAITING_POST_TOPIC
            
            self.telegram.send_text("🔍 Scanning live news for trending topics...", chat_id=chat_id, reply_markup={"remove_keyboard": True})
            topics_keyboard = get_topic_keyboard(category)
            
            self.telegram.send_text(
                "What topic do you want to tweet about? Tap a suggestion or type your own:",
                chat_id=chat_id,
                reply_markup=topics_keyboard
            )
            return

        if self._state == ConversationState.AWAITING_POST_TOPIC:
            topic = text.strip()
            
            generic_words = {"news", "trending", "latest", "top", "updates", "today"}
            topic_words = set(re.findall(r'[a-z0-9]+', topic.lower()))
            cat_words = set(re.findall(r'[a-z0-9]+', self._pending_category.lower())) if self._pending_category else set()
            
            # If the user typed only generic words or words already in the category name (e.g. "Crime news" for "Crime"),
            # treat it as a generic trending request (topic = None).
            if not (topic_words - generic_words - cat_words):
                self._pending_topic = None
            else:
                self._pending_topic = topic
                
            if self._pending_format == "thread":
                self._pending_posts = 1
                self._state = ConversationState.IDLE
                topic_label = self._get_topic_label(self._pending_topic, self._pending_category)
                self.telegram.send_text(f"Scheduling 1 thread about '{topic_label}'...", chat_id=chat_id)
                
                command = BotCommand(
                    name="post",
                    category=self._pending_category or "Tech",
                    topic=self._pending_topic,
                    count=max(self.default_count, 1),
                    include_seen=False,
                    posts=1,
                    interval_minutes=0,
                    is_thread=True,
                    thread_length=self._pending_thread_length or 4,
                    language=self._pending_language or self.settings.default_language
                )
                self._pending_language = None
                self._dispatch_command(command, chat_id)
                return
                
            self._state = ConversationState.AWAITING_POST_COUNT
            self.telegram.send_text(
                "How many posts do you want to schedule?",
                chat_id=chat_id,
                reply_markup={"remove_keyboard": True}
            )
            return
            
        if self._state == ConversationState.AWAITING_POST_COUNT:
            try:
                self._pending_posts = int(text.strip())
                if self._pending_posts < 1:
                    raise ValueError()
            except ValueError:
                self.telegram.send_text("Please send a valid positive number for the post count.", chat_id=chat_id)
                return
                
            if self._pending_posts == 1:
                self._state = ConversationState.IDLE
                topic_label = self._get_topic_label(self._pending_topic, self._pending_category)
                self.telegram.send_text(f"Scheduling 1 post about '{topic_label}'...", chat_id=chat_id)
                
                command = BotCommand(
                    name="post",
                    category=self._pending_category or "Tech",
                    topic=self._pending_topic,
                    count=max(self.default_count, 1),
                    include_seen=False,
                    posts=1,
                    interval_minutes=0,
                    is_thread=(self._pending_format == "thread"),
                    thread_length=self._pending_thread_length or 4,
                    language=self._pending_language or self.settings.default_language
                )
                self._pending_language = None
                self._dispatch_command(command, chat_id)
                return

            self._state = ConversationState.AWAITING_INTERVAL
            self.telegram.send_text("How many minutes between each post?", chat_id=chat_id)
            return
            
        if self._state == ConversationState.AWAITING_INTERVAL:
            try:
                interval = int(text.strip())
                if interval < 0:
                    raise ValueError()
            except ValueError:
                self.telegram.send_text("Please send a valid non-negative number for the interval.", chat_id=chat_id)
                return
                
            self._state = ConversationState.IDLE
            topic_label = self._get_topic_label(self._pending_topic, self._pending_category)
            self.telegram.send_text(f"Scheduling {self._pending_posts} posts about '{topic_label}'...", chat_id=chat_id)
            
            internal_command_name = "autopost" if self._pending_posts > 1 else "post"
            command = BotCommand(
                name=internal_command_name,
                category=self._pending_category or "Tech",
                topic=self._pending_topic,
                count=max(self.default_count, self._pending_posts),
                include_seen=False,
                posts=self._pending_posts,
                interval_minutes=float(interval),
                is_thread=(self._pending_format == "thread"),
                thread_length=self._pending_thread_length or 4,
                language=self._pending_language or self.settings.default_language
            )
            self._pending_language = None
            self._dispatch_command(command, chat_id)
            return

        if self._state == ConversationState.AWAITING_DRAFT_CATEGORY:
            category = parse_category(text)
            if not category:
                self.telegram.send_text("Invalid category. Please select from the keyboard.", chat_id=chat_id)
                return
            if category.lower() == "other":
                self.telegram.send_text("Please type the genre/category you want to use:", chat_id=chat_id, reply_markup={"remove_keyboard": True})
                return
            self._pending_category = category
            self._state = ConversationState.AWAITING_DRAFT_TOPIC
            
            self.telegram.send_text("🔍 Scanning live news for trending topics...", chat_id=chat_id, reply_markup={"remove_keyboard": True})
            topics_keyboard = get_topic_keyboard(category)
            
            self.telegram.send_text(
                "What topic do you want to see drafts for? Tap a suggestion or type your own:",
                chat_id=chat_id,
                reply_markup=topics_keyboard
            )
            return

        if self._state == ConversationState.AWAITING_DRAFT_TOPIC:
            topic = text.strip()
            
            generic_words = {"news", "trending", "latest", "top", "updates", "today"}
            topic_words = set(re.findall(r'[a-z0-9]+', topic.lower()))
            cat_words = set(re.findall(r'[a-z0-9]+', self._pending_category.lower())) if self._pending_category else set()
            
            if not (topic_words - generic_words - cat_words):
                self._pending_topic = None
            else:
                self._pending_topic = topic
                
            if self._pending_format == "thread":
                self._state = ConversationState.IDLE
                topic_label = self._get_topic_label(self._pending_topic, self._pending_category)
                self.telegram.send_text(f"Generating 1 thread draft about '{topic_label}'...", chat_id=chat_id)
                
                command = BotCommand(
                    name="batch",
                    category=self._pending_category or "Tech",
                    topic=self._pending_topic,
                    count=1,
                    include_seen=False,
                    posts=1,
                    is_thread=True,
                    thread_length=self._pending_thread_length or 4,
                    language=self._pending_language or self.settings.default_language
                )
                self._pending_language = None
                self._dispatch_command(command, chat_id)
                return
                
            self._state = ConversationState.AWAITING_DRAFT_COUNT
            self.telegram.send_text(
                "How many drafts do you want to generate?",
                chat_id=chat_id,
                reply_markup={"remove_keyboard": True}
            )
            return
            
        if self._state == ConversationState.AWAITING_DRAFT_COUNT:
            try:
                count = int(text.strip())
                if count < 1:
                    raise ValueError()
            except ValueError:
                self.telegram.send_text("Please send a valid positive number for the draft count.", chat_id=chat_id)
                return
                
            self._state = ConversationState.IDLE
            topic_label = self._get_topic_label(self._pending_topic, self._pending_category)
            self.telegram.send_text(f"Generating {count} drafts about '{topic_label}'...", chat_id=chat_id)
            
            command = BotCommand(
                name="batch",
                category=self._pending_category or "Tech",
                topic=self._pending_topic,
                count=count,
                include_seen=False,
                posts=1,
                is_thread=(self._pending_format == "thread"),
                thread_length=self._pending_thread_length or 4,
                language=self._pending_language or self.settings.default_language
            )
            self._pending_language = None
            self._dispatch_command(command, chat_id)
            return

    def _ask_category(self, chat_id: str) -> None:
        self.telegram.send_text("🔍 Scanning global news for trending genres...", chat_id=chat_id, reply_markup={"remove_keyboard": True})
        favorites = get_trending_genres(self.settings, limit=6)
        category_keyboard = {
            "keyboard": [
                [{"text": favorites[i]}, {"text": favorites[i+1]} if i+1 < len(favorites) else {"text": "Other"}]
                for i in range(0, len(favorites), 2)
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
        
        self.telegram.send_text("Which category of news? Tap a button below:", chat_id=chat_id, reply_markup=category_keyboard)

    def _ask_language(self, chat_id: str, next_state: ConversationState) -> None:
        self._state = next_state
        keyboard = {
            "keyboard": [
                [{"text": "English"}, {"text": "Spanish"}],
                [{"text": "French"}, {"text": "German"}],
                [{"text": "Japanese"}, {"text": "Chinese"}],
                [{"text": "Other"}]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
        self.telegram.send_text("Language selection: What language would you like the post/thread to be written in?", chat_id=chat_id, reply_markup=keyboard)

    def _handle_callback_query(self, callback_query: dict) -> None:
        query_id = callback_query.get("id")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        
        if chat_id != str(self.settings.telegram_chat_id):
            return

        if not data:
            return

        parts = data.split("_", 1)
        if len(parts) < 2:
            return
        
        action, draft_id = parts[0], parts[1]
        
        from twitter_automation_agent.telegram import get_cached_draft, remove_cached_draft, format_draft_message_text
        
        entry = get_cached_draft(draft_id)
        if not entry:
            self.telegram.answer_callback_query(query_id, text="⚠️ Draft not found or expired.", show_alert=True)
            try:
                msg_id = str(message.get("message_id"))
                self.telegram.edit_message_text(
                    text=message.get("text", "") + "\n\n⚠️ (Draft expired/not found)",
                    message_id=msg_id,
                    chat_id=chat_id,
                    reply_markup=None
                )
            except Exception:
                pass
            return

        if action == "post":
            self.telegram.answer_callback_query(query_id, text="🚀 Initiating post to X...")
            self.telegram.send_text("🚀 Posting draft to X from your PC... Please wait.", chat_id=chat_id)
            
            try:
                from twitter_automation_agent.publisher import XPublisher
                publisher = XPublisher(self.settings)
                
                reply_to_id = None
                quote_url = None
                
                if entry.is_debate:
                    if entry.reply:
                        match = re.search(r'/status/(\d+)', str(entry.item.article.url))
                        reply_to_id = match.group(1) if match else None
                    else:
                        quote_url = entry.item.article.url
                
                post_id = publisher.post(
                    text=entry.item.draft.text,
                    image_paths=entry.item.draft.image_paths,
                    thread_texts=entry.item.draft.thread_texts if entry.item.draft.is_thread else None,
                    telegram_sender=self.telegram,
                    reply_to_id=reply_to_id,
                    quote_url=quote_url,
                    is_first=True,
                )
                
                original_text = format_draft_message_text(entry.item, entry.prefix)
                self.telegram.edit_message_text(
                    text=original_text + "\n\n✅ Posted to X!",
                    message_id=str(message.get("message_id")),
                    chat_id=chat_id,
                    reply_markup=None
                )
                
                remove_cached_draft(draft_id)
                self.telegram.send_text("✅ Successfully posted to X!", chat_id=chat_id)
                
            except Exception as e:
                self.telegram.send_text(f"❌ Failed to post draft: {e}", chat_id=chat_id)
                
        elif action == "edit":
            self.telegram.answer_callback_query(query_id, text="✍️ Reply to the draft message to edit.")
            self.telegram.send_text(
                text="✍️ Please reply directly to the draft message above (the one with the buttons) with your new text.",
                chat_id=chat_id,
                reply_to_message_id=str(message.get("message_id"))
            )
            
        elif action == "discard":
            self.telegram.answer_callback_query(query_id, text="❌ Draft discarded.")
            try:
                original_text = format_draft_message_text(entry.item, entry.prefix)
                self.telegram.edit_message_text(
                    text=original_text + "\n\n❌ Discarded.",
                    message_id=str(message.get("message_id")),
                    chat_id=chat_id,
                    reply_markup=None
                )
            except Exception as e:
                print(f"[DEBUG] Failed to edit message on discard: {e}")
            remove_cached_draft(draft_id)

    def _get_topic_label(self, topic: str | None, category: str | None) -> str:
        if topic:
            return topic
        cat_lower = category.lower() if category else "tech"
        return f"trending {cat_lower} news"



