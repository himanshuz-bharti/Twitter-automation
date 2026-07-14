from __future__ import annotations

import re

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.llm import LLMClient
from twitter_automation_agent.models import Article, DraftStyle, TweetDraft
from twitter_automation_agent.safety import validate_tweet_text


STYLE_GUIDANCE = {
    DraftStyle.neutral: "Informative, clear, and conversational. Sound like a knowledgeable human sharing an interesting update, not a robot.",
    DraftStyle.sharp: "Direct, skeptical, and thought-provoking. Sound like a sharp analyst pointing out the catch or the real stakes.",
    DraftStyle.spicy: (
        "Highly engaging, provocative, and conversational. Sound like a passionate human dropping a hot take. "
        "Use tension and a strong hook, but do not invent claims."
    ),
    DraftStyle.ragebait: (
        "Maximum hook and controversy. Start with a bold, eye-catching claim. "
        "Sound like an angry but factual human exposing something crazy. "
        "Stay factual and do not add slurs or harassment."
    ),
}


SYSTEM_PROMPT = """You are a highly engaging human Twitter/X user who shares news in a conversational, interesting way.

Hard rules:
- Stay under 280 characters.
- DO NOT just copy-paste the headline. Synthesize the details into a compelling, human-sounding observation or hook.
- Make it highly engaging (e.g., ask a rhetorical question, point out an irony, or highlight why this matters).
- Use only facts present in the article title, summary, source, and publisher metadata.
- Do not invent numbers, quotes, or fake claims.
- Do not use slurs, dehumanization, threats, or targeted harassment.
- No hashtags unless one is naturally useful.
- Start the tweet with a red emoji signifying urgency or importance (e.g., 🚨, ❗, or 🔴).
- Return only the tweet text. Do not include source labels, URLs, article links, or publisher names unless the publisher is part of the news itself.
"""


def _strip_source_mentions(text: str, article: Article) -> str:
    names = [article.source, article.publisher or ""]
    for name in names:
        clean_name = re.escape(name.strip())
        if not clean_name:
            continue
        text = re.sub(
            rf"\s*,?\s*(?:according to|reports?|via)(?:\s+a\s+report\s+(?:from|by)|\s+an\s+article\s+from)?\s+{clean_name}\b\.?,?",
            ".",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+([?.!,])", r"\1", text)
        text = re.sub(r"\.\s*\.", ".", text)
    return text


def _trim_to_tweet(text: str, article: Article | None = None) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    text = re.sub(r"^tweet:\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"https?://\S+", "", text).strip()
    text = re.sub(r"\s*source:\s*.+$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*publisher:\s*.+$", "", text, flags=re.IGNORECASE).strip()
    if article:
        text = _strip_source_mentions(text, article)
    text = re.sub(r"@([A-Za-z0-9_]{1,15})", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    
    if not text.startswith("🚨"):
        text = f"🚨 {text.lstrip()}"
        
    if len(text) > 280:
        text = text[:277].rsplit(" ", 1)[0].rstrip(" .,;:") + "..."
        
    return text


def _article_context(article: Article) -> str:
    return f"""Title: {article.title}
Source: {article.source}
Publisher: {article.publisher or article.source}
Published: {article.published_at.isoformat() if article.published_at else "unknown"}
Summary: {article.summary or "none"}"""


class TweetDrafter:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.llm = LLMClient(settings, timeout)

    def draft(self, article: Article, style: DraftStyle) -> TweetDraft:
        provider = self.settings.llm_provider.lower().strip()
        text: str | None = None
        
        if provider in {"none", "fallback", "template"}:
            raise ValueError("LLM provider must be configured. Hardcoded fallback templates have been removed.")

        prompt = self._prompt(article, style)
        temperature = 0.9 if style in {DraftStyle.spicy, DraftStyle.ragebait} else 0.35
        
        # Allow up to 4 attempts if it fails validation
        for attempt in range(4):
            text = self.llm.generate(prompt, temperature=temperature, max_tokens=150)
            if not text:
                continue
                
            text = _trim_to_tweet(text, article)
            valid, reason = validate_tweet_text(text, article)
            if valid:
                break
                
            prompt += f"\n\nYour previous draft failed validation because: {reason}. Try again and fix the issue. You MUST be highly concise."
            text = None
            
        if not text:
            raise ValueError(f"Failed to generate a valid tweet using {provider} after 4 attempts.")

        rationale = f"Drafted with {provider} using factuality constraints."

        return TweetDraft(
            text=text,
            style=style,
            article=article,
            image_url=article.image_url,
            rationale=rationale,
        )

    def _prompt(self, article: Article, style: DraftStyle) -> str:
        return f"""{SYSTEM_PROMPT}

Style: {style.value}
Style guidance: {STYLE_GUIDANCE[style]}

Article:
{_article_context(article)}

Draft one tweet."""


