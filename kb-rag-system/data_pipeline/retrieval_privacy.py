"""Deterministic privacy boundary for semantic-retrieval query text.

Pinecone's integrated embedding API receives query text outside our GCP
boundary. Retrieval needs the intent (rollover, balance, withdrawal), not a
participant's identity or financial value. This module removes common direct
identifiers and values before the cache key or outbound SDK call is built.
"""

from __future__ import annotations

import re
from typing import Iterable


class UnsafeRetrievalQuery(ValueError):
    """The query could not be reduced to non-sensitive retrieval intent."""


_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", re.IGNORECASE)
_CURRENCY = re.compile(
    r"(?<!\w)(?:[$€£]\s*\d[\d,]*(?:\.\d{1,2})?|"
    r"\d[\d,]*(?:\.\d{1,2})?\s*(?:usd|dollars?|euros?|pounds?))",
    re.IGNORECASE,
)
_LABELED_IDENTIFIER = re.compile(
    r"\b(?:participant|plan|account|ticket|employee|client)\s*"
    r"(?:id|number|no\.?|#)\s*[:=#-]?\s*[A-Za-z0-9_-]{3,}\b",
    re.IGNORECASE,
)
_SSN = re.compile(r"\b\d{3}[\s.-]+\d{2}[\s.-]+\d{4}\b")
_GROUPED_ACCOUNT = re.compile(
    r"\b(?:(?:bank|retirement)\s+)?(?:account|routing)\s*"
    r"(?:id|number|no\.?|#)?\s*[:=#-]?\s*"
    r"(?:\d{2,4}[\s.-]+){1,5}\d{2,4}\b",
    re.IGNORECASE,
)
_GROUPED_NUMBER = re.compile(
    r"(?<!\d)(?:\d{2,4}[\s.-]+){2,5}\d{2,4}(?!\d)"
)
_PHONE = re.compile(
    r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s])\d{3}[-.\s]\d{4}\b"
)
_DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_TEXTUAL_DATE = re.compile(
    rf"\b(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)"
    rf"\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:\s*,\s*|\s+)"
    rf"\d{{4}})\b",
    re.IGNORECASE,
)
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_LABELED_NAME = re.compile(
    r"\b(Participant|Employee|Client|User)\s+"
    r"(?:named\s+)?[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3}\b"
)
_FINANCIAL_NUMBER = re.compile(
    r"\b(balance|salary|income|amount|contribution|withdrawal|distribution)"
    r"(\s+(?:is|of|equals|was|has))?\s+\d[\d,]*(?:\.\d+)?\b",
    re.IGNORECASE,
)


def _replace_literals(text: str, sensitive_literals: Iterable[str]) -> str:
    for literal in sensitive_literals:
        value = str(literal or "").strip()
        # Tiny/common tokens create destructive false positives. IDs and names
        # placed here by a caller are bounded above this minimum.
        if len(value) < 3:
            continue
        text = re.sub(re.escape(value), " participant value ", text,
                      flags=re.IGNORECASE)
    return text


def sanitize_retrieval_query(
    query_text: str, *, sensitive_literals: Iterable[str] = (),
) -> str:
    """Return retrieval intent with direct identity/value material removed."""
    text = _replace_literals(str(query_text or ""), sensitive_literals)
    text = _EMAIL.sub(" participant email ", text)
    text = _TEXTUAL_DATE.sub(" date ", text)
    text = _SSN.sub(" identifier ", text)
    text = _GROUPED_ACCOUNT.sub(" account identifier ", text)
    text = _GROUPED_NUMBER.sub(" identifier ", text)
    text = _LABELED_IDENTIFIER.sub(" identifier ", text)
    text = _PHONE.sub(" participant phone ", text)
    text = _DATE.sub(" date ", text)
    text = _CURRENCY.sub(" financial amount ", text)
    text = _FINANCIAL_NUMBER.sub(lambda m: f"{m.group(1)} financial amount", text)
    text = _LONG_NUMBER.sub(" identifier ", text)
    text = _LABELED_NAME.sub("participant", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")

    if not text or _contains_sensitive_pattern(text):
        raise UnsafeRetrievalQuery(
            "retrieval query still contains a prohibited participant value"
        )
    return text


def _contains_sensitive_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in (
        _EMAIL,
        _CURRENCY,
        _TEXTUAL_DATE,
        _GROUPED_ACCOUNT,
        _GROUPED_NUMBER,
        _LABELED_IDENTIFIER,
        _SSN,
        _PHONE,
        _DATE,
        _LONG_NUMBER,
        _LABELED_NAME,
        _FINANCIAL_NUMBER,
    ))
