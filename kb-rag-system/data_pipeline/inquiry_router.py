"""
Inquiry Router Engine.

Classifies inbound participant inquiries into one of three downstream routes:

- ``knowledge_question``: punctual, factual KB lookup (delegate to
  ``/api/v1/knowledge-question``).
- ``generate_response``: participant-specific eligibility/outcome question
  that needs the heavy ``required-data -> ForUsBots -> generate-response``
  pipeline.
- ``needs_more_info``: ambiguous; preserve today's flow as a safe fallback.

The classifier is hybrid:

1. Compute deterministic signals via :func:`detect_advisory_concepts` plus
   small classifier-specific predicates.
2. Try a conservative regex fast-path that fires only on unambiguous cases
   (free, sub-millisecond).
3. Otherwise, call the configured ``classify_inquiry`` LLM route with a
   compact JSON-only prompt.
4. Coerce low-confidence LLM verdicts (< ``CONFIDENCE_FALLBACK_THRESHOLD``)
   to ``needs_more_info`` so we never silently send an unsure call to the
   wrong pipeline.
5. Gate every final ``knowledge_question`` verdict through KB coverage,
   including fast-path decisions.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from data_pipeline.llm_router import LLMRouter
from data_pipeline.prompts import build_classify_inquiry_prompt
from data_pipeline.rag_engine import detect_advisory_concepts

logger = logging.getLogger(__name__)


CONFIDENCE_FALLBACK_THRESHOLD = 0.55

VALID_ROUTES = frozenset({"knowledge_question", "generate_response", "needs_more_info"})

# Fallback message used when the classifier coerces a route to needs_more_info
# (low confidence, malformed JSON, missing user_message) and we need *something*
# the caller can send to the participant verbatim.
_DEFAULT_USER_MESSAGE = (
    "Could you share a bit more detail about what you'd like help with?"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    route: str
    confidence: float
    reasoning: str
    signals: Dict[str, Any]
    fast_path_hit: bool
    metadata: Dict[str, Any] = field(default_factory=dict)
    user_message: Optional[str] = None


@dataclass
class FastPathDecision:
    route: str
    confidence: float
    reasoning: str
    latency_ms: float


@dataclass
class CoverageVerdict:
    """Result of the LLM-based coverage check that gates knowledge_question
    verdicts. ``is_covered=False`` causes the engine to downgrade the route to
    ``needs_more_info``. ``top_score`` is informational (Pinecone top similarity)
    and surfaces in metadata for observability.
    """
    is_covered: bool
    top_score: float
    reasoning: str


# ---------------------------------------------------------------------------
# Deterministic predicates
# ---------------------------------------------------------------------------


_INTERROGATIVE_PHRASE_RE = re.compile(
    r"\b("
    r"how\s+long|how\s+many|how\s+much|"
    r"what\s+is|what\s+are|what'?s|"
    r"when|where"
    r")\b",
    re.IGNORECASE,
)

_FIRST_PERSON_STATUS_RE = re.compile(
    r"\bmy\s+(?:balance|employer|plan|account|401\s*\(?k\)?|"
    r"retirement(?:\s+account)?|\w+\s+account)"
    r"|\bi'?m\s+\d+|\bi\s+am\s+\d+"
    r"|\bi\s+(?:left|quit|terminated|retired|separated)\b",
    re.IGNORECASE,
)

_ELIGIBILITY_VERB_RE = re.compile(
    r"\b("
    r"eligible|qualify|qualifies|qualified|vested|"
    r"allowed to|can i|am i|do i qualify"
    r")\b",
    re.IGNORECASE,
)

# Procedural HOW phrases ("how can I", "how do I", "how to", "how would I")
# look like eligibility intent because they contain "can i" / "do i", but they
# are really asking for KB-procedural steps. Detect these so the fast-path
# defers to the LLM instead of routing to generate_response.
_PROCEDURAL_HOW_RE = re.compile(
    r"\bhow\s+(?:can|do|would|should)\s+i\b|\bhow\s+to\b",
    re.IGNORECASE,
)

# Transactional intent: first-person verb phrases that signal the participant
# wants to *execute* an action ("I'd like to...", "help me...", "can you help"),
# not learn about it abstractly. Combined with wants_funds (rollover, withdraw,
# loan, etc.) this signature is enough to demand eligibility verification —
# even when the participant never says "am I eligible" or "can I".
_TRANSACTIONAL_INTENT_RE = re.compile(
    r"\bi(?:'d| would)\s+like\s+to\b"
    r"|\bi\s+(?:want|need|wish|plan|intend)\s+to\b"
    r"|\bi'?m\s+(?:trying|looking|hoping|planning)\s+to\b"
    r"|\bi'?d\s+love\s+to\b"
    r"|\bhelp\s+me\b|\bcan\s+you\s+help\b|\bplease\s+help\b"
    r"|\bhow\s+(?:can|do)\s+i\s+(?:start|begin|initiate|proceed|request|submit|process)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------
#
# Real inbound inquiries arrive wrapped in email scaffolding ("Subject: …",
# "Request: …", "Summary: …"), with PII inline (emails, phones), and trailing
# signatures ("Thanks, -- John"). With Gemini Flash + JSON mode + a non-zero
# thinking budget, this scaffolding causes the model to truncate its JSON
# response (observed completion_tokens=18-22 for wrappered inputs vs 60-90
# for clean ones), and pollutes the embedding query passed to Pinecone for
# the coverage check. Normalizing once at engine entry fixes both.

# Metadata-style label prefixes we strip whenever they appear as a whole
# word followed by a colon. Real production inputs from email/CRM exporters
# concatenate these labels onto a single line ("Request: ... Summary: ..."),
# so a start-of-line anchor wouldn't catch them. The label is removed and
# the content after the colon is preserved.
_METADATA_LABEL_RE = re.compile(
    r"(?i)\b(?:subject|body|from|to|message|request|summary)\s*:\s*"
)

# RFC-loose email matcher. Replace with the literal token EMAIL so that
# "old email vs new email" distinctions remain visible to the classifier
# without the address noise dominating the embedding.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Trailing signatures: a sign-off line ("Thanks", "Best", "Regards", …) or
# an email-style "-- " separator, plus everything that follows to end of
# input. Anchored to the END so we don't chop legitimate body sentences.
_TRAILING_SIGNATURE_RE = re.compile(
    r"(?is)"
    r"(?:[\.!,\s])\s*"
    r"(?:--\s.*"                                       # -- signature block
    r"|(?:thanks|thank\s+you|best|regards|sincerely|cheers)"
    r"(?:[,!.\s].*)?"                                  # trailing pleasantry
    r")\Z"
)


def _normalize_inquiry(raw: Optional[str]) -> str:
    """Strip email scaffolding, inline emails, and trailing signatures.

    Defensive: returns the raw input unchanged if normalization would yield
    an empty string (so we never feed the LLM nothing).
    """
    if not raw:
        return raw or ""
    text = _METADATA_LABEL_RE.sub("", raw)
    text = _EMAIL_RE.sub("EMAIL", text)
    text = _TRAILING_SIGNATURE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or raw


def _is_short_interrogative(inquiry: str) -> bool:
    text = (inquiry or "").strip()
    if not text:
        return False
    if len(text.split()) > 30:
        return False
    return bool(_INTERROGATIVE_PHRASE_RE.search(text))


def _has_first_person_status(inquiry: str) -> bool:
    return bool(_FIRST_PERSON_STATUS_RE.search(inquiry or ""))


def _has_eligibility_verb(inquiry: str) -> bool:
    return bool(_ELIGIBILITY_VERB_RE.search(inquiry or ""))


def _has_action_verb(inquiry: str) -> bool:
    return bool(_TRANSACTIONAL_INTENT_RE.search(inquiry or ""))


def compute_deterministic_features(inquiry: str) -> Dict[str, Any]:
    """Combine the engine's advisory signals with classifier-only predicates."""
    base = detect_advisory_concepts(
        inquiry=inquiry,
        topic=None,
        collected_data=None,
    )
    has_action_verb = _has_action_verb(inquiry)
    # Transactional intent fires when the participant pairs a first-person
    # action verb with any signal that the action targets their funds — not
    # just wants_funds (cash-out / withdraw / rollover) but also loan_signal
    # ("take a loan") and hardship_signal ("hardship withdrawal"), each of
    # which is a transaction in its own right that needs eligibility checks.
    funds_targeted = bool(
        base.get("wants_funds", False)
        or base.get("loan_signal", False)
        or base.get("hardship_signal", False)
    )
    extras = {
        "word_count": len((inquiry or "").split()),
        "is_short_interrogative": _is_short_interrogative(inquiry),
        "has_first_person_status": _has_first_person_status(inquiry),
        "has_eligibility_verb": _has_eligibility_verb(inquiry),
        "has_action_verb": has_action_verb,
        "transactional_intent": has_action_verb and funds_targeted,
    }
    return {**base, **extras}


