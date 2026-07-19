from __future__ import annotations

import json
import re

import httpx

from twitter_automation_agent.config import Settings
from twitter_automation_agent.llm import LLMClient
from twitter_automation_agent.models import Article, DraftStyle, TweetDraft
from twitter_automation_agent.safety import validate_tweet_text, validate_debate_tweet



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
- STRICTLY stay under 260 characters to leave room for emojis and links.
- DO NOT just copy-paste the headline. Synthesize the details into a compelling, human-sounding observation or hook.
- Make it highly engaging (e.g., ask a rhetorical question, point out an irony, or highlight why this matters).
- Use only facts present in the article title, summary, source, and publisher metadata.
- Do not invent numbers, quotes, or fake claims.
- Do not use slurs, dehumanization, threats, or targeted harassment.
- DO NOT include any hashtags whatsoever.
- Start the tweet with a red emoji signifying urgency or importance (e.g., 🚨, ❗, or 🔴).
- Return only the tweet text. Do not include source labels, URLs, article links, or publisher names unless the publisher is part of the news itself.
"""

THREAD_SYSTEM_PROMPT = """You are a highly engaging human Twitter/X user who shares news in a conversational, interesting way.

You are writing a THREAD of 4 to 5 connected tweets that tell a cohesive, detailed story.

Hard rules for the thread:
- Provide a lot of context and detail about the entire story.
- DO NOT make up the story yourself; strictly use only facts present in the article title, summary, source, and publisher metadata.
- Each individual tweet in the thread must strictly stay under 260 characters.
- The FIRST tweet must be an eye-catching and tempting introduction that hooks the reader. It should NOT dive into the detailed facts yet, but rather hype up what the thread will be about and why they must read it.
- Start the FIRST tweet with "Thread 🧵" or "🧵 Thread:".
- The SUBSEQUENT tweets should break down the actual details and facts step-by-step.
- Make the thread highly engaging.
- DO NOT include any hashtags whatsoever.
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
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    
    if not text.startswith("🚨") and not text.startswith("🧵") and "Thread 🧵" not in text:
        text = f"🚨 {text.lstrip()}"
        
    return text


def _clean_human_reply(text: str) -> str:
    text = text.strip()
    emoji_prefix = ""
    emoji_match = re.match(r'^([\U00010000-\U0010ffff]|\u2600-\u27bf|[\u2000-\u3300]\ufe0f?)+', text)
    if emoji_match:
        emoji_prefix = emoji_match.group(0).strip()
        text = text[emoji_match.end():].strip()
    
    # Strip generic starting filler words
    pattern = r'^(?:absolutely|well\s+said|this|exactly|couldn[\'’]t\s+agree\s+more|so\s+true|indeed|spot\s+on|agree|i\s+agree|agreed|nuance|totally|completely)\b\s*[,.!]*\s*'
    while True:
        m = re.match(pattern, text, re.IGNORECASE)
        if not m:
            break
        text = text[m.end():].strip()
        
    # Strip preachy starting phrases
    preachy_pattern = r'^(?:it[\'’]s\s+all\s+about|let[\'’]s\s+focus\s+on|let[\'’]s\s+keep\s+the\s+focus\s+on|we\s+need\s+to|we\s+must|it[\'’]s\s+crucial\s+to|it[\'’]s\s+important\s+to)\b\s*[,.!]*\s*'
    while True:
        m = re.match(preachy_pattern, text, re.IGNORECASE)
        if not m:
            break
        text = text[m.end():].strip()

    # Strip trailing filler agreement words
    trailing_pattern = r'\b(?:well\s+said|exactly|agreed|indeed|spot\s+on|so\s+true|agree|absolutely)\b\s*[,.!]*\s*$'
    while True:
        m = re.search(trailing_pattern, text, re.IGNORECASE)
        if not m:
            break
        text = text[:m.start()].strip()

    cleaned = f"{emoji_prefix} {text}" if emoji_prefix else text
    return cleaned.strip()


def _article_context(article: Article) -> str:
    return f"""Title: {article.title}
Source: {article.source}
Publisher: {article.publisher or article.source}
Published: {article.published_at.isoformat() if article.published_at else "unknown"}
Summary: {article.summary or "none"}"""


