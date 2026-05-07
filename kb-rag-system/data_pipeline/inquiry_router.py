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
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from data_pipeline.llm_router import LLMRouter
from data_pipeline.prompts import build_classify_inquiry_prompt
from data_pipeline.rag_engine import detect_advisory_concepts

logger = logging.getLogger(__name__)


CONFIDENCE_FALLBACK_THRESHOLD = 0.55

VALID_ROUTES = frozenset({"knowledge_question", "generate_response", "needs_more_info"})


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


@dataclass
class FastPathDecision:
    route: str
    confidence: float
    reasoning: str
    latency_ms: float


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
    r"\b("
    r"my balance|my employer|my plan|my account|"
    r"i'?m \d+|i am \d+|"
    r"i\s+(left|quit|terminated|retired|separated)"
    r")\b",
    re.IGNORECASE,
)

_ELIGIBILITY_VERB_RE = re.compile(
    r"\b("
    r"eligible|qualify|qualifies|qualified|vested|"
    r"allowed to|can i|am i|do i qualify"
    r")\b",
    re.IGNORECASE,
)


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


def compute_deterministic_features(
    inquiry: str,
    topic: Optional[str],
    collected_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Combine the engine's advisory signals with classifier-only predicates."""
    base = detect_advisory_concepts(
        inquiry=inquiry,
        topic=topic,
        collected_data=collected_data,
    )
    extras = {
        "word_count": len((inquiry or "").split()),
        "is_short_interrogative": _is_short_interrogative(inquiry),
        "has_first_person_status": _has_first_person_status(inquiry),
        "has_eligibility_verb": _has_eligibility_verb(inquiry),
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
    if signals.get("has_eligibility_verb") and (
        signals.get("hardship_signal")
        or signals.get("loan_signal")
        or signals.get("separation_signal")
    ):
        return FastPathDecision(
            route="generate_response",
            confidence=0.9,
            reasoning="Eligibility verb plus hardship/loan/separation signal.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    return None


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------


def _safe_parse_classifier_json(content: Optional[str]) -> Dict[str, Any]:
    """Defensive JSON parse. Never raises; returns ``needs_more_info`` on failure."""
    if not content or not content.strip():
        logger.warning("Classifier returned empty content; defaulting to needs_more_info.")
        return {
            "route": "needs_more_info",
            "confidence": 0.0,
            "reasoning": "Classifier output unparseable",
        }

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "Classifier output unparseable (%s): %r",
            type(exc).__name__,
            content[:500],
        )
        return {
            "route": "needs_more_info",
            "confidence": 0.0,
            "reasoning": "Classifier output unparseable",
        }

    if not isinstance(parsed, dict):
        logger.warning("Classifier output was not a JSON object: %r", content[:500])
        return {
            "route": "needs_more_info",
            "confidence": 0.0,
            "reasoning": "Classifier output unparseable",
        }

    route = parsed.get("route")
    if route not in VALID_ROUTES:
        logger.warning("Classifier returned invalid route %r; coercing to needs_more_info.", route)
        parsed["route"] = "needs_more_info"
        parsed["confidence"] = 0.0
        parsed["reasoning"] = (
            f"Invalid route {route!r}; coerced to needs_more_info."
        )

    return parsed


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class InquiryRouterEngine:
    """Hybrid (deterministic + LLM) classifier for inbound inquiries."""

    LLM_MAX_TOKENS = 200

    def __init__(self, llm_router: LLMRouter):
        self._llm = llm_router

    async def classify(
        self,
        inquiry: str,
        record_keeper: Optional[str] = None,
        plan_type: Optional[str] = None,
        topic: Optional[str] = None,
        collected_data: Optional[Dict[str, Any]] = None,
    ) -> ClassificationResult:
        signals = compute_deterministic_features(inquiry, topic, collected_data)

        fast = apply_fast_path_rules(inquiry, signals)
        if fast is not None:
            return ClassificationResult(
                route=fast.route,
                confidence=fast.confidence,
                reasoning=fast.reasoning,
                signals=signals,
                fast_path_hit=True,
                metadata={
                    "model": None,
                    "provider": None,
                    "latency_ms": fast.latency_ms,
                },
            )

        system_prompt, user_prompt = build_classify_inquiry_prompt(
            inquiry=inquiry,
            record_keeper=record_keeper,
            plan_type=plan_type,
            topic=topic,
            participant_data_available=bool(collected_data),
            signals=signals,
        )

        llm_start = time.monotonic()
        llm_result = await self._llm.call(
            task_type="classify_inquiry",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=self.LLM_MAX_TOKENS,
        )
        llm_latency_ms = (time.monotonic() - llm_start) * 1000

        parsed = _safe_parse_classifier_json(llm_result.content)
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
            },
        )