# ---------------------------------------------------------------------------
# Fast-path
# ---------------------------------------------------------------------------


def apply_fast_path_rules(
    inquiry: str,
    signals: Dict[str, Any],
) -> Optional[FastPathDecision]:
    """Conservative deterministic shortcut. Returns ``None`` when ambiguous."""
    start = time.monotonic()

    # Punctual factual question with no participant signals.
    if (
        signals.get("is_short_interrogative")
        and not signals.get("has_first_person_status")
        and not signals.get("has_eligibility_verb")
        and not signals.get("hardship_signal")
        and not signals.get("loan_signal")
        and not signals.get("separation_signal")
    ):
        return FastPathDecision(
            route="knowledge_question",
            confidence=0.9,
            reasoning="Short interrogative with no participant signals.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    # Strong eligibility intent paired with a hardship/loan/separation signal.
    # Skip when the inquiry is a procedural HOW question — "how can I rollover…"
    # contains "can I" but is asking for KB-procedural steps, not eligibility.
    if (
        signals.get("has_eligibility_verb")
        and not _PROCEDURAL_HOW_RE.search(inquiry or "")
        and (
            signals.get("hardship_signal")
            or signals.get("loan_signal")
            or signals.get("separation_signal")
        )
    ):
        return FastPathDecision(
            route="generate_response",
            confidence=0.9,
            reasoning="Eligibility verb plus hardship/loan/separation signal.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    # Transactional action request: the participant expresses intent to execute
    # a transaction on their funds (rollover, withdrawal, loan), even without
    # an explicit eligibility verb. Completing the action requires participant
    # data (active vs terminated, plan rules, vested balance, outstanding loans),
    # so the request belongs to generate_response. Skip when the inquiry is
    # purely procedural ("how do I…") — those are KB-answerable — or a short
    # interrogative — those have no participant context to act on.
    if (
        signals.get("transactional_intent")
        and not _PROCEDURAL_HOW_RE.search(inquiry or "")
        and not signals.get("is_short_interrogative")
    ):
        return FastPathDecision(
            route="generate_response",
            confidence=0.85,
            reasoning="Transactional intent on participant funds.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    return None


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------


_MARKDOWN_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_FIRST_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _unparseable_default() -> Dict[str, Any]:
    return {
        "route": "needs_more_info",
        "confidence": 0.0,
        "reasoning": "Classifier output unparseable",
        "user_message": None,
    }


def _safe_parse_classifier_json(
    content: Optional[str],
) -> Tuple[Dict[str, Any], bool]:
    """Defensive JSON parse. Never raises.

    Returns ``(parsed, parse_ok)``. ``parse_ok`` is False when the content
    could not be parsed into a JSON object (the engine uses this to trigger
    a fallback retry). On parse failure the parsed dict is the
    ``needs_more_info`` default so callers can short-circuit to the existing
    fail-closed contract without further branching.
    """
    if not content or not content.strip():
        logger.warning("Classifier returned empty content; defaulting to needs_more_info.")
        return _unparseable_default(), False

    text = content.strip()
    fence_match = _MARKDOWN_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Last-ditch: extract the first {...} object from chatter and retry.
        # Doesn't help with truncated-mid-string output, but covers Gemini's
        # occasional "preamble + JSON" mode.
        obj_match = _FIRST_JSON_OBJECT_RE.search(text)
        if obj_match:
            try:
                parsed = json.loads(obj_match.group(0))
            except (json.JSONDecodeError, TypeError):
                parsed = None

    if parsed is None:
        logger.warning(
            "Classifier output unparseable (len=%d): %r",
            len(content),
            content[:500],
        )
        return _unparseable_default(), False

    if not isinstance(parsed, dict):
        logger.warning("Classifier output was not a JSON object: %r", content[:500])
        return _unparseable_default(), False

    route = parsed.get("route")
    if route not in VALID_ROUTES:
        logger.warning("Classifier returned invalid route %r; coercing to needs_more_info.", route)
        parsed["route"] = "needs_more_info"
        parsed["confidence"] = 0.0
        parsed["reasoning"] = (
            f"Invalid route {route!r}; coerced to needs_more_info."
        )

    return parsed, True


def _resolve_user_message(route: str, raw: Any) -> Optional[str]:
    """Normalize the classifier's ``user_message`` to the contract.

    - Always ``None`` for non-``needs_more_info`` routes (defensive).
    - Non-empty string for ``needs_more_info``; falls back to the default if
      the LLM omitted it or returned blank/whitespace.
    """
    if route != "needs_more_info":
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            return text
    return _DEFAULT_USER_MESSAGE


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class InquiryRouterEngine:
    """Hybrid (deterministic + LLM) classifier for inbound inquiries."""

    LLM_MAX_TOKENS = 800

    def __init__(
        self,
        llm_router: LLMRouter,
        coverage_checker: Optional[
            Callable[[str], Awaitable["CoverageVerdict"]]
        ] = None,
    ):
        self._llm = llm_router
        self._coverage_checker = coverage_checker

    async def _apply_knowledge_coverage(
        self,
        *,
        route: str,
        reasoning: str,
        inquiry_norm: str,
    ) -> Tuple[str, str, Optional[float], Optional[str]]:
        kb_coverage_top_score: Optional[float] = None
        kb_coverage_reasoning: Optional[str] = None

        if route != "knowledge_question" or self._coverage_checker is None:
            return route, reasoning, kb_coverage_top_score, kb_coverage_reasoning

        verdict = await self._coverage_checker(inquiry_norm)
        kb_coverage_top_score = verdict.top_score
        kb_coverage_reasoning = verdict.reasoning
        if not verdict.is_covered:
            reasoning = (
                f"KB coverage check rejected: {verdict.reasoning}. "
                f"Original: {reasoning}"
            )
            route = "needs_more_info"

        return route, reasoning, kb_coverage_top_score, kb_coverage_reasoning

    async def classify(self, inquiry: str) -> ClassificationResult:
        # Strip email scaffolding / inline emails / signatures once, up front;
        # every downstream consumer (deterministic signals, fast-path, LLM
        # prompt, coverage checker / Pinecone query) sees the cleaned form.
        inquiry_norm = _normalize_inquiry(inquiry)
        signals = compute_deterministic_features(inquiry_norm)

        fast = apply_fast_path_rules(inquiry_norm, signals)
        if fast is not None:
            route, reasoning, kb_coverage_top_score, kb_coverage_reasoning = (
                await self._apply_knowledge_coverage(
                    route=fast.route,
                    reasoning=fast.reasoning,
                    inquiry_norm=inquiry_norm,
                )
            )
            # Fast paths only return knowledge_question / generate_response,
            # but knowledge_question must still prove KB coverage.
            return ClassificationResult(
                route=route,
                confidence=fast.confidence,
                reasoning=reasoning,
                signals=signals,
                fast_path_hit=True,
                metadata={
                    "model": None,
                    "provider": None,
                    "latency_ms": fast.latency_ms,
                    "kb_coverage_top_score": kb_coverage_top_score,
                    "kb_coverage_reasoning": kb_coverage_reasoning,
                },
                user_message=_resolve_user_message(route, None),
            )

        system_prompt, user_prompt = build_classify_inquiry_prompt(
            inquiry=inquiry_norm,
            signals=signals,
        )

        llm_start = time.monotonic()
        llm_result = await self._llm.call(
            task_type="classify_inquiry",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=self.LLM_MAX_TOKENS,
        )

        parsed, parse_ok = _safe_parse_classifier_json(llm_result.content)

        # Gemini Flash with thinking + JSON mode occasionally truncates output
        # mid-string for inputs the normalizer didn't fully clean. Retry once
        # against the configured fallback model — costs a single extra call
        # only on the rare failure path.
        if not parse_ok:
            try:
                llm_result = await self._llm.call(
                    task_type="classify_inquiry",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=self.LLM_MAX_TOKENS,
                    force_fallback=True,
                )
                parsed, parse_ok = _safe_parse_classifier_json(llm_result.content)
            except ValueError:
                # No fallback configured for classify_inquiry; keep the
                # already-coerced needs_more_info default.
                pass
            except Exception as exc:
                logger.warning(
                    "Classifier fallback retry failed (%s); keeping needs_more_info.",
                    type(exc).__name__,
                )

        llm_latency_ms = (time.monotonic() - llm_start) * 1000

        route = parsed.get("route", "needs_more_info")
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reasoning = parsed.get("reasoning") or ""

        if confidence < CONFIDENCE_FALLBACK_THRESHOLD and route != "needs_more_info":
            reasoning = (
                f"Low confidence ({confidence:.2f}); falling back. Original: {reasoning}"
            )
            route = "needs_more_info"

        route, reasoning, kb_coverage_top_score, kb_coverage_reasoning = (
            await self._apply_knowledge_coverage(
                route=route,
                reasoning=reasoning,
                inquiry_norm=inquiry_norm,
            )
        )

        user_message = _resolve_user_message(route, parsed.get("user_message"))

        return ClassificationResult(
            route=route,
            confidence=confidence,
            reasoning=reasoning,
            signals=signals,
            fast_path_hit=False,
            metadata={
                "model": llm_result.model_used,
                "provider": llm_result.provider_used,
                "usage": llm_result.usage,
                "latency_ms": llm_latency_ms,
                "kb_coverage_top_score": kb_coverage_top_score,
                "kb_coverage_reasoning": kb_coverage_reasoning,
            },
            user_message=user_message,
        )
