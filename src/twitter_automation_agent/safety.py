from __future__ import annotations

import re

from twitter_automation_agent.models import Article


DISALLOWED_PATTERNS = [
    r"\bconfirmed\b.*\b(if|unless|rumou?r|reportedly)\b",
    r"\bdemands?\s+silence\b",
    r"\bcaving\s+to\s+pressure\b",
    r"\bone\s+thing'?s\s+certain\b",
    r"\bchilling\b",
    r"\bundermines?\b",
    r"\bapparently\b.*['\"]requested['\"]",
    r"\btraitor\b",
    r"\bcriminal\b",
    r"\bscam\b",
    r"\bcorrupt\b",
    r"\bkill\b",
    r"\bdestroy them\b",
]


HIGH_RISK_CLAIMS = [
    "banned",
    "blocked",
    "restricted",
    "illegal",
    "lawsuit",
    "fraud",
    "criminal",
    "government",
    "demands",
    "silence",
    "pressure",
    "chilling",
    "undermines",
]


def _source_text(article: Article) -> str:
    return f"{article.title} {article.summary or ''} {article.source}".lower()


def validate_tweet_text(text: str, article: Article) -> tuple[bool, str | None]:
    if len(text) > 280:
        return False, "Tweet exceeds 280 characters."

    lowered = text.lower()
    for pattern in DISALLOWED_PATTERNS:
        if re.search(pattern, lowered):
            return False, f"Tweet matched disallowed pattern: {pattern}"

    source = _source_text(article)
    unsupported_terms = [
        term for term in HIGH_RISK_CLAIMS if term in lowered and term not in source
    ]
    if unsupported_terms:
        return False, f"Tweet includes unsupported high-risk terms: {', '.join(unsupported_terms)}"

    return True, None
