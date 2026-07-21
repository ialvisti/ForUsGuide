"""Controlled-vocabulary privacy boundary for semantic retrieval.

Pinecone's integrated embedding API receives query text outside the GCP
boundary.  Redacting a handful of regular-expression patterns is not a safe
boundary: unlabelled names, postal addresses, possessives and LLM-authored
subqueries still pass through.  Instead, outbound text is rebuilt solely from
reviewed retirement-domain concepts.  The original words are never copied.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


class UnsafeRetrievalQuery(ValueError):
    """The input contains no reviewed retrieval concept and is not sent."""


def _pattern(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.IGNORECASE)


# Rich inquiry context stays inside the application/LLM path, but it still must
# not carry known participant values copied from the ticket or common direct
# identifiers.  This redaction layer deliberately remains separate from the
# controlled-vocabulary Pinecone boundary below: semantic prose can survive for
# response quality, while only reviewed constants can leave through retrieval.
_EMAIL = _pattern(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_CURRENCY = _pattern(
    r"(?<!\w)(?:[$€£]\s*\d[\d,]*(?:\.\d{1,2})?|"
    r"\d[\d,]*(?:\.\d{1,2})?\s*(?:usd|dollars?|euros?|pounds?))"
)
_LABELED_IDENTIFIER = _pattern(
    r"\b(?:participant|plan|account|ticket|employee|client)\s*"
    r"(?:id|number|no\.?|#)\s*[:=#-]?\s*[A-Za-z0-9_-]{3,}\b"
)
_SSN = _pattern(r"\b\d{3}[\s.-]+\d{2}[\s.-]+\d{4}\b")
_GROUPED_ACCOUNT = _pattern(
    r"\b(?:(?:bank|retirement)\s+)?(?:account|routing)\s*"
    r"(?:id|number|no\.?|#)?\s*[:=#-]?\s*"
    r"(?:\d{2,4}[\s.-]+){1,5}\d{2,4}\b"
)
_GROUPED_NUMBER = _pattern(r"(?<!\d)(?:\d{2,4}[\s.-]+){2,5}\d{2,4}(?!\d)")
_PHONE = _pattern(
    r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s])\d{3}[-.\s]\d{4}\b"
)
_DATE = _pattern(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_TEXTUAL_DATE = _pattern(
    rf"\b(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)"
    rf"\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:\s*,\s*|\s+)"
    rf"\d{{4}})\b"
)
_LONG_NUMBER = _pattern(r"\b\d{6,}\b")
_LABELED_NAME = re.compile(
    r"\b(Participant|Employee|Client|User)\s+"
    r"(?:named\s+)?[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3}\b"
)
_FINANCIAL_NUMBER = _pattern(
    r"\b(balance|salary|income|amount|contribution|withdrawal|distribution)"
    r"(\s+(?:is|of|equals|was|has))?\s+\d[\d,]*(?:\.\d+)?\b"
)
_SENSITIVE_CONTEXT_PATTERNS = (
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
)


# Ordered canonical concept bag.  Output phrases are constants; regex matches
# only decide whether a phrase is present and can never contribute raw text.
_CONTROLLED_CONCEPTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_pattern(
        r"\bknowledge\s+base\b|\barticle\s+content\b|"
        r"\bretirement\s+plan\s+guidance\b"
    ),
     "retirement plan guidance"),
    (_pattern(r"\b401\s*\(?k\)?\b"), "401(k)"),
    (_pattern(r"\b403\s*\(?b\)?\b"), "403(b)"),
    (_pattern(r"\b457\s*\(?b\)?\b"), "457(b)"),
    (_pattern(r"\broth\b"), "Roth"),
    (_pattern(r"\bira\b|individual retirement account"), "IRA"),
    (_pattern(r"\bretirement\b|\bpension\b"), "retirement plan"),
    (_pattern(r"\bindirect\b.{0,40}\broll\s*over\b"), "indirect rollover"),
    (_pattern(r"\bdirect\b.{0,40}\broll\s*over\b"), "direct rollover"),
    (_pattern(r"\bincoming\b.{0,40}\broll\s*over\b|roll\s*over\s+in\b"),
     "incoming rollover"),
    (_pattern(r"\boutgoing\b.{0,40}\broll\s*over\b|roll\s*over\s+out\b"),
     "outgoing rollover"),
    (_pattern(r"\broll\s*over(?:s|ed|ing)?\b"), "rollover"),
    (_pattern(r"\btransfer(?:s|red|ring)?\b"), "plan transfer"),
    (_pattern(r"\bdistribution(?:s)?\b|\bwithdraw(?:al|als|ing)?\b|cash[- ]out"),
     "distribution withdrawal"),
    (_pattern(r"\bhardship\b"), "hardship"),
    (_pattern(r"\bterminat(?:e|ed|ion)\b|former\s+employee|left\s+(?:the\s+)?employer"),
     "termination"),
    (_pattern(r"\bforce[- ]?out\b|involuntary\s+distribution"),
     "force-out distribution"),
    (_pattern(r"\bsafe\s+harbor\b"), "safe harbor"),
    (_pattern(r"\bloan(?:s)?\b|borrow(?:ing)?\b"), "plan loan"),
    (_pattern(r"\bvest(?:ed|ing)?\b"), "vesting"),
    (_pattern(r"\bcontribut(?:e|ion|ions|ing)\b|deferral(?:s)?\b"),
     "contributions"),
    (_pattern(r"\bemployer\s+match|matching\s+contribution"),
     "employer match"),
    (_pattern(r"\bbalance\b"), "account balance"),
    (_pattern(r"\bbeneficiar(?:y|ies)\b"), "beneficiary"),
    (_pattern(r"\bdivorc(?:e|ed)\b|\bqdro\b|domestic relations order"),
     "qualified domestic relations order"),
    (_pattern(r"\bdeceas(?:ed|e)\b|\bdeath\b"), "deceased participant"),
    (_pattern(r"required minimum distribution|\brmds?\b"),
     "required minimum distribution"),
    (_pattern(r"\btax(?:es|ation)?\b"), "tax"),
    (_pattern(r"\bwithhold(?:ing)?\b"), "tax withholding"),
    (_pattern(r"\bpenalt(?:y|ies)\b"), "penalty"),
    (_pattern(r"60[- ]day"), "60-day rule"),
    (_pattern(r"\beligib(?:le|ility)\b"), "eligibility"),
    (_pattern(r"\benroll(?:ment|ed|ing)?\b"), "enrollment"),
    (_pattern(r"\baddress\b.*\b(?:change|update)\b|\b(?:change|update)\b.*\baddress\b"),
     "address update"),
    (_pattern(r"\bemail\b.*\b(?:change|update)\b|\b(?:change|update)\b.*\bemail\b"),
     "email update"),
    (_pattern(r"\blog[ -]?in\b|\bpassword\b|account\s+access|\bmfa\b|multi-factor"),
     "account access"),
    (_pattern(r"\bcheck\b"), "check delivery"),
    (_pattern(r"\bach\b|wire\s+transfer|direct\s+deposit|electronic\s+delivery"),
     "electronic delivery"),
    (_pattern(r"\bdeadline\b|\btimeline\b|how\s+long|processing\s+time"),
     "processing timeline"),
    (_pattern(r"\bfee(?:s)?\b|\bcost(?:s)?\b"), "fees"),
    (_pattern(r"\bform(?:s)?\b|paperwork"), "forms"),
    (_pattern(r"\bsignature\b|\bnotar(?:y|ize|ized)\b"), "signature requirements"),
    (_pattern(r"plan\s+sponsor|\bemployer\b"), "plan sponsor"),
    (_pattern(r"record\s+keeper"), "record keeper"),
    (_pattern(r"\bcensus\b|\bpayroll\b|plan\s+administration\s+data"),
     "plan administration data"),
    (_pattern(r"\bcompliance\b|\bcorrection\b|\brefund\b|overpayment|excess"),
     "plan compliance correction"),
    (_pattern(
        r"required\s+data|required\s+(?:plan\s+)?information|"
        r"information\s+needed"
    ),
     "required plan information"),
    (_pattern(r"business\s+rule|plan\s+rule|procedure|process|steps"),
     "plan rules procedure"),
)


def _normalized_input(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[_/]+", " ", normalized)


def _replace_sensitive_literals(
    text: str, sensitive_literals: Iterable[str],
) -> str:
    for literal in sensitive_literals:
        value = _normalized_input(literal).strip()
        # Tiny/common tokens create destructive false positives.  Request IDs,
        # usernames and company names supplied by callers are bounded above it.
        if len(value) < 3:
            continue
        text = re.sub(
            re.escape(value), " participant value ", text,
            flags=re.IGNORECASE,
        )
    return text


def redact_retrieval_context(
    query_text: str, *, sensitive_literals: Iterable[str] = (),
) -> str:
    """Preserve useful prose while removing direct participant values.

    This is the context sanitizer used before RAG prompts.  It is not the
    external retrieval boundary; callers must still pass outbound Pinecone
    text through :func:`sanitize_retrieval_query`, which copies no input text.
    """
    text = _replace_sensitive_literals(
        unicodedata.normalize("NFKC", str(query_text or "")),
        sensitive_literals,
    )
    text = _EMAIL.sub(" participant email ", text)
    text = _TEXTUAL_DATE.sub(" date ", text)
    text = _SSN.sub(" identifier ", text)
    text = _GROUPED_ACCOUNT.sub(" account identifier ", text)
    text = _GROUPED_NUMBER.sub(" identifier ", text)
    text = _LABELED_IDENTIFIER.sub(" identifier ", text)
    text = _PHONE.sub(" participant phone ", text)
    text = _DATE.sub(" date ", text)
    text = _CURRENCY.sub(" financial amount ", text)
    text = _FINANCIAL_NUMBER.sub(
        lambda match: f"{match.group(1)} financial amount", text,
    )
    text = _LONG_NUMBER.sub(" identifier ", text)
    text = _LABELED_NAME.sub("participant", text)
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    if not text or any(pattern.search(text) for pattern in _SENSITIVE_CONTEXT_PATTERNS):
        raise UnsafeRetrievalQuery(
            "retrieval context still contains a prohibited participant value"
        )
    return text


def sanitize_retrieval_query(
    query_text: str, *, sensitive_literals: Iterable[str] = (),
) -> str:
    """Build a query from reviewed concepts; never copy caller/LLM text.

    ``sensitive_literals`` remains in the signature for existing callers.  It
    is intentionally unnecessary now because no input literal can reach the
    result, but consuming the iterable avoids surprising lazy-generator side
    effects at the boundary.
    """
    text = _normalized_input(query_text)
    tuple(sensitive_literals)
    concepts: list[str] = []
    for matcher, canonical in _CONTROLLED_CONCEPTS:
        if matcher.search(text) and canonical not in concepts:
            concepts.append(canonical)
    if "retirement plan guidance" in concepts:
        concepts = [item for item in concepts if item != "retirement plan"]
    if not concepts:
        raise UnsafeRetrievalQuery(
            "retrieval input contains no reviewed retirement-plan concept"
        )
    return " ".join(concepts)
