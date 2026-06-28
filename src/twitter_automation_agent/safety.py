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

NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "third": "3",
}


def _source_text(article: Article) -> str:
    return f"{article.title} {article.summary or ''} {article.source}".lower()


def _number_claims(value: str) -> set[str]:
    lowered = value.lower()
    claims = set(re.findall(r"\b\d+(?:\.\d+)?\b", lowered))
    for word, number in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            claims.add(number)
    return claims


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

    unsupported_numbers = sorted(_number_claims(text) - _number_claims(source))
    if unsupported_numbers:
        return False, f"Tweet includes unsupported numeric claims: {', '.join(unsupported_numbers)}"

    return True, None