from __future__ import annotations

import re

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.models import Article, DraftStyle, TweetDraft
from twitter_automation_agent.safety import validate_tweet_text


STYLE_GUIDANCE = {
    DraftStyle.neutral: "Concise tech-news editor. Clear, restrained, no hype.",
    DraftStyle.sharp: "Direct, skeptical, high-contrast framing. Make the stakes obvious.",
    DraftStyle.spicy: (
        "Eye-catching and provocative: use tension, stakes, and a strong hook. "
        "Do not invent claims, smear people, or add unsupported outrage."
    ),
    DraftStyle.ragebait: (
        "Maximum hook and controversy framing while staying factual. "
        "Use a sharp first sentence and make the stakes feel urgent, but do not add moral "
        "judgments like 'chilling', 'corrupt', 'caving', 'silenced', or 'undermining' unless "
        "the article text uses those words. No fake claims, slurs, threats, or harassment."
    ),
}


SYSTEM_PROMPT = """You draft factual X/Twitter posts from source-grounded tech news.

Hard rules:
- Stay under 280 characters.
- Use only facts present in the article title, summary, source, and publisher metadata.
- Do not invent numbers, quotes, accusations, motives, government actions, or release details.
- Do not claim something is confirmed unless the source text says it is confirmed.
- Do not add loaded conclusions such as "chilling", "corrupt", "caving", "silenced",
  "undermines", or "cover-up" unless the article text says that.
- Do not use slurs, dehumanization, threats, or targeted harassment.
- No hashtags unless one is naturally useful.
- Return only the tweet text. Do not include source labels, URLs, article links, or publisher names unless the publisher is part of the news itself.
"""


def _trim_to_tweet(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    text = re.sub(r"^tweet:\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"https?://\S+", "", text).strip()
    text = re.sub(r"\s*source:\s*.+$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*publisher:\s*.+$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"@([A-Za-z0-9_]{1,15})", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) <= 280:
        return text

    shortened = text[:277].rsplit(" ", 1)[0].rstrip(" .,;:")
    return f"{shortened}..."


def _article_context(article: Article) -> str:
    return f"""Title: {article.title}
Source: {article.source}
Publisher: {article.publisher or article.source}
Published: {article.published_at.isoformat() if article.published_at else "unknown"}
Summary: {article.summary or "none"}"""


def fallback_draft(article: Article, style: DraftStyle) -> str:
    title = article.title.rstrip(".")
    if style == DraftStyle.neutral:
        text = title
    elif style == DraftStyle.sharp:
        text = f"{title}. Watch the access rules, not just the launch headline."
    elif style == DraftStyle.spicy:
        text = f"{title}. The headline is loud, but the access restrictions are the real fight."
    else:
        text = f"{title}. AI insiders will argue over the rollout more than the model names."
    return _trim_to_tweet(text)


class TweetDrafter:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def draft(self, article: Article, style: DraftStyle) -> TweetDraft:
        provider = self.settings.llm_provider.lower().strip()
        text: str | None = None
        provider_note = provider

        if provider == "ollama":
            text = self._draft_with_ollama(article, style)
        elif provider in {"huggingface", "hf"}:
            text = self._draft_with_huggingface(article, style)
        elif provider in {"none", "fallback", "template"}:
            provider_note = "fallback"
        else:
            provider_note = f"unknown provider '{provider}', fallback"

        text = _trim_to_tweet(text or fallback_draft(article, style))
        valid, reason = validate_tweet_text(text, article)
        if not valid:
            text = fallback_draft(article, style)
            rationale = f"{provider_note} output failed validation ({reason}); fallback template used."
        elif provider_note == "fallback":
            rationale = "Fallback template used."
        else:
            rationale = f"Drafted with {provider_note} using factuality constraints."

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

    def _draft_with_ollama(self, article: Article, style: DraftStyle) -> str | None:
        try:
            response = httpx.post(
                f"{self.settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": self.settings.ollama_model,
                    "prompt": self._prompt(article, style),
                    "stream": False,
                    "options": {
                        "temperature": 0.9 if style in {DraftStyle.spicy, DraftStyle.ragebait} else 0.35,
                        "num_predict": 120,
                    },
                },
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = response.json()
        return data.get("response")

    def _draft_with_huggingface(self, article: Article, style: DraftStyle) -> str | None:
        if not self.settings.huggingface_api_token:
            return None

        prompt = f"<s>[INST] {self._prompt(article, style)} [/INST]"
        try:
            response = httpx.post(
                f"https://api-inference.huggingface.co/models/{self.settings.huggingface_model}",
                headers={"Authorization": f"Bearer {self.settings.huggingface_api_token}"},
                json={
                    "inputs": prompt,
                    "parameters": {
                        "max_new_tokens": 120,
                        "temperature": 0.9 if style in {DraftStyle.spicy, DraftStyle.ragebait} else 0.35,
                        "return_full_text": False,
                    },
                },
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = response.json()
        if isinstance(data, list) and data:
            return data[0].get("generated_text")
        if isinstance(data, dict):
            return data.get("generated_text")
        return None
