from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import HttpUrl, TypeAdapter

from twitter_automation_agent.config import Settings
from twitter_automation_agent.drafter import TweetDrafter
from twitter_automation_agent.images import ImageFinder
from twitter_automation_agent.models import BatchPipelineResult, DraftItem, DraftStyle, ImageSuggestion
from twitter_automation_agent.news import NewsCollector
from twitter_automation_agent.publisher import XPublisher
from twitter_automation_agent.telegram import TelegramSender

HttpUrlAdapter = TypeAdapter(HttpUrl)
HistoryScope = Literal["drafted", "posted", "sent"]


def _title_fingerprint(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.news = NewsCollector(settings=self.settings)
        self.drafter = TweetDrafter(settings)
        self.images = ImageFinder(settings)
        self.publisher = XPublisher(settings)
        self.telegram = TelegramSender(settings)

    def run(
        self,
        topic: str | None,
        style: DraftStyle,
        output_dir: Path,
        count: int = 20,
        post: bool = False,
        skip_history: bool = True,
        history_scope: HistoryScope = "drafted",
        record_history: bool = True,
        category: str = "Tech",
        is_thread: bool = False,
        thread_length: int = 4,
    ) -> BatchPipelineResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        history = self._load_history(output_dir) if skip_history else self._empty_history()
        target = topic or f"trending {category.lower()} news"

        print(f"[DEBUG] Collecting news for {target}...")
        articles = self.news.collect(
            topic=topic,
            lookback_hours=self.settings.news_lookback_hours,
            limit=max(self.settings.max_articles, count * 4),
            category=category,
        )
        fresh_articles = self._filter_history(articles, history, history_scope)
        if not fresh_articles:
            if articles:
                raise RuntimeError(
                    f"No fresh recent articles found for: {target}. "
                    "Use --include-seen to allow articles already generated before."
                )
            raise RuntimeError(
                f"No recent articles found for: {target}. Try a broader spelling, "
                "a more famous related name, or increase NEWS_LOOKBACK_HOURS."
            )

        drafts: list[DraftItem] = []
        has_posted = False
        
        def process_articles(article_list):
            nonlocal has_posted
            for article in article_list:
                if len(drafts) >= count:
                    break
                    
                print(f"[DEBUG] Attempting to enrich article: {article.title}")
                is_rich = self.news.enrich_article(article)
                if not is_rich:
                    print(f"[DEBUG] Skipping article due to insufficient content (< 150 chars after scraping).")
                    continue
                    
                print(f"[DEBUG] Drafting tweet for article: {article.title}")
                try:
                    draft = self.drafter.draft(article, style)
                except ValueError as e:
                    print(f"[DEBUG] Skipping article due to LLM failure: {e}")
                    continue
                print(f"[DEBUG] Finding image candidates for draft...")
                image_candidates = self.images.find_candidates(article, draft_text=draft.text, limit=12)
                for image_url in image_candidates:
                    if len(draft.image_suggestions) >= 5:
                        break
                        
                    if "image.pollinations.ai" in image_url:
                        print("\n[DEBUG] [Pollinations AI] Generating and downloading AI image...")
                        
                    image_path = self.images.download(image_url, output_dir / "images")
                    if not image_path:
                        continue
                    suggestion = ImageSuggestion(
                        url=HttpUrlAdapter.validate_python(image_url),
                        path=str(image_path),
                    )
                    draft.image_suggestions.append(suggestion)
                    if len(draft.image_paths) < 3:
                        draft.image_paths.append(suggestion.path)
                if not draft.image_paths:
                    print(f"[DEBUG] Skipping article because no images could be found or generated.")
                    continue

                item = DraftItem(article=article, draft=draft)
                if post and not has_posted:
                    print(f"[DEBUG] Attempting to post to X...")
                    
                    if self.settings.can_send_to_telegram:
                        try:
                            self.telegram.send_draft(item, 1, 1, chat_id=None)
                        except Exception as e:
                            print(f"[DEBUG] Failed to send to Telegram: {e}")
                            
                    item.post_id = self.publisher.post(
                        text=draft.text, 
                        image_paths=draft.image_paths, 
                        thread_texts=draft.thread_texts if draft.is_thread else None,
                        telegram_sender=self.telegram,
                    )
                    item.posted = True
                    has_posted = True
                    self._cleanup_post_artifacts(item)
                drafts.append(item)

        process_articles(fresh_articles)
        
        if len(drafts) < count:
            print(f"[DEBUG] Not enough rich articles from primary sources (found {len(drafts)}). Triggering API fallbacks...")
            api_articles = self.news.collect_from_apis(
                topic=topic,
                lookback_hours=self.settings.news_lookback_hours,
                limit=max(self.settings.max_articles, count * 4),
                category=category,
            )
            fresh_api_articles = self._filter_history(api_articles, history, history_scope)
            process_articles(fresh_api_articles)

        if not drafts:
            raise RuntimeError(f"Failed to find any high-quality, detail-rich articles for: {target}. All articles were skipped.")

        result = BatchPipelineResult(
            topic=target,
            generated_at=datetime.now(UTC),
            candidates=articles,
            drafts=drafts,
        )

        if not post:
            self._write_result(result, output_dir)
            
        if record_history:
            self._append_history(output_dir, drafts, "posted" if post else "drafted")
        return result

    def autopost(
        self,
        topic: str | None,
        style: DraftStyle,
        output_dir: Path,
        queue_size: int = 20,
        posts: int = 20,
        interval_minutes: float = 90.0,
        skip_history: bool = True,
        dry_run: bool = False,
        category: str = "Tech",
        is_thread: bool = False,
        thread_length: int = 4,
    ) -> BatchPipelineResult:
        result = self.run(
            topic=topic,
            style=style,
            output_dir=output_dir,
            count=queue_size,
            post=False,
            skip_history=skip_history,
            history_scope="posted",
            record_history=False,
            category=category,
            is_thread=is_thread,
            thread_length=thread_length,
        )

        delivered_count = 0
        attempted_items: list[DraftItem] = []
        for item in result.drafts:
            if delivered_count >= posts:
                break

            if not item.draft.image_paths:
                continue

            attempted_items.append(item)
            if dry_run:
                item.posted = False
                item.post_id = "dry-run"
            else:
                if self.settings.can_send_to_telegram:
                    try:
                        self.telegram.send_draft(item, delivered_count + 1, posts, chat_id=None)
                    except Exception as e:
                        print(f"[DEBUG] Failed to send to Telegram: {e}")

                item.post_id = self.publisher.post(
                    text=item.draft.text, 
                    image_paths=item.draft.image_paths, 
                    thread_texts=item.draft.thread_texts if item.draft.is_thread else None,
                    telegram_sender=self.telegram,
                )
                item.posted = True
                self._append_history(output_dir, [item], "posted")
                self._cleanup_post_artifacts(item)

            delivered_count += 1
            if delivered_count < posts:
                time.sleep(interval_minutes * 60)

        result.drafts = attempted_items
        if delivered_count < posts:
            target = topic or f"trending {category.lower()} news"
            raise RuntimeError(
                f"Only {delivered_count} image-backed draft(s) were available for {target}; "
                f"requested {posts}. Try a larger --queue-size or run again later."
            )
        if dry_run:
            self._write_result(result, output_dir)
        return result

    def send_telegram_batch(
        self,
        topic: str | None,
        style: DraftStyle,
        output_dir: Path,
        count: int = 10,
        skip_history: bool = True,
        dry_run: bool = False,
        chat_id: str | None = None,
        category: str = "Tech",
        is_thread: bool = False,
        thread_length: int = 4,
    ) -> BatchPipelineResult:
        result = self.run(
            topic=topic,
            style=style,
            output_dir=output_dir,
            count=count,
            post=False,
            skip_history=skip_history,
            history_scope="sent",
            record_history=False,
            category=category,
            is_thread=is_thread,
            thread_length=thread_length,
        )

        sent_items: list[DraftItem] = []
        for item in result.drafts:
            if not item.draft.image_paths:
                continue

            sent_items.append(item)
            if dry_run:
                item.posted = False
                item.post_id = "telegram-dry-run"
            else:
                item.post_id = self.telegram.send_draft(
                    item,
                    len(sent_items),
                    count,
                    chat_id=chat_id,
                )
                item.posted = True
                self._append_history(output_dir, [item], "sent")

            if len(sent_items) >= count:
                break

        result.drafts = sent_items
        if len(sent_items) < count:
            target = topic or f"trending {category.lower()} news"
            raise RuntimeError(
                f"Only {len(sent_items)} image-backed draft(s) were available for {target}; "
                f"requested {count}. Try --include-seen or run again later."
            )

        self._write_result(result, output_dir)
        return result

    def _filter_history(
        self,
        articles: list,
        history: dict[str, list[str]],
        scope: HistoryScope,
    ) -> list:
        seen_urls = set(history.get(f"{scope}_urls", []))
        seen_titles = set(history.get(f"{scope}_titles", []))
        fresh = []
        for article in articles:
            title_key = _title_fingerprint(article.title)
            if str(article.url) in seen_urls or title_key in seen_titles:
                continue
            fresh.append(article)
        return fresh

    def _history_path(self, output_dir: Path) -> Path:
        return output_dir / "history.json"

    def _empty_history(self) -> dict[str, list[str]]:
        return {
            "drafted_urls": [],
            "drafted_titles": [],
            "posted_urls": [],
            "posted_titles": [],
            "sent_urls": [],
            "sent_titles": [],
        }

    def _load_history(self, output_dir: Path) -> dict[str, list[str]]:
        path = self._history_path(output_dir)
        if not path.exists():
            return self._empty_history()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_history()

        legacy_urls = list(data.get("urls", []))
        legacy_titles = list(data.get("titles", []))

        return {
            "drafted_urls": sorted(set(legacy_urls) | set(data.get("drafted_urls", []))),
            "drafted_titles": sorted(set(legacy_titles) | set(data.get("drafted_titles", []))),
            "posted_urls": list(data.get("posted_urls", [])),
            "posted_titles": list(data.get("posted_titles", [])),
            "sent_urls": list(data.get("sent_urls", [])),
            "sent_titles": list(data.get("sent_titles", [])),
        }

    def _append_history(self, output_dir: Path, drafts: list[DraftItem], scope: HistoryScope) -> None:
        history = self._load_history(output_dir)
        urls = set(history.get(f"{scope}_urls", []))
        titles = set(history.get(f"{scope}_titles", []))

        for item in drafts:
            urls.add(str(item.article.url))
            titles.add(_title_fingerprint(item.article.title))

        history[f"{scope}_urls"] = sorted(urls)
        history[f"{scope}_titles"] = sorted(titles)

        path = self._history_path(output_dir)
        path.write_text(
            json.dumps(
                {
                    "drafted_urls": sorted(set(history.get("drafted_urls", []))),
                    "drafted_titles": sorted(set(history.get("drafted_titles", []))),
                    "posted_urls": sorted(set(history.get("posted_urls", []))),
                    "posted_titles": sorted(set(history.get("posted_titles", []))),
                    "sent_urls": sorted(set(history.get("sent_urls", []))),
                    "sent_titles": sorted(set(history.get("sent_titles", []))),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_result(self, result: BatchPipelineResult, output_dir: Path) -> Path:
        timestamp = result.generated_at.strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"draft-batch-{timestamp}.json"
        path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return path

    def _cleanup_post_artifacts(self, item: DraftItem) -> None:
        """Deletes the downloaded image files associated with a draft."""
        for suggestion in item.draft.image_suggestions:
            try:
                Path(suggestion.path).unlink(missing_ok=True)
            except OSError:
                pass