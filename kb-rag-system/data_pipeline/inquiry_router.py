"""
Inquiry Router Engine.

Classifies inbound participant inquiries into one of three downstream routes:

- ``knowledge_question``: punctual, factual KB lookup (delegate to
  ``/api/v1/knowledge-question``). Used when the retrieved chunks contain a
  direct answer (timeline, fee, definition, single procedural step) and no
  participant-specific eligibility evaluation is needed.
- ``generate_response``: participant-specific eligibility/outcome question
  that needs the heavy ``required-data -> ForUsBots -> generate-response``
  pipeline. Used when answering requires reasoning about
  ``decision_guide`` + ``required_data_*`` chunks against participant facts.
- ``needs_more_info``: ambiguous topic OR no KB coverage for the specific
  question asked.

The classifier is coverage-driven: every classification runs a Pinecone
retrieval first and passes the resulting chunks (their types, scores, and
short excerpts) to a single LLM call. The LLM decides the route by reasoning
about (a) the inquiry and (b) the kind of KB content that came back, instead
of pattern-matching on surface form.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from data_pipeline.llm_router import LLMRouter
from data_pipeline.prompts import build_classify_inquiry_prompt
from data_pipeline.rag_engine import detect_advisory_concepts

logger = logging.getLogger(__name__)


CONFIDENCE_FALLBACK_THRESHOLD = 0.55

# Safety net: when the LLM emits knowledge_question but the top-retrieved
# chunk's similarity is below this threshold, treat the call as a hallucinated
# "covered" verdict and downgrade to needs_more_info. Calibrated against the
# 40-case eval (20 GR + 15 KQ + 5 NMI): every correctly-classified KQ in that
# set had top_score >= 0.43, so 0.40 catches marginal-coverage KQs without
# squashing real positives. The previous value (0.30) was too low to ever
# fire and provided no protection. Bump if real KQs start getting squashed.
KQ_TOP_SCORE_FLOOR = 0.40

# Number of chunks pulled into the coverage pack. Five is enough for the LLM
# to see the dominant article and a couple of related ones, without bloating
# the prompt.
COVERAGE_TOP_K = 5

# Excerpt cap (chars) per chunk in the rendered coverage block. The LLM only
# needs enough text to judge whether the chunk answers the question.
COVERAGE_EXCERPT_CHARS = 250

VALID_ROUTES = frozenset({"knowledge_question", "generate_response", "needs_more_info"})

VALID_COVERAGE_BASIS = frozenset({
    "kb_direct_answer",
    "participant_eligibility",
    "no_coverage",
    "topic_unclear",
})

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
class CoveragePack:
    """Snapshot of what Pinecone returned for the routing query.

    Passed to the LLM as a rendered block so the classifier reasons about
    actual KB content, not just the surface text of the inquiry. Also kept
    around so the engine can apply the post-LLM safety net (``top_score``)
    and surface coverage_signals in the response metadata.

    ``retrieval_status``:
        - ``ok``: at least one chunk came back.
        - ``empty``: Pinecone returned zero chunks for this query.
        - ``failed``: Pinecone raised an exception. ``pinecone_error`` carries
          the exception type name for observability.
    """

    retrieval_status: str
    top_score: float
    chunk_count: int
    distinct_articles: List[str]
    chunk_types_present: List[str]
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    pinecone_error: Optional[str] = None

    @classmethod
    def empty(cls) -> "CoveragePack":
        return cls(
            retrieval_status="empty",
            top_score=0.0,
            chunk_count=0,
            distinct_articles=[],
            chunk_types_present=[],
            chunks=[],
        )

    @classmethod
    def failed(cls, exception_name: str) -> "CoveragePack":
        return cls(
            retrieval_status="failed",
            top_score=0.0,
            chunk_count=0,
            distinct_articles=[],
            chunk_types_present=[],
            chunks=[],
            pinecone_error=exception_name,
        )

    def signals_dict(self) -> Dict[str, Any]:
        """Compact form of the pack for response metadata."""
        return {
            "retrieval_status": self.retrieval_status,
            "top_score": self.top_score,
            "chunk_count": self.chunk_count,
            "distinct_articles": self.distinct_articles,
            "chunk_types_present": self.chunk_types_present,
            "pinecone_error": self.pinecone_error,
        }

    def to_prompt_block(self) -> str:
        """Render the RETRIEVED_COVERAGE section injected into the user prompt.

        For ``ok`` packs, emits a summary header plus a numbered list of the
        top chunks (article title, chunk_type, chunk_tier, topic, score, and a
        short excerpt). For ``empty`` / ``failed`` packs, emits only the
        summary header — the LLM should treat this as "no chunks support the
        answer" and prefer NMI.
        """
        header_lines = [
            "RETRIEVED_COVERAGE:",
            f"  retrieval_status: {self.retrieval_status}",
            f"  top_score: {self.top_score:.2f}",
            f"  chunk_count: {self.chunk_count}",
            f"  distinct_articles: {self.distinct_articles}",
            f"  chunk_types_present: {self.chunk_types_present}",
        ]
        if self.pinecone_error:
            header_lines.append(f"  pinecone_error: {self.pinecone_error}")

        if not self.chunks:
            return "\n".join(header_lines)

        header_lines.append("  chunks:")
        for i, c in enumerate(self.chunks, 1):
            md = c.get("metadata", {}) or {}
            title = (
                md.get("article_title") or md.get("title") or "(untitled)"
            )
            chunk_type = md.get("chunk_type", "unknown")
            chunk_tier = md.get("chunk_tier", "unknown")
            topic = md.get("topic", "unknown")
            score = float(c.get("score", 0.0) or 0.0)
            excerpt = (md.get("content") or md.get("text") or "").strip()
            excerpt = re.sub(r"\s+", " ", excerpt)
            if len(excerpt) > COVERAGE_EXCERPT_CHARS:
                excerpt = excerpt[:COVERAGE_EXCERPT_CHARS].rstrip() + "..."
            header_lines.append(
                f"    {i}. [{title}] type={chunk_type} tier={chunk_tier} "
                f"topic={topic} score={score:.2f}"
            )
            header_lines.append(f"       {excerpt}")

        return "\n".join(header_lines)


# Callable contract: given a normalized inquiry, return a CoveragePack.
CoveragePackBuilder = Callable[[str], Awaitable[CoveragePack]]


# ---------------------------------------------------------------------------
# Deterministic predicates (now informative hints, not authoritative)
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
# signatures ("Thanks, -- John"). Normalizing once at engine entry keeps the
# LLM prompt and the Pinecone query both seeing the same clean intent.

_METADATA_LABEL_RE = re.compile(
    r"(?i)\b(?:subject|body|from|to|message|request|summary)\s*:\s*"
)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

_TRAILING_SIGNATURE_RE = re.compile(
    r"(?is)"
    r"(?:[\.!,\s])\s*"
    r"(?:--\s.*"
    r"|(?:thanks|thank\s+you|best|regards|sincerely|cheers)"
    r"(?:[,!.\s].*)?"
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
    """Combine the engine's advisory signals with classifier-only predicates.

    These features are no longer authoritative — the LLM treats them as hints
    and can override them when the retrieved chunks contradict.
    """
    base = detect_advisory_concepts(
        inquiry=inquiry,
        topic=None,
        collected_data=None,
    )
    has_action_verb = _has_action_verb(inquiry)
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
        "coverage_basis": "topic_unclear",
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
        parsed["coverage_basis"] = "topic_unclear"

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


def _resolve_coverage_basis(route: str, raw: Any) -> str:
    """Normalize the classifier's ``coverage_basis`` to a valid value.

    Coerces to a sensible default based on the final route when the LLM
    omits the field or emits an unknown value.
    """
    if isinstance(raw, str) and raw in VALID_COVERAGE_BASIS:
        return raw
    if route == "knowledge_question":
        return "kb_direct_answer"
    if route == "generate_response":
        return "participant_eligibility"
    return "topic_unclear"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class InquiryRouterEngine:
    """Coverage-driven (RAG + LLM) classifier for inbound inquiries."""

    LLM_MAX_TOKENS = 800

    def __init__(
        self,
        llm_router: LLMRouter,
        coverage_pack_builder: Optional[CoveragePackBuilder] = None,
    ):
        self._llm = llm_router
        self._coverage_pack_builder = coverage_pack_builder

    async def _build_coverage_pack(self, inquiry_norm: str) -> CoveragePack:
        """Run the configured pack builder, or return an empty pack.

        When no builder is wired (unit tests, degraded mode), the engine still
        proceeds — the LLM will see ``retrieval_status=empty`` and is steered
        toward NMI for anything that needs coverage evidence.
        """
        if self._coverage_pack_builder is None:
            return CoveragePack.empty()
        try:
            return await self._coverage_pack_builder(inquiry_norm)
        except Exception as exc:
            # Belt-and-suspenders: the builder is expected to catch its own
            # exceptions and return a ``failed`` pack, but if it doesn't we
            # don't want to take down the whole route-inquiry call.
            logger.warning(
                "Coverage pack builder raised (%s); treating as failed retrieval.",
                type(exc).__name__,
            )
            return CoveragePack.failed(type(exc).__name__)

    async def classify(self, inquiry: str) -> ClassificationResult:
        # Strip email scaffolding / inline emails / signatures once, up front;
        # every downstream consumer (deterministic signals, coverage pack,
        # LLM prompt) sees the cleaned form.
        inquiry_norm = _normalize_inquiry(inquiry)
        signals = compute_deterministic_features(inquiry_norm)

        coverage_pack = await self._build_coverage_pack(inquiry_norm)

        system_prompt, user_prompt = build_classify_inquiry_prompt(
            inquiry=inquiry_norm,
            signals=signals,
            coverage_block=coverage_pack.to_prompt_block(),
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
        # mid-string. Retry once against the configured fallback model.
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

        # Safety net: the LLM said KQ but the top chunk score is too weak to
        # support that verdict — the chunks are likely only topically adjacent
        # and the LLM hallucinated coverage. Downgrade to NMI.
        if (
            route == "knowledge_question"
            and coverage_pack.top_score < KQ_TOP_SCORE_FLOOR
        ):
            reasoning = (
                f"Safety net: top chunk score {coverage_pack.top_score:.2f} "
                f"below floor {KQ_TOP_SCORE_FLOOR:.2f}. Original: {reasoning}"
            )
            route = "needs_more_info"

        coverage_basis = _resolve_coverage_basis(route, parsed.get("coverage_basis"))
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
                "coverage_signals": coverage_pack.signals_dict(),
                "coverage_basis": coverage_basis,
                # Kept for backwards compatibility with existing consumers.
                "kb_coverage_top_score": coverage_pack.top_score,
                "kb_coverage_reasoning": reasoning,
            },
            user_message=user_message,
        )