class TweetDrafter:
    def __init__(self, settings: Settings, timeout: float = 180.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.llm = LLMClient(settings, timeout)

    def draft(self, article: Article, style: DraftStyle, is_thread: bool = False, thread_length: int = 4) -> TweetDraft:
        provider = self.settings.llm_provider.lower().strip()
        text: str | None = None
        
        if provider in {"none", "fallback", "template"}:
            raise ValueError("LLM provider must be configured. Hardcoded fallback templates have been removed.")

        prompt = self._prompt(article, style, is_thread, thread_length)
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

    def _prompt(self, article: Article, style: DraftStyle, is_thread: bool = False, thread_length: int = 4) -> str:
        sys_prompt = THREAD_SYSTEM_PROMPT if is_thread else SYSTEM_PROMPT
        if is_thread:
            sys_prompt = sys_prompt.replace("4 to 5", str(thread_length))
        return f"""{sys_prompt}

Style: {style.value}
Style guidance: {STYLE_GUIDANCE[style]}

Article:
{_article_context(article)}

Draft {'a cohesive thread of ' + str(thread_length) + ' tweets' if is_thread else 'one tweet'}.
IMPORTANT: You MUST write the tweet in English, regardless of the language of the source article."""

    def draft_debate(self, article: Article, style: DraftStyle, reply: bool = False, stance: str = "contradict", is_targeted: bool = False) -> TweetDraft:
        provider = self.settings.llm_provider.lower().strip()
        if provider in {"none", "fallback", "template"}:
            raise ValueError("LLM provider must be configured. Hardcoded fallback templates have been removed.")

        action_desc = "directly replying to the tweet" if reply else "quote-tweeting the post"
        
        if stance == "support":
            stance_instructions = (
                "Your goal is to write a compelling commentary that supports, expands on, or adds a constructive, reinforcing perspective to the original post.\n"
                "- Do not challenge or criticize the original post. Offer a supporting point, highlight a key benefit/reason why this is true, or present a sharp agreement 'hot take'."
            )
            action_goal = "Draft a sharp supportive/agreement response in English"
        else:
            stance_instructions = (
                "Your goal is to write a compelling commentary that challenges, critiques, or adds a skeptical, thought-provoking perspective to the original post.\n"
                "- Do not just agree or repeat the original post. Offer a factual counter-point, highlight a hidden catch, or present a sharp 'hot take'."
            )
            action_goal = "Draft a sharp counter-argument/hot take response in English"

        invalid_check_rule = ""
        if not is_targeted:
            invalid_check_rule = '- CRITICAL CHECK: If the original tweet text appears to be a user profile biography, a personal introduction (e.g., starting with "I\'ve spent...", "I\'m a founder...", "Follow me on X"), or a list of follow links/prompts rather than a tweet/statement/news claim, do not draft a response. Instead, return exactly: "INVALID_TWEET_CONTENT".'

        debate_sys_prompt = f"""You are a sharp, analytical Twitter/X user who loves engaging in debates and offering interesting, conversational perspectives.

You are quote-tweeting a viral post. {stance_instructions}

Hard rules:
- Keep the commentary extremely brief and casual: write only one or two short sentences (maximum 120 characters total). 
- Do not try to write a complete structured paragraph. Keep it punchy and direct.
- Do not make up fake news, numbers, or claims. Use logic, common tech/business context, or general factual knowledge.
- Be direct, slightly provocative, but remain respectful. Do not use slurs, threats, personal attacks, or harassment.
- DO NOT include any hashtags or URLs.
- Start the tweet with an appropriate emoji (e.g., 🤔, 🧐, 💡, 🤷‍♂️, ✅, 👏, 🎯, 🔥 depending on whether you support or contradict).
- Return only your commentary text.
{invalid_check_rule}

Style & Tone Matching Rules:
- Carefully analyze the style, tone, punctuation, vocabulary, and casing (capitalization) of the Original Tweet.
- Mimic and mirror that style in your reply (e.g., if the original tweet uses all lowercase, casual slang, or short fragments, format your reply the same way).
- CRITICAL (Anti-AI & Anti-Preachy Rules): Absolutely do NOT write preachy lectures, academic advice, corporate statements, or PR/marketing slogans.
  - DO NOT write advice or grand statements about society, progress, or the future (e.g., do NOT say "progress comes from...", "let's focus on...", "it's about...", "we need to...", "we must...").
  - DO NOT start with any generic agreement words or filler expressions (e.g., do NOT start with "absolutely!", "well said!", "this!", "exactly!", "agree", "so true", "spot on", "indeed"). Start directly with your statement.
- Be a peer, not a public speaker: Write like a normal person sending a quick, casual message. Speak directly to the specific point made (e.g., instead of "we must stop letting loud hate...", say "generalization ruins everything, glad someone called this out").
- Keep it short & conversational: Normal tweets are usually short and direct. Do not write a long, perfectly structured paragraph to fill up the character limit. A punchy 80-120 character reply is much more realistic.
"""

        prompt = f"""{debate_sys_prompt}

Style: {style.value}
Style guidance: {STYLE_GUIDANCE[style]}

Original Tweet (by {article.publisher}):
{article.summary}

{action_goal} for {action_desc}.
"""
        temperature = 0.9 if style in {DraftStyle.spicy, DraftStyle.ragebait} else 0.35

        text: str | None = None
        for attempt in range(4):
            raw_text = self.llm.generate(
                prompt,
                temperature=temperature,
                max_tokens=150,
                json_format=False
            )
            if not raw_text:
                continue

            if "INVALID_TWEET_CONTENT" in raw_text:
                raise ValueError("Original tweet text appears to be a profile bio or invalid content.")

            # Trim the response
            commentary = re.sub(r"\s+", " ", raw_text).strip().strip('"')
            commentary = re.sub(r"^tweet:\s*", "", commentary, flags=re.IGNORECASE).strip()
            commentary = re.sub(r"https?://\S+", "", commentary).strip()
            commentary = re.sub(r"@([A-Za-z0-9_]{1,15})", r"\1", commentary)
            commentary = re.sub(r"#\w+", "", commentary)
            commentary = re.sub(r"\s+", " ", commentary).strip(" .")
            commentary = _clean_human_reply(commentary)

            # Format Tweet depending on reply mode
            if reply:
                full_tweet = commentary
            else:
                full_tweet = f"{commentary} {article.url}"

            valid, reason = validate_debate_tweet(full_tweet, article)
            if valid:
                text = full_tweet
                break

            prompt += f"\n\nYour previous draft failed validation because: {reason}. Try again and fix the issue. You MUST be highly concise."

        if not text:
            raise ValueError(f"Failed to generate a valid debate tweet using {provider} after 4 attempts.")

        rationale = f"Drafted debate {'reply' if reply else 'quote tweet'} response using {provider}."
        return TweetDraft(
            text=text,
            is_thread=False,
            thread_texts=[text],
            style=style,
            article=article,
            image_url=None,
            image_paths=[],
            image_suggestions=[],
            rationale=rationale,
        )


DEBATE_SYSTEM_PROMPT = """You are a sharp, analytical Twitter/X user who loves engaging in debates and offering counter-arguments, "hot takes", or "devil's advocate" perspectives.

You are quote-tweeting a viral post. Your goal is to write a compelling commentary that challenges, critiques, or adds a skeptical, thought-provoking perspective to the original post.

Hard rules:
- STRICTLY stay under 230 characters to leave room for the original tweet URL and formatting.
- Do not just agree or repeat the original post. Offer a factual counter-point, highlight a hidden catch, or present a sharp "hot take".
- Do not make up fake news, numbers, or claims. Use logic, common tech/business context, or general factual knowledge.
- Be direct, slightly provocative, but remain respectful. Do not use slurs, threats, personal attacks, or harassment.
- DO NOT include any hashtags or URLs.
- Start the tweet with a thinking or warning emoji (e.g., 🤔, 🧐, ⚠️, 💡, 🤷‍♂️).
- Return only your commentary text.
- CRITICAL CHECK: If the original tweet text appears to be a user profile biography, a personal introduction (e.g., starting with "I've spent...", "I'm a founder...", "Follow me on X"), or a list of follow links/prompts rather than a tweet/statement/news claim, do not draft a response. Instead, return exactly: "INVALID_TWEET_CONTENT".
"""



