from __future__ import annotations

import json
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

THREAD_SYSTEM_PROMPT = """You are a highly engaging human Twitter/X user who shares news in a conversational, interesting way.

You are writing a THREAD of 4 to 5 connected tweets that tell a cohesive, detailed story.

Hard rules for the thread:
- Provide a lot of context and detail about the entire story.
- DO NOT make up the story yourself; strictly use only facts present in the article title, summary, source, and publisher metadata.
- Each individual tweet in the thread must stay under 280 characters.
- Start the FIRST tweet with "Thread 🧵" or "🧵 Thread:".
- Make the thread highly engaging, breaking down the details step-by-step.
- Return a JSON object with exactly one key: "tweets", containing an array of strings (the tweets).
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
    
    if not text.startswith("🚨") and not text.startswith("🧵") and "Thread 🧵" not in text:
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

    def draft(self, article: Article, style: DraftStyle, is_thread: bool = False) -> TweetDraft:
        provider = self.settings.llm_provider.lower().strip()
        text: str | None = None
        
        if provider in {"none", "fallback", "template"}:
            raise ValueError("LLM provider must be configured. Hardcoded fallback templates have been removed.")

        prompt = self._prompt(article, style, is_thread)
        temperature = 0.9 if style in {DraftStyle.spicy, DraftStyle.ragebait} else 0.35
        
        thread_texts = []
        
        # Allow up to 4 attempts if it fails validation
        for attempt in range(4):
            raw_text = self.llm.generate(
                prompt, 
                temperature=temperature, 
                max_tokens=600 if is_thread else 150, 
                json_format=is_thread
            )
            
            if not raw_text:
                continue
                
            if is_thread:
                try:
                    data = json.loads(raw_text)
                    tweets = data.get("tweets", [])
                    if not isinstance(tweets, list) or len(tweets) < 2:
                        prompt += "\n\nError: You must output a JSON object with a 'tweets' array containing at least 2 tweets."
                        continue
                        
                    valid_thread = True
                    reason = ""
                    formatted_tweets = []
                    
                    for i, t in enumerate(tweets):
                        t_formatted = _trim_to_tweet(t, article)
                        if i == 0 and "Thread 🧵" not in t_formatted and "🧵" not in t_formatted:
                            t_formatted = f"Thread 🧵 {t_formatted}"
                        elif i > 0 and t_formatted.startswith("🚨 "):
                            # Remove the siren from subsequent tweets if it was auto-added
                            t_formatted = t_formatted[2:].strip()
                            
                        is_valid, err = validate_tweet_text(t_formatted, article)
                        if not is_valid:
                            valid_thread = False
                            reason = f"Tweet {i+1} failed: {err}"
                            break
                        formatted_tweets.append(t_formatted)
                        
                    if valid_thread:
                        text = formatted_tweets[0]
                        thread_texts = formatted_tweets
                        break
                    else:
                        prompt += f"\n\nYour previous draft failed validation because: {reason}. Try again and fix the issue. You MUST be highly concise."
                        text = None
                        
                except json.JSONDecodeError:
                    prompt += "\n\nError: Output must be valid JSON."
                    continue
            else:
                text = _trim_to_tweet(raw_text, article)
                valid, reason = validate_tweet_text(text, article)
                if valid:
                    thread_texts = [text]
                    break
                    
                prompt += f"\n\nYour previous draft failed validation because: {reason}. Try again and fix the issue. You MUST be highly concise."
                text = None
            
        if not text:
            raise ValueError(f"Failed to generate a valid tweet using {provider} after 4 attempts.")

        rationale = f"Drafted with {provider} using factuality constraints."

        return TweetDraft(
            text=text,
            is_thread=is_thread,
            thread_texts=thread_texts,
            style=style,
            article=article,
            image_url=article.image_url,
            rationale=rationale,
        )

    def _prompt(self, article: Article, style: DraftStyle, is_thread: bool = False) -> str:
        sys_prompt = THREAD_SYSTEM_PROMPT if is_thread else SYSTEM_PROMPT
        return f"""{sys_prompt}

Style: {style.value}
Style guidance: {STYLE_GUIDANCE[style]}

Article:
{_article_context(article)}

Draft {'a cohesive thread of 4-5 tweets' if is_thread else 'one tweet'}."""


