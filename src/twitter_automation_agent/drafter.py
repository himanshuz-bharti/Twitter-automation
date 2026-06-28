from __future__ import annotations

import re

from openai import OpenAI

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import Article, DraftStyle, TweetDraft
from twitter_automation_agent.safety import validate_tweet_text


STYLE_GUIDANCE = {
    DraftStyle.neutral: "Write like a concise tech-news editor. No hype.",
    DraftStyle.sharp: "Write with a direct, high-contrast angle. Be skeptical but fair.",
    DraftStyle.spicy: (
        "Write a punchy, debate-friendly tweet with tension and stakes. "
        "Do not exaggerate, invent facts, or target private people."
    ),
}


SYSTEM_PROMPT = """You draft factual X/Twitter posts from source-grounded tech news.

Rules:
- Stay under 280 characters.
- Use only facts present in the article title/summary/source metadata.
- Do not invent numbers, quotes, accusations, motives, or government actions.
- Do not say something is confirmed unless the source text supports it.
- Avoid slurs, targeted harassment, and calls for abuse.
- No hashtags unless one is naturally useful.
- Make the tweet clickable and self-contained.
"""


def _trim_to_tweet(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    if len(text) <= 280:
        return text

    shortened = text[:277].rsplit(" ", 1)[0].rstrip(" .,;:")
    return f"{shortened}..."


def fallback_draft(article: Article, style: DraftStyle) -> str:
    title = article.title.rstrip(".")
    if style == DraftStyle.neutral:
        text = f"{title}. Source: {article.source}"
    elif style == DraftStyle.sharp:
        text = f"{title}. The part worth watching: what this changes next. Source: {article.source}"
    else:
        text = f"{title}. Big if it holds up, and the fallout could move fast. Source: {article.source}"
    return _trim_to_tweet(text)


class TweetDrafter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def draft(self, article: Article, style: DraftStyle) -> TweetDraft:
        if not self.settings.openai_api_key:
            text = fallback_draft(article, style)
            return TweetDraft(
                text=text,
                style=style,
                article=article,
                image_url=article.image_url,
                rationale="Fallback template used because OPENAI_API_KEY is not configured.",
            )

        client = OpenAI(api_key=self.settings.openai_api_key)
        user_prompt = f"""Style: {style.value}
Style guidance: {STYLE_GUIDANCE[style]}

Article:
Title: {article.title}
Source: {article.source}
Published: {article.published_at.isoformat() if article.published_at else "unknown"}
Summary: {article.summary or "none"}

Draft one tweet. Return only the tweet text."""

        response = client.responses.create(
            model=self.settings.openai_model,
            instructions=SYSTEM_PROMPT,
            input=user_prompt,
            temperature=0.7 if style == DraftStyle.spicy else 0.35,
            max_output_tokens=120,
        )
        text = _trim_to_tweet(response.output_text)
        valid, reason = validate_tweet_text(text, article)
        if not valid:
            text = fallback_draft(article, style)
            rationale = f"Model output failed safety validation ({reason}); fallback template used."
        else:
            rationale = "Drafted from article metadata with factuality constraints."

        return TweetDraft(
            text=text or fallback_draft(article, style),
            style=style,
            article=article,
            image_url=article.image_url,
            rationale=rationale,
        )
