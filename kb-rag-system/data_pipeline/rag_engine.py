"""
RAG Engine - Motor principal de búsqueda y generación.

Este módulo implementa el RAG engine con dos funciones principales:
1. get_required_data() - Determina qué datos se necesitan
2. generate_response() - Genera respuesta contextualizada

All public methods are async to avoid blocking FastAPI's event loop.
Pinecone SDK calls (synchronous) are wrapped with asyncio.to_thread().
LLM calls are delegated to an `LLMRouter` that dispatches each task type
(decompose, required_data, gr_outcome, gr_response, knowledge_question) to
the configured provider (OpenAI or Gemini) with cross-provider fallback.
"""

import json
import asyncio
import copy
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from cachetools import TTLCache

from .pinecone_uploader import (
    PineconeCircuitOpen,
    PineconeRetrievalError,
    PineconeUploader,
)
from . import inquiry_semantics
from .token_manager import TokenManager
from .llm_router import LLMRouter, LLMResponse, LLMEmptyResponseError
from collections import defaultdict

from .prompts import (
    build_required_data_prompt,
    build_generate_response_prompt,
    build_knowledge_question_prompt,
    build_decompose_question_prompt,
    build_gr_outcome_prompt,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class RequiredField:
    """Campo de datos requerido."""
    field: str
    description: str
    why_needed: str
    data_type: str
    required: bool


@dataclass
class RequiredDataResponse:
    """Respuesta del endpoint /required-data."""
    article_reference: Dict[str, Any]
    required_fields: Dict[str, List[Dict[str, Any]]]
    confidence: float
    source_articles: List[Dict[str, Any]]
    used_chunks: List[Dict[str, Any]]
    coverage_gaps: List[str]
    metadata: Dict[str, Any]


@dataclass
class GenerateResponseResult:
    """
    Respuesta del endpoint /generate-response.

    Campos:
        decision: Calidad del retrieval RAG ("can_proceed", "uncertain", "out_of_scope").
                  Calculado por el engine basado en confidence score de Pinecone.
        confidence: Score de confianza del retrieval (0.0 - 1.0).
        response: Respuesta estructurada del LLM con schema outcome-driven.
                  Contiene: outcome, outcome_reason, response_to_participant,
                  questions_to_ask, escalation, guardrails_applied, data_gaps.
        source_articles: Deduplicated list of KB articles consulted.
        used_chunks: Individual chunks fed to the LLM, ordered by score descending.
        coverage_gaps: Core topics entirely absent from KB context.
        metadata: Info de diagnóstico (chunks_used, tokens, modelo).
    """
    decision: str  # "can_proceed", "uncertain", "out_of_scope"
    confidence: float
    response: Dict[str, Any]
    source_articles: List[Dict[str, Any]]
    used_chunks: List[Dict[str, Any]]
    coverage_gaps: List[str]
    metadata: Dict[str, Any]


@dataclass
class KnowledgeQuestionResult:
    """Respuesta del endpoint /knowledge-question."""
    answer: str
    key_points: List[str]
    source_articles: List[Dict[str, Any]]
    used_chunks: List[Dict[str, Any]]
    confidence_note: str
    metadata: Dict[str, Any]


# ============================================================================
# Topic normalization & deterministic detection helpers
# ============================================================================
#
# These helpers are shared with `inquiry_router` (and any future module that
# needs the same deterministic signals) so they live at module level rather
# than as RAGEngine methods. RAGEngine keeps thin wrapper methods that
# delegate here so internal callers and existing tests are unaffected.

# Callers send topics in their own vocabulary (often plural, colloquial,
# or hyphenated) while Pinecone chunks are tagged with a fixed set of
# canonical values. This table translates caller-side topics to the
# exact metadata values used at chunking time. An empty list means
# "no meaningful mapping — skip the topic filter entirely". Unmapped
# topics return ``None`` (skip the topic filter) so we don't activate
# topic-filtered lanes against a value that can't match anything.
TOPIC_NORMALIZATION_MAP: Dict[str, List[str]] = {
    "distribution": ["distribution"],
    "distributions": ["distribution"],
    "rollover": ["rollover", "termination_distribution_request", "distribution"],
    "rollovers": ["rollover", "termination_distribution_request", "distribution"],
    "incoming_rollover": ["rollover", "distribution"],
    "loan": ["loan"],
    "loans": ["loan"],
    "hardship": ["hardship_withdrawal"],
    "hardships": ["hardship_withdrawal"],
    "hardship_withdrawal": ["hardship_withdrawal"],
    "termination": ["termination_distribution_request"],
    "termination_distribution": ["termination_distribution_request"],
    "termination_distribution_request": ["termination_distribution_request"],
    "termination_movement": ["termination_distribution_request"],
    "excess_contribution": ["excess_contribution_refund"],
    "excess_contributions": ["excess_contribution_refund"],
    "excess_contribution_refund": ["excess_contribution_refund"],
    "in_service": ["in_service_withdrawal_options"],
    "in-service": ["in_service_withdrawal_options"],
    "in_service_withdrawal": ["in_service_withdrawal_options"],
    "in_service_withdrawal_options": ["in_service_withdrawal_options"],
    # RMDs are a distribution sub-type in the KB
    "rmd": ["distribution"],
    "rmds": ["distribution"],
    # Account access / security blocker (F3 split, eval 2026-06-22): login, MFA,
    # password-reset, stale-email and portal-access issues. The extractor emits
    # topic `account_access`; map it to the indexed account/security article
    # topics so the security inquiry retrieves real KB coverage instead of an
    # unfiltered text-only search.
    "account_access": [
        "account_access_and_management",
        "multi_factor_authentication",
        "ForUsAll_online_account_setup",
    ],
    # No indexed topic exists for these — skip the topic filter
    "investments": [],
    "investment": [],
    "contributions": [],
    "contribution": [],
    "general_inquiry": [],
    "taxes": [],
    "savings_rate": [],
    "dashboard_access": [],
}


def _contains_any(text: str, needles: List[str]) -> bool:
    return any(needle in text for needle in needles)


def _contains_bounded_phrase(text: str, phrase: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _hardship_with_context(text: str) -> bool:
    """Hardship triggers that are too generic on their own (``house``, ``home``,
    ``rent``, ``emergency``, ``medical``) only count when paired with an explicit
    hardship context phrase. This prevents non-hardship inquiries — e.g. an
    incoming rollover that merely mentions a house, or a routine question that
    says "emergency" — from falsely setting ``hardship_signal`` and injecting a
    hardship sub-query into retrieval (see eval 2026-06-22, F2)."""
    housing = (
        any(_contains_bounded_phrase(text, w) for w in ("house", "home", "rent", "rented"))
        and _contains_any(text, [
            "eviction", "foreclosure", "primary residence",
            "sold", "sale", "lose my", "losing my", "afford", "behind on",
        ])
    )
    medical = (
        _contains_bounded_phrase(text, "medical")
        and _contains_any(text, ["bill", "expense", "treatment", "surgery", "care"])
    )
    emergency = (
        _contains_bounded_phrase(text, "emergency")
        and _contains_any(text, ["financial", "hardship", "family", "medical"])
    )
    return housing or medical or emergency


def _ordered_unique(values: List[str]) -> List[str]:
    seen = set()
    unique = []
    for value in values:
        if not value:
            continue
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def resolve_topic_filter(topic: Optional[str]) -> Optional[List[str]]:
    """Translate a caller topic into Pinecone ``topic`` metadata values.

    Returns ``None`` when no topic filter should be applied: caller did
    not provide a topic, the mapping is explicitly empty, or the topic
    is unknown. Unknown topics return ``None`` instead of ``[topic]``
    because raw caller vocabulary almost never matches the canonical
    indexed values, so activating a topic-filtered lane against it
    would burn a rerank call on a filter guaranteed to return zero.
    """
    if not topic:
        return None
    key = topic.lower().strip()
    mapped = TOPIC_NORMALIZATION_MAP.get(key)
    if mapped is not None:
        return mapped or None
    return None


# F7 (eval 2026-06-22): phrases denoting an EXPLICIT, decided/completed claim that
# the participant has separated from THIS employer. Tighter than the broad
# separation list: excludes ambiguous/process tokens ("separation", bare
# "terminated", "quitting") and the rollover-source phrases "previous/former
# EMPLOYER" — so it never collides with the incoming-rollover path (F1).
_EXPLICIT_SEPARATION_PHRASES = [
    "resigned", "i quit", "quit my job", "was fired", "got fired",
    "fired me", "was laid off", "laid off", "was let go", "let me go",
    "no longer work", "no longer works", "no longer employed",
    "former employee", "left the company", "left my company",
    "left the job", "left my job", "used to work here", "used to work there",
    "my employment ended", "my employment was terminated",
    "i was terminated", "got terminated",
]

# TKT-911961: "separation distribution" / "separation from service distribution"
# / "termination distribution" is the NAME OF A PRODUCT, not a statement about
# the participant's employment. Asking about the product (while on leave and
# possibly returning) must never manufacture a termination, so the noun phrase
# is removed before any separation token is looked for. Genuine claims ("I
# separated from service", "I was terminated", "no longer work") are untouched
# because they carry no product noun.
# Only DISTRIBUTION product nouns are listed. Severance vocabulary
# ("separation package/paperwork/payment") is genuine separation language and
# must keep firing the signal, so it is deliberately absent here.
# TKT-911961 sibling: the leading noun is phrase-anchored — only the exact
# "<separation|termination> <distribution|withdrawal|payout>" adjacency is
# stripped. "after my termination, ...", "my termination date" and "I was
# terminated" keep their token because no product noun follows.
_SEPARATION_PRODUCT_NAME_RE = re.compile(
    r"\b(?:separation(?:\s+from\s+service)?|termination)[\s-]+"
    r"(?:distribution|withdrawal|payout)s?\b"
)


def strip_separation_product_names(text: Optional[str]) -> str:
    """Lowercase ``text`` with separation/termination product nouns removed."""
    return _SEPARATION_PRODUCT_NAME_RE.sub(" ", (text or "").lower())


# TKT-910081: a password reset the participant ALREADY TRIED that did not
# restore access. Shared verbatim by the knowledge-question recovery policy,
# the generate-response ``account_access`` signal and the orchestrator split
# guard so one sentence cannot route three different ways. Merely mentioning a
# reset — including an unsolicited reset email — is deliberately excluded, and
# a participant who reports regaining access is excluded as well.
_FAILED_PASSWORD_RESET_PATTERNS = (
    r"\b(?:password reset(?: (?:attempt|process|link))?|resetting (?:my|the|her|his|their) password)"
    r"(?: (?:still|already|has|had|was|is))? (?:did not work|didn t work|does not work|doesn t work|failed|unsuccessful)\b",
    r"\breset (?:my|the|her|his|their) password\b.{0,32}\bstill "
    r"(?:cannot|can t|could not|couldn t|am unable to|unable to) (?:log|sign) in\b",
)
_ACCESS_RECOVERED_RE = re.compile(r"\b(?:can now|now able to|now can) (?:log|sign) in\b")


def mentions_failed_password_reset(text: Optional[str]) -> bool:
    """True only when a reset was attempted AND did not restore access."""
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if _ACCESS_RECOVERED_RE.search(normalized):
        return False
    return any(re.search(pattern, normalized) for pattern in _FAILED_PASSWORD_RESET_PATTERNS)


def detect_advisory_concepts(
    inquiry: str,
    topic: Optional[str],
    collected_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministically detect cross-topic advisory paths before retrieval.

    Pure-function port of ``RAGEngine._detect_advisory_concepts``. Importable
    by ``inquiry_router`` and any other module that needs the same
    deterministic signals without instantiating a full ``RAGEngine``.

    Returned dict keys: ``active_participant``, ``wants_funds``,
    ``separation_signal``, ``hardship_signal``, ``loan_signal``,
    ``detected_concepts``, ``alternative_concepts``.
    """
    inquiry_text = (inquiry or "").lower()
    topic_text = (topic or "").lower().strip()
    text = f"{inquiry_text} {topic_text}"

    participant_data = (collected_data or {}).get("participant_data") or {}
    status_values = [
        participant_data.get("employment_status"),
        participant_data.get("participant_status"),
        participant_data.get("eligibility_status"),
        participant_data.get("status"),
    ]
    normalized_statuses = [
        re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()
        for v in status_values
        if v is not None
    ]

    active_from_status = any(
        status == "active" or status.startswith("active ")
        for status in normalized_statuses
    )
    inactive_from_status = any(
        status == "inactive"
        or status.startswith("inactive ")
        or status in {"terminated", "separated", "former"}
        or status.startswith("terminated ")
        or status.startswith("separated ")
        for status in normalized_statuses
    )

    active_text_signal = (
        _contains_any(text, [
            "still working",
            "while employed",
            "currently employed",
            "considering quitting",
            "quit in",
            "quitting in",
            "next 4 weeks",
            "next four weeks",
        ])
        or _contains_bounded_phrase(text, "active participant")
    )
    active_participant = (
        active_from_status
        or (not inactive_from_status and active_text_signal)
    )
    wants_funds = _contains_any(text, [
        "cash out",
        "cashout",
        "withdraw",
        "withdrawal",
        "take money",
        "take some money",
        "access money",
        "access funds",
        "need money",
        "money from",
        "distribution",
        "rollover",
        "roll over",
        "rolling over",
        "rolled over",
        "transfer",
        # "move my balance / 401k", "moving my funds" — possessive phrasing
        # that signals a transfer over the participant's own funds, without
        # false-positives from generic "move to a new state" usage.
        "move my",
        "moving my",
        "moved my",
    ])
    # F7 (eval 2026-06-22): an EXPLICIT, decided/completed claim that the
    # participant has separated from THIS employer (resigned, fired, laid off, no
    # longer works here). Deliberately tighter than separation_signal: it excludes
    # ambiguous/process tokens ("separation", bare "terminated", "quitting") and —
    # critically — the rollover-source phrases "previous/former EMPLOYER" (which
    # describe where an INCOMING rollover's money sits, not a self-separation), so
    # it never collides with the incoming-rollover path (F1). Used to override a
    # stale "active" system status: withhold active-only options
    # (hardship/loan/in-service) and route to ask-termination-date + escalate.
    explicit_separation_claim = _contains_any(text, _EXPLICIT_SEPARATION_PHRASES)
    # TKT-911961 sibling: the product-name strip must not silently drop a
    # participant who states a real separation AND names the product ("I no
    # longer work there. How do I request a termination distribution?"). An
    # explicit claim carries the signal on its own, so stripping the product
    # noun can only remove product-only firings.
    separation_signal = explicit_separation_claim or _contains_any(strip_separation_product_names(text), [
        "separation",
        "separated",
        "quit",
        "quitting",
        "leave my job",
        "leaving my job",
        "left my job",
        "left his employer",
        "left her employer",
        "left their employer",
        "left his company",
        "left her company",
        "left their company",
        "left the company",
        "left my company",
        "no longer employed",
        "former employer",
        "prior employer",
        "previous employer",
        "old employer",
        "former employee",
        "previous company",
        "old company",
        "previous job",
        "old job",
        "recently left",
        "termination",
        "terminated",
        "after employment",
    ])
    hardship_signal = _contains_any(text, [
        "hardship",
        "financial emergency",
        "eviction",
        "foreclosure",
        "primary residence",
        "funeral",
        "tuition",
    ]) or _hardship_with_context(text)
    loan_signal = _contains_any(text, ["loan", "borrow", "401(k) loan", "401k loan"])
    while_employed_funds_access = active_participant and wants_funds

    resolved_topic = resolve_topic_filter(topic) or ([topic_text] if topic_text else [])
    detected = list(resolved_topic)

    if separation_signal and wants_funds:
        detected.append("termination_distribution_request")
    if hardship_signal:
        detected.append("hardship_withdrawal")
    if loan_signal:
        detected.append("loan")
    if while_employed_funds_access:
        detected.append("in_service_withdrawal_options")

    alternatives: List[str] = []
    if while_employed_funds_access:
        alternatives.extend([
            "in_service_withdrawal_options",
            "hardship_withdrawal",
            "loan",
        ])
    elif hardship_signal:
        alternatives.append("hardship_withdrawal")
    if loan_signal:
        alternatives.append("loan")

    detected = _ordered_unique(detected)
    alternatives = [
        concept for concept in _ordered_unique(alternatives)
        if concept not in {"termination_distribution_request"}
    ]

    return {
        "active_participant": active_participant,
        "wants_funds": wants_funds,
        "separation_signal": separation_signal,
        "explicit_separation_claim": explicit_separation_claim,
        "hardship_signal": hardship_signal,
        "loan_signal": loan_signal,
        "detected_concepts": detected,
        "alternative_concepts": alternatives,
    }


# ============================================================================
# RAG Engine
# ============================================================================

class RAGEngine:
    """
    Motor RAG para búsqueda y generación de respuestas.

    Maneja dos endpoints principales:
    1. get_required_data() - ¿Qué datos necesitamos?
    2. generate_response() - ¿Cómo respondemos?
    """

    # Search cache: avoids repeated Pinecone queries for identical parameters.
    CACHE_MAX_SIZE = 128
    CACHE_TTL_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        llm_router: LLMRouter,
        pinecone_uploader: Optional['PineconeUploader'] = None,
    ):
        """
        Inicializa el RAG engine.

        Args:
            llm_router: Pre-configured LLMRouter with routes installed. All
                LLM calls are dispatched through this router by task_type.
            pinecone_uploader: Pre-configured PineconeUploader instance.
                If None, creates a new one (standalone usage).
        """
        if llm_router is None:
            raise ValueError("llm_router is required")

        self.router = llm_router

        # Pinecone uploader para búsquedas (sync SDK, wrapped with asyncio.to_thread).
        # Reuse a shared instance when provided to avoid duplicate connections.
        self.pinecone = pinecone_uploader or PineconeUploader()

        self.token_manager = TokenManager(model="gpt-4")

        # TTL cache for Pinecone search results
        self._search_cache: TTLCache = TTLCache(
            maxsize=self.CACHE_MAX_SIZE,
            ttl=self.CACHE_TTL_SECONDS
        )

        logger.info("RAG Engine initialised with LLM router")

    # ========================================================================
    # ENDPOINT 1: Get Required Data
    # ========================================================================

    async def get_required_data(
        self,
        inquiry: str,
        record_keeper: Optional[str],
        plan_type: str,
        topic: str,
        related_inquiries: Optional[List[str]] = None
    ) -> RequiredDataResponse:
        """
        Determina qué datos se necesitan para responder una inquiry.

        Pipeline:
        1. Decompose inquiry into focused sub-queries (LLM)
        2. Parallel multi-query search with RK cascade
        3. Merge, deduplicate, and rank all retrieved chunks
        4. Build context with article diversity enforcement
        5. Generate required fields via LLM (with coverage gap detection)
        6. Hybrid confidence (retrieval + LLM gap signal)
        7. Source articles and used chunks transparency

        Args:
            inquiry: La consulta del participante
            record_keeper: Record keeper (ej: "LT Trust")
            plan_type: Tipo de plan (ej: "401(k)")
            topic: Tema principal (ej: "rollover", "distribution")
            related_inquiries: Otras inquiries relacionadas (opcional)

        Returns:
            RequiredDataResponse con campos necesarios
        """
        logger.info("get_required_data started (inquiry_length=%d)", len(inquiry))

        try:
            # 1. Decompose inquiry into sub-queries (Fix 7: anchor on RK + topic)
            sub_queries = await self._decompose_question(
                inquiry,
                record_keeper=record_keeper or "",
                topic=topic or "",
            )
            advisory_signal = self._detect_advisory_concepts(
                inquiry=inquiry,
                topic=topic,
                collected_data=None,
            )
            sub_queries = self._expand_queries_with_advisory_concepts(
                sub_queries=sub_queries,
                inquiry=inquiry,
                topic=topic,
                advisory_signal=advisory_signal,
            )
            enriched_queries = [f"{sq} {topic}" for sq in sub_queries]

            # Include original inquiry as fallback if decomposition didn't preserve it
            if inquiry not in sub_queries:
                enriched_queries.append(f"{inquiry} {topic}")

            logger.info(f"Decomposed into {len(sub_queries)} sub-queries "
                        f"(text redacted, {sum(len(s) for s in sub_queries)} chars)")

            # Fix 4: Build the deterministic retrieval profile so required_data
            # can use the same primary_article_id / excluded_articles signals
            # already computed for generate_response. The named-employer
            # relaxation is enabled because participants often describe
            # terminations as "rollover process from <X> to <Y>".
            retrieval_profile = self._build_retrieval_profile(
                inquiry=inquiry,
                topic=topic,
                record_keeper=record_keeper,
                plan_type=plan_type,
                collected_data=None,
                assume_termination_on_named_employer=True,
            )

            # 2. Parallel multi-query search with RK cascade
            chunks, per_query_scores = await self._search_for_required_data(
                enriched_queries=enriched_queries,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                inquiry=inquiry,
                retrieval_profile=retrieval_profile,
            )

            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_empty_required_data_response(
                    "No relevant articles found for this topic"
                )

            # 2b. Guard 1: Retrieval quality gate — skip LLM if no rdmh chunk is relevant.
            #    Fix 6: require both boosted score >= RD_RETRIEVAL_MIN_SCORE AND
            #    raw cosine score >= RD_RETRIEVAL_MIN_RAW_SCORE so the boost
            #    can save a near-miss but cannot manufacture a false positive.
            rdmh_chunks = [
                c for c in chunks
                if c['metadata'].get('chunk_type') == 'required_data_must_have'
            ]
            best_rdmh_score = max(
                (c.get('score', 0) for c in rdmh_chunks),
                default=0.0
            )
            best_rdmh_raw_score = max(
                (c.get('raw_score', c.get('score', 0)) for c in rdmh_chunks),
                default=0.0
            )
            if (
                best_rdmh_score < self.RD_RETRIEVAL_MIN_SCORE
                or best_rdmh_raw_score < self.RD_RETRIEVAL_MIN_RAW_SCORE
            ):
                logger.info(
                    f"Retrieval quality gate: best rdmh score {best_rdmh_score:.4f} "
                    f"(raw {best_rdmh_raw_score:.4f}) below thresholds "
                    f"score>={self.RD_RETRIEVAL_MIN_SCORE} "
                    f"raw>={self.RD_RETRIEVAL_MIN_RAW_SCORE}. Skipping LLM."
                )
                pqs = {
                    sq: per_query_scores.get(eq, 0.0)
                    for sq, eq in zip(
                        sub_queries, enriched_queries, strict=False
                    )
                }
                source_articles = self._build_source_articles(
                    chunks[:self.RD_MAX_CHUNKS_PER_ARTICLE * 3]
                )
                return self._build_no_match_required_data_response(
                    reason=(
                        f"Best required_data_must_have score ({best_rdmh_score:.4f}) "
                        f"below threshold ({self.RD_RETRIEVAL_MIN_SCORE})"
                    ),
                    confidence=0.0,
                    source_articles=source_articles,
                    used_chunks=[],
                    coverage_gaps=[f"No relevant KB article found for topic: {topic}"],
                    sub_queries=sub_queries,
                    per_query_scores=pqs,
                    tokens_used=0,
                )

            # 3. Build context — primary-first when a primary article was
            #    deterministically routed (Fix 5), otherwise fall back to
            #    diversity-balanced building.
            primary_article_id = (retrieval_profile or {}).get(
                "primary_article_id"
            )
            if primary_article_id:
                context, selected_chunks, tokens_used = (
                    self._build_required_data_context_primary_first(
                        chunks=chunks,
                        budget=self.RD_CONTEXT_BUDGET,
                        max_per_article=self.RD_MAX_CHUNKS_PER_ARTICLE,
                        primary_article_id=primary_article_id,
                    )
                )
            else:
                context, selected_chunks, tokens_used = self._build_context_with_diversity(
                    chunks=chunks,
                    budget=self.RD_CONTEXT_BUDGET,
                    prioritize_types=['required_data_must_have', 'eligibility', 'business_rules'],
                    max_per_article=self.RD_MAX_CHUNKS_PER_ARTICLE,
                    enforce_type_order=True,
                )

            logger.info(f"Context construido: {len(selected_chunks)} chunks, {tokens_used} tokens")

            # 3b. Nice-to-have augmentation: append the top article's
            #     required_data_nice_to_have chunk(s) so the LLM can emit
            #     required:false fields. Surgical by design — one filtered
            #     query, never affects the must_have quality gate above.
            context, selected_chunks, tokens_used = (
                await self._augment_context_with_nice_to_have(
                    inquiry=inquiry,
                    primary_article_id=primary_article_id,
                    context=context,
                    selected_chunks=selected_chunks,
                    tokens_used=tokens_used,
                )
            )

            # Required-data sections are a structured KB contract. Interpret
            # that contract directly when every selected field is valid;
            # an LLM is only needed for legacy/non-contract chunks.
            parsed, extraction_metadata = self._required_data_from_chunks(selected_chunks)
            extraction_attempts: List[Dict[str, Any]] = []
            llm_usage = None
            llm_provider_used: Optional[str] = None
            llm_model_used: Optional[str] = None
            llm_response = ""
            coverage_gaps: List[str] = []
            if parsed is not None:
                extraction_metadata["extraction_method"] = "validated_chunk_contract"
                llm_response = json.dumps(parsed)
            else:
                extraction_metadata["extraction_method"] = "llm"
                system_prompt, user_prompt = build_required_data_prompt(
                    context=context,
                    inquiry=inquiry,
                    record_keeper=record_keeper,
                    plan_type=plan_type,
                    topic=topic,
                )
                failure_kind: Optional[str] = None
                for attempt in range(2):
                    attempt_info: Dict[str, Any] = {"attempt": attempt + 1}
                    try:
                        if attempt == 0:
                            llm_result = await self._call_llm(
                                system_prompt=system_prompt,
                                user_prompt=user_prompt,
                                max_tokens=800,
                                task_type="required_data",
                            )
                        else:
                            llm_result = await self.router.call(
                                task_type="required_data",
                                system_prompt=system_prompt,
                                user_prompt=user_prompt,
                                max_tokens=800,
                                force_fallback=True,
                            )
                        llm_response = llm_result.content
                        llm_usage = llm_result.usage
                        llm_provider_used = llm_result.provider_used
                        llm_model_used = llm_result.model_used
                        failure_kind = self._required_data_extraction_failure(llm_response)
                        attempt_info["response_characters"] = len(llm_response or "")
                    except LLMEmptyResponseError as exc:
                        failure_kind = "empty_extraction"
                        attempt_info["finish_reason"] = exc.finish_reason
                        llm_usage = exc.usage
                        llm_provider_used = exc.provider_used
                        llm_model_used = exc.model_used
                    except Exception as exc:
                        failure_kind = "provider_error"
                        logger.error("Required-data provider failed (error_type=%s)", type(exc).__name__)
                    attempt_info["failure_kind"] = failure_kind
                    extraction_attempts.append(attempt_info)
                    if failure_kind is None:
                        parsed, coverage_gaps = self._parse_required_data_response(llm_response)
                        break
                if failure_kind is not None:
                    failed = self._build_empty_required_data_response(
                        "required_data_failed",
                        retrieval_failure_kind="unknown",
                        retrieval_retryable=False,
                    )
                    failed.used_chunks = self._serialize_used_chunks(selected_chunks)
                    failed.source_articles = self._build_source_articles(selected_chunks)
                    failed.metadata.update({
                        **extraction_metadata,
                        "required_data_failure_kind": failure_kind,
                        "extraction_attempts": extraction_attempts,
                        "chunks_used": len(selected_chunks),
                        "context_tokens": tokens_used,
                    })
                    return failed
            assert parsed is not None

            # Remove coverage_gaps from required_fields (it's a separate response field)
            required_fields = {k: v for k, v in parsed.items() if k != "coverage_gaps"}

            # 6. Hybrid confidence (retrieval + LLM gap signal)
            confidence = self._calculate_required_data_confidence(
                chunks, query_topic=topic, coverage_gaps=coverage_gaps
            )

            # 6b. Guard 2: No-match gate — null article reference when confidence
            #     is low and LLM reports gaps
            if confidence < self.RD_NO_MATCH_CONFIDENCE and coverage_gaps:
                logger.info(
                    f"No-match gate: confidence {confidence:.3f} "
                    f"< {self.RD_NO_MATCH_CONFIDENCE} with "
                    f"{len(coverage_gaps)} coverage gap(s). Nulling article reference."
                )
                source_articles = self._build_source_articles(selected_chunks)
                used_chunks_serialized = self._serialize_used_chunks(selected_chunks)
                pqs = {
                    sq: per_query_scores.get(eq, 0.0)
                    for sq, eq in zip(
                        sub_queries, enriched_queries, strict=False
                    )
                }
                return self._build_no_match_required_data_response(
                    reason=(
                        f"Confidence ({confidence:.3f}) below threshold "
                        f"with coverage gaps: {coverage_gaps}"
                    ),
                    confidence=confidence,
                    source_articles=source_articles,
                    used_chunks=used_chunks_serialized,
                    coverage_gaps=coverage_gaps,
                    sub_queries=sub_queries,
                    per_query_scores=pqs,
                    tokens_used=tokens_used,
                    llm_usage=llm_usage,
                    llm_model=llm_model_used,
                    llm_provider=llm_provider_used,
                    llm_response=llm_response,
                )

            # 7. Build source articles and used chunks
            source_articles = self._build_source_articles(selected_chunks)
            used_chunks = self._serialize_used_chunks(selected_chunks)

            total_articles = len(source_articles)
            relevant_articles = sum(
                1 for sa in source_articles if sa.get("used_info", False)
            )

            # Remap per_query_scores keys to original sub-queries for readability
            pqs = {
                sq: per_query_scores.get(eq, 0.0)
                for sq, eq in zip(sub_queries, enriched_queries, strict=False)
            }

            return RequiredDataResponse(
                article_reference={
                    "article_id": chunks[0]['metadata'].get('article_id'),
                    "title": chunks[0]['metadata'].get('article_title'),
                    "confidence": confidence
                },
                required_fields=required_fields,
                confidence=confidence,
                source_articles=source_articles,
                used_chunks=used_chunks,
                coverage_gaps=coverage_gaps,
                metadata={
                    "chunks_used": len(selected_chunks),
                    "context_tokens": tokens_used,
                    "response_tokens": self.token_manager.count_tokens(llm_response),
                    "prompt_tokens": llm_usage.get("prompt_tokens", 0) if llm_usage else 0,
                    "completion_tokens": llm_usage.get("completion_tokens", 0) if llm_usage else 0,
                    "total_tokens": llm_usage.get("total_tokens", 0) if llm_usage else 0,
                    "model": llm_model_used,
                    "provider": llm_provider_used,
                    "sub_queries": sub_queries,
                    "per_query_scores": pqs,
                    "unique_articles": total_articles,
                    "relevant_articles": relevant_articles,
                    "coverage_gaps": coverage_gaps,
                    **extraction_metadata,
                    "extraction_attempts": extraction_attempts,
                    "detected_concepts": advisory_signal.get("detected_concepts", []),
                    "alternative_concepts": advisory_signal.get("alternative_concepts", []),
                }
            )

        except Exception as exc:
            logger.error("Error en get_required_data")
            transient_kinds = {
                "timeout", "transport", "rate_limit", "server_error",
                "circuit_open",
            }
            is_retrieval_failure = isinstance(
                exc, (PineconeRetrievalError, PineconeCircuitOpen)
            )
            failure_kind = (
                getattr(exc, "failure_kind", "unknown")
                if is_retrieval_failure else "unknown"
            )
            if failure_kind not in transient_kinds | {"client_error"}:
                failure_kind = "unknown"
            return self._build_empty_required_data_response(
                "required_data_failed",
                retrieval_failure_kind=failure_kind,
                retrieval_retryable=(
                    is_retrieval_failure
                    and getattr(exc, "retryable", False) is True
                    and failure_kind in transient_kinds
                ),
            )

    # ========================================================================
    # ENDPOINT 2: Generate Response
    # ========================================================================

    async def generate_response(
        self,
        inquiry: str,
        record_keeper: Optional[str],
        plan_type: str,
        topic: str,
        collected_data: Dict[str, Any],
        max_response_tokens: int,
        total_inquiries_in_ticket: int = 1
    ) -> GenerateResponseResult:
        """
        Generate a contextualised response using a two-phase LLM architecture.

        Pipeline:
        1. Decompose inquiry into focused sub-queries (LLM)
        2. KQ-style parallel search (simplified, unfiltered + plan_type safety net)
        3. Merge, deduplicate, and rank all retrieved chunks
        4. Build context with article diversity + tier priority
        5a. Phase 1 — Outcome determination (lightweight LLM call, max 500 tokens)
            Fast path: if out_of_scope_inquiry, return immediately
        5b. Phase 2 — Response generation (outcome-conditional schema, remaining budget)
        6. Hybrid confidence + source article transparency
        """
        logger.info(
            "generate_response started (budget_tokens=%d)", max_response_tokens
        )

        try:
            # 0. Derive participant age from birth_date (deterministically, in
            # code) so both phases can make age-dependent determinations
            # (in-service 59½ eligibility, early-withdrawal penalty) DEFINITE
            # instead of hedging "if you're under 59½". No-op when birth_date is
            # absent or unparseable.
            collected_data = self._enrich_collected_data_with_age(collected_data)

            # 1. Context budget with minimum floor
            context_budget = max(
                self.RESPONSE_MIN_CONTEXT_TOKENS,
                max_response_tokens - self.RESPONSE_MIN_TOKENS
            )

            logger.info(f"Context budget: {context_budget} tokens (de {max_response_tokens} total, reservando {self.RESPONSE_MIN_TOKENS} para response)")

            # 2. Decompose inquiry into sub-queries
            sub_queries = await self._decompose_question(inquiry)
            advisory_signal = self._detect_advisory_concepts(
                inquiry=inquiry,
                topic=topic,
                collected_data=collected_data,
            )
            retrieval_profile = self._build_retrieval_profile(
                inquiry=inquiry,
                topic=topic,
                record_keeper=record_keeper,
                plan_type=plan_type,
                collected_data=collected_data,
            )
            sub_queries = self._expand_queries_with_advisory_concepts(
                sub_queries=sub_queries,
                inquiry=inquiry,
                topic=topic,
                advisory_signal=advisory_signal,
            )
            logger.info(f"Decomposed into {len(sub_queries)} sub-queries "
                        f"(text redacted, {sum(len(s) for s in sub_queries)} chars)")

            enriched_queries = []
            for sq in sub_queries:
                parts = [sq, topic]
                # Task 8 (HT-14): sólo NOMBRES de campo (conceptos) enriquecen
                # la query; los VALORES del participante (balances, fechas,
                # emails...) nunca viajan a embeddings/Pinecone.
                if collected_data and "participant_data" in collected_data:
                    field_names = list(collected_data["participant_data"].keys())[:3]
                    parts.extend(str(name).replace("_", " ") for name in field_names)
                enriched_queries.append(" ".join(parts))

            if inquiry not in sub_queries:
                fallback_parts = [inquiry, topic]
                enriched_queries.append(" ".join(fallback_parts))

            # 3. Parallel RK/topic-aware cascade search
            if retrieval_profile.get("mode") == "exact_procedure":
                chunks, per_query_scores = await self._search_for_exact_response_procedure(
                    enriched_queries=enriched_queries,
                    retrieval_profile=retrieval_profile,
                )
                if not chunks:
                    logger.info(
                        "Exact procedure routing found insufficient primary article "
                        "coverage; falling back to parallel cascade."
                    )
                    chunks, per_query_scores = await self._search_for_response_parallel_cascade(
                        enriched_queries=enriched_queries,
                        record_keeper=record_keeper,
                        plan_type=plan_type,
                        topic=topic,
                    )
            else:
                chunks, per_query_scores = await self._search_for_response_parallel_cascade(
                    enriched_queries=enriched_queries,
                    record_keeper=record_keeper,
                    plan_type=plan_type,
                    topic=topic,
                )

            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_uncertain_response(
                    "no_relevant_articles",
                    confidence=0.0
                )

            chunks_pre_filter = chunks
            chunks = self._filter_excluded_response_articles(
                chunks=chunks,
                retrieval_profile=retrieval_profile,
            )
            if not chunks and chunks_pre_filter:
                logger.warning(
                    f"All {len(chunks_pre_filter)} retrieved chunks were excluded "
                    f"(mode={retrieval_profile.get('mode')}, "
                    f"excluded={len(retrieval_profile.get('excluded_articles', []))}); "
                    f"relaxing exclusions to avoid false-negative response"
                )
                chunks = chunks_pre_filter
            chunks, bundle_info = await self._add_response_article_bundles(
                chunks=chunks,
                advisory_signal=advisory_signal,
                retrieval_profile=retrieval_profile,
            )
            chunks = self._rank_response_chunks(
                chunks=chunks,
                advisory_signal=advisory_signal,
                topic=topic,
            )

            # 4. Build context with diversity + tier priority (or dominant-article mode)
            context, selected_chunks, tokens_used, dominance_info = self._build_context_with_diversity_and_tiers(
                chunks=chunks,
                budget=context_budget,
                max_per_article=self.GR_MAX_CHUNKS_PER_ARTICLE,
                advisory_signal=advisory_signal,
                retrieval_profile=retrieval_profile,
            )
            dominant_mode = dominance_info.get("dominant_mode", False)

            logger.info(
                f"Context: {len(selected_chunks)} chunks, {tokens_used} tokens, "
                f"dominant_mode={dominant_mode}"
            )

            # ================================================================
            # 5a. Phase 1 — Outcome Determination
            # ================================================================
            phase1_start = time.monotonic()
            p1_system, p1_user = build_gr_outcome_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                dominant_mode=dominant_mode,
            )

            phase1_usage = None
            phase1_provider: Optional[str] = None
            phase1_model: Optional[str] = None
            outcome = None
            outcome_reason = None
            p1_parsed: Optional[Dict[str, Any]] = None

            try:
                p1_result = await asyncio.wait_for(
                    self._call_llm(
                        system_prompt=p1_system,
                        user_prompt=p1_user,
                        max_tokens=self.GR_PHASE1_MAX_TOKENS,
                        task_type="gr_outcome",
                    ),
                    timeout=self.GR_PHASE1_TIMEOUT_SECONDS,
                )
                phase1_usage = p1_result.usage
                phase1_provider = p1_result.provider_used
                phase1_model = p1_result.model_used
                p1_parsed = json.loads(p1_result.content)
                outcome = p1_parsed.get("outcome", "ambiguous_plan_rules")
                outcome_reason = p1_parsed.get("outcome_reason", "")
                logger.info("Phase 1 outcome parsed")
            except asyncio.TimeoutError:
                logger.warning(f"Phase 1 timed out after {self.GR_PHASE1_TIMEOUT_SECONDS}s")
                outcome = "ambiguous_plan_rules"
                outcome_reason = (
                    "Phase 1 outcome determination timed out; proceeding with "
                    "the full response-generation pass."
                )
            except (json.JSONDecodeError, LLMEmptyResponseError) as exc:
                logger.error("Phase 1 failed (error_type=%s)", type(exc).__name__)
                outcome = "ambiguous_plan_rules"
                outcome_reason = "Phase 1 outcome determination failed; proceeding with conservative outcome."

            phase1_elapsed = time.monotonic() - phase1_start
            logger.info(f"Phase 1 completed in {phase1_elapsed:.1f}s")

            # ── Fast path: out_of_scope_inquiry ──
            if outcome == "out_of_scope_inquiry":
                logger.info("Off-topic inquiry selected fast path")
                oos_opening = (p1_parsed.get("opening") or "").strip() if isinstance(p1_parsed, dict) else ""
                if not oos_opening:
                    oos_opening = (
                        "I can only assist with retirement plan-related questions such as "
                        "401(k) distributions, rollovers, loans, and account access. "
                        "Your inquiry appears to be outside this scope."
                    )
                oos_parsed = {
                    "outcome": "out_of_scope_inquiry",
                    "outcome_reason": outcome_reason,
                    "response_to_participant": {
                        "opening": oos_opening,
                        "key_points": [],
                        "steps": [],
                        "warnings": []
                    },
                    "questions_to_ask": [],
                    "escalation": {"needed": False, "reason": None},
                    "guardrails_applied": [],
                    "data_gaps": [],
                    "coverage_gaps": [],
                }
                pqs = {
                    sq: per_query_scores.get(eq, 0.0)
                    for sq, eq in zip(
                        sub_queries, enriched_queries, strict=False
                    )
                }
                return GenerateResponseResult(
                    decision="out_of_scope",
                    confidence=0.1,
                    response=oos_parsed,
                    source_articles=[],
                    used_chunks=[],
                    coverage_gaps=[],
                    metadata={
                        **self._gr_metadata(
                            [], tokens_used, json.dumps(oos_parsed), phase1_usage,
                            total_inquiries_in_ticket, sub_queries,
                            per_query_scores, enriched_queries,
                            phase="phase1_oos",
                            llm_model=phase1_model,
                            llm_provider=phase1_provider,
                            dominance_info=dominance_info,
                        ),
                        "off_topic": True,
                    },
                )

            # ================================================================
            # 5b. Unified LLM call (outcome + response in a single pass)
            # ================================================================
            # Phase 1's outcome is used only as an out-of-scope gate (handled
            # above); for in-scope inquiries we discard it and let the unified
            # call determine outcome + redact the full response with access to
            # its own reasoning and the richer cross-article / deduplication
            # rules in SYSTEM_PROMPT_GENERATE_RESPONSE.
            completion_budget = max(self.RESPONSE_MIN_TOKENS, max_response_tokens - tokens_used)
            unified_timeout = max(30, self.GR_LLM_TIMEOUT_SECONDS - phase1_elapsed - 5)

            unified_system, unified_user = build_generate_response_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                max_tokens=completion_budget,
                dominant_mode=dominant_mode,
            )

            phase2_usage = None
            phase2_provider: Optional[str] = None
            phase2_model: Optional[str] = None
            try:
                unified_result = await asyncio.wait_for(
                    self._call_llm(
                        system_prompt=unified_system,
                        user_prompt=unified_user,
                        max_tokens=completion_budget,
                        task_type="gr_response",
                    ),
                    timeout=unified_timeout,
                )
                llm_response = unified_result.content
                phase2_usage = unified_result.usage
                phase2_provider = unified_result.provider_used
                phase2_model = unified_result.model_used
            except asyncio.TimeoutError:
                logger.warning(f"Unified LLM call timed out after {unified_timeout:.0f}s")
                llm_response = json.dumps(
                    self._build_llm_timeout_fallback(inquiry, selected_chunks)
                )
            except LLMEmptyResponseError:
                logger.error("Unified LLM returned empty content")
                llm_response = json.dumps(
                    self._build_llm_fallback_parsed("empty_response")
                )
            except Exception as exc:
                logger.error("Unified LLM error (error_type=%s)",
                             type(exc).__name__)
                llm_response = json.dumps(
                    self._build_llm_timeout_fallback(inquiry, selected_chunks)
                )

            # 6. Parse unified LLM response
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError:
                logger.error(
                    "Unified LLM JSON parse failure (length=%d)",
                    len(llm_response or ""),
                )
                parsed = {
                    "outcome": "ambiguous_plan_rules",
                    "outcome_reason": "Response parsing failed — raw LLM output could not be parsed as JSON.",
                    "response_to_participant": {
                        "opening": "We were unable to generate a structured response for your inquiry.",
                        # HT-09/HT-15: nunca incrustar output LLM raw en una estructura
                        # participant-facing; solo un marcador tecnico
                        # (la escalacion abajo fuerza revision humana).
                        "key_points": ["[technical_parse_failure]"] if llm_response else [],
                        "steps": [],
                        "warnings": []
                    },
                    "questions_to_ask": [],
                    "escalation": {
                        "needed": True,
                        "reason": "Response parsing failed. Please contact Support for assistance."
                    },
                    "guardrails_applied": [],
                    "data_gaps": ["LLM response was not valid JSON"],
                    "coverage_gaps": []
                }

            # Validate required structure
            missing_keys = self._LLM_RESPONSE_REQUIRED_KEYS - parsed.keys()
            if missing_keys:
                logger.error(
                    "Unified response schema rejected "
                    "(missing_key_count=%d, present_key_count=%d)",
                    len(missing_keys),
                    len(parsed),
                )
                parsed = self._build_llm_fallback_parsed(
                    f"missing keys: {', '.join(sorted(missing_keys))}"
                )

            parsed, outcome_policy_info = self._apply_informational_outcome_policy(
                parsed=parsed,
                retrieval_profile=retrieval_profile,
                collected_data=collected_data,
                selected_chunks=selected_chunks,
            )
            parsed, termination_response_policy_info = (
                self._apply_termination_response_policy(
                    parsed=parsed,
                    retrieval_profile=retrieval_profile,
                    collected_data=collected_data,
                )
            )
            # A rollover request carrying an identifier subquestion keeps its
            # procedure answer, but the identifier itself stays distinct from
            # the plan code and the receiving account and is never invented.
            parsed, account_identifier_info = (
                self._apply_account_identifier_distinctions(
                    parsed=parsed,
                    retrieval_profile=retrieval_profile,
                    collected_data=collected_data,
                )
            )
            termination_response_policy_info["account_identifier"] = account_identifier_info

            # Defense-in-depth: a can_proceed answer is a complete first-contact
            # resolution. Any items the LLM left in questions_to_ask are
            # non-blocking execution/next-step details (requested amount,
            # repayment term, delivery method, ...) the participant handles during
            # the process. Asking them only reopens the ticket, so they are
            # dropped here — the prompts already instruct the model to surface
            # them as forward guidance inside steps/key_points instead.
            stray_questions = self._suppress_nonblocking_questions_for_can_proceed(parsed)
            if stray_questions:
                logger.info(
                    "Suppressed %d non-blocking question(s) on a can_proceed "
                    "outcome",
                    len(stray_questions),
                )

            from data_pipeline.response_handoff import response_requires_review

            question_metadata = self._validate_question_coverage(parsed, collected_data)
            from data_pipeline.gr_payload_builder import (
                project_verified_participant_facts, project_verified_plan_facts,
            )
            disclosure = project_verified_participant_facts((collected_data or {}).get("internal_disclosure_context"))
            if disclosure:
                question_metadata["verified_participant_facts"] = disclosure
            plan_disclosure = project_verified_plan_facts((collected_data or {}).get("internal_plan_disclosure_context"))
            if plan_disclosure:
                question_metadata["verified_plan_facts"] = plan_disclosure
            if response_requires_review(parsed, question_metadata):
                question_metadata["human_review_required"] = True
            if termination_response_policy_info.get("custody_review_required") or termination_response_policy_info.get("plan_review_required"):
                question_metadata["human_review_required"] = True
                question_metadata["handoff"] = {
                    "reason": "plan_servicing_verification" if termination_response_policy_info.get("plan_review_required") else "custody_verification",
                    "next_action": "Verify the dated plan lifecycle and participant disposition record before approving execution guidance.",
                    "plan_context": (collected_data or {}).get("internal_plan_context", {}),
                    "preflight": (collected_data or {}).get("internal_preflight_context", {}),
                }
            if termination_response_policy_info.get("inapplicable_options_unresolved"):
                question_metadata["human_review_required"] = True

            coverage_gaps = parsed.get("coverage_gaps", [])
            if not isinstance(coverage_gaps, list):
                coverage_gaps = []
            coverage_gaps = [g for g in coverage_gaps if isinstance(g, str) and g.strip()]

            # 7. Hybrid confidence (retrieval + LLM gap signal + outcome floor)
            confidence = self._calculate_confidence(selected_chunks, coverage_gaps=coverage_gaps)

            # Outcome is a stronger signal than retrieval metrics alone.
            # Index the floor by the outcome produced by the unified LLM call.
            final_outcome = parsed.get("outcome")
            OUTCOME_CONFIDENCE_FLOORS = {
                "can_proceed": 0.65,
                "blocked_not_eligible": 0.65,
                "blocked_missing_data": 0.55,
                "ambiguous_plan_rules": 0.50,
            }
            floor = OUTCOME_CONFIDENCE_FLOORS.get(final_outcome, 0.0)
            if floor and confidence < floor:
                logger.info(
                    "Confidence floor applied: %.3f -> %.3f",
                    confidence,
                    floor,
                )
                confidence = floor

            decision = self._determine_decision(confidence)

            # 8. Source articles and used chunks
            source_articles = self._build_source_articles(selected_chunks)
            used_chunks = self._serialize_used_chunks(selected_chunks)
            total_articles = len(source_articles)
            relevant_articles = sum(
                1 for sa in source_articles if sa.get("used_info", False)
            )

            pqs = {
                sq: per_query_scores.get(eq, 0.0)
                for sq, eq in zip(sub_queries, enriched_queries, strict=False)
            }

            # Combine usage from both phases
            combined_usage = self._combine_llm_usage(phase1_usage, phase2_usage)

            return GenerateResponseResult(
                decision=decision,
                confidence=confidence,
                response=parsed,
                source_articles=source_articles,
                used_chunks=used_chunks,
                coverage_gaps=coverage_gaps,
                metadata={
                    **question_metadata,
                    "chunks_used": len(selected_chunks),
                    "context_tokens": tokens_used,
                    "response_tokens": self.token_manager.count_tokens(llm_response),
                    "prompt_tokens": combined_usage.get("prompt_tokens", 0),
                    "completion_tokens": combined_usage.get("completion_tokens", 0),
                    "total_tokens": combined_usage.get("total_tokens", 0),
                    "phase1_model": phase1_model,
                    "phase1_provider": phase1_provider,
                    "phase2_model": phase2_model,
                    "phase2_provider": phase2_provider,
                    "total_inquiries": total_inquiries_in_ticket,
                    "sub_queries": sub_queries,
                    "per_query_scores": pqs,
                    "unique_articles": total_articles,
                    "relevant_articles": relevant_articles,
                    "coverage_gaps": coverage_gaps,
                    "phase1_elapsed_s": round(phase1_elapsed, 1),
                    "phase1_relevance_signal": outcome,
                    "final_outcome": final_outcome,
                    "dominant_mode": dominant_mode,
                    "dominance_top_signal": dominance_info.get("top_signal"),
                    "dominance_runner_up_signal": dominance_info.get("runner_up_signal"),
                    "dominance_ratio": dominance_info.get("ratio"),
                    "concept_quotas_applied": dominance_info.get("concept_quotas_applied", []),
                    "detected_concepts": advisory_signal.get("detected_concepts", []),
                    "alternative_concepts": advisory_signal.get("alternative_concepts", []),
                    "article_bundles_added": bundle_info.get("articles_added", []),
                    "retrieval_profile": retrieval_profile,
                    "outcome_policy": outcome_policy_info,
                    "termination_response_policy": termination_response_policy_info,
                    "primary_article_id": retrieval_profile.get("primary_article_id"),
                    "excluded_articles": retrieval_profile.get("excluded_articles", []),
                    "exclusion_reasons": retrieval_profile.get("exclusion_reasons", {}),
                }
            )

        except Exception as e:
            logger.error("Error en generate_response")
            transient_kinds = {
                "timeout", "transport", "rate_limit", "server_error",
                "circuit_open",
            }
            failure_kind = getattr(e, "failure_kind", "unknown")
            if failure_kind not in transient_kinds | {"client_error", "unknown"}:
                failure_kind = "unknown"
            return self._build_uncertain_response(
                "generate_response_failed",
                confidence=0.0,
                error_type=type(e).__name__,
                retrieval_failure_kind=failure_kind,
                retrieval_retryable=(
                    getattr(e, "retryable", False) is True
                    and failure_kind in transient_kinds
                ),
            )

    # ========================================================================
    # ENDPOINT 3: Knowledge Question
    # ========================================================================

    # Required Data settings
    RD_CONTEXT_BUDGET = 3500
    RD_TOP_K_PER_QUERY = 5
    RD_MAX_CHUNKS_PER_ARTICLE = 4
    RD_RETRIEVAL_MIN_SCORE = 0.25
    # Fix 6: independent floor on raw cosine score so the boost added by
    # `_rank_rdmh_chunks` cannot fully rescue a chunk with no semantic signal.
    RD_RETRIEVAL_MIN_RAW_SCORE = 0.18
    RD_NO_MATCH_CONFIDENCE = 0.40
    # Below this chunk count the topic-scoped lane is considered too narrow
    # and the cascade is re-run without the topic filter as a safety net.
    RD_TOPIC_LANE_MIN_CHUNKS = 2
    # Fix 4: cap the number of distinct articles whose rdmh chunks reach
    # the LLM context. 1 primary + up to 2 supporting articles (compatible
    # by record_keeper + topic via the upstream cascade filters).
    RD_MAX_DISTINCT_ARTICLES = 3
    # Fix 6: when primary_article_id is set AND the boosted score gap from
    # chunks[0] to chunks[1] reaches this threshold, prune everything below
    # the leader so the LLM context contains only the dominant article.
    RD_SCORE_GAP_PRUNE_THRESHOLD = 0.08

    # Generate Response settings
    RESPONSE_MIN_CONTEXT_TOKENS = 4000
    GR_MAX_CHUNKS_PER_ARTICLE = 6
    GR_UNFILTERED_TOP_K = 15
    GR_LLM_TIMEOUT_SECONDS = 180
    GR_PHASE1_TIMEOUT_SECONDS = 20
    GR_PHASE1_MAX_TOKENS = 500
    GR_FALLBACK_MIN_CHUNKS = 6
    GR_FALLBACK_MIN_SCORE = 0.35
    GR_MAX_ADVISORY_QUERIES = 3
    GR_MAX_QUERY_COUNT = 6
    GR_BUNDLE_MAX_ARTICLES = 6
    GR_BUNDLE_MAX_CHUNKS_PER_ARTICLE = 8
    GR_BUNDLE_MIN_SCORE = 0.20
    GR_BUNDLE_CHUNK_TYPES = frozenset({
        "decision_guide",
        "response_frames",
        "eligibility",
        "business_rules",
        "steps",
        "fees_details",
    })
    GR_CONTEXT_QUOTA_CONCEPTS = frozenset({
        "termination_distribution_request",
        "in_service_withdrawal_options",
        "hardship_withdrawal",
        "loan",
    })
    GR_CONTEXT_MIN_CHUNKS_PER_CONCEPT = 1
    GR_RERANK_ENABLED = os.getenv("GR_RERANK_ENABLED", "true").lower() in {
        "1", "true", "yes", "on"
    }

    # Dominant-article heuristic for Generate Response context building.
    # When one article's retrieval signal dominates the others, skip forced
    # multi-article diversity and focus the context on that article. Keeps
    # cross-article synthesis intact when multiple articles score similarly.
    GR_DOMINANCE_ENABLED           = True   # kill-switch
    GR_DOMINANCE_MIN_TOP_SIGNAL    = 2.00   # sum of top-3 chunk scores from dominant article
    GR_DOMINANCE_MAX_RATIO         = 0.40   # runner-up signal must be < 40% of dominant
    GR_DOMINANCE_MIN_CHUNKS        = 4      # dominant article must have >=4 retrieved chunks
    GR_DOMINANCE_MAX_CHUNKS_SINGLE = 8      # cap on dominant-article chunks in single mode
    GR_DOMINANCE_SECONDARY_SLOT    = 1      # insurance chunks from runner-up article

    # Exact-procedure routing keeps narrow procedural requests from being
    # diluted by adjacent but non-applicable rollover/distribution articles.
    EXACT_TERMINATION_ROLLOVER_ARTICLE_ID = (
        "lt_request_401k_termination_withdrawal_or_rollover"
    )
    SPLIT_ROLLOVER_ARTICLE_ID = "can_i_split_my_401_k_rollover_between_multiple_providers"
    MISSED_60_DAY_ARTICLE_ID = "missed_60_day_rollover_window"
    RMD_ARTICLE_ID = (
        "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact"
    )
    IN_SERVICE_ARTICLE_ID = (
        "can_i_take_money_from_my_401k_while_employed_your_options_explained"
    )
    HARDSHIP_ARTICLE_ID = "forusall_401k_hardship_withdrawal_complete_guide"
    # Fix K (Round 2): the actual KB slug is `lt_trust_401k_…` — the prior
    # constants used `lt_401k_…` and `lt_401_k_…` which never matched live
    # Pinecone data. The legacy variants are kept in LOAN_ARTICLE_IDS as a
    # defensive fallback in case a re-ingest writes them again.
    LT_LOAN_ARTICLE_ID = "lt_trust_401k_loan_complete_guide_submission_repayment_support"
    LOAN_ARTICLE_IDS = (
        LT_LOAN_ARTICLE_ID,
        "lt_401k_loan_complete_guide_submission_repayment_support",
        "lt_401_k_loan_complete_guide_submission_repayment_support",
    )
    GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID = (
        "401k_savings_after_leaving_your_job_rollovers_cash_outs_roth_pre_tax"
    )
    ACCOUNT_SETUP_ARTICLE_ID = "lt_how_to_set_up_your_forusall_401k_account"
    MFA_ARTICLE_ID = "multi_factor_authentication_mfa_for_forusall_401k_account"
    FORCE_OUT_ARTICLE_IDS = (
        "401k_force_out_process_involuntary_distribution_balance_thresholds_safe_harbor_ira_rollovers_fee_outs_compliance",
        "401k_force_out_process_sponsor_faq",
    )
    GR_EXACT_MIN_CHUNKS = 4
    GR_EXACT_CONTEXT_CHUNK_TYPES = frozenset({
        "critical_flags",
        "decision_guide",
        "response_frames",
        "guardrails",
        "eligibility",
        "business_rules",
        "steps",
        "common_issues",
        "example",
        "faqs",
        "fees_details",
        "required_data_if_missing",
        "required_data_disambiguation",
    })
    GR_EXACT_INFORMATIONAL_CONTEXT_CHUNK_TYPES = frozenset({
        "critical_flags",
        "decision_guide",
        "guardrails",
        "eligibility",
        "business_rules",
        "steps",
        "common_issues",
        "example",
        "faqs",
        "fees_details",
    })
    GR_EXACT_CONTEXT_TYPE_ORDER = {
        "decision_guide": 0,
        "eligibility": 1,
        "steps": 2,
        "business_rules": 3,
        "guardrails": 4,
        "critical_flags": 5,
        "response_frames": 6,
        "required_data_if_missing": 7,
        "required_data_disambiguation": 8,
        "common_issues": 9,
        "fees_details": 10,
        "example": 11,
        "faqs": 12,
    }
    GR_EXACT_INFORMATIONAL_CONTEXT_TYPE_ORDER = {
        "decision_guide": 0,
        "eligibility": 1,
        "business_rules": 2,
        "fees_details": 3,
        "steps": 4,
        "guardrails": 5,
        "critical_flags": 6,
        "common_issues": 7,
        "example": 8,
        "faqs": 9,
    }
    GR_EXACT_INFORMATIONAL_CATEGORY_ORDER = {
        "fees": 0,
        "delivery": 1,
        "processing_times": 2,
        "tax_withholding": 3,
        "wire_instructions": 4,
        "check_rollover_instructions": 5,
        "rollover_payment_method": 6,
        "eligibility": 7,
        "submission_method": 8,
        "portal_access_best_practices": 9,
        "data_entry_best_practices": 10,
        "small_balance_fee_out": 11,
        "outstanding_loans": 12,
    }
    GR_EXACT_INFORMATIONAL_PRIORITY_BUSINESS_CATEGORIES = frozenset({
        "eligibility",
        "fees",
        "delivery",
        "processing_times",
        "tax_withholding",
        "wire_instructions",
        "check_rollover_instructions",
        "rollover_payment_method",
        "submission_method",
        "portal_access_best_practices",
    })

    # Knowledge Question settings
    KQ_CONTEXT_BUDGET = 4000
    KQ_TOP_K_PER_QUERY = 15
    KQ_MAX_CHUNKS_PER_ARTICLE = 6
    KQ_SOURCE_MIN_SCORE = 0.20
    KQ_PRIORITIZED_TYPES = [
        'business_rules', 'eligibility', 'steps', 'faqs',
        'guardrails', 'fees_details'
    ]

    @staticmethod
    def _requires_phone_support_for_unknown_email(question: str) -> bool:
        normalized = re.sub(
            r"[^a-z0-9]+", " ", (question or "").lower()
        ).strip()
        unknown_email_markers = (
            "cannot remember which email",
            "cant remember which email",
            "do not remember which email",
            "dont remember which email",
            "do not know which email",
            "dont know which email",
            "no longer have the email",
            "lost access to my email",
            "cannot access my email",
            "cant access my email",
        )
        unknown_which_email = "which email" in normalized and any(
            marker in normalized
            for marker in (
                "cannot remember", "can t remember", "do not remember",
                "don t remember", "does not remember", "doesn t remember",
                "cannot recall", "can t recall", "do not know", "don t know",
                "does not know", "doesn t know",
            )
        )
        inaccessible_email = "email" in normalized and any(
            marker in normalized
            for marker in (
                "cannot access", "can t access", "do not have access",
                "don t have access", "does not have access",
                "doesn t have access", "lost access", "no longer have access",
                "email is inaccessible", "inaccessible email",
            )
        )
        unknown_named_email = re.search(
            r"\b(?:cannot|can t|do not|don t|does not|doesn t) "
            r"(?:remember|know|recall) (?:my |her |his |their |the )?"
            r"email\b(?! (?:password|login password)\b)",
            normalized,
        ) is not None
        forgot_named_email = re.search(
            r"\b(?:forgot|forgotten) (?:my |her |his |their |the )?"
            r"email\b(?! (?:password|login password)\b)",
            normalized,
        ) is not None
        unknown_literal_email = re.search(
            r"\b(?:unknown|unrecognized) email(?: address)?\b"
            r"(?! (?:password|login password)\b)",
            normalized,
        ) is not None
        return (
            unknown_which_email
            or unknown_named_email
            or forgot_named_email
            or unknown_literal_email
            or inaccessible_email
            or any(marker in normalized for marker in unknown_email_markers)
        )

    @classmethod
    def _apply_account_recovery_knowledge_policy(
        cls,
        parsed: Dict[str, Any],
        question: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Advance failed recovery attempts without inventing their cause."""
        unknown_email = cls._requires_phone_support_for_unknown_email(question)
        reset_failed = mentions_failed_password_reset(question)
        if not unknown_email and not reset_failed:
            return parsed, {"applied": False}

        fixed = copy.deepcopy(parsed)
        if reset_failed and not unknown_email:
            fixed["answer"] = (
                "Since the password reset did not restore access, call ForUsAll "
                "Participant Support at 844-401-2253, Monday-Friday, 7:00 AM-5:00 PM PT, "
                "for account-access recovery help. If the reset email did not arrive "
                "and you have not checked spam or junk, check those folders before "
                "calling. Do not send your password or authentication code in a support message."
            )
            fixed["key_points"] = [
                "Contact Participant Support for help with the unsuccessful password reset.",
                "Use 844-401-2253, Monday-Friday, 7:00 AM-5:00 PM PT.",
                "If the reset email is missing and you have not checked spam or junk, check those folders.",
                "Do not send your password or authentication code in a support message.",
            ]
            return fixed, {"applied": True, "reason": "password_reset_failed"}
        fixed["answer"] = (
            "If you do not know or cannot access the email on file, call "
            "ForUsAll Participant Support at 844-401-2253, Monday-Friday, "
            "7:00 AM-5:00 PM PT, for identity verification and account-access "
            "help. Self-service password reset requires knowing and being able "
            "to access the email on file, so it is not the right path here. Do "
            "not send your password or authentication code in a support message."
        )
        fixed["key_points"] = [
            "Call Participant Support because the email on file is unknown or inaccessible.",
            "Use 844-401-2253, Monday-Friday, 7:00 AM-5:00 PM PT.",
            "Do not send your password or authentication code in a support message.",
        ]
        return fixed, {"applied": True, "reason": "unknown_or_inaccessible_email"}

    @staticmethod
    def _apply_identity_knowledge_policy(parsed: Dict[str, Any], context: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if context is None:
            # The KQ endpoint is educational by contract. Callers falling back
            # after account lookup must provide the typed lookup context.
            return parsed, {"response_source_reason": "general_knowledge"}
        if not isinstance(context, dict):
            return parsed, {}
        status, reason = context.get("identity_resolution_status"), context.get("response_source_reason")
        if status not in {"matched", "ambiguous", "not_found", "access_error"}:
            return parsed, {}
        if reason not in {"general_knowledge", "account_not_found", "account_ambiguous", "account_lookup_failed", "account_context_required"}:
            return parsed, {}
        metadata = {"identity_resolution_status": status, "response_source_reason": reason,
                    "identity_verified": context.get("identity_verified") is True}
        identifiers = context.get("provided_identifiers")
        allowed_identifiers = {"name", "email", "employer", "date_of_birth", "last_four_ssn"}
        metadata["provided_identifiers"] = list(dict.fromkeys(
            item for item in identifiers if isinstance(item, str) and item in allowed_identifiers
        )) if isinstance(identifiers, list) else []
        if reason == "general_knowledge":
            return parsed, metadata
        # This is an account lookup fallback, not a general question. An
        # approved identity workflow must determine which additional facts
        # are necessary; do not invent or repeat a security checklist here.
        fixed = dict(parsed)
        fixed["answer"] = "Our team needs to verify the account and its current status before providing instructions specific to your request. We will first use the information you have already provided."
        fixed["key_points"] = []
        metadata["human_review_required"] = True
        metadata["handoff"] = {"reason": reason, "next_action": "Resolve the account using available identifiers and the approved verification procedure; ask only for missing information."}
        return fixed, metadata

    async def ask_knowledge_question(
        self,
        question: str,
        identity_context: Optional[Dict[str, Any]] = None,
    ) -> KnowledgeQuestionResult:
        """
        Answer a general knowledge question using the KB — no participant data required.

        Pipeline:
        1. Decompose question into focused sub-queries (LLM)
        2. Parallel semantic search for each sub-query + original
        3. Merge, deduplicate, and rank all retrieved chunks
        4. Build context with article diversity enforcement
        5. Generate answer via LLM
        6. Engine-calculated confidence and source articles

        Args:
            question: The knowledge question to answer

        Returns:
            KnowledgeQuestionResult with answer, key points, and sources
        """
        logger.info(f"ask_knowledge_question() - question of {len(question)} chars (redacted)")

        try:
            # 1. Decompose question into sub-queries
            sub_queries = await self._decompose_question(question)
            logger.info(f"Decomposed into {len(sub_queries)} sub-queries "
                        f"(text redacted, {sum(len(s) for s in sub_queries)} chars)")

            # 2. Parallel search: sub-queries + original question
            search_tasks = [
                self._cached_query(
                    query_text=sq,
                    top_k=self.KQ_TOP_K_PER_QUERY,
                    filter_dict=None
                )
                for sq in sub_queries
            ]
            if question not in sub_queries:
                search_tasks.append(
                    self._cached_query(
                        query_text=question,
                        top_k=self.KQ_TOP_K_PER_QUERY,
                        filter_dict=None
                    )
                )

            results = await asyncio.gather(*search_tasks)

            # 2b. Compute per-sub-query best scores (for confidence & metadata)
            query_labels = list(sub_queries)
            if question not in sub_queries:
                query_labels.append(question)
            per_query_scores = {}
            for label, result_list in zip(
                query_labels, results, strict=True
            ):
                best = max((c.get('score', 0) for c in result_list), default=0)
                per_query_scores[label] = round(best, 4)

            # 3. Merge, deduplicate, rank by score
            chunks = self._merge_and_rank_chunks(*results)

            if not chunks:
                logger.warning("No chunks found for knowledge question")
                fallback, identity_metadata = self._apply_identity_knowledge_policy({
                    "answer": "I couldn't find relevant information in the knowledge base to answer this question.",
                    "key_points": [],
                }, identity_context)
                return KnowledgeQuestionResult(
                    answer=fallback["answer"],
                    key_points=[],
                    source_articles=[],
                    used_chunks=[],
                    confidence_note="limited_coverage",
                    metadata={**identity_metadata, "chunks_used": 0, "model": None, "provider": None, "sub_queries": sub_queries}
                )

            # 4. Build context with article diversity
            context, selected_chunks, tokens_used = self._build_context_with_diversity(
                chunks=chunks,
                budget=self.KQ_CONTEXT_BUDGET,
                prioritize_types=self.KQ_PRIORITIZED_TYPES,
                max_per_article=self.KQ_MAX_CHUNKS_PER_ARTICLE
            )

            logger.info(f"Context built: {len(selected_chunks)} chunks, {tokens_used} tokens")

            # 5. Build prompts and call LLM
            system_prompt, user_prompt = build_knowledge_question_prompt(
                context=context,
                question=question
            )

            llm_usage = None
            llm_provider_used: Optional[str] = None
            llm_model_used: Optional[str] = None
            try:
                llm_result = await self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=2000,
                    task_type="knowledge_question",
                )
                llm_response = llm_result.content
                llm_usage = llm_result.usage
                llm_provider_used = llm_result.provider_used
                llm_model_used = llm_result.model_used
            except LLMEmptyResponseError:
                logger.error("LLM returned empty content in knowledge_question")
                llm_response = json.dumps({
                    "answer": "Unable to generate a response. Please try again or contact Support.",
                    "key_points": [],
                    "coverage_gaps": []
                })

            # 6. Parse response
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError:
                logger.error(
                    "Knowledge-question JSON parse failure (length=%d)",
                    len(llm_response or ""),
                )
                parsed = {
                    "answer": "Unable to generate a structured answer.",
                    "key_points": [],
                    "coverage_gaps": []
                }

            parsed, account_recovery_policy_info = (
                self._apply_account_recovery_knowledge_policy(parsed, question)
            )
            parsed, identity_metadata = self._apply_identity_knowledge_policy(parsed, identity_context)

            # 7. Extract LLM-reported coverage gaps
            coverage_gaps = parsed.get("coverage_gaps", [])
            if not isinstance(coverage_gaps, list):
                coverage_gaps = []
            coverage_gaps = [g for g in coverage_gaps if isinstance(g, str) and g.strip()]

            # 8. Engine-calculated confidence (uses LLM gaps + retrieval signals)
            confidence_note = self._calculate_knowledge_confidence(
                selected_chunks, coverage_gaps
            )
            source_articles = self._build_source_articles(selected_chunks)

            used_chunks = self._serialize_used_chunks(selected_chunks)

            total_articles = len(source_articles)
            relevant_articles = sum(
                1 for sa in source_articles if sa.get("used_info", False)
            )

            return KnowledgeQuestionResult(
                answer=parsed.get("answer", ""),
                key_points=parsed.get("key_points", []),
                source_articles=source_articles,
                used_chunks=used_chunks,
                confidence_note=confidence_note,
                metadata={
                    **identity_metadata,
                    "chunks_used": len(selected_chunks),
                    "context_tokens": tokens_used,
                    "response_tokens": self.token_manager.count_tokens(llm_response),
                    "prompt_tokens": llm_usage.get("prompt_tokens", 0) if llm_usage else 0,
                    "completion_tokens": llm_usage.get("completion_tokens", 0) if llm_usage else 0,
                    "total_tokens": llm_usage.get("total_tokens", 0) if llm_usage else 0,
                    "model": llm_model_used,
                    "provider": llm_provider_used,
                    "sub_queries": sub_queries,
                    "unique_articles": total_articles,
                    "relevant_articles": relevant_articles,
                    "coverage_gaps": coverage_gaps,
                    "account_recovery_policy": account_recovery_policy_info,
                    "per_query_scores": per_query_scores
                }
            )

        except Exception as e:
            logger.error("Error in ask_knowledge_question")
            transient_kinds = {
                "timeout", "transport", "rate_limit", "server_error",
                "circuit_open",
            }
            failure_kind = getattr(e, "failure_kind", "unknown")
            if failure_kind not in transient_kinds | {"client_error", "unknown"}:
                failure_kind = "unknown"
            retryable = (
                getattr(e, "retryable", False) is True
                and failure_kind in transient_kinds
            )
            return KnowledgeQuestionResult(
                answer="An internal error occurred while processing your question.",
                key_points=[],
                source_articles=[],
                used_chunks=[],
                confidence_note="limited_coverage",
                metadata={
                    "error": "knowledge_question_failed",
                    "retrieval_failure_kind": failure_kind,
                    "retrieval_retryable": retryable,
                    "chunks_used": 0,
                    "model": None,
                    "provider": None,
                },
            )

    # ========================================================================
    # Helper Methods - Búsqueda
    # ========================================================================

    # Tokens mínimos reservados para la respuesta del LLM.
    RESPONSE_MIN_TOKENS = 1200

    # Umbral mínimo para considerar que una búsqueda con filtro de topic
    # tuvo resultados suficientes. Si no se alcanza, se hace fallback sin topic.
    TOPIC_FILTER_MIN_CHUNKS = 3
    TOPIC_FILTER_MIN_SCORE = 0.20

    # Record-keeper cascade settings
    RK_FALLBACK_DEFAULT = "LT Trust"
    RK_CASCADE_MIN_CHUNKS = 1
    RK_CASCADE_MIN_SCORE = 0.15
    # Fix 2: when record_keeper is in this allowlist, required_data first
    # runs only the RK-specific cascade level. The global lane only fires
    # if Stage 1a fails to meet RK_PRIMARY_SUFFICIENT_{CHUNKS,SCORE}. This
    # prevents weakly-relevant global articles from competing with the
    # dedicated record-keeper article for well-covered RKs.
    RK_PRIMARY_STAGED_RECORD_KEEPERS = frozenset({"LT Trust"})
    RK_PRIMARY_SUFFICIENT_CHUNKS = 1
    RK_PRIMARY_SUFFICIENT_SCORE = 0.20
    # Fix H (Round 2): topics where no record-keeper-specific article exists
    # in the KB. For these, RK is a preference (handled by `_rank_rdmh_chunks`
    # boost) — not a requirement. The cascade must NOT gate the global lane
    # behind Stage 1a sufficiency, otherwise hardship/in-service inquiries for
    # an RK like "LT Trust" would starve and return no-match.
    RK_OPTIONAL_TOPICS = frozenset({
        "hardship_withdrawal",
        "in_service_withdrawal_options",
    })

    # Canonical topics for which the KB ONLY has scope=global content (0
    # RK-specific articles). Activating record_keeper-filtered lanes/levels
    # for these burns reranks and injects off-topic RK noise.
    # Maintenance: (1) if an RK-specific article is added for any of these
    #   topics, REMOVE it from this set in the SAME PR (enforced by the
    #   static drift test, Cambio 4). (2) These topics may have
    #   `required_data.must_have = []` (hardship, excess today); in that case
    #   required_data returns empty for the topic — that is correct, not a bug.
    GLOBAL_ONLY_TOPICS = frozenset({
        "hardship_withdrawal",
        "in_service_withdrawal_options",
        "excess_contribution_refund",
    })

    # Kill-switch: revert via env-var without redeploy. Same pattern as
    # GR_RERANK_ENABLED. The failure mode of the skip is silent (a worse
    # answer, no error), so a cheap env-var revert is the right safety valve.
    GLOBAL_ONLY_SKIP_ENABLED = os.getenv("GLOBAL_ONLY_SKIP_ENABLED", "true").lower() in {
        "1", "true", "yes", "on",
    }

    # ``TOPIC_NORMALIZATION_MAP`` and ``resolve_topic_filter`` live at module
    # level (see top of this file) so other modules can reuse them without
    # instantiating the engine.

    def _resolve_topic_filter(self, topic: Optional[str]) -> Optional[List[str]]:
        return resolve_topic_filter(topic)

    def _is_global_only_topic(self, resolved_topics: Optional[List[str]]) -> bool:
        """True if the full resolved set is a non-empty subset of GLOBAL_ONLY_TOPICS.

        Computed once from the original resolved set and propagated explicitly;
        never recomputed from a ``resolved_topics`` that may have been mutated to
        ``None``. The feature flag lives inside the helper so a single
        ``_is_global_only_topic(...) -> False`` disables the behaviour across
        both retrieval paths at once.
        """
        return (
            self.GLOBAL_ONLY_SKIP_ENABLED
            and resolved_topics is not None
            and len(resolved_topics) > 0
            and set(resolved_topics).issubset(self.GLOBAL_ONLY_TOPICS)
        )

    def _get_topic_variations(self, topic: str) -> List[str]:
        """
        Genera variaciones de case para un topic (Pinecone es case-sensitive).

        Args:
            topic: Topic en lowercase (normalizado por el validator)

        Returns:
            Lista de variaciones únicas: ['rollover', 'Rollover', 'ROLLOVER']
        """
        variations = list(set([
            topic,
            topic.lower(),
            topic.capitalize(),
            topic.title(),
            topic.upper()
        ]))
        return variations

    @staticmethod
    def _contains_any(text: str, needles: List[str]) -> bool:
        return _contains_any(text, needles)

    @staticmethod
    def _contains_bounded_phrase(text: str, phrase: str) -> bool:
        return _contains_bounded_phrase(text, phrase)

    @staticmethod
    def _ordered_unique(values: List[str]) -> List[str]:
        return _ordered_unique(values)

    @staticmethod
    def _normalize_plan_type(plan_type: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(plan_type or "").lower())

    @staticmethod
    def _textify_metadata_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            return " ".join(
                RAGEngine._textify_metadata_value(v) for v in value.values()
            )
        if isinstance(value, (list, tuple, set)):
            return " ".join(RAGEngine._textify_metadata_value(v) for v in value)
        return str(value)

    @staticmethod
    def _extract_numeric_amount(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value)
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    def _infer_inquiry_intent(self, text: str) -> str:
        """
        Separate informational option/cost/timeline questions from requests
        that ask ForUsAll to execute a transaction now.
        """
        normalized = re.sub(r"[^a-z0-9$]+", " ", (text or "").lower()).strip()
        transactional_phrases = [
            "submit this",
            "submit my request",
            "process my request",
            "process this request",
            "start the distribution",
            "send the wire",
            "complete the request now",
            "complete this request",
            "initiate the distribution",
            "initiate this request",
            "do it now",
        ]
        if any(phrase in normalized for phrase in transactional_phrases):
            return "transactional_submission"

        informational_phrases = [
            "what are my options",
            "what options do i have",
            "what are the options",
            "delivery options",
            "fastest delivery",
            "what do they cost",
            "how much",
            "what are the fees",
            "what fees",
            "how long",
            "timeline",
            "timelines",
            "instructions",
            "what does it cost",
            "cost",
            "costs",
            "fees",
        ]
        if any(phrase in normalized for phrase in informational_phrases):
            return "informational_options"

        if normalized.startswith(("what ", "how ", "which ", "can you explain")):
            return "informational_options"
        return "transactional_submission"

    def _resolve_employment_state(
        self,
        participant_data: Dict[str, Any],
        text: str,
    ) -> str:
        status_values = [
            participant_data.get("employment_status"),
            participant_data.get("participant_status"),
            participant_data.get("eligibility_status"),
            participant_data.get("status"),
        ]
        normalized = [
            re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
            for value in status_values
            if value is not None
        ]
        if any(
            status == "active" or status.startswith("active ")
            for status in normalized
        ):
            return "active"
        if any(
            status in {"terminated", "separated", "former"}
            or status.startswith("terminated ")
            or status.startswith("separated ")
            for status in normalized
        ):
            return "terminated"
        if any(
            status == "inactive" or status.startswith("inactive ")
            for status in normalized
        ):
            return "inactive"
        if participant_data.get("termination_date"):
            return "terminated"
        if self._contains_any(text, [
            "left my job",
            "left his employer",
            "left her employer",
            "left their employer",
            "left his company",
            "left her company",
            "left their company",
            "left the company",
            "left my company",
            "no longer employed",
            "former employer",
            "prior employer",
            "recently left",
            "separated from employment",
            "separation of service",
            "terminated",
        ]):
            return "terminated"
        return "unknown"

    # Fix 4: when the caller opts in (required_data), a "rollover|moving|
    # transferring … from … to …" structure is treated as evidence of a
    # termination context even if no explicit "former/left/separated" wording
    # appears. The pattern requires both the action verb and the from/to
    # structure in the same clause so phrases like "rollover into IRA" or
    # "rollover at Fidelity" do NOT trigger.
    _NAMED_EMPLOYER_ROLLOVER_PATTERN = re.compile(
        r"\b(rollover|rolling over|roll over|moving|moved|transferring|transfer)\b"
        r"[^.!?]*\bfrom\b[^.!?]*\bto\b",
        re.IGNORECASE,
    )

    def _infer_incoming_rollover_signal(self, text: str, topic: Optional[str]) -> bool:
        """F1 (eval 2026-06-22): detect an INCOMING rollover — bringing an external
        prior-account balance INTO the current ForUsAll plan — so it routes to the
        incoming procedure instead of the outgoing/termination machinery. Mirrors
        the direction logic in gr_body_build.md: an external source is named,
        ForUsAll is the destination, and there is no outgoing payment instruction.
        Robust to third-person phrasing because the inquiry is paraphrased."""
        if (topic or "").lower().strip() == "incoming_rollover":
            return True
        external_source = self._contains_any(text, [
            "fidelity", "vanguard", "schwab", "empower", "principal", "merrill",
            "t. rowe", "tiaa", "calsavers", "ira at", "401k at", "401(k) at",
            "previous employer", "prior employer", "former employer", "old employer",
            "old 401", "previous 401", "prior 401", "another provider",
        ])
        incoming_destination = self._contains_any(text, [
            "into my current", "into her current", "into their current",
            "into your current", "into the current", "current account",
            "into my forusall", "into this plan", "into my plan", "into the plan",
            "incoming rollover", "rollover contribution", "roll into", "roll it into",
            "consolidate", "bring it here", "move it here", "transfer into",
            "transfer it into",
        ])
        outgoing_payment = self._contains_any(text, [
            "check payable to", "fbo", "send check", "send the check", "wire to",
            "payable to my", "to my schwab", "to my fidelity", "to my vanguard",
            "to my ira", "into my ira", "to my external", "rollover out",
        ])
        return external_source and incoming_destination and not outgoing_payment

    def _infer_retrieval_signals(
        self,
        inquiry: str,
        topic: str,
        collected_data: Optional[Dict[str, Any]],
        assume_termination_on_named_employer: bool = False,
    ) -> Dict[str, Any]:
        participant_data = (collected_data or {}).get("participant_data") or {}
        plan_data = (collected_data or {}).get("plan_data") or {}
        age_59_5_status = participant_data.get("is_age_59_5_or_older")
        if not isinstance(age_59_5_status, bool):
            age_59_5_status = None
        raw_termination_date = participant_data.get("termination_date")
        termination_date_present = str(raw_termination_date or "").strip().lower() not in {
            "", "unknown", "n/a", "none", "not available",
        }
        profile_text = " ".join([
            inquiry or "",
            topic or "",
            self._textify_metadata_value(participant_data),
            self._textify_metadata_value(plan_data),
        ]).lower()

        employment_state = self._resolve_employment_state(
            participant_data=participant_data,
            text=profile_text,
        )
        balance_values = [
            participant_data.get("total_vested_balance"),
            participant_data.get("vested_balance"),
            participant_data.get("account_balance"),
            participant_data.get("balance"),
        ]
        balances = [
            amount for amount in (
                self._extract_numeric_amount(value) for value in balance_values
            )
            if amount is not None
        ]
        lowest_balance = min(balances) if balances else None

        rollover_intent = (
            topic.lower().strip() in {"rollover", "rollovers"}
            or self._contains_any(profile_text, [
                "rollover",
                "roll over",
                "roll-over",
                "move my 401",
                "move his 401",
                "move her 401",
                "move their 401",
                "new manager",
                "new provider",
                "receiving institution",
                "direct rollover",
            ])
        )
        distribution_intent = (
            topic.lower().strip() in {
                "distribution",
                "distributions",
                "termination",
                "termination_distribution",
                "termination_distribution_request",
            }
            or self._contains_any(profile_text, [
                "distribution",
                "withdrawal",
                "cash distribution",
                "take money",
                "access money",
                "access funds",
                "need my 401",
                "need money",
                "money as fast as possible",
            ])
        )
        cash_component = self._contains_any(profile_text, [
            "cash out", "cashout", "cash distribution", "lump sum cash",
            "partial cash", "cash portion", "take the money", "withdraw to",
            "deposit into my bank", "deposit into their bank",
        ])
        pure_rollover = rollover_intent and not cash_component
        # Delivery focus must come from the request, not payroll/check fields.
        selected_delivery = None
        if (
            re.search(r"\b(?:rollover|roll over|send(?: it| the funds)?)\b.{0,30}\b(?:by|via)\s+(?:a\s+)?check\b", inquiry or "", re.IGNORECASE)
            and not re.search(r"\b(?:wire|not|don['’]?t)\b", inquiry or "", re.IGNORECASE)
        ):
            selected_delivery = "check"
        delivery_or_fee_request = self._contains_any(profile_text, [
            "delivery",
            "deliver",
            "fastest",
            "as fast as possible",
            "cost",
            "fee",
            "fees",
            "wire",
            "overnight",
            "overnight check",
            "regular mail",
            "check",
            "p.o. box",
            "po box",
        ])
        loan_signal = self._contains_any(profile_text, [
            "loan",
            "borrow",
            "401(k) loan",
            "401k loan",
        ])
        split_rollover = rollover_intent and self._contains_any(profile_text, [
            "split rollover",
            "split my rollover",
            "split his rollover",
            "split her rollover",
            "split their rollover",
            "split it",
            "split funds",
            "split the funds",
            "multiple providers",
            "multiple destinations",
            "multiple accounts",
            "two providers",
            "different providers",
            "percentage",
            "percent",
            "dollar amount",
            "divide the rollover",
            "divided by",
            # Fix I (Round 2): broaden triggers so phrases like
            # "split my 401(k) rollover between Vanguard and Fidelity,
            # half to each provider" qualify as split intent.
            "split my 401",
            "split this rollover",
            "split the rollover",
            "between two",
            "to two providers",
            "to two iras",
            "to two accounts",
            "half to each",
            "half and half",
        ])
        indirect_rollover_60_day = rollover_intent and self._contains_any(profile_text, [
            "60-day",
            "60 day",
            "sixty day",
            "indirect rollover",
            "missed deadline",
            "missed the deadline",
            "rollover window",
            "check made payable to me",
            "payable to me",
            "received a check",
            "received the funds",
            "funds were sent to me",
        ])
        force_out = self._contains_any(profile_text, [
            "force-out",
            "force out",
            "involuntary distribution",
            "safe harbor ira",
            "forceout",
            "force out notice",
            "force-out notice",
            "sponsor can initiate",
        ])
        rmd = self._contains_any(profile_text, [
            "required minimum distribution",
            "rmd",
            "age 73",
            "age 75",
            "born in 1960",
        ])
        contact_or_reference = self._contains_any(profile_text, [
            "phone",
            "email",
            "contact",
            "support hours",
            "hours",
            "link",
            "url",
        ])
        overnight_request = self._contains_any(profile_text, [
            "overnight", "overnight check", "next day", "expedited check",
        ])
        account_access = self._contains_any(profile_text, [
            "cannot log in", "can't log in", "cant log in", "unable to log in",
            "cannot access", "can't access", "cant access", "no access",
            "forgot password", "forgotten password", "lost password",
            "do not have the password", "don't have the password",
            "no longer have the email", "lost access to my email",
            "cannot remember which email", "can't remember which email",
            "cant remember which email", "cannot remember my email",
            "do not remember which email", "don't remember which email",
            "forgot email", "forgotten email",
            "cannot remember my password", "can't remember my password",
            "cant remember my password", "do not remember my password",
            "don't remember my password",
            "old email", "email on file", "new phone", "mfa",
            "authentication code", "authenticator app", "create an account",
            "set up my account", "account setup",
        ])
        unknown_email_access = self._requires_phone_support_for_unknown_email(
            profile_text
        )
        # TKT-910081: the KQ recovery policy already treats a failed reset as an
        # access blocker. The GR signal must agree, or the same sentence yields
        # a recovery answer without the account-access support guidance.
        account_access = (
            account_access
            or unknown_email_access
            or mentions_failed_password_reset(profile_text)
        )
        guidance_request = self._contains_any(profile_text, [
            "requesting guidance", "guidance on", "next steps", "what steps",
            "steps to", "how do i", "how can i", "what should i do",
        ])
        rollover_provider_named = rollover_intent and self._contains_any(
            profile_text,
            [
                "fidelity", "vanguard", "schwab", "empower", "principal",
                "merrill", "t. rowe", "tiaa",
            ],
        )
        brokerage_destination = "brokerage account" in profile_text
        explicit_retirement_destination = self._contains_any(profile_text, [
            "rollover ira", "traditional ira", "roth ira", "ira at",
            "qualified plan", "qualified retirement plan", "retirement account",
            "new employer plan", "new employer's plan", "new 401(k)",
            "new 401k", "403(b)", "403b",
        ])
        explicit_taxable_destination = self._contains_any(profile_text, [
            "taxable brokerage", "individual brokerage", "regular brokerage",
            "non-retirement account", "nonretirement account",
        ])
        brokerage_destination_ambiguous = (
            rollover_intent
            and brokerage_destination
            and not explicit_retirement_destination
            and not explicit_taxable_destination
        )
        termination_distribution = (
            employment_state == "terminated"
            # TKT-911961: the product names "separation distribution" and
            # "termination distribution" are stripped first so asking about one
            # never asserts that the participant left.
            or self._contains_any(strip_separation_product_names(profile_text), [
                "left my job",
                "left his employer",
                "left her employer",
                "left their employer",
                "left his company",
                "left her company",
                "left their company",
                "left the company",
                "left my company",
                "no longer employed",
                "former employer",
                "prior employer",
                "recently left",
                "separation",
                "separated",
                "terminated",
                "after employment",
                "former employee",
            ])
        )
        # F1 (eval 2026-06-22): detect an incoming rollover (external source ->
        # current ForUsAll plan). Computed before the named-employer override so an
        # unambiguous incoming rollover is never forced into termination context.
        incoming_rollover_signal = self._infer_incoming_rollover_signal(
            profile_text, topic
        )
        # Fix 4: required_data callers opt into the named-employer pattern so
        # "rollover process … from <employer> to <destination>" qualifies as
        # termination context. Gated behind the parameter so generate_response
        # behavior is unchanged.
        if (
            assume_termination_on_named_employer
            and rollover_intent
            and not termination_distribution
            and not incoming_rollover_signal
            and self._NAMED_EMPLOYER_ROLLOVER_PATTERN.search(inquiry or "")
        ):
            termination_distribution = True

        # F7 (eval 2026-06-22): an explicit, decided separation claim overrides a
        # stale "active" system status. When the participant says they no longer
        # work here / resigned / were fired, route as a termination case (so
        # active-only options are excluded and the termination flow asks for the
        # date) even if the scrape still shows active or status is unknown.
        explicit_separation_claim = self._contains_any(
            profile_text, _EXPLICIT_SEPARATION_PHRASES
        )
        separation_conflicts_active = (
            explicit_separation_claim and employment_state == "active"
        )
        if explicit_separation_claim:
            termination_distribution = True

        return {
            "text": profile_text,
            "employment_state": employment_state,
            "explicit_separation_claim": explicit_separation_claim,
            "separation_conflicts_active": separation_conflicts_active,
            "lowest_balance": lowest_balance,
            "rollover_intent": rollover_intent,
            "distribution_intent": distribution_intent,
            "cash_component": cash_component,
            "pure_rollover": pure_rollover,
            "selected_delivery": selected_delivery,
            "delivery_or_fee_request": delivery_or_fee_request,
            "overnight_request": overnight_request,
            "account_access": account_access,
            "unknown_email_access": unknown_email_access,
            "guidance_request": guidance_request,
            "rollover_provider_named": rollover_provider_named,
            "brokerage_destination_ambiguous": brokerage_destination_ambiguous,
            "loan_signal": loan_signal,
            "termination_distribution": termination_distribution,
            "incoming_rollover": incoming_rollover_signal,
            "split_rollover": split_rollover,
            "indirect_rollover_60_day": indirect_rollover_60_day,
            "force_out": force_out,
            "rmd": rmd,
            "contact_or_reference": contact_or_reference,
            "is_age_59_5_or_older": age_59_5_status,
            "termination_date_present": termination_date_present,
            "rollover_source_balance_state": self._typed_rollover_source_balance_state(
                collected_data
            ),
            "inquiry_intent": self._infer_inquiry_intent(profile_text),
        }

    def _build_retrieval_profile(
        self,
        inquiry: str,
        topic: str,
        record_keeper: Optional[str],
        plan_type: str,
        collected_data: Optional[Dict[str, Any]],
        assume_termination_on_named_employer: bool = False,
    ) -> Dict[str, Any]:
        """
        Build a deterministic routing profile before Pinecone search.

        The profile is intentionally conservative: it only enables exact
        procedure routing when the participant asks for a single-destination
        LT Trust rollover after employment has ended and no adjacent procedure
        (split rollover, 60-day indirect rollover, force-out, or RMD) is
        signalled.

        ``assume_termination_on_named_employer`` is opt-in (used by
        required_data) so a "rollover from <employer> to <destination>"
        phrasing also counts as termination context. generate_response keeps
        its default (False) so its golden tests remain stable.
        """
        signals = self._infer_retrieval_signals(
            inquiry=inquiry,
            topic=topic,
            collected_data=collected_data,
            assume_termination_on_named_employer=assume_termination_on_named_employer,
        )
        query_text = (inquiry or "").lower()
        procedure_requested = bool(re.search(
            r"\b(?:steps|instructions|process|procedure|submit|initiate)\b|\bhow (?:do|can|to)\b",
            query_text,
        ))
        signals["procedure_requested"] = procedure_requested
        retaining_funds_question = not procedure_requested and bool(re.search(
            r"\b(?:keep|leave|retain)\b.{0,50}\b(?:funds|money|invested|account|plan)\b"
            r"|\b(?:funds|money)\b.{0,40}\bremain\b|\bfees\b.{0,35}\bstay\b",
            query_text,
        ))
        if retaining_funds_question:
            signals["inquiry_intent"] = "informational_options"
        identifier_question = bool(re.search(r"\bplan (?:id|identifier|number|code)\b", query_text)) and not procedure_requested
        question_inventory = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions")
        if isinstance(question_inventory, list) and len(question_inventory) > 1:
            identifier_question = identifier_question and all(
                isinstance(question, str) and re.search(r"\bplan (?:id|identifier|number|code)\b", question, re.I)
                for question in question_inventory
            )
        if identifier_question:
            return {
                "mode": "broad_search", "primary_action": "plan_identifier",
                "inquiry_intent": "informational_options", "record_keeper": record_keeper,
                "plan_type": plan_type, "primary_article_id": None,
                "signals": signals, "excluded_articles": [],
                "include_references": signals.get("contact_or_reference", False),
            }
        # A request for the participant's OWN account identifier stays a
        # factual lookup even when the participant explains a rollover motive.
        # The extractor restates the inquiry in the third person and keeps the
        # participant's verbatim wording in requested_questions, so both shapes
        # are inspected. A destination modifier ("their new employer's account
        # number") binds the identifier to the receiving account instead, which
        # is a different fact and keeps its normal routing.
        identifier_texts = [query_text]
        if isinstance(question_inventory, list):
            identifier_texts += [
                question.lower() for question in question_inventory
                if isinstance(question, str) and question.strip()
            ]

        own_account_identifier = inquiry_semantics.requests_own_account_identifier
        # "How do I go about getting my account number?" is procedural wording
        # about obtaining the identifier itself, not a request for the
        # distribution procedure the macro answers.
        identifier_retrieval = any(
            inquiry_semantics.requests_identifier_retrieval(text)
            for text in identifier_texts
        )
        # A stated motive ("so I can roll it into ...", "I am trying to roll my
        # Roth over") is not a request to execute the transaction. An explicit
        # transactional request alongside the identifier keeps both intents on
        # the procedure route instead of collapsing into an identifier answer.
        transaction_requested = any(
            inquiry_semantics.requests_transaction_action(text)
            for text in identifier_texts
        )
        # A quote that asks nothing and requests nothing is background. It
        # cannot satisfy the predicate and it must not veto it either — the
        # bounded answer below still names the motive and stays under review.
        deciding_texts = [
            text for text in identifier_texts
            if not inquiry_semantics.is_background_statement(text)
        ]
        transaction_motive_only = (
            len(deciding_texts) != len(identifier_texts)
            and any(inquiry_semantics.mentions_transaction(text) for text in identifier_texts)
        )
        # Kept on the profile so a mixed rollover+identifier inquiry can still
        # be held to the identifier distinctions after the draft is generated.
        signals["account_identifier_requested"] = any(
            own_account_identifier(text) for text in identifier_texts
        )
        account_identifier_question = (
            bool(deciding_texts)
            and all(own_account_identifier(text) for text in deciding_texts)
            and not transaction_requested
            and (not procedure_requested or identifier_retrieval)
        )
        if account_identifier_question:
            signals["transaction_motive_only"] = transaction_motive_only
            return {
                "mode": "broad_search", "primary_action": "participant_account_identifier",
                "inquiry_intent": "informational_options", "record_keeper": record_keeper,
                "plan_type": plan_type, "primary_article_id": None,
                "signals": signals, "excluded_articles": [],
                "include_references": signals.get("contact_or_reference", False),
            }
        custody_question = bool(re.search(
            r"\bwhere\b.{0,80}\b(?:funds|money)\b"
            r"|\bwhy\b.{0,120}\b(?:zero|no money|no funds|empty)\b"
            r"|\b(?:funds|money|balance|account)\b.{0,60}\b(?:missing|disappeared|zero)\b",
            query_text,
        ))
        if custody_question and not signals.get("incoming_rollover"):
            return {
                "mode": "broad_search", "primary_action": "custody_investigation",
                "inquiry_intent": "informational_options", "record_keeper": record_keeper,
                "plan_type": plan_type, "primary_article_id": None,
                "signals": signals, "excluded_articles": [],
                "include_references": signals.get("contact_or_reference", False),
            }
        record_keeper_text = (record_keeper or "").strip().lower()
        plan_type_text = self._normalize_plan_type(plan_type)
        is_lt_401k = record_keeper_text == "lt trust" and plan_type_text in {
            "401k",
            "401",
        }
        exact_termination_rollover = (
            is_lt_401k
            and not retaining_funds_question
            and signals["rollover_intent"]
            and signals["termination_distribution"]
            and not signals["split_rollover"]
            and not signals["indirect_rollover_60_day"]
            and not signals["force_out"]
            and not signals["rmd"]
        )
        exact_termination_distribution = (
            is_lt_401k
            and not retaining_funds_question
            and signals["distribution_intent"]
            and signals["termination_distribution"]
            and not signals["force_out"]
            and not signals["rmd"]
        )
        exact_termination_procedure = (
            exact_termination_rollover or exact_termination_distribution
        )
        # Fix J (Round 2): when the inquiry triggers the 60-day indirect
        # rollover signal, route to the dedicated global article instead of
        # the LT termination procedure. The article is RK-agnostic so this
        # fires regardless of `is_lt_401k`.
        exact_indirect_rollover_60_day = signals["indirect_rollover_60_day"]
        # Fix K (Round 2): dedicated exact-procedure mode for LT loan
        # requests. Requires loan signal AND no competing exact-procedure
        # signals to avoid mis-routing ambiguous inquiries.
        exact_loan_request = (
            is_lt_401k
            and signals["loan_signal"]
            and not signals["split_rollover"]
            and not signals["termination_distribution"]
            and not signals["force_out"]
            and not signals["rmd"]
            and not signals["indirect_rollover_60_day"]
        )
        # F1 (eval 2026-06-22): exact-procedure mode for INCOMING rollovers
        # (external source -> current ForUsAll plan). Routes to the incoming
        # procedure instead of the outgoing/termination machinery.
        exact_incoming_rollover = (
            is_lt_401k
            and signals["rollover_intent"]
            and signals.get("incoming_rollover")
            and not signals["termination_distribution"]
            and not signals["split_rollover"]
            and not signals["indirect_rollover_60_day"]
            and not signals["force_out"]
            and not signals["rmd"]
            and not signals["loan_signal"]
        )
        # A named hardship request has its own global procedure. Generic active
        # options mention hardship, but must not replace its Guidelines/form flow.
        exact_hardship_request = (
            self._resolve_topic_filter(topic) == ["hardship_withdrawal"]
            and signals["employment_state"] != "terminated"
            and not signals.get("explicit_separation_claim")
            and not signals["loan_signal"]
            and not signals["rollover_intent"]
            and not exact_indirect_rollover_60_day
        )
        exact_procedure_routing = (
            exact_termination_procedure
            or exact_indirect_rollover_60_day
            or exact_loan_request
            or exact_incoming_rollover
            or exact_hardship_request
        )

        excluded_articles: List[str] = []
        exclusion_reasons: Dict[str, str] = {}

        def exclude(article_id: str, reason: str) -> None:
            if article_id not in excluded_articles:
                excluded_articles.append(article_id)
            exclusion_reasons[article_id] = reason

        if (
            (signals["rollover_intent"] or exact_procedure_routing)
            and not signals["split_rollover"]
        ):
            exclude(
                self.SPLIT_ROLLOVER_ARTICLE_ID,
                "No split-rollover signal; participant appears to request a single destination.",
            )
        if (
            (signals["rollover_intent"] or exact_procedure_routing)
            and not signals["indirect_rollover_60_day"]
        ):
            exclude(
                self.MISSED_60_DAY_ARTICLE_ID,
                "No indirect-rollover or missed-60-day deadline signal.",
            )
        if exact_procedure_routing and not signals["loan_signal"]:
            for article_id in self.LOAN_ARTICLE_IDS:
                exclude(
                    article_id,
                    "Exact procedure routing selected; no loan signal.",
                )
        if exact_procedure_routing:
            exclude(
                self.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID,
                "Exact procedure routing selected; general post-termination options are tangential.",
            )
        # Fix J/K: when a non-termination exact procedure wins (60-day or
        # loan), explicitly exclude the LT termination article so it does
        # not compete for primary slot.
        if exact_indirect_rollover_60_day or exact_loan_request or exact_hardship_request:
            exclude(
                self.EXACT_TERMINATION_ROLLOVER_ARTICLE_ID,
                "Different exact procedure selected; LT termination is tangential.",
            )
        if not signals["force_out"]:
            for article_id in self.FORCE_OUT_ARTICLE_IDS:
                exclude(
                    article_id,
                    "No force-out, sponsor-initiated distribution, notice, or low-balance signal.",
                )
        if not signals["rmd"]:
            exclude(
                self.RMD_ARTICLE_ID,
                "No RMD, age-73/75, or required-minimum-distribution signal.",
            )
        if signals["employment_state"] == "terminated":
            exclude(
                self.IN_SERVICE_ARTICLE_ID,
                "Participant is terminated; no while-employed access signal.",
            )
        # F7 (eval 2026-06-22): an explicit separation claim overrides a stale
        # "active" status — withhold the active-only options (in-service, hardship,
        # loan). The generate-response prompt then asks for the termination date,
        # explains the system still shows the participant active and needs internal
        # review, and escalates. Subsumes the terminated-only in-service exclusion
        # above for the active/unknown-but-claimed case (ex-F6).
        if signals.get("explicit_separation_claim"):
            exclude(
                self.IN_SERVICE_ARTICLE_ID,
                "Participant explicitly claims separation; in-service (active-only) not offered.",
            )
            exclude(
                self.HARDSHIP_ARTICLE_ID,
                "Participant explicitly claims separation; hardship (active-only) not offered.",
            )
            for article_id in self.LOAN_ARTICLE_IDS:
                exclude(
                    article_id,
                    "Participant explicitly claims separation; loan (active-only) not offered.",
                )

        if exact_incoming_rollover:
            exclude(
                self.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID,
                "Incoming rollover; post-termination/outgoing distribution options not applicable.",
            )
            for article_id in self.FORCE_OUT_ARTICLE_IDS:
                exclude(article_id, "Incoming rollover; force-out not applicable.")

        rollover_mode = "not_applicable"
        if signals["split_rollover"]:
            rollover_mode = "split"
        elif signals["indirect_rollover_60_day"]:
            rollover_mode = "indirect_60_day"
        elif exact_incoming_rollover:
            rollover_mode = "incoming"
        elif signals["rollover_intent"]:
            rollover_mode = "single_destination"

        # Fix J/K (Round 2): primary_article_id is resolved by exact-procedure
        # precedence — 60-day deadline > termination > loan request — so the
        # narrowest-fit procedure wins when multiple signals are plausible.
        if retaining_funds_question:
            primary_article_id = self.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID
        elif exact_indirect_rollover_60_day:
            primary_article_id = self.MISSED_60_DAY_ARTICLE_ID
        elif exact_termination_rollover or exact_termination_distribution:
            primary_article_id = self.EXACT_TERMINATION_ROLLOVER_ARTICLE_ID
        elif exact_incoming_rollover:
            # TODO F4: the incoming-rollover procedure currently lives inside the LT
            # termination article (its "Incoming Rollover From Prior Employer"
            # section). Re-point to a dedicated INCOMING_ROLLOVER article when it
            # exists in the KB.
            primary_article_id = self.EXACT_TERMINATION_ROLLOVER_ARTICLE_ID
        elif exact_loan_request:
            primary_article_id = self.LT_LOAN_ARTICLE_ID
        elif exact_hardship_request:
            primary_article_id = self.HARDSHIP_ARTICLE_ID
        else:
            primary_article_id = None

        primary_action = "unknown"
        if retaining_funds_question:
            primary_action = "retention_options"
        elif exact_indirect_rollover_60_day:
            primary_action = "indirect_rollover_60_day"
        elif exact_termination_rollover:
            primary_action = "termination_rollover"
        elif exact_termination_distribution:
            primary_action = "termination_distribution"
        elif exact_loan_request:
            primary_action = "loan_request"
        elif exact_hardship_request:
            primary_action = "hardship_withdrawal"
        elif exact_incoming_rollover:
            primary_action = "incoming_rollover"
        elif signals["rollover_intent"]:
            primary_action = "rollover"
        elif signals["distribution_intent"]:
            primary_action = "distribution"
        elif signals["loan_signal"]:
            primary_action = "loan"

        exclusion_signals = [
            name for name, value in {
                "no_split_rollover_signal": not signals["split_rollover"],
                "no_indirect_rollover_60_day_signal": not signals["indirect_rollover_60_day"],
                "no_force_out_signal": not signals["force_out"],
                "no_rmd_signal": not signals["rmd"],
            }.items()
            if value
        ]

        # A termination request can coexist with an access problem. Preserve
        # that second intent by fetching the account-setup/MFA procedures as
        # companions to the exact outgoing procedure. Incoming rollovers are
        # intentionally excluded from this remediation path.
        companion_article_ids: List[str] = []
        if signals.get("account_access") and exact_termination_procedure:
            companion_article_ids = [
                self.ACCOUNT_SETUP_ARTICLE_ID,
                self.MFA_ARTICLE_ID,
            ]

        return {
            "mode": (
                "exact_procedure"
                if exact_procedure_routing or retaining_funds_question
                else "broad_search"
            ),
            "primary_action": primary_action,
            "inquiry_intent": signals["inquiry_intent"],
            "employment_state": signals["employment_state"],
            "separation_status_conflict": signals.get("separation_conflicts_active", False),
            "record_keeper": record_keeper,
            "plan_type": plan_type,
            "rollover_mode": rollover_mode,
            "primary_article_id": primary_article_id,
            "signals": {
                key: value
                for key, value in signals.items()
                if key not in {"text", "lowest_balance"}
            },
            "lowest_balance": signals["lowest_balance"],
            "include_references": signals["contact_or_reference"],
            "companion_article_ids": companion_article_ids,
            "excluded_articles": excluded_articles,
            "exclusion_reasons": exclusion_reasons,
            "exclusion_signals": exclusion_signals,
        }

    @staticmethod
    def _is_truthy_metadata_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _dedupe_preserving_order(values: List[str]) -> List[str]:
        seen = set()
        deduped = []
        for value in values:
            clean = str(value or "").strip()
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(clean)
        return deduped

    def _classify_missing_data(self, missing_items: List[Any]) -> Dict[str, List[str]]:
        """
        Classify missing details by whether they block eligibility or are
        next-step execution/lookup details.
        """
        classes = {
            "core_eligibility_missing": [],
            "execution_details_missing": [],
            "identity_lookup_missing": [],
            "other_nonblocking_missing": [],
        }

        for item in missing_items or []:
            if isinstance(item, dict):
                text = " ".join(
                    str(item.get(key, ""))
                    for key in ("question", "why", "field", "description")
                ).strip()
                label = item.get("question") or item.get("field") or text
            else:
                text = str(item or "").strip()
                label = text
            # Classify the requested fact itself. An identity lookup's reason
            # may mention eligibility facts that are already on file.
            normalized = re.sub(r"[^a-z0-9$]+", " ", str(label).lower()).strip()
            if not normalized:
                continue

            identity_signal = (
                "participant name" in normalized
                or "full name" in normalized
                or "your name" in normalized
                or "email address" in normalized
                or normalized == "email"
                or "company name" in normalized
                or "name of your company" in normalized
            )
            execution_signal = any(phrase in normalized for phrase in [
                "wire",
                "routing",
                "aba",
                "bank account",
                "account number",
                "delivery method",
                "delivery preference",
                "final choice",
                "physical street address",
                "street address",
                "p o box",
                "po box",
                "overnight check",
                "distribution type",
                "lump sum",
                "cash distribution",
                "rollover",
                "email confirmation",
                "portal login",
                "portal access",
                "mfa",
                "participant age",
                "age for",
            ])
            core_signal = any(phrase in normalized for phrase in [
                "employment status",
                "terminated status",
                "termination date",
                "vested balance",
                "account balance",
                "balance threshold",
                "blackout",
                "rehire",
                "plan type",
                "record keeper",
            ])

            # A substantive eligibility gap stays blocking even when its
            # explanation mentions delivery, identity or the rollover process.
            if core_signal:
                classes["core_eligibility_missing"].append(str(label).strip())
            elif identity_signal:
                classes["identity_lookup_missing"].append(str(label).strip())
            elif execution_signal:
                classes["execution_details_missing"].append(str(label).strip())
            else:
                classes["other_nonblocking_missing"].append(str(label).strip())

        return {
            key: self._dedupe_preserving_order(values)
            for key, values in classes.items()
        }

    def _extract_missing_data_mentions(self, parsed: Dict[str, Any]) -> List[Any]:
        mentions: List[Any] = []
        data_gaps = parsed.get("data_gaps", [])
        if isinstance(data_gaps, list):
            mentions.extend(data_gaps)
        questions = parsed.get("questions_to_ask", [])
        if isinstance(questions, list):
            mentions.extend(questions)
        return mentions

    def _termination_distribution_core_eligibility_status(
        self,
        collected_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from data_pipeline.gr_payload_builder import _strict_money

        participant_data = (collected_data or {}).get("participant_data") or {}
        plan_data = (collected_data or {}).get("plan_data") or {}
        profile_text = " ".join([
            self._textify_metadata_value(participant_data),
            self._textify_metadata_value(plan_data),
        ]).lower()

        missing: List[str] = []
        blockers: List[str] = []

        employment_state = self._resolve_employment_state(
            participant_data=participant_data,
            text=profile_text,
        )
        if employment_state == "unknown":
            missing.append("employment status")
        elif employment_state != "terminated":
            blockers.append(f"employment status is {employment_state}")

        if not participant_data.get("termination_date"):
            missing.append("termination date")

        sources = ((collected_data or {}).get("internal_preflight_context") or {}).get("sources") or {}
        if "vested_balance" in sources:
            # An explicit failed/unknown extraction cannot be replaced with a
            # legacy value. Account Balance is never evidence of total vested.
            vested = sources["vested_balance"]
            balance_values = [vested.get("value")] if isinstance(vested, dict) and vested.get("status") == "known" else []
        else:
            balance_values = [
                participant_data.get("total_vested_balance"),
                participant_data.get("vested_balance"),
            ]
        balance = next(
            (
                amount for amount in (
                    _strict_money(value) for value in balance_values
                )
                if amount is not None
            ),
            None,
        )
        if balance is None:
            missing.append("vested balance")
        elif balance <= 0:
            blockers.append("vested balance is not positive")

        if "blackout_period" not in plan_data and "blackout" not in plan_data:
            missing.append("blackout status")
        elif self._is_truthy_metadata_value(
            plan_data.get("blackout_period", plan_data.get("blackout"))
        ):
            blockers.append("blackout period is active")

        if participant_data.get("rehire_date"):
            blockers.append("rehire date is present")

        return {
            "supported": not missing and not blockers,
            "core_eligibility_missing": missing,
            "blocking_conditions": blockers,
            "employment_state": employment_state,
            "vested_balance": balance,
        }

    # Allowed values for the `blocking_intent` field on each must_have item.
    # See PA/article_guide_for_LLM.md (section 3.3.12) for full semantics.
    _VALID_BLOCKING_INTENTS = frozenset({
        "always",
        "execution_only",
        "personalization_only",
        "eligibility_confirmation",
    })
    # blocking_intent values that, when missing, should NOT block an
    # informational answer. `eligibility_confirmation` is intentionally kept
    # OUT of this set because it does block personalized eligibility claims;
    # it only stops blocking when no eligibility claim is being made (the
    # `core_eligibility_supported` core_status path takes care of that case
    # for terminations).
    _RESCUABLE_BLOCKING_INTENTS = frozenset({
        "execution_only",
        "personalization_only",
    })

    def _extract_must_have_blocking_intents(
        self,
        selected_chunks: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, str]:
        """Return a mapping of must_have data_point -> blocking_intent.

        Reads the canonical `must_have_blocking_intents` metadata field
        (a list of "data_point|blocking_intent" strings) added by the
        chunker. Falls back to the legacy default of `always` for any
        item whose blocking_intent is missing or not recognized.
        """
        intents: Dict[str, str] = {}
        for chunk in selected_chunks or []:
            metadata = chunk.get("metadata") or {}
            if metadata.get("chunk_type") != "required_data_must_have":
                continue
            entries = metadata.get("must_have_blocking_intents") or []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, str) or "|" not in entry:
                    continue
                data_point, _, intent = entry.partition("|")
                data_point = data_point.strip()
                intent = intent.strip().lower()
                if not data_point:
                    continue
                if intent not in self._VALID_BLOCKING_INTENTS:
                    intent = "always"
                # Don't overwrite a stricter intent already recorded for the
                # same data_point (e.g. if two articles surface the same
                # field with different intents, prefer the strictest).
                existing = intents.get(data_point)
                if existing == "always":
                    continue
                if existing and intent in self._RESCUABLE_BLOCKING_INTENTS and existing not in self._RESCUABLE_BLOCKING_INTENTS:
                    continue
                intents[data_point] = intent
        return intents

    # Common low-information tokens that should not, on their own, count
    # as evidence that two data-point labels refer to the same field.
    _MATCH_STOPWORDS = frozenset({
        "a", "an", "and", "the", "of", "for", "is", "are", "was", "were",
        "to", "in", "on", "by", "or", "your", "you", "i", "me", "my",
        "this", "that", "what", "when", "where", "which", "how", "do",
        "does", "did", "with", "from", "into", "as", "if", "be", "been",
        "have", "has", "had", "any", "some",
        # KB-specific noise tokens that recur across many data_points
        "participant", "participants", "data", "value", "values",
        "field", "fields", "information", "info",
    })

    @staticmethod
    def _normalize_text_for_match(value: Any) -> str:
        text = str(value or "").lower()
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    @classmethod
    def _meaningful_tokens(cls, normalized: str) -> set:
        return {
            tok for tok in normalized.split()
            if len(tok) >= 3 and tok not in cls._MATCH_STOPWORDS
        }

    def _resolve_missing_intent(
        self,
        missing_label: str,
        intents_by_data_point: Dict[str, str],
    ) -> str:
        """Map a missing-data label (free text from the LLM or schema) to a
        blocking_intent value by best-effort matching against the names of
        must_have items surfaced from chunks. Defaults to `always` when no
        confident match is found, preserving the prior blocking behaviour.

        Matching strategy (most specific first):

        1. Substring / containment after lowercasing and stripping punctuation.
        2. Jaccard similarity over meaningful (non-stopword, len>=3) tokens
           must be >= 0.34 AND share at least one meaningful token.
        3. Fallback: if a single rare data-point token (len>=4 and not a
           stopword) appears in the missing label and the data_point has
           only 1-2 meaningful tokens, accept the match.
        """
        if not missing_label:
            return "always"
        target = self._normalize_text_for_match(missing_label)
        if not target:
            return "always"
        target_tokens = self._meaningful_tokens(target)
        best_intent = None
        best_score = 0.0
        for data_point, intent in intents_by_data_point.items():
            normalized_dp = self._normalize_text_for_match(data_point)
            if not normalized_dp:
                continue
            # 1. Containment (strong signal).
            if (
                normalized_dp == target
                or normalized_dp in target
                or target in normalized_dp
            ):
                return intent
            dp_tokens = self._meaningful_tokens(normalized_dp)
            if not dp_tokens or not target_tokens:
                continue
            shared = dp_tokens & target_tokens
            if not shared:
                continue
            # 2. Jaccard over meaningful tokens.
            union = dp_tokens | target_tokens
            jaccard = len(shared) / len(union)
            if jaccard >= 0.34 and jaccard > best_score:
                best_score = jaccard
                best_intent = intent
                continue
            # 3. Single rare-token fallback for short data_points.
            if len(dp_tokens) <= 2 and any(len(t) >= 4 for t in shared):
                # Treat as a softer match; only adopt if nothing stronger found.
                if best_intent is None:
                    best_intent = intent
        return best_intent or "always"

    @staticmethod
    def _compute_age_from_birth_date(raw_dob: Any) -> Optional[int]:
        """Parse a birth date in the formats ForUsBots emits (and a few common
        variants) and return completed age in whole years. None when the value
        is missing or unparseable."""
        if raw_dob is None:
            return None
        text = str(raw_dob).strip()
        if not text:
            return None
        parsed = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        today = datetime.now(timezone.utc).date()
        bd = parsed.date()
        if bd > today:
            return None
        age = today.year - bd.year - (
            (today.month, today.day) < (bd.month, bd.day)
        )
        return age if 0 <= age <= 130 else None

    @staticmethod
    def _is_age_59_5_or_older(raw_dob: Any) -> Optional[bool]:
        """Return whether the participant is at least 59½, derived from birth
        date. None when birth date is missing or unparseable."""
        if raw_dob is None or not str(raw_dob).strip():
            return None
        text = str(raw_dob).strip()
        parsed = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        today = datetime.now(timezone.utc).date()
        bd = parsed.date()
        if bd > today:
            return None
        # 59½ = 59 years and 6 months after the birth date.
        half_year_year = bd.year + 59
        half_year_month = bd.month + 6
        if half_year_month > 12:
            half_year_month -= 12
            half_year_year += 1
        # Clamp the day for short months (e.g. born on the 31st).
        try:
            threshold = bd.replace(year=half_year_year, month=half_year_month)
        except ValueError:
            # Day-of-month overflow (e.g. 31 -> month with 30 days): use day 28.
            threshold = bd.replace(year=half_year_year, month=half_year_month, day=28)
        return today >= threshold

    def _enrich_collected_data_with_age(
        self, collected_data: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Inject derived ``age`` and ``is_age_59_5_or_older`` into
        ``participant_data`` from ``birth_date`` so the GR phases can make
        age-dependent determinations definite (in-service 59½ eligibility,
        early-withdrawal penalty). Returns the input unchanged when there is no
        parseable birth date. Does not mutate the caller's dict."""
        if not isinstance(collected_data, dict):
            return collected_data
        participant = collected_data.get("participant_data")
        if not isinstance(participant, dict):
            return collected_data
        raw_dob = participant.get("birth_date")
        age = self._compute_age_from_birth_date(raw_dob)
        if age is None:
            return collected_data
        enriched = dict(collected_data)
        new_participant = dict(participant)
        new_participant["age"] = age
        over = self._is_age_59_5_or_older(raw_dob)
        if over is not None:
            new_participant["is_age_59_5_or_older"] = over
        enriched["participant_data"] = new_participant
        return enriched

    @staticmethod
    def _suppress_nonblocking_questions_for_can_proceed(
        parsed: Dict[str, Any]
    ) -> List[Any]:
        """A can_proceed answer is a complete first-contact resolution. Any items
        in ``questions_to_ask`` are non-blocking next-step details (requested
        amount, repayment term, delivery method, ...) the participant handles
        during the process; asking them only reopens the ticket. Drop them in
        place and return the dropped items (for logging). No-op for any other
        outcome — there, questions are genuine blockers we must ask."""
        if not isinstance(parsed, dict) or parsed.get("outcome") != "can_proceed":
            return []
        stray = parsed.get("questions_to_ask") or []
        if not isinstance(stray, list):
            stray = []
        parsed["questions_to_ask"] = []
        return stray

    @staticmethod
    def _validate_question_coverage(parsed: Dict[str, Any], collected_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate coverage evidence after policies have changed the draft.

        This proves that a claimed answer is present, not that its financial
        interpretation is correct. Semantic replay remains a separate gate.
        Missing evidence is an internal review, not a technical retry.
        """
        context = (collected_data or {}).get("internal_response_context")
        questions = context.get("requested_questions") if isinstance(context, dict) else None
        raw = parsed.pop("question_coverage", None)
        if not isinstance(questions, list) or not questions:
            return {}
        response_text = json.dumps(parsed.get("response_to_participant") or {}, ensure_ascii=False).casefold()
        entries: Dict[int, Dict[str, Any]] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            index = item.get("question_index")
            if type(index) is int and 0 <= index < min(len(questions), 12) and index not in entries:
                entries[index] = item
        coverage = []
        for index in range(min(len(questions), 12)):
            item = entries.get(index, {})
            reference = item.get("answer_reference")
            reference = reference.strip()[:600] if isinstance(reference, str) else ""
            status = item.get("status")
            if status not in {"answered", "needs_verification", "not_applicable"} or not reference:
                status = "needs_verification"
            if status in {"answered", "not_applicable"} and reference.casefold() not in response_text:
                status = "needs_verification"
            coverage.append({"question_index": index, "status": status, "answer_reference": reference})
        incomplete = sum(item["status"] == "needs_verification" for item in coverage)
        unresolved = parsed.get("inapplicable_options_unresolved")
        has_unresolved = isinstance(unresolved, list) and bool(unresolved)
        metadata = {"question_coverage": coverage, "incomplete_question_count": incomplete,
                "requested_questions": [question[:1000] if isinstance(question, str) else "" for question in questions[:12]],
                "human_review_required": incomplete > 0 or has_unresolved}
        if has_unresolved:
            metadata["inapplicable_options_unresolved"] = unresolved
        return metadata

    _TERMINATION_FORM_URL = (
        "https://secure.rightsignature.com/templates/"
        "105723c3-eaf1-4a44-aaed-09ad5c253ec8/template-signer-link/"
        "a9d83fbb137e5315fbd641bb522b9723"  # pragma: allowlist secret
    )

    _ACCOUNT_IDENTIFIER_PENDING = (
        "Our team needs to verify your account number in this plan before we "
        "can provide it."
    )
    _ACCOUNT_IDENTIFIER_DISTINCTION = (
        "Your account number identifies you inside this plan: it is not the "
        "recordkeeper plan code that identifies the plan, and it is not the "
        "receiving account at the provider you want to move the funds to."
    )
    _ACCOUNT_IDENTIFIER_MOTIVE_NOTE = (
        "You also told us why you need it. This reply answers only the account "
        "number question; nothing has been started, submitted or changed on "
        "your account, and our team is reviewing the rest of your message."
    )

    @staticmethod
    def _verified_plan_identity_point(collected_data: Optional[Dict[str, Any]]) -> Optional[str]:
        """Name the plan from VERIFIED plan facts, or say nothing at all.

        A2 asks for the plan name/ID with correct provenance when available.
        ``project_verified_plan_facts`` is the only source that carries one: it
        requires a matched identity, the exact scrape source for the field and
        an observed/as-of date, and drops anything that fails. With no such
        fact this returns None and the answer stays silent plan-side, which is
        also what keeps A3/A5 true — the plan code is never a stand-in for the
        participant's own account number.
        """
        from data_pipeline.gr_payload_builder import project_verified_plan_facts

        disclosure = project_verified_plan_facts(
            (collected_data or {}).get("internal_plan_disclosure_context")
        )
        facts = disclosure.get("facts") if isinstance(disclosure, dict) else None
        if not isinstance(facts, dict):
            return None
        def _fact(key: str) -> tuple[Optional[str], Optional[str]]:
            entry = facts.get(key)
            if not isinstance(entry, dict):
                return None, None
            return entry.get("value"), (entry.get("as_of") or entry.get("observed_at"))

        name, name_date = _fact("legal_plan_name")
        code, code_date = _fact("rk_plan_id")
        keeper, keeper_date = _fact("record_keeper")
        if not name and not code:
            return None
        # Each fact carries its own observation date. Attributing one fact's
        # date to another would misstate provenance, so a shared date is
        # rendered once and differing dates are rendered per fact.
        labelled = [(label, value, date) for label, value, date in (
            ("the plan name", name, name_date),
            ("the recordkeeper plan code", code, code_date),
            ("the recordkeeper", keeper, keeper_date),
        ) if value]
        dates = {date for _, _, date in labelled if date}
        shared = dates.pop() if len(dates) == 1 else None

        if name and code:
            subject = f"{name} (recordkeeper plan code {code})"
        elif name:
            subject = str(name)
        else:
            subject = f"the plan with recordkeeper plan code {code}"
        point = f"Your plan is {subject}"
        # A2 asks for the recordkeeper itself, not only the plan code it issued.
        if keeper:
            point += f", held at {keeper}"
        if shared:
            point += f", from the plan record as of {shared}"
        elif dates:
            # Differing observation dates: each fact is attributed to its own,
            # because borrowing one fact's date for another misstates provenance.
            point += "; from the plan record, " + ", ".join(
                f"{label} as of {date}" for label, _, date in labelled if date)
        point += "."
        if code:
            point += (" That recordkeeper plan code identifies the plan, not you, "
                      "so it is not your account number.")
        return point

    # The participant's own account number, the recordkeeper plan code and the
    # receiving account at the destination provider are three different facts.
    # The reading lives in ``inquiry_semantics`` so the orchestrator applies the
    # same one when it canonicalises the inquiry list before routing.
    @staticmethod
    def _requests_own_account_identifier(text: str) -> bool:
        """True when the text asks for the participant's own account number."""
        return inquiry_semantics.requests_own_account_identifier(text)

    @staticmethod
    def _response_item_text(item: Any) -> str:
        if isinstance(item, str):
            return item.lower()
        try:
            return json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
        except (TypeError, ValueError):
            return str(item).lower()

    @staticmethod
    def _plan_code_identifier_claim(plan_code: str) -> "re.Pattern[str]":
        """Matches text that offers ``plan_code`` as the account number.

        The two alternatives are the two orders the claim is written in
        ("your account number is <code>" / "<code> is your account number").
        ``[^.]`` keeps a match inside one sentence, so a neighbouring sentence
        that legitimately names the plan code is never pulled into the match.
        """
        code = re.escape(plan_code.lower())
        return re.compile(
            r"account\s*(?:number|id|#)\b[^.]{0,60}" + code
            + r"|" + code + r"[^.]{0,60}account\s*(?:number|id|#)\b"
        )

    @classmethod
    def _strike_identifier_claim(
        cls, text: Any, claim: "re.Pattern[str]",
    ) -> tuple[str, int]:
        """Drop only the sentences of ``text`` that make the wrong claim."""
        sentences = re.split(r"(?<=[.!?])\s+", str(text or ""))
        kept = [part for part in sentences if not claim.search(part.lower())]
        return " ".join(kept).strip(), len(sentences) - len(kept)

    @classmethod
    def _correct_claimed_account_identifier(
        cls, response: Dict[str, Any], claim: "re.Pattern[str]",
    ) -> int:
        """Remove a claimed participant identifier from the whole response.

        A draft that states "your account number is <plan code>" and then
        denies it hands the reviewer a contradiction whose concrete half is
        the wrong one, so the claim is struck rather than annotated. The cut
        is sentence-level: an independent instruction sharing a step or a key
        point with the claim keeps its own wording, and only an item left with
        nothing at all is dropped. An opening reduced to nothing falls back to
        the pending verification, because a blank opening is not a response
        and inventing the identifier is never the alternative.
        """
        corrected = 0
        opening, removed = cls._strike_identifier_claim(response.get("opening"), claim)
        if removed:
            corrected += removed
            response["opening"] = opening or cls._ACCOUNT_IDENTIFIER_PENDING
        for field in ("key_points", "warnings"):
            items = response.get(field)
            if not isinstance(items, list):
                continue
            kept: List[Any] = []
            for item in items:
                if not isinstance(item, str):
                    kept.append(item)
                    continue
                text, removed = cls._strike_identifier_claim(item, claim)
                corrected += removed
                if text:
                    kept.append(text)
            response[field] = kept
        steps = response.get("steps")
        if isinstance(steps, list):
            kept_steps: List[Any] = []
            for original in steps:
                if not isinstance(original, dict):
                    kept_steps.append(original)
                    continue
                step = dict(original)
                action, removed = cls._strike_identifier_claim(step.get("action"), claim)
                corrected += removed
                if removed:
                    if not action:
                        # The step said nothing except the wrong claim.
                        continue
                    step["action"] = action
                if isinstance(step.get("detail"), str):
                    detail, removed = cls._strike_identifier_claim(step["detail"], claim)
                    corrected += removed
                    if removed:
                        step["detail"] = detail or None
                kept_steps.append(step)
            for index, step in enumerate(kept_steps, start=1):
                if isinstance(step, dict) and "step_number" in step:
                    step["step_number"] = index
            response["steps"] = kept_steps
        return corrected

    @classmethod
    def _apply_account_identifier_distinctions(
        cls,
        parsed: Dict[str, Any],
        retrieval_profile: Optional[Dict[str, Any]],
        collected_data: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Hold a requested own-account identifier to its own distinctions.

        A real rollover request that carries an identifier subquestion keeps
        its procedure answer; what it must not do is satisfy the identifier
        with the recordkeeper plan code or with the receiving account at the
        destination provider, or claim the identifier was answered. The pass
        adds the missing distinction and the specific pending verification,
        and never supplies an identifier value of its own.
        """
        info: Dict[str, Any] = {
            "applied": False, "plan_code_echoed": False,
            "identifier_claims_corrected": 0,
        }
        profile = retrieval_profile or {}
        signals = profile.get("signals") or {}
        if signals.get("account_identifier_requested") is not True:
            return parsed, info
        if profile.get("primary_action") == "participant_account_identifier":
            # Already answered as a bounded identifier lookup.
            return parsed, info
        response = parsed.get("response_to_participant")
        if not isinstance(response, dict):
            return parsed, info

        fixed = copy.deepcopy(parsed)
        response = fixed["response_to_participant"]
        plan_code = ((collected_data or {}).get("plan_data") or {}).get("rk_plan_id")
        plan_code = str(plan_code).strip() if type(plan_code) in (str, int) else ""
        rendered = cls._response_item_text(response)
        if plan_code:
            claim = cls._plan_code_identifier_claim(plan_code)
            if claim.search(rendered):
                # The plan code was offered as the participant's account
                # number. Detecting that is not enough: the wrong value must
                # leave the draft before a human ever reads it.
                info["plan_code_echoed"] = True
                corrected = cls._correct_claimed_account_identifier(response, claim)
                info["identifier_claims_corrected"] = corrected
                if corrected:
                    # Only claimed when something was actually struck: the
                    # match above is taken over the whole rendered response and
                    # can straddle two items that each read correctly alone.
                    guardrails = list(fixed.get("guardrails_applied") or [])
                    guardrails.append(
                        "Removed a drafted claim that the recordkeeper plan "
                        "code was the participant's own account number."
                    )
                    fixed["guardrails_applied"] = cls._dedupe_preserving_order(guardrails)

        points = list(response.get("key_points") or [])
        points.append(cls._ACCOUNT_IDENTIFIER_DISTINCTION)
        plan_point = cls._verified_plan_identity_point(collected_data)
        if plan_point:
            points.append(plan_point)
        points.append(cls._ACCOUNT_IDENTIFIER_PENDING)
        response["key_points"] = cls._dedupe_preserving_order(points)

        gaps = list(fixed.get("data_gaps") or [])
        gaps.append("Verified participant account number")
        fixed["data_gaps"] = cls._dedupe_preserving_order(gaps)
        escalation = fixed.get("escalation")
        escalation = dict(escalation) if isinstance(escalation, dict) else {}
        escalation["needed"] = True
        escalation["reason"] = (
            escalation.get("reason")
            or "Verify the participant's own account number on the correct plan and participant record before providing it."
        )
        fixed["escalation"] = escalation

        questions = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions")
        if isinstance(questions, list) and questions:
            coverage = fixed.get("question_coverage")
            coverage = list(coverage) if isinstance(coverage, list) else []
            by_index = {
                item.get("question_index"): dict(item)
                for item in coverage if isinstance(item, dict)
            }
            for index, question in enumerate(questions[:12]):
                if not isinstance(question, str):
                    continue
                if not cls._requests_own_account_identifier(question.lower()):
                    continue
                entry = by_index.get(index, {"question_index": index})
                entry["question_index"] = index
                entry["status"] = "needs_verification"
                entry["answer_reference"] = cls._ACCOUNT_IDENTIFIER_PENDING
                by_index[index] = entry
            if by_index:
                fixed["question_coverage"] = [
                    by_index[index] for index in sorted(
                        key for key in by_index if type(key) is int
                    )
                ]
        info["applied"] = True
        return fixed, info

    @staticmethod
    def _preflight_loan_state(preflight: Dict[str, Any]) -> str:
        """Normalized loan exposure: ``positive`` | ``zero`` | ``unknown``."""
        loans = preflight.get("loans")
        if not isinstance(loans, dict):
            return "unknown"
        state = loans.get("outstanding_status", "unknown")
        if loans.get("status") not in {"known", "empty"}:
            return "unknown"
        return state if state in {"positive", "zero"} else "unknown"

    @staticmethod
    def _preflight_obligation_warnings(loan_state: str, holdings: Dict[str, Any]) -> List[str]:
        """The participant-facing loan/crypto obligations for a preflight state.

        Shared by the submission preflight and the zero-total custody guard so
        one recorded loan cannot be described two different ways.
        """
        obligations: List[str] = []
        if loan_state == "positive":
            obligations.append("An outstanding loan is recorded. Our team needs to verify its payoff or offset handling before your distribution request can be submitted.")
        elif loan_state == "unknown":
            obligations.append("Your loan status has not been verified. Support needs to confirm whether any outstanding loan affects this request.")
        holdings = holdings if isinstance(holdings, dict) else {}
        known_holdings = holdings.get("status") == "known"
        if known_holdings and holdings.get("value") == 0:
            return obligations
        if known_holdings and isinstance(holdings.get("value"), (int, float)) and holdings["value"] > 0:
            obligations.append("Crypto positions are recorded. Support must verify the applicable transfer requirements before the distribution; enrollment alone does not establish those requirements.")
        else:
            obligations.append("Crypto positions have not been verified. Our team needs to verify holdings and the applicable transfer requirements before submission; enrollment alone does not establish whether you hold crypto.")
        return obligations

    @classmethod
    def _zero_total_is_contested(cls, preflight: Dict[str, Any]) -> bool:
        """True when a zero Account Balance is not proof of an empty account.

        TKT-912043: the total is one asynchronously updated source row. A
        recorded loan, a positive sibling source row, or recorded crypto
        enrollment with unverified holdings all contradict "the account is
        empty", and each carries an obligation the participant must not lose.
        Plain unknown-ness (no loan panel at all) is deliberately NOT treated as
        a contradiction, so a genuinely empty account keeps its concise answer.
        """
        loans = preflight.get("loans")
        loans = loans if isinstance(loans, dict) else {}
        if cls._preflight_loan_state(preflight) == "positive" or loans.get("conflict") is True:
            return True
        sources = preflight.get("sources")
        for key, source in (sources if isinstance(sources, dict) else {}).items():
            if key == "account_balance" or not isinstance(source, dict):
                continue
            value = source.get("value")
            if source.get("status") == "known" and isinstance(value, (int, float)) and value > 0:
                return True
        crypto = preflight.get("crypto")
        crypto = crypto if isinstance(crypto, dict) else {}
        enrollment = crypto.get("enrollment")
        enrollment = enrollment if isinstance(enrollment, dict) else {}
        holdings = crypto.get("holdings")
        holdings = holdings if isinstance(holdings, dict) else {}
        enrolled = enrollment.get("status") == "known" and enrollment.get("value") is True
        holdings_cleared = holdings.get("status") == "known" and holdings.get("value") == 0
        return enrolled and not holdings_cleared

    _INSERVICE_OPTION_SUBJECTS = {
        "hardship": re.compile(r"hardship", re.I),
        "loan": re.compile(r"\bloan", re.I),
        "rollover_source": re.compile(r"rollover[-\s]source", re.I),
        "age_based_in_service": re.compile(r"in[-\s]service distribution", re.I),
    }
    _INSERVICE_LIST_INTROS = (
        re.compile(r"^(?P<prefix>options:)\s*", re.I),
        re.compile(r"(?P<prefix>.*\bthe main possible options are)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\bpossible options are)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\bchoices are)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\broutes that may exist are)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\boptions include)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\bmay take)\s*", re.I | re.S),
        re.compile(r"(?P<prefix>.*\bcan take)\s*", re.I | re.S),
    )
    _INSERVICE_NON_OFFER = re.compile(
        r"\b(?:not available|generally not available|is not an option|"
        r"are not available|does not apply|do not apply|may not apply|"
        r"would typically not|do not qualify|does not qualify|"
        r"not applicable|isn['’]t applicable|"
        r"once you reach|when you (?:reach|turn)|after you (?:reach|turn)|"
        r"may become eligible|become eligible for)\b",
        re.I,
    )
    _INSERVICE_BALANCE_ZERO = re.compile(
        r"(?:\$\s*0(?:\.0+)?|\b0\.0+\b|"
        r"\bbalance\b[^.]{0,48}\b(?:zero|0)\b|"
        r"\b(?:zero|0)\b[^.]{0,48}\bbalance\b)",
        re.I,
    )
    _INSERVICE_SOURCE_ZERO = re.compile(
        r"(?:rollover[-\s]source.{0,96}(?:\$\s*0(?:\.0+)?|\b0\.0+\b|\bzero\b)|"
        r"(?:\$\s*0(?:\.0+)?|\b0\.0+\b|\bzero\b).{0,96}rollover[-\s]source)",
        re.I,
    )
    _INSERVICE_ROLLOVER_NON_OFFER_REPLACEMENT = (
        "A rollover-source withdrawal does not apply to you."
    )
    _INSERVICE_LEAF_MAX_DEPTH = 8
    _INSERVICE_CORRUPT_PROSE = re.compile(
        r"\bare,|\b;;|distributionss|,\s*;|are\s+;|;\s*\.|"
        r"\b(?:may|can) take but\b",
        re.I,
    )
    _INSERVICE_CLAUSE_SPLIT = re.compile(r",\s+(?:and|or)\s+", re.I)

    @staticmethod
    def _typed_rollover_source_balance_state(
        collected_data: Optional[Dict[str, Any]],
    ) -> str:
        sources = ((collected_data or {}).get("internal_preflight_context") or {}).get("sources")
        sources = sources if isinstance(sources, dict) else {}
        rollover = sources.get("rollover_balance")
        rollover = rollover if isinstance(rollover, dict) else {}
        if rollover.get("status") != "known":
            return "unknown"
        value = rollover.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "unknown"
        if value == 0:
            return "known_zero"
        if value > 0:
            return "known_positive"
        return "unknown"

    @classmethod
    def _inservice_option_applicability(
        cls, collected_data: Optional[Dict[str, Any]]
    ) -> Dict[str, str]:
        state = cls._typed_rollover_source_balance_state(collected_data)
        if state == "known_zero":
            rollover = "inapplicable"
        elif state == "known_positive":
            rollover = "applicable"
        else:
            rollover = "unknown"
        age_flag = ((collected_data or {}).get("participant_data") or {}).get(
            "is_age_59_5_or_older"
        )
        if age_flag is False:
            age = "inapplicable"
        elif age_flag is True:
            age = "applicable"
        else:
            age = "unknown"
        return {
            "rollover_source": rollover,
            "age_based_in_service": age,
        }

    @classmethod
    def _option_subjects_in_text(cls, text: str) -> set:
        return {
            key
            for key, pattern in cls._INSERVICE_OPTION_SUBJECTS.items()
            if pattern.search(text or "")
        }

    @classmethod
    def _inservice_item_text(cls, item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in ("text", "content", "point", "value", "key_point"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return " ".join(
                value for value in item.values()
                if isinstance(value, str) and value.strip()
            )
        return cls._response_item_text(item)

    @classmethod
    def _is_inservice_non_offer_note(cls, text: str) -> bool:
        return bool(cls._INSERVICE_NON_OFFER.search(text or ""))

    @classmethod
    def _has_inservice_source_zero_figure(cls, text: str) -> bool:
        return bool(text) and bool(cls._INSERVICE_SOURCE_ZERO.search(text))

    @classmethod
    def _inservice_source_zero_clause(cls, text: str) -> bool:
        return cls._has_inservice_source_zero_figure(text) or (
            "rollover_source" in cls._option_subjects_in_text(text)
            and bool(cls._INSERVICE_BALANCE_ZERO.search(text))
        )

    @classmethod
    def _truthful_rollover_non_offer(cls, text: str) -> str:
        if cls._is_inservice_non_offer_note(text) and re.search(
            r"\bis not an option\b", text or "", re.I
        ):
            return "A rollover-source withdrawal is not an option."
        return cls._INSERVICE_ROLLOVER_NON_OFFER_REPLACEMENT

    @classmethod
    def _strip_inservice_known_zero_disclosure(cls, text: str) -> str:
        """Keep a non-offer, drop an account-specific source zero figure."""
        if not isinstance(text, str) or not text.strip():
            return text
        if not cls._inservice_source_zero_clause(text):
            return text
        kept_sentences: List[str] = []
        changed = False
        for sentence in cls._split_inservice_sentences(text):
            if not cls._inservice_source_zero_clause(sentence):
                kept_sentences.append(sentence)
                continue
            clauses = [
                part.strip()
                for part in re.split(r"\s*;\s*", sentence)
                if part.strip()
            ]
            kept_clauses = [
                clause for clause in clauses
                if not cls._inservice_source_zero_clause(clause)
            ]
            if kept_clauses:
                rebuilt = "; ".join(kept_clauses)
                if not rebuilt.endswith((".", "!", "?")):
                    rebuilt += "."
                if (
                    cls._is_inservice_non_offer_note(rebuilt)
                    and not cls._inservice_source_zero_clause(rebuilt)
                    and not re.search(r"your (?:current )?balance is", rebuilt, re.I)
                ):
                    kept_sentences.append(rebuilt)
                    changed = True
                    continue
            subjects = cls._option_subjects_in_text(sentence)
            if (
                "rollover_source" in subjects
                and cls._is_inservice_non_offer_note(sentence)
            ):
                kept_sentences.append(cls._truthful_rollover_non_offer(sentence))
                changed = True
                continue
            return text
        stripped = " ".join(kept_sentences).strip()
        if (
            changed
            and stripped
            and not cls._inservice_source_zero_clause(stripped)
            and not re.search(r"your (?:current )?balance is", stripped, re.I)
        ):
            return stripped
        if (
            stripped
            and cls._is_inservice_non_offer_note(stripped)
            and not cls._inservice_source_zero_clause(stripped)
            and not re.search(r"your (?:current )?balance is", stripped, re.I)
        ):
            return stripped
        return text

    @classmethod
    def _split_inservice_sentences(cls, text: str) -> List[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]

    @classmethod
    def _inservice_text_leaves(cls, item: Any, depth: int = 0) -> tuple[List[str], bool]:
        if depth > cls._INSERVICE_LEAF_MAX_DEPTH:
            return [], True
        if isinstance(item, str):
            return ([item] if item.strip() else []), False
        if isinstance(item, dict):
            leaves: List[str] = []
            truncated = False
            for value in item.values():
                child_leaves, child_truncated = cls._inservice_text_leaves(
                    value, depth + 1
                )
                leaves.extend(child_leaves)
                truncated = truncated or child_truncated
            return leaves, truncated
        if isinstance(item, (list, tuple)):
            leaves: List[str] = []
            truncated = False
            for value in item:
                child_leaves, child_truncated = cls._inservice_text_leaves(
                    value, depth + 1
                )
                leaves.extend(child_leaves)
                truncated = truncated or child_truncated
            return leaves, truncated
        return [], False

    @classmethod
    def _offered_inapplicable_subjects(cls, text: str, inapplicable: set) -> set:
        offered: set = set()
        if not text or not inapplicable:
            return offered
        for sentence in cls._split_inservice_sentences(text):
            units = [
                part.strip()
                for part in re.split(r"\s*;\s*", sentence)
                if part.strip()
            ] or [sentence]
            for unit in units:
                all_subjects = cls._option_subjects_in_text(unit)
                subjects = all_subjects & inapplicable
                if not subjects:
                    continue
                if cls._is_inservice_non_offer_note(unit):
                    if len(subjects) == 1 and all_subjects == subjects:
                        continue
                    offered |= subjects
                    continue
                offered |= subjects
        return offered

    @classmethod
    def _offered_inapplicable_subjects_in_item(cls, item: Any, inapplicable: set) -> set:
        offered: set = set()
        if not inapplicable:
            return offered
        leaves, truncated = cls._inservice_text_leaves(item)
        if truncated:
            blob = cls._response_item_text(item)
            blob_subjects = cls._option_subjects_in_text(blob) & inapplicable
            return blob_subjects or set(inapplicable)
        if not leaves:
            blob = cls._response_item_text(item)
            return cls._offered_inapplicable_subjects(blob, inapplicable)
        for leaf in leaves:
            offered |= cls._offered_inapplicable_subjects(leaf, inapplicable)
        return offered

    @classmethod
    def _inservice_present_inapplicable_subjects(cls, item: Any, inapplicable: set) -> set:
        """Names still present in one participant field.

        A truncated leaf walk cannot prove absence, so every inapplicable name
        stays present and bookkeeping must not claim it was removed.
        """
        if not inapplicable:
            return set()
        leaves, truncated = cls._inservice_text_leaves(item)
        if truncated:
            return set(inapplicable)
        present: set = set()
        for leaf in leaves or [cls._response_item_text(item)]:
            present |= cls._option_subjects_in_text(leaf) & inapplicable
        return present

    @classmethod
    def _split_inservice_list_tail(cls, remainder: str) -> tuple[str, str]:
        if ";" not in remainder:
            return remainder, ""
        if "," in remainder:
            head, tail = remainder.split(";", 1)
            if not cls._option_subjects_in_text(tail):
                return head.strip(), ";" + tail
            return remainder, ""
        parts = remainder.split(";")
        if parts and not cls._option_subjects_in_text(parts[-1]):
            return ";".join(parts[:-1]).strip(), ";" + parts[-1]
        return remainder, ""

    @classmethod
    def _split_inservice_option_list(cls, list_text: str) -> List[str]:
        text = list_text.strip()
        if not text:
            return []
        if ";" in text and "," not in text:
            parts = [part.strip() for part in text.split(";") if part.strip()]
        elif "," in text:
            parts = []
            for chunk in re.split(r",\s*", text):
                chunk = re.sub(r"^(?:or|and)\s+", "", chunk.strip(), flags=re.I)
                if not chunk:
                    continue
                parts.extend(
                    piece.strip()
                    for piece in re.split(r"\s+(?:or|and)\s+", chunk, flags=re.I)
                    if piece.strip()
                )
        else:
            parts = [
                piece.strip()
                for piece in re.split(r"\s+(?:or|and)\s+", text, flags=re.I)
                if piece.strip()
            ]
        cleaned = [
            re.sub(r"^(?:or|and)\s+", "", part, flags=re.I).strip(" .")
            for part in parts
        ]
        return [part for part in cleaned if part]

    @classmethod
    def _join_inservice_option_list(
        cls, items: List[str], style: str, conjunction: str
    ) -> str:
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        if style == "semicolon":
            return "; ".join(items)
        if len(items) == 2:
            return f"{items[0]} {conjunction} {items[1]}"
        return f"{', '.join(items[:-1])}, {conjunction} {items[-1]}"

    @classmethod
    def _explicitly_requested_inservice_options(
        cls, collected_data: Optional[Dict[str, Any]]
    ) -> set:
        """In-service options named by the participant.

        Only requested questions count. Assistant prose does not, and the
        subject patterns do not treat a generic rollover word as rollover-source.
        """
        questions = (
            ((collected_data or {}).get("internal_response_context") or {}).get(
                "requested_questions"
            )
        )
        if not isinstance(questions, list):
            return set()
        requested: set = set()
        for question in questions:
            if isinstance(question, str) and question.strip():
                requested |= cls._option_subjects_in_text(question)
        return requested

    @classmethod
    def _try_rewrite_inservice_enumeration(
        cls, text: str, inapplicable: set
    ) -> Optional[str]:
        match = None
        for pattern in cls._INSERVICE_LIST_INTROS:
            found = pattern.search(text)
            if found:
                match = found
                break
        if not match:
            return None
        prefix = match.group("prefix")
        remainder = text[match.end():]
        list_text, tail = cls._split_inservice_list_tail(remainder)
        ended_with_period = list_text.rstrip().endswith(".")
        items = cls._split_inservice_option_list(list_text)
        if len(items) < 2:
            return None
        classified = [
            (item, cls._option_subjects_in_text(item)) for item in items
        ]
        if any(not subjects for _, subjects in classified):
            return None
        kept = [
            item for item, subjects in classified
            if not (subjects and subjects <= inapplicable)
        ]
        if kept == items:
            return None
        if not kept:
            return None
        # Contrastive leftovers ("but a loan is not available") are clauses,
        # not option-list items. Refuse rather than glue them onto a verb prefix.
        if any(
            cls._is_inservice_non_offer_note(item)
            or re.match(
                r"(?:but|however|though|although)\b", item, flags=re.I
            )
            for item in kept
        ):
            return None
        conjunction = "or" if re.search(r"\bor\b", list_text, re.I) else "and"
        style = "semicolon" if (";" in list_text and "," not in list_text) else "comma"
        joined = cls._join_inservice_option_list(kept, style, conjunction)
        rebuilt = f"{prefix} {joined}".strip()
        if tail:
            rebuilt = rebuilt.rstrip(".; ") + tail
        elif ended_with_period and not rebuilt.endswith("."):
            rebuilt += "."
        return rebuilt

    @classmethod
    def _finish_inservice_sentence(cls, text: str) -> str:
        rebuilt = text.strip()
        if rebuilt and rebuilt[0].islower():
            rebuilt = rebuilt[0].upper() + rebuilt[1:]
        if rebuilt and not rebuilt.endswith((".", "!", "?")):
            rebuilt += "."
        return rebuilt

    @classmethod
    def _reduce_unsolicited_inservice_clauses(
        cls, segment: str, inapplicable: set, retain: set
    ) -> Optional[str]:
        """Drop unsolicited inapplicable clauses. None keeps the segment."""
        subjects = cls._option_subjects_in_text(segment)
        targets = subjects & inapplicable
        if not targets:
            return None
        if subjects <= inapplicable and subjects <= retain:
            stripped = cls._strip_inservice_known_zero_disclosure(segment)
            return None if stripped == segment else stripped
        clauses = [
            clause.strip()
            for clause in cls._INSERVICE_CLAUSE_SPLIT.split(segment)
            if clause.strip()
        ]
        # Omit only a minimal option clause. A sibling clause is not option
        # content just because this segment names an inapplicable option.
        if len(clauses) < 2 and (
            subjects <= inapplicable
            and not (subjects & retain)
            and (
                len(subjects) == 1
                or not cls._is_inservice_non_offer_note(segment)
            )
        ):
            return ""
        if len(clauses) < 2:
            return None
        conjunctions = [
            "or" if "or" in match.group(0).casefold() else "and"
            for match in cls._INSERVICE_CLAUSE_SPLIT.finditer(segment)
        ]
        kept_clauses: List[tuple[str, Optional[str]]] = []
        changed = False
        for index, clause in enumerate(clauses):
            leading = conjunctions[index - 1] if index else None
            clause_subjects = cls._option_subjects_in_text(clause)
            if clause_subjects and clause_subjects <= inapplicable:
                if clause_subjects <= retain:
                    kept_clauses.append((clause, leading))
                else:
                    changed = True
                continue
            kept_clauses.append((clause, leading))
        if not changed or len(kept_clauses) == len(clauses):
            return None
        if not kept_clauses:
            return ""
        rendered = [kept_clauses[0][0].rstrip(".").strip()]
        for clause, leading in kept_clauses[1:]:
            rendered.append(f"{leading or 'and'} {clause.rstrip('.').strip()}")
        joined = rendered[0] if len(rendered) == 1 else ", ".join(rendered)
        return cls._finish_inservice_sentence(joined)

    @staticmethod
    def _as_inservice_segment(reduced: str, original: str) -> str:
        """Return a reduced unit in the shape of the segment it replaces.

        The clause reducer finishes its result as a standalone sentence. Inside
        a semicolon list that terminal period and forced capital belong to the
        rejoin, not to the source, and would emit ".;" mid-sentence.
        """
        shaped = reduced.strip()
        if not shaped:
            return shaped
        if shaped.endswith(".") and not original.rstrip().endswith("."):
            shaped = shaped[:-1].rstrip()
        if shaped and original[:1].islower() and shaped[:1].isupper():
            shaped = shaped[:1].lower() + shaped[1:]
        return shaped

    @classmethod
    def _reduce_unsolicited_inservice_sentence(
        cls, sentence: str, inapplicable: set, retain: set
    ) -> Optional[str]:
        """Drop unsolicited inapplicable semicolon units. None keeps the sentence."""
        segments = (
            [part.strip() for part in re.split(r"\s*;\s*", sentence) if part.strip()]
            if ";" in sentence
            else [sentence]
        )
        kept_segments: List[str] = []
        changed = False
        for segment in segments:
            reduced = cls._reduce_unsolicited_inservice_clauses(
                segment, inapplicable, retain
            )
            if reduced is None:
                kept_segments.append(segment)
                continue
            changed = True
            if reduced:
                kept_segments.append(
                    cls._as_inservice_segment(reduced, segment)
                )
        if not changed:
            return None
        if not kept_segments:
            return ""
        return cls._finish_inservice_sentence("; ".join(kept_segments))

    @classmethod
    def _try_rewrite_inservice_clauses(
        cls, text: str, inapplicable: set, retain: Optional[set] = None
    ) -> Optional[str]:
        retain = set(retain or ())
        sentences = cls._split_inservice_sentences(text)
        kept_sentences: List[str] = []
        changed = False
        for sentence in sentences:
            subjects = cls._option_subjects_in_text(sentence)
            targets = subjects & inapplicable
            if not targets:
                kept_sentences.append(sentence)
                continue
            if subjects <= inapplicable and subjects <= retain:
                stripped = cls._strip_inservice_known_zero_disclosure(sentence)
                if stripped != sentence:
                    changed = True
                kept_sentences.append(stripped)
                continue
            reduced = cls._reduce_unsolicited_inservice_sentence(
                sentence, inapplicable, retain
            )
            if reduced is None:
                # This sentence cannot be split safely. Keep scanning later ones.
                kept_sentences.append(sentence)
                continue
            changed = True
            if reduced:
                kept_sentences.append(reduced)
        if not changed:
            return None
        return " ".join(kept_sentences).strip()

    @classmethod
    def _rewrite_inapplicable_inservice_option_text(
        cls,
        text: str,
        inapplicable: set,
        retain: Optional[set] = None,
    ) -> str:
        """Omit unsolicited inapplicable options. Unproven edits are refused.

        A non-offer note is not permission to keep the option in participant
        text. A truthful denial stays only when the inquiry named that option.
        Text is reduced to sentences and clauses before omission. Only a
        minimal option clause with no surviving remainder becomes empty.
        """
        if not isinstance(text, str) or not inapplicable:
            return text
        retain = set(retain or ())
        if "rollover_source" in inapplicable:
            text = cls._strip_inservice_known_zero_disclosure(text)
        subjects = cls._option_subjects_in_text(text)
        targets = subjects & inapplicable
        if not targets:
            return text
        if subjects <= inapplicable and subjects <= retain:
            return cls._strip_inservice_known_zero_disclosure(text)
        rewritten = cls._try_rewrite_inservice_enumeration(text, inapplicable)
        if rewritten is None:
            rewritten = cls._try_rewrite_inservice_clauses(
                text, inapplicable, retain
            )
        if rewritten is None:
            return text
        rewritten = re.sub(r"\s+", " ", rewritten).strip()
        if not rewritten:
            return ""
        if cls._INSERVICE_CORRUPT_PROSE.search(rewritten):
            return text
        if "rollover_source" in inapplicable and cls._inservice_source_zero_clause(rewritten):
            rewritten = cls._strip_inservice_known_zero_disclosure(rewritten)
            if cls._inservice_source_zero_clause(rewritten):
                return text
        original_applicable = subjects - inapplicable
        if original_applicable - cls._option_subjects_in_text(rewritten):
            return text
        return rewritten

    @classmethod
    def _rewrite_inapplicable_inservice_option_item(
        cls, item: Any, inapplicable: set, retain: Optional[set] = None
    ) -> Any:
        retain = set(retain or ())
        if isinstance(item, str):
            return cls._rewrite_inapplicable_inservice_option_text(
                item, inapplicable, retain
            )
        if isinstance(item, dict):
            updated = copy.deepcopy(item)
            saw_option_text = False
            all_dropped = True
            changed = False
            for key, value in list(updated.items()):
                if not isinstance(value, str) or not cls._option_subjects_in_text(value):
                    continue
                saw_option_text = True
                rewritten = cls._rewrite_inapplicable_inservice_option_text(
                    value, inapplicable, retain
                )
                if rewritten == "":
                    updated[key] = rewritten
                    changed = True
                    continue
                all_dropped = False
                if rewritten != value:
                    updated[key] = rewritten
                    changed = True
            if not saw_option_text:
                return item
            if all_dropped and changed:
                for value in updated.values():
                    if isinstance(value, str):
                        continue
                    leaves, truncated = cls._inservice_text_leaves(value)
                    if truncated or any(
                        cls._option_subjects_in_text(leaf) for leaf in leaves
                    ):
                        return updated
                return ""
            return updated if changed else item
        return item

    @staticmethod
    def _dedupe_inservice_key_points(points: List[Any]) -> List[Any]:
        seen = set()
        deduped: List[Any] = []
        for item in points:
            if isinstance(item, str):
                key = item.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            elif isinstance(item, dict):
                try:
                    key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    key = str(item)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            else:
                deduped.append(item)
        return deduped

    @classmethod
    def _sync_coverage_after_inservice_option_rewrite(
        cls,
        fixed: Dict[str, Any],
        original_to_updated: Dict[str, str],
        inapplicable: set,
        retain: Optional[set] = None,
    ) -> None:
        retain = set(retain or ())
        coverage = fixed.get("question_coverage")
        if not isinstance(coverage, list):
            return
        for item in coverage:
            if not isinstance(item, dict):
                continue
            reference = item.get("answer_reference")
            if not isinstance(reference, str) or not reference.strip():
                continue
            if reference in original_to_updated:
                updated = original_to_updated[reference]
            else:
                updated = cls._rewrite_inapplicable_inservice_option_text(
                    reference, inapplicable, retain
                )
            if updated == "":
                item["status"] = "needs_verification"
                continue
            if updated == reference:
                continue
            enumeration = cls._try_rewrite_inservice_enumeration(
                reference, inapplicable
            )
            if enumeration is None:
                item["status"] = "needs_verification"
                continue
            item["answer_reference"] = updated

    @classmethod
    def _apply_termination_response_policy(
        cls,
        parsed: Dict[str, Any],
        retrieval_profile: Optional[Dict[str, Any]],
        collected_data: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Enforce the reviewed outgoing-distribution boundaries after the LLM.

        This is deliberately narrow and does not run for incoming rollovers.
        It removes obsolete/tangential content, keeps pure rollovers free of
        cash-only tax/delivery guidance, and converts an unqualified
        "brokerage account" into one genuine blocking clarification.
        """
        profile = retrieval_profile or {}
        action = profile.get("primary_action")
        signals = profile.get("signals") or {}
        info: Dict[str, Any] = {
            "applied": False,
            "primary_action": action,
            "removed_items": 0,
            "brokerage_clarification": False,
            "rollover_execution_normalized": False,
            "separation_conflict_trimmed": False,
        }
        if action == "incoming_rollover" or signals.get("incoming_rollover") is True:
            return parsed, info
        if action == "plan_identifier":
            fixed = copy.deepcopy(parsed)
            from data_pipeline.gr_payload_builder import project_verified_plan_facts

            disclosure = project_verified_plan_facts(
                (collected_data or {}).get("internal_plan_disclosure_context")
            )
            entry = (disclosure.get("facts") or {}).get("rk_plan_id") or {}
            identifier = entry.get("value")
            as_of = entry.get("as_of") or entry.get("observed_at")
            known = bool(identifier and as_of)
            fixed["response_to_participant"] = {
                "opening": (
                    f"The recordkeeper plan ID for your plan is {identifier}, from the plan record as of {as_of}."
                    if known
                    else "Our team needs to verify the recordkeeper plan ID for your plan before providing it for the form."
                ),
                "key_points": [], "steps": [], "warnings": [],
            }
            fixed["outcome"] = "can_proceed" if known else "blocked_missing_data"
            fixed["outcome_reason"] = "The requested identifier is available in the recordkeeper plan field; no distribution eligibility determination is made." if known else "The recordkeeper plan ID is missing; a portal route ID is not a substitute."
            fixed["questions_to_ask"] = []
            fixed["data_gaps"] = [] if known else ["Verified recordkeeper plan ID"]
            fixed["escalation"] = {"needed": not known, "reason": None if known else "Verify the recordkeeper identifier against the correct plan."}
            questions = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions")
            if isinstance(questions, list) and len(questions) == 1:
                # This policy answers exactly one factual request from the
                # authoritative field. Its old model wording is no longer the
                # coverage reference; unresolved identifiers still need review.
                fixed["question_coverage"] = [{
                    "question_index": 0,
                    "status": "answered" if known else "needs_verification",
                    "answer_reference": fixed["response_to_participant"]["opening"],
                }]
            info.update(applied=True, identifier_answer_only=True)
            return fixed, info
        if action == "participant_account_identifier":
            # The participant asked for their own account number. No verified
            # source for that identifier exists in the collected record: the
            # recordkeeper plan code identifies the plan and the receiving
            # account belongs to the destination provider, so neither can
            # stand in for it and none of them may be invented here.
            fixed = copy.deepcopy(parsed)
            points = [cls._ACCOUNT_IDENTIFIER_DISTINCTION]
            plan_point = cls._verified_plan_identity_point(collected_data)
            if plan_point:
                points.append(plan_point)
            if signals.get("transaction_motive_only") is True:
                # The participant explained a rollover motive. It is not being
                # answered here and it is not being dropped either: the case
                # goes to review below with the motive named.
                points.append(cls._ACCOUNT_IDENTIFIER_MOTIVE_NOTE)
            fixed["response_to_participant"] = {
                "opening": cls._ACCOUNT_IDENTIFIER_PENDING,
                "key_points": points,
                "steps": [], "warnings": [],
            }
            fixed["outcome"] = "blocked_missing_data"
            fixed["outcome_reason"] = (
                "The participant's own account number is not part of the verified "
                "record; the recordkeeper plan code and a receiving account number "
                "are not substitutes."
            )
            fixed["questions_to_ask"] = []
            fixed["data_gaps"] = ["Verified participant account number"]
            fixed["escalation"] = {
                "needed": True,
                "reason": "Verify the participant's own account number on the correct plan and participant record before providing it.",
            }
            questions = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions")
            if isinstance(questions, list) and questions:
                fixed["question_coverage"] = [
                    {"question_index": index, "status": "needs_verification",
                     "answer_reference": cls._ACCOUNT_IDENTIFIER_PENDING}
                    for index in range(min(len(questions), 12))
                ]
            info.update(applied=True, identifier_answer_only=True,
                        participant_account_identifier=True)
            return fixed, info
        if action == "hardship_withdrawal":
            participant = (collected_data or {}).get("participant_data") or {}
            confirmed = participant.get("confirmation_of_review_of_hardship_distribution_guidelines_pdf") is True
            if confirmed or parsed.get("outcome") == "blocked_not_eligible":
                return parsed, info
            fixed = copy.deepcopy(parsed)
            response = fixed.get("response_to_participant") or {}
            response["opening"] = "Before we provide the hardship request form, our team needs to confirm that you have reviewed the Hardship Distribution Guidelines."
            response["steps"] = [{
                "step_number": 1,
                "action": "Review the Hardship Distribution Guidelines with our team.",
                "detail": "Our team needs to provide the Guidelines and confirm your review before sharing the hardship request form.",
            }]
            # A link or premature submission instruction may also occur outside
            # steps. Retain supported explanations, fees and tax caveats only.
            for section in ("key_points", "warnings"):
                response[section] = [item for item in response.get(section, []) if not re.search(
                    r"rightsignature|\b(?:submit|complete|sign|upload)\b|\bform\b|\bportal\b",
                    cls._response_item_text(item), re.I,
                )]
            fixed["response_to_participant"] = response
            fixed["outcome"] = "blocked_missing_data"
            fixed["outcome_reason"] = "Procedural review confirmation is missing; this is not a denial of hardship eligibility."
            fixed["questions_to_ask"] = []
            fixed["data_gaps"] = list(fixed.get("data_gaps") or []) + ["Confirmed review of the Hardship Distribution Guidelines"]
            fixed["escalation"] = {"needed": True, "reason": "Provide the approved Guidelines, obtain review confirmation, then share the hardship request form."}
            info.update(applied=True, guidelines_confirmation_required=True)
            return fixed, info
        if action in {"loan_request", "loan"} and parsed.get("outcome") == "blocked_not_eligible":
            fixed = copy.deepcopy(parsed)
            response = fixed.get("response_to_participant")
            if isinstance(response, dict):
                response["steps"] = []
            info.update({"applied": True, "ineligible_loan_steps_removed": True})
            return fixed, info
        if action not in {
            "termination_rollover", "termination_distribution", "rollover",
            "distribution", "custody_investigation", "retention_options",
        }:
            return parsed, info
        if (
            action in {"rollover", "distribution"}
            and signals.get("termination_distribution") is not True
        ):
            if profile.get("inquiry_intent") == "informational_options" and signals.get("procedure_requested") is False:
                fixed = copy.deepcopy(parsed)
                response = fixed.get("response_to_participant")
                if isinstance(response, dict):
                    response["steps"] = []
                    if signals.get("delivery_or_fee_request") is not True:
                        points = [item for item in response.get("key_points") or [] if not re.search(
                            r"\b(?:fees?|charges?|costs?)\b", cls._response_item_text(item),
                        )]
                        applicability = cls._inservice_option_applicability(collected_data)
                        inapplicable = {
                            name for name, state in applicability.items()
                            if state == "inapplicable"
                        }
                        if inapplicable:
                            retain = cls._explicitly_requested_inservice_options(
                                collected_data
                            )
                            opening = response.get("opening")
                            if isinstance(opening, str):
                                response["opening"] = (
                                    cls._rewrite_inapplicable_inservice_option_text(
                                        opening, inapplicable, retain
                                    )
                                )
                            raw_warnings = response.get("warnings")
                            if isinstance(raw_warnings, list):
                                rewritten_warnings: List[Any] = []
                                for item in raw_warnings:
                                    updated = cls._rewrite_inapplicable_inservice_option_item(
                                        item, inapplicable, retain
                                    )
                                    if updated == "":
                                        continue
                                    rewritten_warnings.append(updated)
                                response["warnings"] = rewritten_warnings
                            rewritten_points: List[Any] = []
                            original_to_updated: Dict[str, str] = {}
                            for item in points:
                                updated = cls._rewrite_inapplicable_inservice_option_item(
                                    item, inapplicable, retain
                                )
                                if isinstance(item, str):
                                    original_to_updated[item] = (
                                        updated if isinstance(updated, str) else item
                                    )
                                if updated == "":
                                    if isinstance(item, str):
                                        original_to_updated[item] = ""
                                    continue
                                rewritten_points.append(updated)
                            points = rewritten_points
                            containers: List[Any] = []
                            if isinstance(response.get("opening"), str):
                                containers.append(response["opening"])
                            containers.extend(points)
                            if isinstance(response.get("warnings"), list):
                                containers.extend(response["warnings"])
                            offered: set = set()
                            present: set = set()
                            for item in containers:
                                offered |= cls._offered_inapplicable_subjects_in_item(
                                    item, inapplicable
                                )
                                present |= cls._inservice_present_inapplicable_subjects(
                                    item, inapplicable
                                )
                                if "rollover_source" in inapplicable:
                                    leaves, truncated = cls._inservice_text_leaves(item)
                                    if truncated:
                                        offered.add("rollover_source")
                                        continue
                                    for leaf in leaves:
                                        if cls._inservice_source_zero_clause(leaf):
                                            offered.add("rollover_source")
                                            break
                            unique_removed = sorted(
                                name for name in inapplicable if name not in present
                            )
                            unresolved = sorted(offered)
                            cls._sync_coverage_after_inservice_option_rewrite(
                                fixed, original_to_updated, inapplicable, retain
                            )
                            if unresolved:
                                info["inapplicable_options_unresolved"] = unresolved
                                fixed["inapplicable_options_unresolved"] = unresolved
                            if unique_removed:
                                info["inapplicable_options_removed"] = unique_removed
                                guardrails = list(fixed.get("guardrails_applied") or [])
                                for name in unique_removed:
                                    guardrails.append(
                                        "Did not present inapplicable "
                                        f"{name.replace('_', ' ')} as an available "
                                        "in-service option."
                                    )
                                fixed["guardrails_applied"] = cls._dedupe_preserving_order(
                                    guardrails
                                )
                        points.append("Tell us which option you would like to explore, and our team will verify whether your plan permits it and what requirements apply.")
                        response["key_points"] = cls._dedupe_inservice_key_points(points)
                info.update(applied=True, informational_scope_preserved=True)
                return fixed, info
            return parsed, info

        fixed = copy.deepcopy(parsed)
        info["applied"] = True

        preflight = (collected_data or {}).get("internal_preflight_context") or {}
        sources = preflight.get("sources") or {}
        account = sources.get("account_balance") or {}
        zero_balance = account.get("status") == "known" and account.get("value") == 0

        lifecycle = (collected_data or {}).get("internal_plan_context") or {}
        facts = lifecycle.get("operational_facts") or []
        dated_facts = [fact for fact in facts if isinstance(fact, dict)] if isinstance(facts, list) else []
        dated_facts.sort(key=lambda f: (str(f.get("effective_on") or f.get("recorded_at") or ""), str(f.get("recorded_at") or "")))
        latest = {fact.get("kind"): fact for fact in dated_facts if isinstance(fact.get("kind"), str)}
        hold = latest.get("distribution_hold", {}).get("value") is True
        transition_fact = latest.get("custody_transition_status", {})
        successor_fact = latest.get("successor_recordkeeper", {})
        reported_successor = latest.get("servicing_successor_reported", {})
        transition = transition_fact.get("value")
        # A successor and a completed transfer must describe the same event.
        # Never join independent "latest" facts from different transitions.
        transition_ref = transition_fact.get("record_ref")
        successor_ref = successor_fact.get("record_ref")
        if transition_ref or successor_ref:
            same_transition = (
                isinstance(transition_ref, str) and bool(transition_ref)
                and transition_ref == successor_ref
            )
        else:
            # Compatibility with dated snapshots predating record_ref. The
            # absence of provenance is not enough to establish correlation.
            same_transition = bool(transition_fact.get("source")) and bool(transition_fact.get("recorded_at")) and all(
                transition_fact.get(key) == successor_fact.get(key)
                for key in ("source", "recorded_at", "effective_on")
            )
        successor = successor_fact.get("value") if same_transition else None
        current = lifecycle.get("current") or {}
        inactive_plan = current.get("active") is False or current.get("status") in {"terminated", "deconverted", "closed"}
        if hold or inactive_plan or transition in {"completed", "pending"}:
            fixed["outcome"] = "blocked_not_eligible" if hold else "blocked_missing_data"
            if hold:
                opening = "The plan has a recorded hold on distributions. Our team needs to confirm that the hold has been released before a distribution can proceed."
            elif (
                reported_successor.get("value") in ("Fidelity", "ADP")
                and reported_successor.get("source") == "plan_history.notes"
                and str(reported_successor.get("recorded_at") or "") >= str(transition_fact.get("recorded_at") or "")
            ):
                opening = f"Plan records name {reported_successor['value']} as the successor recordkeeper. Our team needs to verify your account's current servicing details before providing transfer instructions."
            elif transition == "completed" and successor in ("Fidelity", "ADP"):
                opening = f"Plan records show a completed transition to {successor}. Our team needs to verify your account's current servicing details before providing transfer instructions."
            else:
                opening = "The plan's status requires verification of current servicing before we can provide transfer instructions."
            if zero_balance:
                opening = "Your account shows a zero balance. " + opening
            fixed["outcome_reason"] = "Plan-side lifecycle evidence requires servicing review before participant execution."
            fixed["response_to_participant"] = {"opening": opening, "key_points": [], "steps": [], "warnings": []}
            fixed["questions_to_ask"] = []
            fixed["data_gaps"] = ["Verified current plan servicing and participant disposition"]
            fixed["escalation"] = {"needed": True, "reason": "Internal plan servicing review; verify lifecycle dates, any distribution hold and the individual account record."}
            info["plan_review_required"] = True
            return fixed, info

        if zero_balance:
            # TKT-912043: the zero total is one asynchronously updated source
            # row. When a recorded loan, a positive sibling source row or
            # recorded crypto enrollment contradicts it, the account is not
            # proven empty and its obligations must survive the custody guard.
            # The guard itself still returns here: no draft steps or questions
            # may survive a blocked custody answer.
            contested = cls._zero_total_is_contested(preflight)
            fixed["outcome"] = "blocked_missing_data"
            if contested:
                fixed["outcome_reason"] = "The reported account total is zero but other recorded obligations contradict an empty account; the disposition and current custodian require internal verification."
                opening = (
                    "Your account total currently shows zero, but our records also show obligations "
                    "that a zero total would not explain. Our team needs to verify where your funds "
                    "are held, and settle those items, before giving you transfer instructions."
                )
            else:
                fixed["outcome_reason"] = "The account balance is confirmed zero; the disposition and current custodian require internal verification."
                opening = "Your account shows a zero balance. Our team needs to verify where the funds are held before giving you transfer instructions."
            # Source rows are never restated as current holdings; only the
            # obligation itself is named, with the same wording the submission
            # preflight uses.
            crypto_context = preflight.get("crypto")
            obligations = cls._preflight_obligation_warnings(
                cls._preflight_loan_state(preflight),
                (crypto_context if isinstance(crypto_context, dict) else {}).get("holdings") or {},
            ) if contested else []
            if contested and not obligations:
                # Contested solely by a sibling source row. Name the conflict
                # itself rather than leaving the opening unsupported; the
                # amounts stay internal.
                obligations = ["Recorded source balances do not agree with a zero account total. Our team needs to reconcile them before your request can be submitted."]
            fixed["response_to_participant"] = {
                "opening": opening,
                "key_points": [], "steps": [], "warnings": obligations,
            }
            fixed["questions_to_ask"] = []
            fixed["data_gaps"] = ["Verified disposition and current custodian of the participant's funds"]
            fixed["escalation"] = {
                "needed": True,
                "reason": "Internal custody review: verify the participant's disposition record and current custodian; a zero balance does not prove a force-out or completed transfer.",
            }
            if contested:
                fixed["escalation"]["reason"] += " The zero total conflicts with recorded loan, source-balance or crypto-enrollment evidence; reconcile them before responding."
                info["zero_total_conflict"] = True
            info["custody_review_required"] = True
            return fixed, info

        if action == "custody_investigation":
            # A custody question alone never starts a withdrawal procedure.
            return parsed, info
        if action == "retention_options":
            response = fixed.get("response_to_participant")
            if isinstance(response, dict):
                response["steps"] = []
                questions = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions") or []
                partial_indices = [index for index, question in enumerate(questions[:12]) if isinstance(question, str)
                                   and re.search(r"\bpartial\b|\bonly (?:a )?part\b|\ba portion\b", question, re.I)]
                if partial_indices:
                    # The current payload has no verified rule for retaining
                    # the remainder. Generic partial cash/distribution support
                    # does not establish that separate permission.
                    points = list(response.get("key_points") or [])
                    for index in partial_indices:
                        answer = f"{index + 1}. Our team needs to verify whether the plan permits a partial rollover while you leave the rest invested, including any balance requirements and applicable fees. A general partial-distribution option does not confirm this."
                        position = next((pos for pos, point in enumerate(points) if isinstance(point, str)
                                         and re.match(rf"^\s*{index + 1}[.)]\s", point)), None)
                        if position is None:
                            points.append(answer)
                        else:
                            points[position] = answer
                    response["key_points"] = points
                    response["opening"] = "We can review your options before you decide whether to move any funds."
                    fixed["outcome"] = "blocked_missing_data"
                    fixed["outcome_reason"] = "The informational question about a partial rollover with a retained balance needs a verified plan rule; this does not authorize a transaction."
                    fixed["data_gaps"] = list(fixed.get("data_gaps") or []) + ["Plan permission, balance requirements and fees for a partial rollover retaining the remainder"]
                    fixed["escalation"] = {"needed": True, "reason": "Verify the plan-specific partial rollover and retained-balance rules before confirming this option."}
                    for item in fixed.get("question_coverage") or []:
                        if isinstance(item, dict) and item.get("question_index") in partial_indices:
                            item["status"] = "needs_verification"
                    info["partial_retention_verification_required"] = True
                points = list(response.get("key_points") or [])
                if partial_indices and not any("which option" in cls._response_item_text(point).lower() for point in points):
                    points.append("Which option would you like to explore: keeping your funds here, moving all of them, or a partial rollover if your plan permits it?")
                response["key_points"] = points
            return fixed, info

        if signals.get("brokerage_destination_ambiguous"):
            fixed["outcome"] = "blocked_missing_data"
            fixed["outcome_reason"] = (
                "The destination was described only as a brokerage account. "
                "The correct transaction and tax treatment depend on whether "
                "that account is retirement-qualified or taxable."
            )
            fixed["response_to_participant"] = {
                "opening": (
                    "Before I give you the rollover steps, I need to confirm "
                    "what type of brokerage account will receive the funds."
                ),
                "key_points": [
                    "A direct rollover goes to an eligible retirement account; "
                    "a taxable brokerage account follows a different cash-distribution path."
                ],
                "steps": [],
                "warnings": [],
            }
            fixed["questions_to_ask"] = [{
                "question": (
                    "Is the brokerage destination a retirement account "
                    "(such as an IRA or qualified employer plan) or a taxable "
                    "brokerage account?"
                ),
                "why": (
                    "This determines whether the request is a direct rollover "
                    "or a taxable cash distribution."
                ),
            }]
            fixed["escalation"] = {"needed": False, "reason": None}
            guardrails = list(fixed.get("guardrails_applied") or [])
            guardrails.append(
                "Did not assume that an unspecified brokerage account was an IRA."
            )
            fixed["guardrails_applied"] = cls._dedupe_preserving_order(guardrails)
            info["brokerage_clarification"] = True
            return fixed, info

        # Informational/factual inquiries keep their ordered answers. The
        # existence of a rollover keyword does not request a submission flow.
        if (
            profile.get("inquiry_intent") == "informational_options"
            and signals.get("procedure_requested") is False
        ):
            response = fixed.get("response_to_participant")
            if isinstance(response, dict):
                response["steps"] = []
            info["informational_scope_preserved"] = True
            return fixed, info

        if (
            fixed.get("outcome") == "blocked_missing_data"
            and action == "termination_rollover"
            and signals.get("pure_rollover") is True
            and signals.get("employment_state") == "terminated"
            and signals.get("termination_date_present") is True
            and signals.get("guidance_request") is True
            and signals.get("rollover_provider_named") is True
        ):
            mentions: List[Any] = []
            for field in ("questions_to_ask", "data_gaps"):
                value = fixed.get(field)
                if isinstance(value, list):
                    mentions.extend(value)

            def is_destination_subtype(item: Any) -> bool:
                text = cls._response_item_text(item)
                normalized_text = re.sub(r"[^a-z0-9]+", " ", text).strip()
                if any(marker in text for marker in (
                    "termination date", "employment status", "outstanding loan",
                    "loan status", "vested balance", "account balance", "blackout",
                    "rehire", "plan status", "separation date", "last day",
                    "eligibility status", "spousal consent", "marital status",
                    "participant status", "participant name", "beneficiary", "address",
                    "identity verification", "when did you leave", "date you left",
                )):
                    return False
                direct_markers = (
                    "rollover destination type",
                    "destination account type",
                    "receiving account type",
                )
                tokens = set(normalized_text.split())
                typed_account = (
                    "account" in tokens
                    and bool(tokens.intersection({"type", "kind", "category"}))
                    and any(marker in text for marker in (
                        "ira", "qualified plan", "qualified retirement plan", "roth",
                        "retirement account", "taxable brokerage",
                        "taxable managed account",
                    ))
                )
                return any(marker in text for marker in direct_markers) or typed_account

            if mentions and all(is_destination_subtype(item) for item in mentions):
                fixed["outcome"] = "can_proceed"
                fixed["outcome_reason"] = (
                    "Termination status and date are supported. The receiving "
                    "account subtype is an execution detail that the participant "
                    "can confirm with the receiving provider before submitting."
                )
                response = fixed.get("response_to_participant")
                if not isinstance(response, dict):
                    response = {}
                response["opening"] = (
                    "You can start the direct-rollover process. Before submitting, "
                    "confirm with the receiving provider that the destination is "
                    "an eligible retirement account and accepts each source."
                )
                response.setdefault("key_points", [])
                response.setdefault("steps", [])
                response.setdefault("warnings", [])
                fixed["response_to_participant"] = response
                fixed["questions_to_ask"] = []
                fixed["data_gaps"] = []
                fixed["escalation"] = {"needed": False, "reason": None}
                guardrails = list(fixed.get("guardrails_applied") or [])
                guardrails.append(
                    "Treated receiving-account subtype as a rollover execution "
                    "detail, without assuming the account is retirement-qualified."
                )
                fixed["guardrails_applied"] = cls._dedupe_preserving_order(guardrails)
                info["rollover_execution_normalized"] = True

        response = fixed.get("response_to_participant")
        if not isinstance(response, dict):
            return fixed, info

        obsolete_markers = (
            "fee-out", "$75 or less", "balance threshold", "small balance",
            "known issue", "multiple 401(k)", "more than one 401(k)",
        )
        pure_rollover_markers = (
            "ach", "20%", "withholding", "withheld", "cash distribution",
            "cash withdrawal", "cash payment", "cash payout", "take cash",
            "taxable distribution", "taxable withdrawal", "federal tax",
            "irs penalty", "10 percent", "early withdrawal penalty",
            "early-distribution tax", "additional 10%", "10% tax",
            "10% early distribution", "early distribution penalty",
            "partial cash", "cash portion",
        )
        remove_markers = list(obsolete_markers)
        if signals.get("pure_rollover"):
            remove_markers.extend(pure_rollover_markers)
            if signals.get("selected_delivery") == "check":
                remove_markers.extend(("wire", "$35"))
        if not signals.get("overnight_request"):
            remove_markers.extend(("overnight check", "overnight delivery"))
        if signals.get("unknown_email_access"):
            remove_markers.extend((
                "forgot your password", "password reset", "reset link",
                "reset email",
            ))
        separation_conflict = (
            fixed.get("outcome") == "blocked_missing_data"
            and action in {"termination_distribution", "termination_rollover"}
            and (
                signals.get("separation_conflicts_active") is True
                or signals.get("employment_state") == "active"
            )
        )
        if separation_conflict:
            remove_markers.extend((
                "rightsignature", cls._TERMINATION_FORM_URL.lower(),
                "cash-out form", "cash out form",
            ))

        def contains_removed_marker(value: Any) -> bool:
            text = cls._response_item_text(value)
            # Loan offsets are a separate tax component. Keep a grounded
            # loan-specific warning without importing cash withholding into
            # the direct rollover. Mixed cash/offset paragraphs still fail
            # this exception and must be composed as separate components.
            loan_offset = "loan offset" in text or "loan is offset" in text
            cash_withholding = "20%" in text or "withholding" in text or "withheld" in text
            offset_tax_component = loan_offset and not cash_withholding
            offset_tax_markers = {
                "taxable distribution", "taxable withdrawal", "federal tax",
                "irs penalty", "10 percent", "early withdrawal penalty",
                "early-distribution tax", "additional 10%", "10% tax",
                "10% early distribution", "early distribution penalty",
            }
            applicable_markers = [
                marker for marker in remove_markers
                if not (offset_tax_component and marker in offset_tax_markers)
            ]
            marker_match = any(
                re.search(r"\bach\b", text) is not None
                if marker == "ach"
                else marker in text
                for marker in applicable_markers
            )
            pure_rollover_pattern_match = (
                signals.get("pure_rollover") is True
                and any(re.search(pattern, text) is not None for pattern in (
                    r"\btake\s+(?:the\s+|a\s+|your\s+)?cash\b",
                    r"\btax(?:es)?\b.{0,40}\bwithheld\b",
                    r"\bwithheld\b.{0,40}\btax(?:es)?\b",
                    r"\btaxable\b.{0,30}\bcash\b",
                    r"\bcash\b.{0,30}\btaxable\b",
                ))
            )
            percentage_tax_match = (
                signals.get("pure_rollover") is True and not offset_tax_component
                and re.search(r"\b(?:10|ten)\s*(?:%|percent)\b", text) is not None
            )
            return marker_match or pure_rollover_pattern_match or percentage_tax_match

        opening = response.get("opening")
        if contains_removed_marker(opening):
            response["opening"] = (
                "You can proceed with the direct-rollover process after "
                "confirming the receiving provider's requirements."
                if signals.get("pure_rollover") and fixed.get("outcome") == "can_proceed"
                else "Here is the applicable outgoing-distribution guidance."
            )
            info["removed_items"] += 1
        if contains_removed_marker(fixed.get("outcome_reason")):
            fixed["outcome_reason"] = (
                "The participant can proceed with the direct-rollover process, "
                "subject to the receiving provider's requirements."
                if signals.get("pure_rollover") and fixed.get("outcome") == "can_proceed"
                else "The response follows the applicable outgoing-distribution rules."
            )
            info["removed_items"] += 1

        if separation_conflict:
            original_steps = response.get("steps")
            if isinstance(original_steps, list) and original_steps:
                info["removed_items"] += len(original_steps)
            response["steps"] = []
            info["separation_conflict_trimmed"] = True

        def filter_items(value: Any) -> List[Any]:
            if not isinstance(value, list):
                return []
            kept: List[Any] = []
            for item in value:
                if signals.get("pure_rollover") and isinstance(item, str):
                    # A negative ACH clause is not an ACH instruction. Remove
                    # only the explicit exclusion, then apply the normal
                    # filter to any remaining positive or ambiguous mention.
                    item = re.sub(
                        r"[;.]?\s*\bACH is not (?:used|available|supported)"
                        r" (?:for direct rollovers|for a direct rollover)\.?",
                        "", item, flags=re.IGNORECASE,
                    )
                    item = re.sub(r",\s*not ACH\s*,", "", item, flags=re.IGNORECASE)
                if contains_removed_marker(item):
                    info["removed_items"] += 1
                    continue
                kept.append(item)
            return kept

        key_points = filter_items(response.get("key_points"))
        warnings = filter_items(response.get("warnings"))
        steps = filter_items(response.get("steps"))

        # A blocked outcome is a gate on execution, even when employment and
        # separation are already known. Model-authored steps used to survive
        # here unless the block was specifically an employment conflict.
        if fixed.get("outcome") in {
            "blocked_missing_data", "blocked_not_eligible", "ambiguous_plan_rules",
        }:
            steps = []
            if fixed["outcome"] == "blocked_not_eligible":
                pending = "The recorded eligibility conditions prevent this distribution from proceeding. Submission instructions are not available while that block applies."
            else:
                pending = "Our team needs to verify the unresolved eligibility requirements before we can confirm this distribution or provide submission instructions."
            if re.search(
                r"\byou\s+(?:are eligible|can (?:start|proceed|submit|request|initiate|roll|withdraw|take))\b"
                r"|\b(?:process|distribution|rollover)\b[^.!?]{0,80}\b(?:is|are) available\b",
                cls._response_item_text(response.get("opening")),
            ):
                response["opening"] = pending

            def is_submission_guidance(item: Any) -> bool:
                text = cls._response_item_text(item)
                return any(marker in text for marker in (
                    cls._TERMINATION_FORM_URL.lower(), "secure.rightsignature.com",
                    "loans & distributions", "separation of service",
                )) or re.search(
                    r"\b(?:submit|complete|start|initiate|sign|log in|enter|select|choose|open|review)\b"
                    r"[^.!?\n]{0,100}\b(?:request|portal|distribution|rollover|vesting|vested|source information)\b",
                    text,
                ) is not None

            held_points: List[Any] = []
            for item in key_points:
                if is_submission_guidance(item):
                    # Retain the question's place instead of deleting its
                    # process answer or breaking the multi-question inventory.
                    prefix = re.match(r"^\d{1,2}[.)]\s+[^:]{1,70}:\s*", item) if isinstance(item, str) else None
                    item = (prefix.group() if prefix else "") + pending
                    info["removed_items"] += 1
                held_points.append(item)
            key_points = cls._dedupe_preserving_order(held_points)
            warnings = [item for item in warnings if not is_submission_guidance(item)]
            info["submission_guidance_withheld"] = True

        required_points: List[tuple[tuple[str, ...], str]] = []
        if signals.get("pure_rollover") and signals.get("selected_delivery") == "check":
            required_points.extend([
                (("receiving provider",), "For your direct rollover by check, confirm that the receiving provider accepts each source and obtain its check payee, mailing and account instructions."),
                (("distribution fee", "processing fee", "request fee"), "The distribution request fee is $75. Standard check delivery has no additional delivery fee."),
            ])
            if fixed.get("outcome") == "can_proceed":
                required_points.append((("1099-r",), "ForUsAll reviews the request within about two business days; after approval, standard checks typically arrive in 2-3 weeks. Form 1099-R is issued by January 31 of the following year."))
        elif signals.get("pure_rollover"):
            required_points.extend([
                (("receiving provider",), (
                    "Direct rollovers can be delivered by check or wire. "
                    "Before submitting, confirm that the receiving provider "
                    "accepts the tax character of each source and whether it "
                    "requires check or wire delivery."
                )),
                (("$35", "each separate wire"), (
                    "The distribution request fee is $75. A $35 non-refundable "
                    "fee applies to each separate wire transaction; pre-tax and "
                    "Roth sources may require separate wires."
                )),
            ])
            if fixed.get("outcome") == "can_proceed":
                required_points.extend([
                    (("subaccount number", "aba routing"), (
                        "For a wire, use the receiving provider's wire-specific "
                        "bank account name, ABA routing number, bank account "
                        "number, and IRA or Roth IRA subaccount number."
                    )),
                    (("1099-r",), (
                        "ForUsAll reviews the request within about two business "
                        "days; after approval, wires typically complete in 1-2 "
                        "weeks and standard checks in 2-3 weeks. Form 1099-R is "
                        "issued by January 31 of the following year."
                    )),
                ])
        elif fixed.get("outcome") == "can_proceed" and (
            action in {"termination_distribution", "distribution"}
            or signals.get("cash_component")
        ):
            age_59_5_status = signals.get("is_age_59_5_or_older")
            if age_59_5_status is False:
                cash_tax_point = (
                    "If you choose a taxable cash distribution, 20% federal "
                    "income tax withholding generally applies; because you are "
                    "under age 59½, an additional 10% early-distribution tax "
                    "may apply."
                )
            elif age_59_5_status is True:
                cash_tax_point = (
                    "If you choose a taxable cash distribution, 20% federal "
                    "income tax withholding generally applies; the additional "
                    "10% early-distribution tax generally does not apply because "
                    "you are at least age 59½."
                )
            else:
                cash_tax_point = (
                    "If you choose a taxable cash distribution, 20% federal "
                    "income tax withholding generally applies; an additional "
                    "10% early-distribution tax may apply if you are under age "
                    "59½."
                )
            required_points.append((
                (
                    "20%", "10%", "early withdrawal", "early-distribution",
                ),
                cash_tax_point,
            ))

        preflight_applicable = fixed.get("outcome") == "can_proceed" or (
            fixed.get("outcome") == "blocked_missing_data"
            and action in {"termination_distribution", "termination_rollover"}
            and signals.get("employment_state") == "terminated"
            and signals.get("termination_date_present") is True
            and not separation_conflict
        )
        if preflight_applicable:
            if preflight:
                info["structured_preflight_applied"] = True
                loan_state = cls._preflight_loan_state(preflight)
                crypto = preflight.get("crypto") or {}
                holdings = crypto.get("holdings") or {}
                crypto_zero = holdings.get("status") == "known" and holdings.get("value") == 0
                # Remove unsupported model conditionals when absence was
                # actually established, not merely when extraction was empty.
                def contradicted_preflight(item: Any) -> bool:
                    text = cls._response_item_text(item)
                    return (
                        loan_state == "zero" and any(marker in text for marker in (
                            "outstanding loan", "loan offset", "loan payoff", "loan balance",
                        ))
                    ) or (crypto_zero and "crypto" in text)
                key_points = [i for i in key_points if not contradicted_preflight(i)]
                warnings = [i for i in warnings if not contradicted_preflight(i)]
                preflight_warnings = cls._preflight_obligation_warnings(loan_state, holdings)
                warnings = cls._dedupe_preserving_order(preflight_warnings + warnings)
                required_points.append((("final payroll",), "Before submitting, wait at least 7 business days after final payroll."))
            elif signals.get("pure_rollover") and fixed.get("outcome") == "can_proceed":
                # TKT-911756: enrollment is not a holdings record. Without a
                # structured preflight nothing about crypto is established, so
                # the fallback asks Support to verify holdings instead of
                # conditioning the participant's next step on enrollment.
                required_points.append((("outstanding loan", "final payroll", "crypto"), (
                    "Before submitting, contact Support about any outstanding loan; wait at least 7 business days after final payroll; "
                    "and ask Support to verify whether you hold crypto positions and the applicable transfer requirements, because "
                    "the transfer or liquidation treatment is not defined."
                )))

        if signals.get("overnight_request"):
            overnight = (
                "OverNight Check delivery cannot be selected in the participant "
                "portal. It is available only on the secure RightSignature form "
                "and adds a $50 non-refundable fee."
            )
            required_points.append((("$50", "overnight check"), overnight))

        if signals.get("account_access") and fixed.get("outcome") != "can_proceed":
            support = (
                "If account access cannot be recovered, call ForUsAll Participant "
                "Support at 844-401-2253, Monday-Friday, 7:00 AM-5:00 PM PT."
            )
            if "844-401-2253" not in " ".join(
                cls._response_item_text(i) for i in key_points
            ):
                key_points.append(support)

        requested_questions = ((collected_data or {}).get("internal_response_context") or {}).get("requested_questions")
        preserve_question_order = isinstance(requested_questions, list) and bool(requested_questions)
        key_point_limit = 6
        # Reviewed facts are deterministic and must survive the response cap.
        # Replace any model-authored partial rendition with the complete
        # canonical point, then use remaining capacity for model-specific
        # context.  Incoming rollovers returned before this policy.
        if required_points and preserve_question_order:
            # Keep each answer in its original position. Replacing a fee or
            # delivery clause must not promote it ahead of an unrelated source
            # answer. Add missing reviewed facts after the requested answers.
            key_points = key_points[:12]
            original_key_points = response.get("key_points")
            if not isinstance(original_key_points, list):
                original_key_points = []
            for markers, point in required_points:
                positions = [i for i, item in enumerate(key_points) if any(
                    marker in cls._response_item_text(item) for marker in markers
                )]
                question_pattern = (
                    r"\b(?:deliver\w*|electronically|check|wire)\b" if "receiving provider" in markers
                    else r"\b(?:fees?|costs?|charges?)\b" if "$35" in markers
                    else None
                )
                if question_pattern:
                    targets = [i for i, question in enumerate(requested_questions[:12])
                               if isinstance(question, str) and re.search(question_pattern, question, re.IGNORECASE)]
                    # Filtering can remove a whole numbered answer. Locate
                    # its question number rather than its shifted list index;
                    # a source/process answer can mention the same provider.
                    if len(targets) == 1:
                        target = targets[0]
                        numbered = any(
                            isinstance(item, str) and re.match(r"^\d{1,2}[.)]\s", item)
                            for item in original_key_points
                        )
                        if numbered:
                            positions = [i for i, item in enumerate(key_points)
                                         if isinstance(item, str) and re.match(rf"^{target + 1}[.)]\s", item)]
                            if not positions:
                                label = "Delivery" if "receiving provider" in markers else "Fees"
                                prefix_text = f"{target + 1}. {label}: "
                                # Preserve a surviving section title from the
                                # original answer, never its filtered body.
                                for original in original_key_points:
                                    prefix = re.match(rf"^{target + 1}[.)]\s+[^:]{{1,70}}:\s*", original) if isinstance(original, str) else None
                                    if prefix:
                                        prefix_text = prefix.group()
                                        break
                                insert_at = len(key_points)
                                for index, item in enumerate(key_points):
                                    section = re.match(r"^(\d{1,2})[.)]\s", item) if isinstance(item, str) else None
                                    if section and int(section.group(1)) > target + 1:
                                        insert_at = index
                                        break
                                key_points.insert(insert_at, prefix_text + point)
                                continue
                if positions:
                    original_point = key_points[positions[0]]
                    prefix = re.match(r"^\d{1,2}[.)]\s+[^:]{1,70}:\s*", original_point) if isinstance(original_point, str) else None
                    if prefix:
                        point = prefix.group() + point
                    key_points[positions[0]] = point
                    key_points = [item for i, item in enumerate(key_points) if i not in positions[1:]]
                else:
                    key_points.append(point)
            key_point_limit = 12 + len(required_points)
        elif required_points:
            aliases = {
                marker
                for markers, _point in required_points
                for marker in markers
            }
            optional_points = [
                item for item in key_points
                if not any(
                    marker in cls._response_item_text(item)
                    for marker in aliases
                )
            ]
            key_points = [point for _markers, point in required_points]
            key_points.extend(optional_points[: max(0, 6 - len(key_points))])

        # Preserve a concise portal-first path. The fallback form is always
        # supplied after eligibility for outgoing termination requests.  Both
        # reviewed steps are reserved before applying the six-step cap.
        if fixed.get("outcome") == "can_proceed":
            canonical_markers = (
                "loans & distributions", "termination distribution",
                "rightsignature", cls._TERMINATION_FORM_URL.lower(),
                "844-401-2253",
            )
            other_steps = [
                item for item in steps
                if not any(
                    marker in cls._response_item_text(item)
                    for marker in canonical_markers
                )
            ]
            # Move duplicate contact references into the canonical steps,
            # while retaining each numbered answer to the participant's
            # questions. Dropping the whole item also drops its process answer.
            contact_references = (
                (cls._TERMINATION_FORM_URL, "the secure electronic form in the steps below"),
                ("844-401-2253", "the support number in the steps below"),
            )
            deduped_points: List[Any] = []
            for item in key_points:
                section = re.match(r"^(\d{1,2})[.)]\s", item) if isinstance(item, str) else None
                if preserve_question_order and section and 1 <= int(section.group(1)) <= len(requested_questions):
                    for reference, replacement in contact_references:
                        # Remove the full Markdown link before replacing a
                        # bare reference, so its target remains valid Markdown.
                        item = re.sub(
                            rf"\[[^\]]*\]\(<?(?:tel:)?{re.escape(reference)}>?\)",
                            replacement, item, flags=re.IGNORECASE,
                        )
                        item = re.sub(re.escape(reference), replacement, item, flags=re.IGNORECASE)
                    deduped_points.append(item)
                elif not any(reference.lower() in cls._response_item_text(item) for reference, _ in contact_references):
                    deduped_points.append(item)
            key_points = deduped_points
            portal_step = {
                "step_number": 1,
                "action": (
                    "Log in to the participant portal, select Loans & "
                    "Distributions, then Separation of Service (or Default if "
                    "shown) and start Termination Distribution."
                ),
                "detail": None,
            }
            fallback_step = {
                "step_number": 2,
                "action": (
                    "Use the secure electronic form only for website/login "
                    "problems. For access help, call ForUsAll Participant "
                    "Support at 844-401-2253, Monday-Friday, 7:00 AM-5:00 PM PT."
                ),
                "detail": cls._TERMINATION_FORM_URL,
            }
            steps = [portal_step, *other_steps[:4], fallback_step]
        for index, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                step["step_number"] = index

        response["key_points"] = key_points[:key_point_limit]
        response["warnings"] = warnings[:4]
        response["steps"] = steps[:6]
        fixed["response_to_participant"] = response
        return fixed, info

    def _apply_informational_outcome_policy(
        self,
        parsed: Dict[str, Any],
        retrieval_profile: Dict[str, Any],
        collected_data: Optional[Dict[str, Any]],
        selected_chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Optionally rescue a `blocked_missing_data` outcome to `can_proceed`
        when the inquiry is informational AND every missing must_have item is
        execution_only / personalization_only (per `blocking_intent`).

        Two parallel rescue paths are supported:

        1. `blocking_intent` path (generic): inspect must_have chunks for
           the new `blocking_intent` field and rescue when no missing item
           has `blocking_intent in {always, eligibility_confirmation}`.
        2. Legacy LT-termination path: kept for backwards compatibility
           on retrieval profiles where `blocking_intent` data is not yet
           available (e.g. articles not yet re-ingested into Pinecone).
        """
        missing_mentions = self._extract_missing_data_mentions(parsed)
        missing_classes = self._classify_missing_data(missing_mentions)
        core_status = self._termination_distribution_core_eligibility_status(
            collected_data
        )
        intents_by_data_point = self._extract_must_have_blocking_intents(selected_chunks)
        missing_intents: List[Dict[str, str]] = []
        for mention in missing_mentions:
            label = ""
            if isinstance(mention, dict):
                label = (
                    mention.get("question")
                    or mention.get("field")
                    or mention.get("description")
                    or ""
                )
            else:
                label = str(mention or "")
            label = label.strip()
            if not label:
                continue
            intent = self._resolve_missing_intent(label, intents_by_data_point)
            missing_intents.append({"label": label, "blocking_intent": intent})

        policy_info = {
            "normalized": False,
            "reason": None,
            "inquiry_intent": (retrieval_profile or {}).get("inquiry_intent"),
            "primary_action": (retrieval_profile or {}).get("primary_action"),
            "core_eligibility_supported": core_status["supported"],
            "core_eligibility_status": core_status,
            "missing_data_classes": missing_classes,
            "must_have_blocking_intents": intents_by_data_point,
            "missing_data_blocking_intents": missing_intents,
            "blocking_intent_overrides": [],
            "rescue_path": None,
        }

        if parsed.get("outcome") != "blocked_missing_data":
            return parsed, policy_info
        if (retrieval_profile or {}).get("inquiry_intent") != "informational_options":
            return parsed, policy_info

        # ---------------- Rescue path 1: blocking_intent generic ----------------
        if intents_by_data_point and missing_intents:
            hard_blockers = [
                m for m in missing_intents
                if m["blocking_intent"] not in self._RESCUABLE_BLOCKING_INTENTS
            ]
            if not hard_blockers:
                # Every missing must_have is execution_only or personalization_only.
                # Safe to rescue the answer to general/educational guidance.
                normalized = dict(parsed)
                normalized["outcome"] = "can_proceed"
                topic = (retrieval_profile or {}).get("primary_action") or "this topic"
                normalized["outcome_reason"] = (
                    f"All missing must_have items for {topic} are tagged "
                    "blocking_intent=execution_only or personalization_only. "
                    "Per the schema, the agent can provide general, educational "
                    "guidance about the article without these data points; only "
                    "an execution request would require them."
                )
                guardrails = list(normalized.get("guardrails_applied") or [])
                guardrails.append(
                    "Did not let missing execution_only / personalization_only "
                    "must_have items override an informational answer."
                )
                normalized["guardrails_applied"] = self._dedupe_preserving_order(guardrails)
                policy_info["normalized"] = True
                policy_info["reason"] = (
                    "Informational-options outcome normalized to can_proceed via "
                    "blocking_intent inspection."
                )
                policy_info["rescue_path"] = "blocking_intent"
                policy_info["blocking_intent_overrides"] = [
                    m["label"] for m in missing_intents
                ]
                return normalized, policy_info
            # Hard blocker present (always or eligibility_confirmation): keep block.
            policy_info["reason"] = (
                "At least one missing must_have item has blocking_intent="
                + hard_blockers[0]["blocking_intent"]
                + " (label: " + hard_blockers[0]["label"] + ")."
            )
            # Fall through to legacy path so we don't accidentally rescue
            # eligibility_confirmation cases via the LT-termination heuristic
            # when blocking_intent metadata is authoritative.
            return parsed, policy_info

        # ---------------- Rescue path 2: legacy LT-termination ----------------
        # Preserved for articles whose chunks do not yet expose
        # `must_have_blocking_intents` metadata.
        if (retrieval_profile or {}).get("primary_action") not in {
            "termination_distribution",
            "termination_rollover",
        }:
            return parsed, policy_info
        if not core_status["supported"]:
            policy_info["reason"] = "Core eligibility is not sufficiently supported."
            return parsed, policy_info
        if missing_classes["core_eligibility_missing"]:
            policy_info["reason"] = "LLM identified core eligibility gaps."
            return parsed, policy_info

        normalized = dict(parsed)
        normalized["outcome"] = "can_proceed"
        normalized["outcome_reason"] = (
            "Core eligibility for an informational LT Trust termination "
            "distribution answer is supported by the collected data. Missing "
            "identity lookup or execution details are next steps, not blockers "
            "to explaining delivery options, costs, and timelines."
        )
        guardrails = list(normalized.get("guardrails_applied") or [])
        guardrails.append(
            "Did not let missing identity lookup or execution details override a supported informational delivery/cost answer."
        )
        normalized["guardrails_applied"] = self._dedupe_preserving_order(guardrails)
        policy_info["normalized"] = True
        policy_info["reason"] = "Informational-options outcome normalized to can_proceed."
        policy_info["rescue_path"] = "legacy_lt_termination"
        return normalized, policy_info

    def _detect_advisory_concepts(
        self,
        inquiry: str,
        topic: str,
        collected_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return detect_advisory_concepts(inquiry, topic, collected_data)

    def _expand_queries_with_advisory_concepts(
        self,
        sub_queries: List[str],
        inquiry: str,
        topic: str,
        advisory_signal: Dict[str, Any],
    ) -> List[str]:
        """Add bounded, deterministic retrieval queries for advisory options."""
        del inquiry  # kept in the signature for future richer expansion
        expanded = [q for q in sub_queries if isinstance(q, str) and q.strip()]

        # F2 (eval 2026-06-22): gate the hardship sub-query so it only fires for a
        # hardship/in-service topic or a genuine active-participant funds-access
        # context — never on rollover/termination inquiries that merely tripped a
        # generic hardship token.
        topic_norm = (topic or "").lower().strip()
        hardship_allowed = topic_norm in {
            "hardship_withdrawal", "in_service_withdrawal_options"
        } or (
            advisory_signal.get("hardship_signal")
            and advisory_signal.get("active_participant")
            and advisory_signal.get("wants_funds")
        )

        advisory_queries = {
            "in_service_withdrawal_options": (
                "while employed 401k access options hardship withdrawal "
                "401k loan rollover source"
            ),
            "hardship_withdrawal": (
                "hardship withdrawal eviction foreclosure primary residence "
                "financial emergency self certification"
            ),
            "loan": (
                "401k loan eligibility active employee plan allows loans "
                "vested balance active loans"
            ),
        }

        additions = []
        for concept in advisory_signal.get("alternative_concepts", []):
            if concept == "hardship_withdrawal" and not hardship_allowed:
                continue
            query = advisory_queries.get(concept)
            if query:
                additions.append(query)

        advisory_additions = additions[:self.GR_MAX_ADVISORY_QUERIES]
        base_limit = max(1, self.GR_MAX_QUERY_COUNT - len(advisory_additions))
        expanded = self._ordered_unique(expanded)[:base_limit]

        for query in advisory_additions:
            expanded.append(query)

        return self._ordered_unique(expanded)[:self.GR_MAX_QUERY_COUNT]

    @staticmethod
    def _concept_aliases(concept: str) -> List[str]:
        aliases = {
            "hardship_withdrawal": [
                "hardship_withdrawal",
                "hardship",
                "hardship request",
                "eviction",
                "foreclosure",
            ],
            "loan": ["loan", "loans", "loan request", "borrow"],
            "in_service_withdrawal_options": [
                "in_service_withdrawal_options",
                "in service",
                "in-service",
                "while employed",
                "rollover source",
            ],
            "termination_distribution_request": [
                "termination_distribution_request",
                "termination",
                "separation",
                "terminated",
            ],
        }
        return aliases.get(concept, [concept])

    def _chunk_matches_response_concepts(
        self,
        chunk: Dict[str, Any],
        concepts: List[str],
    ) -> bool:
        meta = chunk.get("metadata", {})
        def metadata_text(value: Any) -> str:
            if isinstance(value, (list, tuple, set)):
                return " ".join(str(v) for v in value)
            return str(value or "")

        searchable_parts = [
            str(meta.get("topic", "")),
            str(meta.get("article_id", "")),
            str(meta.get("article_title", "")),
            metadata_text(meta.get("tags")),
            metadata_text(meta.get("subtopics")),
            metadata_text(meta.get("specific_topics")),
        ]
        searchable = " ".join(searchable_parts).lower()
        for concept in concepts:
            for alias in self._concept_aliases(concept):
                if alias and alias.lower() in searchable:
                    return True
        return False

    def _chunk_response_concept_matches(
        self,
        chunk: Dict[str, Any],
        concepts: List[str],
    ) -> List[str]:
        meta = chunk.get("metadata", {})

        def metadata_text(value: Any) -> str:
            if isinstance(value, (list, tuple, set)):
                return " ".join(str(v) for v in value)
            return str(value or "")

        searchable_parts = [
            str(meta.get("topic", "")),
            str(meta.get("article_id", "")),
            str(meta.get("article_title", "")),
            metadata_text(meta.get("tags")),
            metadata_text(meta.get("subtopics")),
            metadata_text(meta.get("specific_topics")),
        ]
        searchable = " ".join(searchable_parts).lower()

        matched = []
        for concept in concepts:
            if any(
                alias and alias.lower() in searchable
                for alias in self._concept_aliases(concept)
            ):
                matched.append(concept)
        return matched

    async def _add_response_article_bundles(
        self,
        chunks: List[Dict[str, Any]],
        advisory_signal: Dict[str, Any],
        retrieval_profile: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """
        If a relevant article appears through a weak chunk, add its high-value
        chunks so the LLM sees decision rules and steps instead of only links.
        """
        if not chunks:
            return chunks, {"articles_added": [], "chunks_added": 0}

        concepts = self._bundleable_response_concepts(
            advisory_signal=advisory_signal,
            retrieval_profile=retrieval_profile,
        )
        if not concepts:
            return chunks, {"articles_added": [], "chunks_added": 0}

        excluded_articles = set((retrieval_profile or {}).get("excluded_articles", []))
        article_best_score: Dict[str, float] = defaultdict(float)
        candidate_info: Dict[str, Dict[str, Any]] = {}
        for chunk in chunks:
            aid = chunk.get("metadata", {}).get("article_id")
            if not aid:
                continue
            if aid in excluded_articles:
                continue
            article_best_score[aid] = max(article_best_score[aid], chunk.get("score", 0.0))
            if chunk.get("score", 0.0) < self.GR_BUNDLE_MIN_SCORE:
                continue

            matched_concepts = self._chunk_response_concept_matches(chunk, concepts)
            if not matched_concepts:
                continue

            if aid not in candidate_info:
                candidate_info[aid] = {
                    "score": 0.0,
                    "concepts": set(),
                    "order": len(candidate_info),
                }
            candidate_info[aid]["score"] = max(candidate_info[aid]["score"], chunk.get("score", 0.0))
            candidate_info[aid]["concepts"].update(matched_concepts)

        candidate_articles: List[str] = []
        priority_concepts = self._ordered_unique(
            [
                concept for concept in (
                    advisory_signal.get("alternative_concepts", [])
                    + advisory_signal.get("detected_concepts", [])
                )
                if concept in concepts
            ]
        )

        for concept in priority_concepts:
            if len(candidate_articles) >= self.GR_BUNDLE_MAX_ARTICLES:
                break
            matches = [
                aid for aid, info in candidate_info.items()
                if concept in info["concepts"] and aid not in candidate_articles
            ]
            if not matches:
                continue
            matches.sort(
                key=lambda aid: (
                    candidate_info[aid]["score"],
                    -candidate_info[aid]["order"],
                ),
                reverse=True,
            )
            candidate_articles.append(matches[0])

        remaining = [
            aid for aid in candidate_info
            if aid not in candidate_articles
        ]
        remaining.sort(
            key=lambda aid: (
                candidate_info[aid]["score"],
                -candidate_info[aid]["order"],
            ),
            reverse=True,
        )
        for aid in remaining:
            if len(candidate_articles) >= self.GR_BUNDLE_MAX_ARTICLES:
                break
            candidate_articles.append(aid)

        if not candidate_articles:
            return chunks, {"articles_added": [], "chunks_added": 0}

        fetch_tasks = [
            asyncio.to_thread(
                self.pinecone.list_and_fetch_chunks,
                prefix=aid,
                limit=100,
            )
            for aid in candidate_articles
        ]
        fetched_lists = await asyncio.gather(*fetch_tasks)

        seen_ids = {chunk.get("id") for chunk in chunks}
        enriched = list(chunks)
        articles_added: List[str] = []
        chunks_added = 0

        for aid, bundle_chunks in zip(
            candidate_articles, fetched_lists, strict=True
        ):
            added_for_article = 0
            high_value = [
                chunk for chunk in bundle_chunks
                if chunk.get("metadata", {}).get("chunk_type") in self.GR_BUNDLE_CHUNK_TYPES
            ]
            high_value.sort(
                key=lambda c: self._response_chunk_rank_score(c, advisory_signal, None),
                reverse=True,
            )
            for chunk in high_value[:self.GR_BUNDLE_MAX_CHUNKS_PER_ARTICLE]:
                cid = chunk.get("id")
                if cid in seen_ids:
                    continue
                copied = {
                    "id": cid,
                    "score": min(0.95, article_best_score.get(aid, 0.0) + 0.05),
                    "metadata": chunk.get("metadata", {}),
                }
                enriched.append(copied)
                seen_ids.add(cid)
                added_for_article += 1
                chunks_added += 1
            if added_for_article:
                articles_added.append(aid)

        return enriched, {"articles_added": articles_added, "chunks_added": chunks_added}

    def _response_chunk_rank_score(
        self,
        chunk: Dict[str, Any],
        advisory_signal: Dict[str, Any],
        topic: Optional[str],
    ) -> float:
        score = float(chunk.get("score", 0.0) or 0.0)
        meta = chunk.get("metadata", {})

        tier_bonus = {
            "critical": 0.08,
            "high": 0.05,
            "medium": 0.02,
            "low": 0.0,
        }.get(meta.get("chunk_tier", "low"), 0.0)
        type_bonus = 0.06 if meta.get("chunk_type") in self._GR_HIGH_VALUE_TYPES else 0.0
        if meta.get("chunk_type") in self.GR_BUNDLE_CHUNK_TYPES:
            type_bonus = max(type_bonus, 0.05)

        concepts = self._ordered_unique(
            advisory_signal.get("detected_concepts", [])
            + advisory_signal.get("alternative_concepts", [])
        )
        concept_bonus = 0.08 if self._chunk_matches_response_concepts(chunk, concepts) else 0.0

        topic_bonus = 0.0
        if topic:
            resolved = self._resolve_topic_filter(topic) or [topic.lower().strip()]
            if self._chunk_matches_response_concepts(chunk, resolved):
                topic_bonus = 0.04

        reference_penalty = -0.12 if meta.get("chunk_type") == "references" else 0.0
        return score + tier_bonus + type_bonus + concept_bonus + topic_bonus + reference_penalty

    def _rank_response_chunks(
        self,
        chunks: List[Dict[str, Any]],
        advisory_signal: Dict[str, Any],
        topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        ranked = list(chunks)
        ranked.sort(
            key=lambda chunk: self._response_chunk_rank_score(chunk, advisory_signal, topic),
            reverse=True,
        )
        return ranked

    # ── Cached Pinecone query wrapper ──

    def _cache_key(
        self,
        query_text: str,
        top_k: int,
        filter_dict: Optional[Dict],
        rerank: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build a deterministic cache key from query parameters."""
        raw = json.dumps(
            {"q": query_text, "k": top_k, "f": filter_dict, "r": rerank},
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _cached_query(
        self,
        query_text: str,
        top_k: int,
        filter_dict: Optional[Dict[str, Any]] = None,
        rerank: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Async Pinecone query with TTL caching.

        Wraps the synchronous Pinecone SDK call in asyncio.to_thread
        so it doesn't block the event loop, and caches results to avoid
        redundant network round-trips for identical queries.

        Lock-free: concurrent coroutines with the same cache key may
        fire duplicate Pinecone calls; the last write wins (identical
        result). This trade-off enables true parallel execution across
        all search lanes.
        """
        from data_pipeline.retrieval_privacy import sanitize_retrieval_query

        query_text = sanitize_retrieval_query(query_text)
        key = self._cache_key(query_text, top_k, filter_dict, rerank)

        if key in self._search_cache:
            logger.debug("Cache HIT for Pinecone query")
            return self._search_cache[key]

        result = await asyncio.to_thread(
            self.pinecone.query_chunks,
            query_text=query_text,
            top_k=top_k,
            filter_dict=filter_dict,
            rerank=rerank,
        )

        self._search_cache[key] = result
        return result

    def _exact_context_chunk_types_for_profile(
        self,
        retrieval_profile: Optional[Dict[str, Any]],
    ) -> frozenset:
        if (retrieval_profile or {}).get("inquiry_intent") == "informational_options":
            return self.GR_EXACT_INFORMATIONAL_CONTEXT_CHUNK_TYPES
        return self.GR_EXACT_CONTEXT_CHUNK_TYPES

    def _exact_context_type_order_for_profile(
        self,
        retrieval_profile: Optional[Dict[str, Any]],
    ) -> Dict[str, int]:
        if (retrieval_profile or {}).get("inquiry_intent") == "informational_options":
            return self.GR_EXACT_INFORMATIONAL_CONTEXT_TYPE_ORDER
        return self.GR_EXACT_CONTEXT_TYPE_ORDER

    def _exact_context_sort_key(
        self,
        chunk: Dict[str, Any],
        retrieval_profile: Optional[Dict[str, Any]],
    ) -> tuple:
        meta = chunk.get("metadata", {})
        type_order = self._exact_context_type_order_for_profile(retrieval_profile)
        category = str(meta.get("chunk_category", "")).lower()
        chunk_type = meta.get("chunk_type", "")
        type_rank = type_order.get(chunk_type, 99)
        category_rank = 99
        if (retrieval_profile or {}).get("inquiry_intent") == "informational_options":
            category_rank = self.GR_EXACT_INFORMATIONAL_CATEGORY_ORDER.get(
                category,
                99,
            )
            if (
                chunk_type == "business_rules"
                and category not in self.GR_EXACT_INFORMATIONAL_PRIORITY_BUSINESS_CATEGORIES
            ):
                type_rank = 8
        return (
            type_rank,
            category_rank,
            -chunk.get("score", 0.0),
            meta.get("chunk_index", 999),
        )

    def _exact_procedure_chunk_score(self, chunk: Dict[str, Any]) -> float:
        meta = chunk.get("metadata", {})
        chunk_type = meta.get("chunk_type", "")
        type_rank = self.GR_EXACT_CONTEXT_TYPE_ORDER.get(chunk_type, 99)
        base = max(0.55, 0.98 - (type_rank * 0.015))
        if meta.get("chunk_tier") == "critical":
            base += 0.02
        elif meta.get("chunk_tier") == "high":
            base += 0.01
        category = str(meta.get("chunk_category", "")).lower()
        if category in {
            "eligibility",
            "fees",
            "tax_withholding",
            "delivery",
            "wire_instructions",
            "check_rollover_instructions",
            "rollover_payment_method",
            "processing_times",
        }:
            base += 0.01
        return round(min(0.99, base), 4)

    def _exact_procedure_chunks_sufficient(
        self,
        chunks: List[Dict[str, Any]],
    ) -> bool:
        if len(chunks) < self.GR_EXACT_MIN_CHUNKS:
            return False
        types_present = {
            chunk.get("metadata", {}).get("chunk_type")
            for chunk in chunks
        }
        return bool(
            {"eligibility", "business_rules"} & types_present
            and "steps" in types_present
        )

    async def _search_for_exact_response_procedure(
        self,
        enriched_queries: List[str],
        retrieval_profile: Dict[str, Any],
    ) -> tuple:
        """
        Fetch the pre-resolved primary article directly for exact procedures.

        This avoids broad semantic recall when deterministic routing already
        identifies the applicable procedure. The caller falls back to semantic
        search if the primary article cannot be fetched or lacks enough
        high-value chunks.
        """
        article_id = retrieval_profile.get("primary_article_id")
        per_query_scores: Dict[str, float] = {eq: 0.0 for eq in enriched_queries}
        if not article_id:
            return [], per_query_scores

        article_chunks = await asyncio.to_thread(
            self.pinecone.list_and_fetch_chunks,
            prefix=article_id,
            limit=100,
        )
        if not article_chunks:
            logger.warning("Exact procedure routing could not fetch article")
            return [], per_query_scores

        include_references = bool(retrieval_profile.get("include_references"))
        allowed_chunk_types = self._exact_context_chunk_types_for_profile(
            retrieval_profile
        )
        if include_references:
            allowed_chunk_types = frozenset({*allowed_chunk_types, "references"})
        scored_chunks: List[Dict[str, Any]] = []
        for chunk in article_chunks:
            meta = chunk.get("metadata", {})
            chunk_type = meta.get("chunk_type")
            if chunk_type == "references" and not include_references:
                continue
            if chunk_type not in allowed_chunk_types:
                continue
            copied = {
                "id": chunk.get("id"),
                "score": self._exact_procedure_chunk_score(chunk),
                "metadata": meta,
            }
            scored_chunks.append(copied)

        if not self._exact_procedure_chunks_sufficient(scored_chunks):
            logger.info(
                "Exact procedure article had insufficient context chunks (%d)",
                len(scored_chunks),
            )
            return [], per_query_scores

        # Multi-intent account-access + outgoing-distribution cases retain the
        # exact termination procedure and add bounded companion coverage. No
        # incoming-rollover profile receives these companions.
        companion_ids = list(retrieval_profile.get("companion_article_ids") or [])[:2]
        if companion_ids:
            companion_lists = await asyncio.gather(*[
                asyncio.to_thread(
                    self.pinecone.list_and_fetch_chunks,
                    prefix=companion_id,
                    limit=100,
                )
                for companion_id in companion_ids
            ])
            companion_types = frozenset({
                "decision_guide", "response_frames", "business_rules", "steps",
                "common_issues", "guardrails", "references",
            })
            for companion_chunks in companion_lists:
                candidates = [
                    chunk for chunk in companion_chunks
                    if chunk.get("metadata", {}).get("chunk_type") in companion_types
                ]
                candidates.sort(
                    key=lambda chunk: (
                        self.GR_EXACT_CONTEXT_TYPE_ORDER.get(
                            chunk.get("metadata", {}).get("chunk_type"), 99
                        ),
                        chunk.get("metadata", {}).get("chunk_index", 999),
                    )
                )
                for chunk in candidates[:4]:
                    scored_chunks.append({
                        "id": chunk.get("id"),
                        "score": 0.94,
                        "metadata": chunk.get("metadata", {}),
                    })

        scored_chunks.sort(
            key=lambda c: self._exact_context_sort_key(c, retrieval_profile),
        )
        best_score = max((c.get("score", 0.0) for c in scored_chunks), default=0.0)
        per_query_scores = {
            eq: round(best_score, 4) for eq in enriched_queries
        }
        logger.info(
            "Exact procedure routing selected primary + companions with %d chunks",
            len(scored_chunks),
        )
        return scored_chunks, per_query_scores

    def _filter_excluded_response_articles(
        self,
        chunks: List[Dict[str, Any]],
        retrieval_profile: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        excluded = set((retrieval_profile or {}).get("excluded_articles", []))
        if not excluded:
            return chunks
        filtered = [
            chunk for chunk in chunks
            if chunk.get("metadata", {}).get("article_id") not in excluded
        ]
        removed = len(chunks) - len(filtered)
        if removed:
            logger.info(
                f"Filtered {removed} chunks from excluded tangential articles"
            )
        return filtered

    def _bundleable_response_concepts(
        self,
        advisory_signal: Dict[str, Any],
        retrieval_profile: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        if (retrieval_profile or {}).get("mode") == "exact_procedure":
            return []
        concepts = self._ordered_unique(
            advisory_signal.get("detected_concepts", [])
            + advisory_signal.get("alternative_concepts", [])
        )
        return [
            concept for concept in concepts
            if concept in self.GR_CONTEXT_QUOTA_CONCEPTS
        ]

    # ── Record-Keeper Cascade Strategy ──

    def _build_rk_cascade(
        self,
        record_keeper: Optional[str]
    ) -> List[Dict[str, Any]]:
        """
        Build the ordered cascade of record-keeper filter levels.

        The cascade determines the search priority order based on whether
        a record_keeper was provided by the caller.

        When record_keeper IS provided:
          1. RK-specific   → use provided RK to narrow down quickly
          2. Global scope   → record_keeper=null articles (scope="global")
          3. LT Trust       → default fallback RK (skipped if already tried)
          4. Any            → no RK filter, rely on semantic relevance

        When record_keeper is NOT provided:
          1. Global scope   → record_keeper=null articles first
          2. LT Trust       → default fallback RK
          3. Any            → no RK filter, rely on semantic relevance

        Returns:
            Ordered list of cascade levels, each with "filters" and "label".
        """
        cascade = []

        if record_keeper:
            cascade.append({
                "filters": {"record_keeper": {"$eq": record_keeper}},
                "label": f"RK={record_keeper}"
            })
            cascade.append({
                "filters": {"scope": {"$eq": "global"}},
                "label": "scope=global"
            })
            if record_keeper != self.RK_FALLBACK_DEFAULT:
                cascade.append({
                    "filters": {"record_keeper": {"$eq": self.RK_FALLBACK_DEFAULT}},
                    "label": f"RK={self.RK_FALLBACK_DEFAULT} (fallback)"
                })
            cascade.append({
                "filters": {},
                "label": "any RK (semantic only)"
            })
        else:
            cascade.append({
                "filters": {"scope": {"$eq": "global"}},
                "label": "scope=global"
            })
            cascade.append({
                "filters": {"record_keeper": {"$eq": self.RK_FALLBACK_DEFAULT}},
                "label": f"RK={self.RK_FALLBACK_DEFAULT} (fallback)"
            })
            cascade.append({
                "filters": {},
                "label": "any RK (semantic only)"
            })

        return cascade

    def _rk_results_sufficient(
        self,
        chunks: List[Dict[str, Any]],
        min_chunks: Optional[int] = None,
        min_score: Optional[float] = None
    ) -> bool:
        """
        Evaluate whether a cascade level returned sufficient results.

        Args:
            chunks: Results from this cascade level
            min_chunks: Minimum number of results (default: RK_CASCADE_MIN_CHUNKS)
            min_score: Minimum best score (default: RK_CASCADE_MIN_SCORE)

        Returns:
            True if the results are sufficient to stop cascading
        """
        min_c = min_chunks if min_chunks is not None else self.RK_CASCADE_MIN_CHUNKS
        min_s = min_score if min_score is not None else self.RK_CASCADE_MIN_SCORE

        if len(chunks) < min_c:
            return False
        if chunks and chunks[0].get('score', 0) < min_s:
            return False
        return True

    # ── Search methods (async) ──

    async def _search_for_required_data(
        self,
        enriched_queries: List[str],
        record_keeper: Optional[str],
        plan_type: str,
        topic: str,
        inquiry: str = "",
        retrieval_profile: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """
        Parallel multi-query search for required_data endpoint.

        Runs all enriched sub-queries in parallel across the RK cascade,
        then merges, deduplicates, and ranks results. Tracks per-query
        best scores for observability.

        Topic filter (Fix 1): when `topic` resolves to canonical values via
        TOPIC_NORMALIZATION_MAP, the cascade first runs a topic-scoped lane.
        If it yields fewer than RD_TOPIC_LANE_MIN_CHUNKS, the cascade is
        re-run without the topic filter as a safety net.

        Boost ranker (Fix 3): rdmh chunks get a small additive boost over
        their cosine score (preserving raw_score) when their record_keeper
        matches the request, their topic is in the resolved topic set, or
        any subtopic matches inquiry text. This resolves near-ties in favor
        of RK-specific / topic-specific articles.

        Profile (Fix 4): when ``retrieval_profile`` provides
        ``excluded_articles``, those article_ids are filtered out of the
        rdmh results. When ``primary_article_id`` is set, the primary
        article's rdmh chunk is promoted to position 0 (and explicitly
        fetched if missing). The result is then truncated to a small set
        of distinct articles so the LLM context stays focused on the
        primary article.

        Returns:
            (merged_chunks, per_query_scores)
        """
        resolved_topics = self._resolve_topic_filter(topic)
        is_global_only = self._is_global_only_topic(resolved_topics)

        per_query_scores: Dict[str, float] = {eq: 0.0 for eq in enriched_queries}

        # Topic-scoped lane first (when topic resolves); broad lane as fallback.
        required_data_chunks = await self._run_required_data_rk_cascade(
            enriched_queries=enriched_queries,
            record_keeper=record_keeper,
            plan_type=plan_type,
            resolved_topics=resolved_topics,
            per_query_scores=per_query_scores,
            skip_rk_levels=is_global_only,
        )

        # Broad-fallback: do NOT run for global-only topics. Re-running with
        # resolved_topics=None reintroduces RK levels and pulls
        # required_data_must_have chunks from OTHER topics (loan/termination/MFA)
        # — off-topic noise. For a global-only topic, "0 required_data" is the
        # correct answer (hardship/excess have must_have=[]); in_service fills
        # the lane and never reaches the fallback anyway.
        if (
            resolved_topics
            and not is_global_only
            and len(required_data_chunks) < self.RD_TOPIC_LANE_MIN_CHUNKS
        ):
            logger.info(
                f"Topic-scoped lane returned {len(required_data_chunks)} chunks "
                f"(< {self.RD_TOPIC_LANE_MIN_CHUNKS}). Falling back to broad lane."
            )
            broad_chunks = await self._run_required_data_rk_cascade(
                enriched_queries=enriched_queries,
                record_keeper=record_keeper,
                plan_type=plan_type,
                resolved_topics=None,
                per_query_scores=per_query_scores,
                skip_rk_levels=False,
            )
            required_data_chunks = self._merge_and_rank_chunks(
                required_data_chunks, broad_chunks
            )

        # Fix 4: drop chunks from articles the profile deterministically excluded.
        if retrieval_profile:
            required_data_chunks = self._filter_excluded_response_articles(
                required_data_chunks, retrieval_profile
            )

        # Apply origin / topic-specificity / subtopic boost to rdmh chunks.
        required_data_chunks = self._rank_rdmh_chunks(
            required_data_chunks,
            record_keeper=record_keeper,
            resolved_topics=resolved_topics,
            inquiry_text=inquiry,
        )

        # Fix 4: promote the deterministic primary article (if any) to slot 0
        # and cap the distinct-article tail so the LLM sees a focused context.
        primary_article_id = (retrieval_profile or {}).get("primary_article_id")
        if primary_article_id:
            required_data_chunks = await self._promote_primary_rdmh_chunk(
                required_data_chunks, primary_article_id
            )
            # Fix 6: when the primary article has a clear score lead, prune
            # the rest so the LLM context narrows to just the dominant article.
            required_data_chunks = self._prune_below_score_gap(
                required_data_chunks,
                primary_article_id=primary_article_id,
                gap=self.RD_SCORE_GAP_PRUNE_THRESHOLD,
            )
            # F5 (eval 2026-06-22): when a primary article is routed, the must-have
            # ENUMERATION must come only from that article — sibling must_have
            # chunks (e.g. loan/split execution fields on a plain cash-out)
            # otherwise bloat the required-data ask. Supporting-article
            # eligibility/business_rules chunks are NOT required_data_must_have, so
            # they are untouched; topics with must_have=[] have primary_article_id
            # None and skip this filter entirely.
            required_data_chunks = [
                c for c in required_data_chunks
                if c['metadata'].get('chunk_type') != 'required_data_must_have'
                or c['metadata'].get('article_id') == primary_article_id
            ]
        required_data_chunks = self._cap_rdmh_distinct_articles(
            required_data_chunks,
            limit=self.RD_MAX_DISTINCT_ARTICLES,
        )

        if required_data_chunks:
            best = required_data_chunks[0]
            logger.info(
                "Required data best match: score=%.4f, raw_score=%.4f, "
                "boost=%.4f",
                best["score"],
                best.get("raw_score", best["score"]),
                best.get("score_boost", 0),
            )
        else:
            logger.warning("Required data: No required_data_must_have chunks found across all cascade levels")
            return [], per_query_scores

        # ── Phase 2: Context chunks from the winning article ──
        best_article_id = required_data_chunks[0]['metadata'].get('article_id')

        context_filters = {
            "article_id": {"$eq": best_article_id},
            "chunk_type": {"$in": ["eligibility", "business_rules"]}
        }
        logger.info("Phase 2: focusing context on selected article")

        context_chunks = await self._cached_query(
            enriched_queries[0], top_k=7, filter_dict=context_filters
        )
        logger.info(f"Phase 2 (context): found {len(context_chunks)} chunks")

        merged = self._merge_and_rank_chunks(required_data_chunks, context_chunks)

        logger.info(f"Total merged chunks for required_data: {len(merged)}")
        return merged, per_query_scores

    async def _run_required_data_rk_cascade(
        self,
        enriched_queries: List[str],
        record_keeper: Optional[str],
        plan_type: str,
        resolved_topics: Optional[List[str]],
        per_query_scores: Dict[str, float],
        skip_rk_levels: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Run the RK cascade for required_data with an optional topic filter.

        Mutates `per_query_scores` in place. Returns the merged & ranked chunks
        from the first cascade level that satisfies `_rk_results_sufficient`,
        or an empty list if no level produced any chunks.

        When `skip_rk_levels` is True (global-only topic + record_keeper), the
        record_keeper-filtered cascade levels are dropped entirely; only the
        record_keeper-free levels (scope=global, then any) run.
        """
        rk_cascade = self._build_rk_cascade(record_keeper)
        top_k = self.RD_TOP_K_PER_QUERY
        topic_filter = (
            {"topic": {"$in": resolved_topics}} if resolved_topics else {}
        )
        topic_label = f"topic={resolved_topics}" if resolved_topics else "no-topic"
        required_data_chunks: List[Dict[str, Any]] = []

        if record_keeper:
            if skip_rk_levels:
                logger.info(
                    "Global-only topic: skipping RK-specific levels "
                    "in required_data"
                )
                # _build_rk_cascade(record_keeper) produces:
                #   [0] {record_keeper}, [1] {scope=global},
                #   [2] {record_keeper=LT Trust} (only if rk != LT Trust), [-1] {}.
                # Keep only the levels WITHOUT a record_keeper filter ->
                #   [scope=global, {}]. (For LT Trust the [2] level never exists.)
                non_rk_cascade = [
                    lvl for lvl in rk_cascade if "record_keeper" not in lvl["filters"]
                ]
                return await self._iterate_rk_cascade_levels(
                    non_rk_cascade,
                    enriched_queries,
                    plan_type,
                    topic_filter,
                    per_query_scores,
                    top_k,
                    topic_label,
                )
            staged_primary = record_keeper in self.RK_PRIMARY_STAGED_RECORD_KEEPERS
            # Fix H (Round 2): topics with no RK-specific article must NOT
            # short-circuit on Stage 1a — the global lane must always run so
            # hardship/in-service inquiries reach their global article. The
            # boost ranker still prioritises RK matches when both lanes hit.
            if (
                staged_primary
                and resolved_topics
                and set(resolved_topics).issubset(self.RK_OPTIONAL_TOPICS)
            ):
                logger.info(
                    "RK-optional topic detected: disabling Stage 1a "
                    "short-circuit; running RK + global lanes in parallel"
                )
                staged_primary = False

            if staged_primary:
                # Stage 1a: RK-specific lane only. Skip global lane if sufficient.
                stage_1a_tasks = []
                for eq in enriched_queries:
                    rk_filters = {
                        **rk_cascade[0]["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"},
                        **topic_filter,
                    }
                    stage_1a_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=rk_filters))

                stage_1a_results = await asyncio.gather(*stage_1a_tasks)
                for i, eq in enumerate(enriched_queries):
                    best = max((c.get('score', 0) for c in stage_1a_results[i]), default=0)
                    per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))

                stage_1a_chunks = self._merge_and_rank_chunks(*stage_1a_results)
                logger.info(
                    "Stage 1a record-keeper lane found %d chunks",
                    len(stage_1a_chunks),
                )

                if self._rk_results_sufficient(
                    stage_1a_chunks,
                    min_chunks=self.RK_PRIMARY_SUFFICIENT_CHUNKS,
                    min_score=self.RK_PRIMARY_SUFFICIENT_SCORE,
                ):
                    logger.info(
                        f"Stage 1a sufficient (>= {self.RK_PRIMARY_SUFFICIENT_CHUNKS} "
                        f"chunks, top score >= {self.RK_PRIMARY_SUFFICIENT_SCORE}). "
                        f"Skipping global lane."
                    )
                    return stage_1a_chunks
                required_data_chunks = stage_1a_chunks

                # Stage 1b: open global lane and merge.
                stage_1b_tasks = []
                for eq in enriched_queries:
                    global_filters = {
                        **rk_cascade[1]["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"},
                        **topic_filter,
                    }
                    stage_1b_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=global_filters))

                stage_1b_results = await asyncio.gather(*stage_1b_tasks)
                for i, eq in enumerate(enriched_queries):
                    best = max((c.get('score', 0) for c in stage_1b_results[i]), default=0)
                    per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))

                stage_1b_chunks = self._merge_and_rank_chunks(*stage_1b_results)
                logger.info(
                    "Stage 1b global lane found %d chunks",
                    len(stage_1b_chunks),
                )
                required_data_chunks = self._merge_and_rank_chunks(
                    required_data_chunks, stage_1b_chunks
                )
            else:
                # Legacy parallel behaviour for record-keepers without enough
                # dedicated coverage to benefit from staging.
                search_tasks = []
                for eq in enriched_queries:
                    rk_filters = {
                        **rk_cascade[0]["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"},
                        **topic_filter,
                    }
                    global_filters = {
                        **rk_cascade[1]["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"},
                        **topic_filter,
                    }
                    search_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=rk_filters))
                    search_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=global_filters))

                results = await asyncio.gather(*search_tasks)

                for i, eq in enumerate(enriched_queries):
                    eq_chunks = results[2 * i] + results[2 * i + 1]
                    best = max((c.get('score', 0) for c in eq_chunks), default=0)
                    per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))

                required_data_chunks = self._merge_and_rank_chunks(*results)
                logger.info(
                    "Phase 1 parallel (%d tasks) found %d unique chunks",
                    len(search_tasks),
                    len(required_data_chunks),
                )

            if not self._rk_results_sufficient(required_data_chunks):
                fallback_chunks = await self._iterate_rk_cascade_levels(
                    rk_cascade[2:],
                    enriched_queries,
                    plan_type,
                    topic_filter,
                    per_query_scores,
                    top_k,
                    topic_label,
                )
                if fallback_chunks:
                    required_data_chunks = self._merge_and_rank_chunks(
                        required_data_chunks, fallback_chunks
                    )
        else:
            required_data_chunks = await self._iterate_rk_cascade_levels(
                rk_cascade,
                enriched_queries,
                plan_type,
                topic_filter,
                per_query_scores,
                top_k,
                topic_label,
            )

        return required_data_chunks

    async def _iterate_rk_cascade_levels(
        self,
        cascade: List[Dict[str, Any]],
        enriched_queries: List[str],
        plan_type: str,
        topic_filter: Dict[str, Any],
        per_query_scores: Dict[str, float],
        top_k: int,
        topic_label: str = "",
    ) -> List[Dict[str, Any]]:
        """Iterate cascade levels one by one, short-circuiting on the first
        level that satisfies ``_rk_results_sufficient``.

        Mutates ``per_query_scores`` in place — this dict is returned to the API
        for observability, so the per-level best-score update is load-bearing.
        Centralising it here removes the copy-paste hazard of the previously
        duplicated cascade loops. Returns the first sufficient level's merged &
        ranked chunks, or ``[]`` if no level produced sufficient results.
        """
        chunks: List[Dict[str, Any]] = []
        for level in cascade:
            tasks = [
                self._cached_query(
                    eq,
                    top_k=top_k,
                    filter_dict={
                        **level["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"},
                        **topic_filter,
                    },
                )
                for eq in enriched_queries
            ]
            results = await asyncio.gather(*tasks)
            for i, eq in enumerate(enriched_queries):
                best = max((c.get("score", 0) for c in results[i]), default=0)
                per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))

            level_chunks = self._merge_and_rank_chunks(*results)
            logger.info("Cascade level found %d chunks", len(level_chunks))
            if self._rk_results_sufficient(level_chunks):
                chunks = self._merge_and_rank_chunks(chunks, level_chunks)
                break
        return chunks

    def _merge_and_rank_chunks(
        self,
        *chunk_lists: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Merge múltiples listas de chunks, deduplicar por ID, ordenar por score.

        Returns:
            Lista deduplicada ordenada por score descendente (mejor primero)
        """
        seen_ids = set()
        all_chunks = []

        for chunk_list in chunk_lists:
            for chunk in chunk_list:
                chunk_id = chunk.get('id')
                if chunk_id not in seen_ids:
                    seen_ids.add(chunk_id)
                    all_chunks.append(chunk)

        all_chunks.sort(key=lambda c: c.get('score', 0), reverse=True)
        return all_chunks

    # Boost constants for `_rank_rdmh_chunks` (Fix 3).
    RD_BOOST_RK = 0.05
    RD_BOOST_TOPIC = 0.03
    RD_BOOST_SUBTOPIC = 0.02
    RD_BOOST_CAP = 0.10

    def _rank_rdmh_chunks(
        self,
        chunks: List[Dict[str, Any]],
        record_keeper: Optional[str],
        resolved_topics: Optional[List[str]],
        inquiry_text: str,
    ) -> List[Dict[str, Any]]:
        """
        Apply origin / topic-specificity / subtopic boost to rdmh chunks.

        The boost is additive on `score` and preserves the original cosine
        score as `raw_score` for observability. Total boost is capped at
        RD_BOOST_CAP so a low cosine chunk cannot overtake a strong one;
        the boost only resolves near-ties.

        Components:
          * RD_BOOST_RK   if chunk.metadata.record_keeper == requested RK
          * RD_BOOST_TOPIC if chunk.metadata.topic is in resolved_topics
          * RD_BOOST_SUBTOPIC if any chunk subtopic appears in inquiry_text
        """
        inquiry_lower = (inquiry_text or "").lower()
        resolved_set = set(resolved_topics or [])

        for chunk in chunks:
            raw_score = chunk.get("raw_score")
            if raw_score is None:
                raw_score = chunk.get("score", 0.0)
                chunk["raw_score"] = raw_score

            meta = chunk.get("metadata") or {}
            boost = 0.0

            if record_keeper and meta.get("record_keeper") == record_keeper:
                boost += self.RD_BOOST_RK

            if resolved_set and meta.get("topic") in resolved_set:
                boost += self.RD_BOOST_TOPIC

            subtopics = meta.get("subtopics") or []
            if isinstance(subtopics, str):
                subtopics = [subtopics]
            for st in subtopics:
                if isinstance(st, str) and st and st.lower() in inquiry_lower:
                    boost += self.RD_BOOST_SUBTOPIC
                    break

            boost = min(boost, self.RD_BOOST_CAP)
            chunk["score"] = raw_score + boost
            chunk["score_boost"] = round(boost, 4)

        chunks.sort(key=lambda c: c.get("score", 0), reverse=True)
        return chunks

    async def _promote_primary_rdmh_chunk(
        self,
        chunks: List[Dict[str, Any]],
        primary_article_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Ensure the primary article's rdmh chunk sits at index 0.

        If the chunk is already present, move it to the front (preserving its
        boosted score). If not, fetch it explicitly via Pinecone's prefix
        list-and-fetch and prepend it with score_boost=0.
        """
        for i, chunk in enumerate(chunks):
            if chunk.get("metadata", {}).get("article_id") == primary_article_id:
                if i != 0:
                    chunks.insert(0, chunks.pop(i))
                return chunks

        try:
            fetched = await asyncio.to_thread(
                self.pinecone.list_and_fetch_chunks,
                prefix=primary_article_id,
                limit=50,
            )
        except Exception as exc:
            logger.warning(
                "Could not fetch primary article (error_type=%s)",
                type(exc).__name__,
            )
            return chunks

        rdmh_chunk = next(
            (
                c for c in (fetched or [])
                if (c.get("metadata") or {}).get("chunk_type")
                == "required_data_must_have"
            ),
            None,
        )
        if not rdmh_chunk:
            logger.warning("Selected primary article has no rdmh chunk to promote")
            return chunks

        # Explicit-fetched chunks have no cosine signal. Pin both score AND
        # raw_score above the retrieval gates (RD_RETRIEVAL_MIN_SCORE and
        # RD_RETRIEVAL_MIN_RAW_SCORE introduced in Round 1 Fix 6) so the
        # downstream guard treats this as an authoritative match rather than
        # a near-miss. raw_score must clear RD_RETRIEVAL_MIN_RAW_SCORE on its
        # own — leaving it at 0.0 caused T4 to fail the gate even when the
        # primary article was correctly routed.
        floor_score = max(
            self.RD_RETRIEVAL_MIN_SCORE,
            (chunks[0]["score"] if chunks else 0.0),
        )
        floor_raw = max(
            self.RD_RETRIEVAL_MIN_RAW_SCORE,
            (chunks[0].get("raw_score", 0.0) if chunks else 0.0),
        )
        promoted = {
            "id": rdmh_chunk.get("id"),
            "score": floor_score,
            "raw_score": floor_raw,
            "score_boost": round(floor_score - floor_raw, 4),
            "metadata": rdmh_chunk.get("metadata") or {},
        }
        logger.info(
            "Promoted primary rdmh chunk via explicit fetch "
            "(score=%.4f, raw_score=%.4f)",
            floor_score,
            floor_raw,
        )
        chunks.insert(0, promoted)
        return chunks

    def _prune_below_score_gap(
        self,
        chunks: List[Dict[str, Any]],
        primary_article_id: str,
        gap: float,
    ) -> List[Dict[str, Any]]:
        """
        Fix 6: when chunks[0] is the primary article AND the score lead over
        the next distinct article reaches ``gap``, drop everything except
        the primary's chunks. Preserves chunks belonging to the primary.
        """
        if not chunks or gap <= 0:
            return chunks
        top = chunks[0]
        top_aid = (top.get("metadata") or {}).get("article_id")
        if top_aid != primary_article_id:
            return chunks
        runner_up_score: Optional[float] = None
        for chunk in chunks[1:]:
            aid = (chunk.get("metadata") or {}).get("article_id")
            if aid != primary_article_id:
                runner_up_score = chunk.get("score", 0.0)
                break
        if runner_up_score is None:
            return chunks
        if top.get("score", 0.0) - runner_up_score < gap:
            return chunks
        pruned = [
            c for c in chunks
            if (c.get("metadata") or {}).get("article_id") == primary_article_id
        ]
        logger.info(
            f"Score-gap prune: top {top['score']:.4f} - runner_up "
            f"{runner_up_score:.4f} >= gap {gap}; kept {len(pruned)} primary chunks"
        )
        return pruned

    def _cap_rdmh_distinct_articles(
        self,
        chunks: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """
        Keep chunks belonging to at most ``limit`` distinct article_ids.

        Preserves the existing chunk order (which already reflects the
        boosted ranking + primary promotion).
        """
        if limit <= 0:
            return chunks
        kept: List[Dict[str, Any]] = []
        seen_articles: List[str] = []
        for chunk in chunks:
            aid = (chunk.get("metadata") or {}).get("article_id")
            if aid in seen_articles:
                kept.append(chunk)
                continue
            if len(seen_articles) >= limit:
                continue
            seen_articles.append(aid)
            kept.append(chunk)
        return kept

    async def _search_for_response_parallel_cascade(
        self,
        enriched_queries: List[str],
        record_keeper: Optional[str],
        plan_type: str,
        topic: str,
    ) -> tuple:
        """
        Parallel RK/topic-aware search for generate_response endpoint.

        Runs the active lanes (A:rk+topic, C:rk_broad, E:global_broad,
        G:semantic) in a single asyncio.gather round instead of the sequential
        short-circuit cascade used by required_data. Adds a conditional
        fallback pass H (plan_type-only) when the primary pass yields too few or
        low-scoring chunks.

        For structurally global-only topics (GLOBAL_ONLY_TOPICS) with a
        record_keeper, the RK-specific lanes A and C are skipped (they return 0
        or pure noise); E and G carry the retrieval and the H fallback is pinned
        to scope=global so it cannot reintroduce RK-specific articles.
        """
        plan_filter = {"$in": [plan_type, "all"]}
        has_rk = bool(record_keeper)
        resolved_topics = self._resolve_topic_filter(topic)
        has_topic_filter = resolved_topics is not None

        # Global-only topics: the KB has no RK-specific article, so lanes A
        # (rk+topic) and C (rk_broad) return 0 / pure noise. Skip them and let
        # E (global_broad) + G (semantic) carry the retrieval.
        skip_rk_lanes = has_rk and self._is_global_only_topic(resolved_topics)
        if skip_rk_lanes:
            logger.info(
                "Global-only topic detected; skipping record-keeper lanes A and C"
            )

        tasks: List = []
        task_meta: List[tuple] = []

        def add(eq: str, lane: str, top_k: int, filter_dict: Dict[str, Any]):
            rerank = None
            query_top_k = top_k
            if self.GR_RERANK_ENABLED:
                rerank = {
                    "model": "bge-reranker-v2-m3",
                    "top_n": top_k,
                    "rank_fields": ["content"],
                    "parameters": {"truncate": "END"},
                }
                query_top_k = top_k * 2
            tasks.append(
                self._cached_query(
                    eq,
                    top_k=query_top_k,
                    filter_dict=filter_dict,
                    rerank=rerank,
                )
            )
            task_meta.append((eq, lane))

        for eq in enriched_queries:
            if has_rk and has_topic_filter and not skip_rk_lanes:
                add(eq, "A:rk+topic", 10, {
                    "plan_type": plan_filter,
                    "record_keeper": {"$eq": record_keeper},
                    "topic": {"$in": resolved_topics},
                })
            if has_rk and not skip_rk_lanes:
                add(eq, "C:rk_broad", 12, {
                    "plan_type": plan_filter,
                    "record_keeper": {"$eq": record_keeper},
                })
            add(eq, "E:global_broad", 10, {
                "plan_type": plan_filter,
                "scope": {"$eq": "global"},
            })
            add(eq, "G:semantic", self.GR_UNFILTERED_TOP_K, None)

        results = await asyncio.gather(*tasks)

        per_query_scores: Dict[str, float] = {eq: 0.0 for eq in enriched_queries}
        for (eq, _lane), result_list in zip(task_meta, results, strict=True):
            best = max((c.get('score', 0) for c in result_list), default=0)
            if best > per_query_scores[eq]:
                per_query_scores[eq] = round(best, 4)

        chunks = self._merge_and_rank_chunks(*results)

        lane_counts = {}
        for (_eq, lane), result_list in zip(task_meta, results, strict=True):
            lane_counts[lane] = lane_counts.get(lane, 0) + len(result_list)

        top_score = chunks[0].get('score', 0) if chunks else 0
        if len(chunks) < self.GR_FALLBACK_MIN_CHUNKS or top_score < self.GR_FALLBACK_MIN_SCORE:
            logger.info(
                f"GR fallback triggered: {len(chunks)} chunks, top_score={top_score:.3f}"
            )
            # Fallback H filters only by plan_type (no record_keeper). When the
            # RK lanes were skipped for a global-only topic, also pin scope=global
            # so this last resort cannot reintroduce RK-specific articles.
            fallback_filter = {"plan_type": plan_filter}
            if skip_rk_lanes:
                fallback_filter["scope"] = {"$eq": "global"}
            fallback_tasks = [
                self._cached_query(eq, top_k=15, filter_dict=fallback_filter)
                for eq in enriched_queries
            ]
            fallback_results = await asyncio.gather(*fallback_tasks)
            for eq, result_list in zip(
                enriched_queries, fallback_results, strict=True
            ):
                best = max((c.get('score', 0) for c in result_list), default=0)
                if best > per_query_scores[eq]:
                    per_query_scores[eq] = round(best, 4)
            chunks = self._merge_and_rank_chunks(chunks, *fallback_results)
            lane_counts["H:fallback"] = sum(len(r) for r in fallback_results)

        total_calls = len(tasks) + (len(enriched_queries) if "H:fallback" in lane_counts else 0)

        if not chunks:
            logger.warning("generate_response: no results from parallel cascade search")
        else:
            unique_articles = len(set(
                c['metadata'].get('article_id') for c in chunks
            ))
            logger.info(
                f"generate_response (parallel_cascade): {len(chunks)} chunks from "
                f"{unique_articles} articles ({total_calls} Pinecone calls, lanes={lane_counts})"
            )

        return chunks, per_query_scores

    async def _search_with_topic_strategies(
        self,
        enriched_query: str,
        base_filters: Dict[str, Any],
        topic: str
    ) -> List[Dict[str, Any]]:
        """
        Apply topic-filtering strategies within a given base filter set.

        Strategy 1: Topic exact match
        Strategy 2: Tags filter (with case variations)
        Strategy 3: No topic filter (base filters only)

        Returns the first sufficient result set, or the last attempt.
        """
        if topic:
            # Strategy 1: Topic exact match
            topic_filters = {**base_filters, "topic": {"$eq": topic}}
            chunks = await self._cached_query(
                enriched_query, top_k=30, filter_dict=topic_filters
            )

            if self._topic_results_sufficient(chunks):
                logger.debug(f"Topic strategy 1 (exact): {len(chunks)} chunks")
                return chunks

            # Strategy 2: Tags filter
            topic_variations = self._get_topic_variations(topic)
            tags_filters = {**base_filters, "tags": {"$in": topic_variations}}
            chunks = await self._cached_query(
                enriched_query, top_k=30, filter_dict=tags_filters
            )

            if self._topic_results_sufficient(chunks):
                logger.debug(f"Topic strategy 2 (tags): {len(chunks)} chunks")
                return chunks

        # Strategy 3: No topic filter
        chunks = await self._cached_query(
            enriched_query, top_k=30, filter_dict=base_filters
        )
        logger.debug(f"Topic strategy 3 (no topic): {len(chunks)} chunks")
        return chunks

    def _topic_results_sufficient(self, chunks: List[Dict[str, Any]]) -> bool:
        """
        Evalúa si los resultados filtrados por topic son suficientes.

        Retorna False (trigger fallback) si:
        - Hay menos de TOPIC_FILTER_MIN_CHUNKS resultados
        - El mejor score está por debajo de TOPIC_FILTER_MIN_SCORE
        """
        if len(chunks) < self.TOPIC_FILTER_MIN_CHUNKS:
            return False

        if chunks and chunks[0].get('score', 0) < self.TOPIC_FILTER_MIN_SCORE:
            return False

        return True

    # ========================================================================
    # Helper Methods - Query Decomposition
    # ========================================================================

    async def _decompose_question(
        self,
        question: str,
        record_keeper: str = "",
        topic: str = "",
    ) -> List[str]:
        """
        Decompose a multi-part question into focused sub-queries for parallel search.

        Uses a lightweight LLM call to identify distinct 401(k) concepts in the
        question. Falls back to the original question on any error.

        Fix 7: optional ``record_keeper`` / ``topic`` anchor sub-query
        generation to the caller's scope. Callers that don't pass them get
        the legacy behaviour, so generate_response/knowledge_question paths
        are unchanged.

        Returns:
            List of 1-3 focused sub-queries
        """
        try:
            system_prompt, user_prompt = build_decompose_question_prompt(
                question,
                record_keeper=record_keeper,
                topic=topic,
            )
            llm_result = await self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=150,
                task_type="decompose",
            )
            parsed = json.loads(llm_result.content)
            sub_queries = parsed.get("sub_queries", [])

            if not sub_queries or not isinstance(sub_queries, list):
                return [question]

            return [sq for sq in sub_queries[:3] if isinstance(sq, str) and sq.strip()]
        except Exception as exc:
            logger.warning(
                "Question decomposition failed; using original "
                "(error_type=%s)",
                type(exc).__name__,
            )
            return [question]

    # ========================================================================
    # Helper Methods - Contexto
    # ========================================================================

    def _build_context_from_chunks(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        prioritize_types: Optional[List[str]] = None
    ) -> tuple:
        """
        Construye contexto desde chunks, priorizando ciertos tipos.

        Returns:
            (context_string, selected_chunks, tokens_used)
        """
        if prioritize_types:
            # Reordenar chunks priorizando tipos específicos
            priority_chunks = [
                c for c in chunks
                if c['metadata'].get('chunk_type') in prioritize_types
            ]
            other_chunks = [
                c for c in chunks
                if c['metadata'].get('chunk_type') not in prioritize_types
            ]
            ordered_chunks = priority_chunks + other_chunks
        else:
            ordered_chunks = chunks

        # Agregar chunks hasta llenar presupuesto
        # Usa continue en vez de break para no descartar chunks pequeños
        # que podrían caber después de uno grande que no cupo
        selected = []
        tokens_used = 0

        for chunk in ordered_chunks:
            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)

            if tokens_used + chunk_tokens <= budget:
                selected.append(chunk)
                tokens_used += chunk_tokens
            # Si no cabe, seguir intentando con el próximo chunk (puede ser más pequeño)

        # Formatear como contexto
        context_parts = []
        for i, chunk in enumerate(selected, 1):
            content = chunk['metadata'].get('content', '')
            chunk_type = chunk['metadata'].get('chunk_type', 'unknown')
            context_parts.append(f"--- Section {i} ({chunk_type}) ---\n{content}\n")

        context = "\n".join(context_parts)
        return context, selected, tokens_used

    def _build_required_data_context_primary_first(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        max_per_article: int,
        primary_article_id: str,
        max_secondary_articles: int = 2,
        max_chunks_per_secondary: int = 1,
    ) -> tuple:
        """
        Fix 5: Build the required_data context with a dominant primary article.

        Order of filling:
          1. All eligible chunks from the primary article (rdmh first, then
             eligibility / business_rules) up to ``max_per_article``.
          2. At most ``max_chunks_per_secondary`` chunks from each of the next
             ``max_secondary_articles`` distinct articles, prioritising rdmh.

        Returns ``(context_string, selected_chunks, tokens_used)`` in the same
        shape as ``_build_context_with_diversity``.
        """
        if not chunks:
            return "", [], 0

        type_priority = {
            "required_data_must_have": 0,
            "eligibility": 1,
            "business_rules": 2,
        }

        def _type_key(chunk: Dict[str, Any]) -> int:
            return type_priority.get(
                (chunk.get("metadata") or {}).get("chunk_type"),
                99,
            )

        primary_chunks = sorted(
            (
                c for c in chunks
                if (c.get("metadata") or {}).get("article_id")
                == primary_article_id
            ),
            key=lambda c: (_type_key(c), -c.get("score", 0)),
        )

        selected: List[Dict[str, Any]] = []
        selected_ids: set = set()
        tokens_used = 0

        for chunk in primary_chunks[:max_per_article]:
            content = (chunk.get("metadata") or {}).get("content", "")
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens > budget:
                continue
            selected.append(chunk)
            selected_ids.add(chunk.get("id"))
            tokens_used += chunk_tokens

        secondaries_seen: List[str] = []
        secondary_counts: Dict[str, int] = defaultdict(int)
        for chunk in chunks:
            cid = chunk.get("id")
            if cid in selected_ids:
                continue
            aid = (chunk.get("metadata") or {}).get("article_id")
            if not aid or aid == primary_article_id:
                continue
            if aid not in secondaries_seen:
                if len(secondaries_seen) >= max_secondary_articles:
                    continue
                secondaries_seen.append(aid)
            if secondary_counts[aid] >= max_chunks_per_secondary:
                continue
            content = (chunk.get("metadata") or {}).get("content", "")
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens > budget:
                continue
            selected.append(chunk)
            selected_ids.add(cid)
            secondary_counts[aid] += 1
            tokens_used += chunk_tokens

        selected.sort(key=lambda c: c.get("score", 0), reverse=True)

        logger.info(
            "Primary-first context: %d chunks, %d secondary articles, "
            "%d tokens",
            len(selected),
            len(secondaries_seen),
            tokens_used,
        )

        context_parts = []
        for i, chunk in enumerate(selected, 1):
            meta = chunk.get("metadata") or {}
            content = meta.get("content", "")
            chunk_type = meta.get("chunk_type", "unknown")
            article_title = meta.get("article_title", "")
            context_parts.append(
                f"--- Section {i} ({chunk_type} | Source: {article_title}) ---\n{content}\n"
            )
        return "\n".join(context_parts), selected, tokens_used

    def _build_context_with_diversity(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        prioritize_types: Optional[List[str]] = None,
        max_per_article: int = 6,
        enforce_type_order: bool = False,
    ) -> tuple:
        """
        Build context ensuring representation from multiple articles.

        Uses a two-phase approach:
        Phase 1: Include the best chunk from each unique article (guarantees diversity).
        Phase 2: Fill remaining budget by type-priority ordering with a per-article cap.

        This prevents a single dominant article from consuming all context budget,
        which is critical for cross-article questions.

        Args:
            chunks: Ranked chunks (best score first)
            budget: Token budget for context
            prioritize_types: Chunk types to prioritize in phase 2
            max_per_article: Maximum chunks from any single article

        Returns:
            (context_string, selected_chunks, tokens_used)
        """
        if not chunks:
            return "", [], 0

        # Type-based ordering for phase 2
        if prioritize_types:
            priority = [
                c for c in chunks
                if c['metadata'].get('chunk_type') in prioritize_types
            ]
            other = [
                c for c in chunks
                if c['metadata'].get('chunk_type') not in prioritize_types
            ]
            type_ordered = priority + other
        else:
            type_ordered = list(chunks)

        def selection_key(chunk: Dict[str, Any]) -> tuple:
            kind = chunk['metadata'].get('chunk_type')
            rank = (prioritize_types.index(kind)
                    if prioritize_types and kind in prioritize_types else 99)
            return (rank if enforce_type_order else 0, -chunk.get('score', 0))

        if enforce_type_order:
            # Required-data contracts must survive the per-article cap even
            # when a short business rule has a higher semantic-search score.
            type_ordered.sort(key=selection_key)

        # ── Phase 1: Best chunk from each article ──
        article_best: Dict[str, Dict[str, Any]] = {}
        for chunk in chunks:
            aid = chunk['metadata'].get('article_id', 'unknown')
            if aid not in article_best or selection_key(chunk) < selection_key(article_best[aid]):
                article_best[aid] = chunk

        selected = []
        selected_ids: set = set()
        tokens_used = 0
        article_counts: Dict[str, int] = defaultdict(int)

        for _aid, chunk in sorted(
            article_best.items(),
            key=lambda x: x[1].get('score', 0),
            reverse=True
        ):
            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens <= budget:
                selected.append(chunk)
                selected_ids.add(chunk.get('id'))
                tokens_used += chunk_tokens
                aid = chunk['metadata'].get('article_id', 'unknown')
                article_counts[aid] += 1

        logger.debug(
            f"Diversity phase 1: {len(selected)} chunks from "
            f"{len(article_counts)} articles, {tokens_used} tokens"
        )

        # ── Phase 2: Fill remaining budget by type priority ──
        for chunk in type_ordered:
            cid = chunk.get('id')
            if cid in selected_ids:
                continue
            aid = chunk['metadata'].get('article_id', 'unknown')
            if article_counts[aid] >= max_per_article:
                continue
            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens <= budget:
                selected.append(chunk)
                selected_ids.add(cid)
                tokens_used += chunk_tokens
                article_counts[aid] += 1

        # Sort selected by score for consistent context ordering
        selected.sort(key=lambda c: c.get('score', 0), reverse=True)

        logger.debug(
            f"Diversity phase 2 total: {len(selected)} chunks from "
            f"{len(article_counts)} articles, {tokens_used} tokens"
        )

        # Format context with article attribution
        context_parts = []
        for i, chunk in enumerate(selected, 1):
            content = chunk['metadata'].get('content', '')
            chunk_type = chunk['metadata'].get('chunk_type', 'unknown')
            article_title = chunk['metadata'].get('article_title', '')
            context_parts.append(
                f"--- Section {i} ({chunk_type} | Source: {article_title}) ---\n{content}\n"
            )

        context = "\n".join(context_parts)
        return context, selected, tokens_used

    def _build_exact_procedure_context(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        max_per_article: int,
        retrieval_profile: Dict[str, Any],
    ) -> tuple:
        primary_article_id = retrieval_profile.get("primary_article_id")
        excluded_articles = set(retrieval_profile.get("excluded_articles", []))
        include_references = bool(retrieval_profile.get("include_references"))
        allowed_chunk_types = self._exact_context_chunk_types_for_profile(
            retrieval_profile
        )

        def allowed(chunk: Dict[str, Any]) -> bool:
            meta = chunk.get("metadata", {})
            article_id = meta.get("article_id")
            chunk_type = meta.get("chunk_type")
            if article_id in excluded_articles:
                return False
            if chunk_type == "references" and not include_references:
                return False
            if chunk_type not in allowed_chunk_types:
                return False
            return True

        primary_chunks = [
            chunk for chunk in chunks
            if chunk.get("metadata", {}).get("article_id") == primary_article_id
            and allowed(chunk)
        ]
        candidate_chunks = primary_chunks or [
            chunk for chunk in chunks if allowed(chunk)
        ]

        candidate_chunks.sort(
            key=lambda chunk: self._exact_context_sort_key(
                chunk,
                retrieval_profile,
            )
        )

        selected: List[Dict[str, Any]] = []
        selected_ids: set = set()
        tokens_used = 0
        article_counts: Dict[str, int] = defaultdict(int)

        def try_add(chunk: Dict[str, Any], article_cap: int) -> bool:
            nonlocal tokens_used
            cid = chunk.get("id")
            if cid in selected_ids:
                return True
            article_id = chunk.get("metadata", {}).get("article_id", "unknown")
            if article_counts[article_id] >= article_cap:
                return False
            content = chunk.get("metadata", {}).get("content", "")
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens > budget:
                return False
            selected.append(chunk)
            selected_ids.add(cid)
            article_counts[article_id] += 1
            tokens_used += chunk_tokens
            return True

        primary_cap = max(max_per_article, self.GR_DOMINANCE_MAX_CHUNKS_SINGLE)
        if retrieval_profile.get("inquiry_intent") == "informational_options":
            primary_cap = max(primary_cap, 17)
        for chunk in candidate_chunks:
            article_cap = primary_cap if (
                chunk.get("metadata", {}).get("article_id") == primary_article_id
            ) else max_per_article
            try_add(chunk, article_cap=article_cap)

        context_parts = []
        for i, chunk in enumerate(selected, 1):
            content = chunk['metadata'].get('content', '')
            chunk_type = chunk['metadata'].get('chunk_type', 'unknown')
            article_title = chunk['metadata'].get('article_title', '')
            context_parts.append(
                f"--- Section {i} ({chunk_type} | Source: {article_title}) ---\n{content}\n"
            )

        top_signal = sum(
            sorted(
                [
                    chunk.get("score", 0.0)
                    for chunk in selected
                    if chunk.get("metadata", {}).get("article_id") == primary_article_id
                ],
                reverse=True,
            )[:3]
        )
        dominance_info = {
            "dominant_mode": True,
            "top_article_id": primary_article_id,
            "top_signal": round(top_signal, 3),
            "runner_up_signal": 0.0,
            "ratio": 0.0,
            "concept_quotas_applied": [],
        }
        return "\n".join(context_parts), selected, tokens_used, dominance_info

    def _build_context_with_diversity_and_tiers(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        max_per_article: int = 6,
        advisory_signal: Optional[Dict[str, Any]] = None,
        retrieval_profile: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """
        Build context adaptively: focus on a dominant article when one
        clearly outscores the rest; otherwise enforce multi-article diversity.

        Dominance is decided from per-article signal (sum of top-3 chunk scores).
        If advisory concepts are present, reserve a small quota for each
        mandatory concept before filling the remaining budget by score/tier.
        Multi-article mode (existing behavior): Phase 1 best-chunk-per-article,
        then tier-priority fill with per-article cap.
        Dominant mode: saturate with the top article's chunks plus a single
        insurance chunk from the runner-up after concept quotas are satisfied.

        Returns:
            (context_string, selected_chunks, tokens_used, dominance_info)
            where dominance_info is a dict with keys:
              dominant_mode, top_article_id, top_signal, runner_up_signal, ratio
        """
        if not chunks:
            return "", [], 0, {
                "dominant_mode": False,
                "top_article_id": None,
                "top_signal": 0.0,
                "runner_up_signal": 0.0,
                "ratio": 0.0,
                "concept_quotas_applied": [],
            }

        advisory_signal = advisory_signal or {}
        retrieval_profile = retrieval_profile or {}
        if retrieval_profile.get("mode") == "exact_procedure":
            return self._build_exact_procedure_context(
                chunks=chunks,
                budget=budget,
                max_per_article=max_per_article,
                retrieval_profile=retrieval_profile,
            )

        # ── Compute per-article signal (sum of top-3 chunk scores) ──
        article_scores: Dict[str, List[float]] = defaultdict(list)
        for c in chunks:
            aid = c['metadata'].get('article_id', 'unknown')
            article_scores[aid].append(c.get('score', 0))

        article_signal: Dict[str, float] = {
            aid: sum(sorted(scores, reverse=True)[:3])
            for aid, scores in article_scores.items()
        }
        sorted_aids = sorted(article_signal, key=article_signal.get, reverse=True)
        top_aid = sorted_aids[0]
        top_signal = article_signal[top_aid]
        runner_up_signal = article_signal[sorted_aids[1]] if len(sorted_aids) > 1 else 0.0
        ratio = (runner_up_signal / top_signal) if top_signal > 0 else 1.0

        dominant_mode = (
            self.GR_DOMINANCE_ENABLED
            and top_signal >= self.GR_DOMINANCE_MIN_TOP_SIGNAL
            and ratio <= self.GR_DOMINANCE_MAX_RATIO
            and len(article_scores[top_aid]) >= self.GR_DOMINANCE_MIN_CHUNKS
        )
        logger.info(
            "Dominance: top_signal=%.2f runner_up=%.2f ratio=%.2f mode=%s",
            top_signal,
            runner_up_signal,
            ratio,
            "dominant" if dominant_mode else "multi_article",
        )

        selected: List[Dict[str, Any]] = []
        selected_ids: set = set()
        tokens_used = 0
        article_counts: Dict[str, int] = defaultdict(int)
        concept_quotas_applied: List[str] = []

        def try_add_chunk(chunk: Dict[str, Any], article_cap: int = max_per_article) -> bool:
            nonlocal tokens_used
            cid = chunk.get('id')
            if cid in selected_ids:
                return True

            aid = chunk['metadata'].get('article_id', 'unknown')
            if article_counts[aid] >= article_cap:
                return False

            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens > budget:
                return False

            selected.append(chunk)
            selected_ids.add(cid)
            tokens_used += chunk_tokens
            article_counts[aid] += 1
            return True

        # ── Phase 0: mandatory concept quotas ──
        required_concepts = [
            concept for concept in self._ordered_unique(
                advisory_signal.get("detected_concepts", [])
                + advisory_signal.get("alternative_concepts", [])
            )
            if concept in self.GR_CONTEXT_QUOTA_CONCEPTS
        ]
        for concept in required_concepts:
            concept_chunks = [
                chunk for chunk in chunks
                if self._chunk_matches_response_concepts(chunk, [concept])
            ]
            concept_chunks.sort(
                key=lambda c: self._response_chunk_rank_score(
                    c,
                    advisory_signal,
                    concept,
                ),
                reverse=True,
            )
            added_for_concept = 0
            for chunk in concept_chunks:
                if added_for_concept >= self.GR_CONTEXT_MIN_CHUNKS_PER_CONCEPT:
                    break
                already_selected = chunk.get('id') in selected_ids
                if try_add_chunk(chunk):
                    added_for_concept += 0 if already_selected else 1
                    if concept not in concept_quotas_applied:
                        concept_quotas_applied.append(concept)

        logger.debug(
            "Concept quota phase: %d chunks for %d concepts, %d tokens",
            len(selected),
            len(concept_quotas_applied),
            tokens_used,
        )

        if dominant_mode:
            # ── Dominant mode: saturate with top article + 1 insurance from runner-up ──
            top_chunks = sorted(
                [c for c in chunks if c['metadata'].get('article_id') == top_aid],
                key=lambda x: x.get('score', 0),
                reverse=True
            )
            for chunk in top_chunks:
                if article_counts[top_aid] >= self.GR_DOMINANCE_MAX_CHUNKS_SINGLE:
                    break
                try_add_chunk(chunk, article_cap=self.GR_DOMINANCE_MAX_CHUNKS_SINGLE)

            if self.GR_DOMINANCE_SECONDARY_SLOT > 0 and len(sorted_aids) > 1:
                runner_aid = sorted_aids[1]
                runner_chunks = sorted(
                    [c for c in chunks if c['metadata'].get('article_id') == runner_aid],
                    key=lambda x: x.get('score', 0),
                    reverse=True
                )[:self.GR_DOMINANCE_SECONDARY_SLOT]
                for chunk in runner_chunks:
                    try_add_chunk(chunk)

            logger.debug(
                f"Dominant-mode context: {len(selected)} chunks from "
                f"{len(article_counts)} articles, {tokens_used} tokens"
            )
        else:
            # ── Multi-article mode: Phase 1 diversity + Phase 2 tier fill ──
            article_best: Dict[str, Dict[str, Any]] = {}
            for chunk in chunks:
                aid = chunk['metadata'].get('article_id', 'unknown')
                if aid not in article_best or chunk.get('score', 0) > article_best[aid].get('score', 0):
                    article_best[aid] = chunk

            for _aid, chunk in sorted(
                article_best.items(),
                key=lambda x: x[1].get('score', 0),
                reverse=True
            ):
                try_add_chunk(chunk)

            logger.debug(
                f"Diversity+tiers phase 1: {len(selected)} chunks from "
                f"{len(article_counts)} articles, {tokens_used} tokens"
            )

            by_tier = self._organize_chunks_by_tier(chunks)
            for tier in ['critical', 'high', 'medium', 'low']:
                for chunk in by_tier.get(tier, []):
                    cid = chunk.get('id')
                    if cid in selected_ids:
                        continue
                    try_add_chunk(chunk)

            logger.debug(
                f"Diversity+tiers phase 2 total: {len(selected)} chunks from "
                f"{len(article_counts)} articles, {tokens_used} tokens"
            )

        selected.sort(key=lambda c: c.get('score', 0), reverse=True)

        context_parts = []
        for i, chunk in enumerate(selected, 1):
            content = chunk['metadata'].get('content', '')
            chunk_type = chunk['metadata'].get('chunk_type', 'unknown')
            article_title = chunk['metadata'].get('article_title', '')
            context_parts.append(
                f"--- Section {i} ({chunk_type} | Source: {article_title}) ---\n{content}\n"
            )

        context = "\n".join(context_parts)
        dominance_info = {
            "dominant_mode": dominant_mode,
            "top_article_id": top_aid,
            "top_signal": round(top_signal, 3),
            "runner_up_signal": round(runner_up_signal, 3),
            "ratio": round(ratio, 3),
            "concept_quotas_applied": concept_quotas_applied,
        }
        return context, selected, tokens_used, dominance_info

    def _organize_chunks_by_tier(
        self,
        chunks: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Organiza chunks por tier para priorización."""
        by_tier = {
            'critical': [],
            'high': [],
            'medium': [],
            'low': []
        }

        for chunk in chunks:
            tier = chunk['metadata'].get('chunk_tier', 'low')
            if tier in by_tier:
                # Agregar content al chunk para fácil acceso
                chunk_with_content = {
                    'content': chunk['metadata'].get('content', ''),
                    'metadata': chunk['metadata'],
                    'id': chunk['id'],
                    'score': chunk['score']
                }
                by_tier[tier].append(chunk_with_content)

        return by_tier

    # ========================================================================
    # Helper Methods - LLM
    # ========================================================================

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        task_type: str,
    ) -> LLMResponse:
        """
        Dispatch an LLM call through the `LLMRouter` by task type.

        The router picks the configured primary model for `task_type` and
        falls back to the secondary provider on any exception. Provider-
        specific concerns (GPT-5 reasoning budget, Gemini thinking config,
        empty-content retry) live inside the router.

        Args:
            system_prompt: System prompt.
            user_prompt: User prompt.
            max_tokens: Max output tokens for the generated content (the
                router scales this for GPT-5 reasoning headroom).
            task_type: One of "decompose", "required_data", "gr_outcome",
                "gr_response", "knowledge_question".

        Returns:
            LLMResponse with content, usage, provider_used, model_used.

        Raises:
            LLMEmptyResponseError: If the LLM returns empty content after
                all retry and fallback attempts.
        """
        return await self.router.call(
            task_type=task_type,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )

    # ========================================================================
    # Helper Methods - Confidence & Decision
    # ========================================================================

    def _check_topic_relevance(
        self,
        chunks: List[Dict[str, Any]],
        query_topic: str
    ) -> bool:
        """
        Verifica si el topic del query es relevante al artículo encontrado.

        Compara el query_topic contra el topic, tags y subtopics del artículo.
        Usa substring matching case-insensitive para manejar variaciones
        como "hardship" vs "hardship_withdrawal" o "rollover" vs "Rollover".

        Args:
            chunks: Chunks encontrados (usa metadata del primero)
            query_topic: Topic del request (ej: "rollover", "hardship")

        Returns:
            True si el topic es relevante al artículo
        """
        if not query_topic or not chunks:
            return False

        query_lower = query_topic.lower()
        meta = chunks[0].get('metadata', {})

        # Check 1: ¿El query topic es substring del topic del artículo?
        # Ej: "hardship" está en "hardship_withdrawal"
        article_topic = (meta.get('topic') or '').lower()
        if query_lower in article_topic:
            return True

        # Check 2: ¿El query topic coincide con algún tag?
        # Ej: "rollover" coincide con tag "Rollover"
        for tag in meta.get('tags', []):
            if query_lower in tag.lower():
                return True

        # Check 3: ¿El query topic coincide con algún subtopic?
        # Ej: "fees" coincide con subtopic "fees"
        for subtopic in meta.get('subtopics', []):
            if query_lower in subtopic.lower():
                return True

        return False

    def _calculate_required_data_confidence(
        self,
        chunks: List[Dict[str, Any]],
        query_topic: str,
        coverage_gaps: Optional[List[str]] = None
    ) -> float:
        """
        Calcula confidence para el endpoint /required-data.

        Combina tres tipos de señales más un LLM gap override:

        1. Retrieval + Topic (55%): ¿Encontramos el chunk correcto Y del topic correcto?
        2. Soporte contextual (10%): ¿Hay chunks critical y suficiente contexto?
        3. Similitud semántica (35%): ¿Qué tan bien alinea el query con los chunks?
        4. LLM coverage gaps: Caps confidence when gaps are reported.
        """
        if not chunks:
            return 0.0

        # === Componente 1: Must Have + Topic Match (55%) ===
        retrieval_score = 0.0

        has_must_have = any(
            c['metadata'].get('chunk_type') == 'required_data_must_have'
            for c in chunks
        )

        topic_matched = self._check_topic_relevance(chunks, query_topic)

        if has_must_have:
            if topic_matched:
                retrieval_score += 0.50
            else:
                retrieval_score += 0.15

        # === Componente 2: Soporte Contextual (10%) ===
        critical_count = sum(
            1 for c in chunks
            if c['metadata'].get('chunk_tier') == 'critical'
        )
        retrieval_score += 0.05 * min(1.0, critical_count / 3)
        retrieval_score += 0.05 * min(1.0, len(chunks) / 5)

        # === Componente 3: Similitud Semántica (35%) ===
        top_scores = [c['score'] for c in chunks[:3]]
        avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
        similarity_score = avg_score * 0.35

        confidence = retrieval_score + similarity_score

        # === Componente 4: LLM coverage gap override ===
        coverage_gaps = coverage_gaps or []
        n_gaps = len(coverage_gaps)
        if n_gaps >= 2:
            confidence = min(confidence, 0.40)
        elif n_gaps == 1:
            confidence = min(confidence, 0.60)

        logger.info(
            f"Required data confidence: {confidence:.3f} "
            f"(retrieval={retrieval_score:.3f}, similarity={similarity_score:.3f}, "
            f"must_have={'yes' if has_must_have else 'no'}, "
            f"topic_matched={'yes' if topic_matched else 'no'}, "
            f"critical_chunks={critical_count}, total_chunks={len(chunks)}, "
            f"coverage_gaps={n_gaps})"
        )

        return round(min(1.0, confidence), 3)

    # Chunk types that carry high structural value for generate_response
    _GR_HIGH_VALUE_TYPES = frozenset({
        'decision_guide', 'business_rules', 'eligibility',
        'required_data_must_have', 'response_frames'
    })

    def _calculate_confidence(
        self,
        chunks: List[Dict[str, Any]],
        coverage_gaps: Optional[List[str]] = None
    ) -> float:
        """
        Multi-signal confidence for generate_response.

        Combines three weighted components plus an LLM gap override:

        1. Semantic similarity (40%): avg of top-3 Pinecone scores.
        2. Structural signals  (35%): critical-tier presence, high-value
           chunk-type diversity, and evidence volume.
        3. LLM coverage gaps   (25%): positive boost when the LLM confirms
           zero gaps; reduced/zero when gaps are reported.

        Hard caps are applied when the LLM reports coverage gaps to prevent
        inflated confidence on incomplete retrievals.
        """
        if not chunks:
            return 0.0

        # === Component 1: Semantic Similarity (40%) ===
        top_scores = [chunk['score'] for chunk in chunks[:3]]
        avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
        similarity = avg_score * 0.40

        # === Component 2: Structural Signals (35%) ===
        structural = 0.0

        critical_count = sum(
            1 for chunk in chunks
            if chunk['metadata'].get('chunk_tier') == 'critical'
        )
        structural += 0.15 * min(1.0, critical_count / 2)

        types_present = set(
            c['metadata'].get('chunk_type') for c in chunks
        )
        type_coverage = len(types_present & self._GR_HIGH_VALUE_TYPES) / len(self._GR_HIGH_VALUE_TYPES)
        structural += 0.10 * type_coverage

        structural += 0.10 * min(1.0, len(chunks) / 5)

        # === Component 3: LLM Coverage Gap Signal (25%) ===
        coverage_gaps = coverage_gaps or []
        n_gaps = len(coverage_gaps)
        if n_gaps == 0:
            gap_score = 0.25
        elif n_gaps == 1:
            gap_score = 0.10
        elif n_gaps == 2:
            gap_score = 0.05
        else:
            gap_score = 0.0

        confidence = similarity + structural + gap_score

        # Hard caps from coverage gaps
        if n_gaps >= 3:
            confidence = min(confidence, 0.30)
        elif n_gaps == 2:
            confidence = min(confidence, 0.45)

        logger.info(
            f"generate_response confidence: {confidence:.3f} "
            f"(similarity={similarity:.3f}, structural={structural:.3f}, "
            f"gap_score={gap_score:.3f}, critical={critical_count}, "
            f"type_coverage={type_coverage:.2f}, chunks={len(chunks)}, "
            f"coverage_gaps={n_gaps})"
        )

        return round(min(1.0, confidence), 3)

    def _determine_decision(self, confidence: float) -> str:
        """
        Determina decision basado en confidence score.

        Returns:
            "can_proceed", "uncertain", o "out_of_scope"
        """
        if confidence >= 0.65:
            return "can_proceed"
        elif confidence >= 0.45:
            return "uncertain"
        else:
            return "out_of_scope"

    # ========================================================================
    # Helper Methods - Knowledge Question
    # ========================================================================

    # Chunk types that carry high informational value for knowledge answers
    _KQ_HIGH_VALUE_TYPES = frozenset({
        'business_rules', 'eligibility', 'steps', 'faqs', 'guardrails'
    })

    def _calculate_knowledge_confidence(
        self,
        selected_chunks: List[Dict[str, Any]],
        coverage_gaps: Optional[List[str]] = None
    ) -> str:
        """
        Calculate confidence_note for knowledge questions.

        Primary signal: LLM-reported coverage_gaps (topics the question asked
        about that the KB context does NOT cover). The LLM sees both the
        context and the question, so it reliably detects when the KB lacks
        information on a specific topic.

        Secondary signal (when no gaps reported): retrieval quality metrics
        (avg score, chunk type coverage) as a baseline.

        Returns:
            "well_covered", "partially_covered", or "limited_coverage"
        """
        if not selected_chunks:
            return "limited_coverage"

        coverage_gaps = coverage_gaps or []
        n_gaps = len(coverage_gaps)

        top_scores = [c.get('score', 0) for c in selected_chunks[:5]]
        avg_score = sum(top_scores) / len(top_scores)

        chunk_types_present = set(
            c['metadata'].get('chunk_type') for c in selected_chunks
        )
        covered_high_value = chunk_types_present & self._KQ_HIGH_VALUE_TYPES

        logger.info(
            "Knowledge confidence: avg_score=%.3f, high_value_types=%d/%d, "
            "coverage_gap_count=%d",
            avg_score,
            len(covered_high_value),
            len(self._KQ_HIGH_VALUE_TYPES),
            n_gaps,
        )

        # Primary signal: LLM-reported coverage gaps (core topics entirely absent)
        if n_gaps >= 3:
            return "limited_coverage"
        if n_gaps == 2:
            return "partially_covered"
        if n_gaps == 1:
            if avg_score >= 0.35 and len(covered_high_value) >= 3:
                return "partially_covered"
            return "limited_coverage"

        # No gaps reported — use retrieval quality as baseline
        if avg_score >= 0.35 and len(covered_high_value) >= 3:
            return "well_covered"
        elif avg_score >= 0.22 and len(covered_high_value) >= 2:
            return "partially_covered"
        else:
            return "limited_coverage"

    def _build_source_articles(
        self,
        selected_chunks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Build deduplicated source articles list from selected chunks.

        Groups by article_id (not chunk_id) so each article appears once,
        with a summary of which chunk types contributed.  All consulted
        articles are returned; ``used_info`` indicates whether the article
        had chunks with a score >= KQ_SOURCE_MIN_SCORE.
        """
        article_info: Dict[str, Dict[str, Any]] = {}

        for chunk in selected_chunks:
            score = chunk.get('score', 0)
            article_id = chunk['metadata'].get('article_id', '')
            if not article_id:
                continue

            if article_id in article_info:
                article_info[article_id]['types'].add(
                    chunk['metadata'].get('chunk_type', '')
                )
                article_info[article_id]['score'] = max(
                    article_info[article_id]['score'], score
                )
            else:
                article_info[article_id] = {
                    'article_title': chunk['metadata'].get('article_title', 'Unknown Article'),
                    'topic': chunk['metadata'].get('topic', ''),
                    'types': {chunk['metadata'].get('chunk_type', '')},
                    'score': score
                }

        source_articles = []
        for article_id, info in sorted(
            article_info.items(),
            key=lambda x: x[1]['score'],
            reverse=True
        ):
            types_str = ", ".join(sorted(info['types'] - {''}))
            used_info = info['score'] >= self.KQ_SOURCE_MIN_SCORE
            source_articles.append({
                "article_id": article_id,
                "article_title": info['article_title'],
                "chunk_types_used": types_str,
                "relevance": f"Covers {info['topic']} ({types_str})",
                "used_info": used_info,
                "max_score": round(info['score'], 4)
            })

        return source_articles

    CONTENT_PREVIEW_LENGTH = 200

    def _serialize_used_chunks(
        self,
        selected_chunks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Serialize selected chunks for the API response.

        Returns a lightweight representation of each chunk with a truncated
        content_preview and the full content for optional UI expansion.
        """
        serialized = []
        for chunk in selected_chunks:
            meta = chunk.get('metadata', {})
            full_content = meta.get('content', '')
            preview = full_content[:self.CONTENT_PREVIEW_LENGTH]
            if len(full_content) > self.CONTENT_PREVIEW_LENGTH:
                preview += '...'

            serialized.append({
                "chunk_id": chunk.get('id', ''),
                "score": round(chunk.get('score', 0), 4),
                "chunk_type": meta.get('chunk_type', 'unknown'),
                "chunk_tier": meta.get('chunk_tier', 'low'),
                "article_id": meta.get('article_id', ''),
                "article_title": meta.get('article_title', 'Unknown Article'),
                "content_preview": preview,
                "content": full_content
            })
        return serialized

    # ========================================================================
    # Helper Methods - Fallbacks
    # ========================================================================

    async def _augment_context_with_nice_to_have(
        self,
        inquiry: str,
        primary_article_id: Optional[str],
        context: str,
        selected_chunks: List[Dict[str, Any]],
        tokens_used: int,
    ) -> tuple:
        """Append the top article's ``required_data_nice_to_have`` chunk(s) to
        an already-built required_data context.

        The RD retrieval cascade filters exclusively on
        ``required_data_must_have``, so nice-to-have sections never reach the
        context on their own. This runs ONE extra filtered query against the
        primary (or top-scoring) article and appends the result — it never
        touches the must-have retrieval, gate, or ordering. Failures are
        swallowed: nice-to-have is enrichment, not a dependency.
        """
        article_id = primary_article_id
        if not article_id and selected_chunks:
            article_id = selected_chunks[0].get("metadata", {}).get("article_id")
        if not article_id:
            return context, selected_chunks, tokens_used

        try:
            nice_chunks = await self._cached_query(
                query_text=inquiry,
                top_k=2,
                filter_dict={
                    "article_id": {"$eq": article_id},
                    "chunk_type": {"$eq": "required_data_nice_to_have"},
                },
            )
        except Exception as exc:
            logger.warning(
                "nice_to_have augmentation query failed (error_type=%s)",
                type(exc).__name__,
            )
            return context, selected_chunks, tokens_used

        if not nice_chunks:
            return context, selected_chunks, tokens_used

        selected_ids = {
            c.get("id") or c.get("metadata", {}).get("chunk_id") for c in selected_chunks
        }
        section_idx = len(selected_chunks)
        parts = [context] if context else []
        for chunk in nice_chunks:
            md = chunk.get("metadata", {})
            chunk_id = chunk.get("id") or md.get("chunk_id")
            if chunk_id is not None and chunk_id in selected_ids:
                continue
            content = md.get("content", "")
            if not content:
                continue
            section_idx += 1
            parts.append(
                f"--- Section {section_idx} ({md.get('chunk_type', 'unknown')}) ---\n{content}\n"
            )
            selected_chunks.append(chunk)
            if chunk_id is not None:
                selected_ids.add(chunk_id)
            tokens_used += self.token_manager.count_tokens(content)

        return "\n".join(parts), selected_chunks, tokens_used

    @staticmethod
    def _required_data_from_chunks(
        chunks: List[Dict[str, Any]],
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Read the chunker's explicit field records, never infer from prose.

        A partial/malformed contract is not a successful empty extraction.
        Conversation-owned fields remain available as metadata but never
        become a portal scrape request. This is KB schema parsing, not a new
        source of participant facts.
        """
        result: Dict[str, Any] = {"participant_data": [], "plan_data": []}
        diagnostics: Dict[str, Any] = {
            "contract_status": "not_present", "conversation_fields": [],
            "field_provenance": [],
        }
        seen: Dict[tuple[str, str], Dict[str, Any]] = {}
        selected = [c for c in chunks if (c.get("metadata") or {}).get("chunk_type") in {
            "required_data_must_have", "required_data_nice_to_have",
        }]
        if not selected:
            return None, diagnostics
        sources = {
            "participant_profile": "participant_data", "participant_data": "participant_data",
            "plan_profile": "plan_data", "plan_data": "plan_data",
            "message_text": "conversation", "agent_input": "conversation",
        }
        for chunk in selected:
            md = chunk.get("metadata") or {}
            content = md.get("content")
            required = md.get("chunk_type") == "required_data_must_have"
            tier = "Must Have" if required else "Nice to Have"
            if not isinstance(content, str) or not re.match(
                rf"^# Required Data [—–-] {tier} \(", content
            ):
                diagnostics["contract_status"] = "schema_rejected"
                return None, diagnostics
            blocks = re.split(r"(?m)^### ", content)[1:]
            if not blocks:
                diagnostics["contract_status"] = "empty_extraction"
                return None, diagnostics
            for block in blocks:
                name, _, body = block.partition("\n")
                name = name.strip()
                attributes = dict(re.findall(
                    r"(?m)^\*\*(Description|Why needed|Source):\*\* ([^\n]+)$", body
                ))
                source = attributes.get("Source", "").strip()
                if (
                    not name or len(name) > 200
                    or set(attributes) != {"Description", "Why needed", "Source"}
                    or source not in sources
                    or any(not v.strip() or v.strip() in {"None", "null"} for v in attributes.values())
                ):
                    diagnostics["contract_status"] = "schema_rejected"
                    return None, diagnostics
                slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
                aliases = {
                    "participant_age_relative_to_59_5": "birth_date",
                    "participant_age_relative_to_595": "birth_date",
                    "outstanding_401_k_loan_status": "loan_history",
                }
                slug = aliases.get(slug, slug)
                if not slug:
                    diagnostics["contract_status"] = "schema_rejected"
                    return None, diagnostics
                if slug.endswith("_date"):
                    data_type = "date"
                elif slug.endswith("_balance") or slug in {"amount_needed", "amount_needed_for_the_hardship"}:
                    data_type = "currency"
                elif slug == "loan_history":
                    data_type = "list[text]"
                elif slug in {"crypto_enrollment", "active", "confirmation_of_review_of_hardship_distribution_guidelines_pdf"}:
                    data_type = "boolean"
                elif slug in {"maximum_number_of_loans", "minimum_age"}:
                    data_type = "number"
                else:
                    data_type = "text"
                field = {
                    "field": slug, "description": attributes["Description"].strip(),
                    "why_needed": attributes["Why needed"].strip(),
                    "data_type": data_type, "required": required,
                }
                category = sources[source]
                key = (category, slug)
                if key in seen:
                    seen[key]["required"] = seen[key]["required"] or required
                    continue
                provenance = {
                    "field": slug, "source": source,
                    "article_id": md.get("article_id"), "chunk_id": chunk.get("id"),
                }
                if category == "conversation":
                    field.update(provenance)
                    diagnostics["conversation_fields"].append(field)
                else:
                    result[category].append(field)
                    diagnostics["field_provenance"].append(provenance)
                seen[key] = field
        diagnostics["contract_status"] = "valid"
        diagnostics["field_count"] = len(seen)
        return result, diagnostics

    def _required_data_extraction_failure(self, content: str) -> Optional[str]:
        if not isinstance(content, str) or not content.strip():
            return "empty_extraction"
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return "invalid_json"
        if not isinstance(parsed, dict):
            return "schema_rejected"
        gaps = parsed.get("coverage_gaps", [])
        if self._should_retry_required_data(parsed, gaps, 1.0):
            # Validate again with a non-empty sentinel so schema and a valid
            # empty extraction have distinct diagnostics.
            nonempty = copy.deepcopy(parsed)
            for category in ("participant_data", "plan_data"):
                if nonempty.get(category) == []:
                    nonempty[category] = [{
                        "field": "contract_check", "description": "Schema check",
                        "why_needed": "Schema check", "data_type": "text", "required": True,
                    }]
            if self._should_retry_required_data(nonempty, gaps, 1.0):
                return "schema_rejected"
        if not parsed.get("participant_data") and not parsed.get("plan_data"):
            return "empty_extraction"
        if self._should_retry_required_data(parsed, gaps, 1.0):
            return "schema_rejected"
        return None

    def _parse_required_data_response(
        self, llm_response: str
    ) -> tuple[Dict[str, Any], List[str]]:
        """Parse the LLM JSON for required_data and return (parsed, coverage_gaps).

        Returns an empty-arrays default on JSON errors so callers can detect
        the empty state with the same code path used for valid-but-empty
        responses.
        """
        try:
            parsed = json.loads(llm_response)
        except json.JSONDecodeError:
            logger.error(
                "Required-data JSON parse failure (length=%d)",
                len(llm_response or ""),
            )
            parsed = {"participant_data": [], "plan_data": [], "coverage_gaps": []}

        if not isinstance(parsed, dict):
            parsed = {"participant_data": [], "plan_data": [], "coverage_gaps": []}
        coverage_gaps = parsed.get("coverage_gaps", [])
        if not isinstance(coverage_gaps, list):
            coverage_gaps = []
        coverage_gaps = [g for g in coverage_gaps if isinstance(g, str) and g.strip()]
        return parsed, coverage_gaps

    def _should_retry_required_data(
        self,
        parsed: Dict[str, Any],
        coverage_gaps: List[str],
        best_rdmh_score: float,
    ) -> bool:
        """True when the primary LLM returned an empty extraction despite the
        retrieval gate finding a relevant required_data_must_have chunk."""
        if best_rdmh_score < self.RD_RETRIEVAL_MIN_SCORE:
            return False
        allowed_top_level = {"participant_data", "plan_data", "coverage_gaps"}
        required_field_keys = {
            "field", "description", "why_needed", "data_type", "required",
        }
        allowed_data_types = {
            "text", "currency", "date", "boolean", "number",
            "list[text]", "list[currency]", "list[date]",
            "list[boolean]", "list[number]",
        }
        if (
            not isinstance(parsed, dict)
            or set(parsed) - allowed_top_level
            or "participant_data" not in parsed
            or "plan_data" not in parsed
        ):
            return True
        for category in ("participant_data", "plan_data"):
            fields = parsed.get(category)
            if not isinstance(fields, list):
                return True
            for field in fields:
                if (
                    not isinstance(field, dict)
                    or set(field) != required_field_keys
                    or any(
                        not isinstance(field.get(key), str)
                        or not field[key].strip()
                        for key in (
                            "field", "description", "why_needed", "data_type",
                        )
                    )
                    or field.get("data_type") not in allowed_data_types
                    or not isinstance(field.get("required"), bool)
                ):
                    return True
        raw_coverage_gaps = parsed.get("coverage_gaps", [])
        if (
            not isinstance(raw_coverage_gaps, list)
            or any(not isinstance(gap, str) for gap in raw_coverage_gaps)
        ):
            return True
        if coverage_gaps:
            return False
        arrays = [parsed["participant_data"], parsed["plan_data"]]
        return all(isinstance(v, list) and len(v) == 0 for v in arrays)

    def _build_empty_required_data_response(
        self,
        reason: str,
        *,
        retrieval_failure_kind: Optional[str] = None,
        retrieval_retryable: bool = False,
    ) -> RequiredDataResponse:
        """Construye respuesta vacía para required_data."""
        metadata: Dict[str, Any] = {
            "error": reason,
            "chunks_used": 0,
            "sub_queries": [],
            "per_query_scores": {},
            "unique_articles": 0,
            "relevant_articles": 0,
            "coverage_gaps": [],
        }
        reviewed_failure_kinds = {
            "timeout", "transport", "rate_limit", "server_error",
            "circuit_open", "client_error", "unknown",
        }
        if retrieval_failure_kind in reviewed_failure_kinds:
            metadata["retrieval_failure_kind"] = retrieval_failure_kind
            metadata["retrieval_retryable"] = (
                retrieval_retryable is True
                and retrieval_failure_kind in {
                    "timeout", "transport", "rate_limit", "server_error",
                    "circuit_open",
                }
            )
        return RequiredDataResponse(
            article_reference={
                "article_id": None,
                "title": None,
                "confidence": 0.0
            },
            required_fields={
                "participant_data": [],
                "plan_data": []
            },
            confidence=0.0,
            source_articles=[],
            used_chunks=[],
            coverage_gaps=[],
            metadata=metadata,
        )

    def _build_no_match_required_data_response(
        self,
        reason: str,
        confidence: float,
        source_articles: List[Dict[str, Any]],
        used_chunks: List[Dict[str, Any]],
        coverage_gaps: List[str],
        sub_queries: List[str],
        per_query_scores: Dict[str, float],
        tokens_used: int,
        llm_usage: Optional[Dict[str, int]] = None,
        llm_response: str = "",
        llm_model: Optional[str] = None,
        llm_provider: Optional[str] = None,
    ) -> RequiredDataResponse:
        """Build a no-match response that preserves diagnostic data.

        Unlike _build_empty_required_data_response (used for hard errors),
        this keeps source_articles, used_chunks, coverage_gaps, and query
        metadata so downstream systems and n8n can observe why no article
        matched and decide how to escalate.
        """
        total_articles = len(source_articles)
        relevant_articles = sum(
            1 for sa in source_articles if sa.get("used_info", False)
        )
        return RequiredDataResponse(
            article_reference={
                "article_id": None,
                "title": None,
                "confidence": confidence,
            },
            required_fields={"participant_data": [], "plan_data": []},
            confidence=confidence,
            source_articles=source_articles,
            used_chunks=used_chunks,
            coverage_gaps=coverage_gaps,
            metadata={
                "no_match_reason": reason,
                "chunks_used": len(used_chunks),
                "context_tokens": tokens_used,
                "response_tokens": (
                    self.token_manager.count_tokens(llm_response)
                    if llm_response else 0
                ),
                "prompt_tokens": llm_usage.get("prompt_tokens", 0) if llm_usage else 0,
                "completion_tokens": llm_usage.get("completion_tokens", 0) if llm_usage else 0,
                "total_tokens": llm_usage.get("total_tokens", 0) if llm_usage else 0,
                "model": llm_model,
                "provider": llm_provider,
                "sub_queries": sub_queries,
                "per_query_scores": per_query_scores,
                "unique_articles": total_articles,
                "relevant_articles": relevant_articles,
                "coverage_gaps": coverage_gaps,
            },
        )

    _LLM_RESPONSE_REQUIRED_KEYS = {"outcome", "response_to_participant"}

    def _gr_metadata(
        self,
        selected_chunks: List[Dict[str, Any]],
        tokens_used: int,
        llm_response: str,
        llm_usage: Optional[Dict[str, int]],
        total_inquiries: int,
        sub_queries: List[str],
        per_query_scores: Dict[str, float],
        enriched_queries: List[str],
        phase: str = "",
        llm_model: Optional[str] = None,
        llm_provider: Optional[str] = None,
        dominance_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a metadata dict for GenerateResponseResult."""
        source_articles = self._build_source_articles(selected_chunks)
        pqs = {
            sq: per_query_scores.get(eq, 0.0)
            for sq, eq in zip(sub_queries, enriched_queries, strict=False)
        }
        di = dominance_info or {}
        return {
            "chunks_used": len(selected_chunks),
            "context_tokens": tokens_used,
            "response_tokens": self.token_manager.count_tokens(llm_response) if llm_response else 0,
            "prompt_tokens": llm_usage.get("prompt_tokens", 0) if llm_usage else 0,
            "completion_tokens": llm_usage.get("completion_tokens", 0) if llm_usage else 0,
            "total_tokens": llm_usage.get("total_tokens", 0) if llm_usage else 0,
            "model": llm_model,
            "provider": llm_provider,
            "total_inquiries": total_inquiries,
            "sub_queries": sub_queries,
            "per_query_scores": pqs,
            "unique_articles": len(source_articles),
            "relevant_articles": sum(1 for sa in source_articles if sa.get("used_info", False)),
            "coverage_gaps": [],
            "phase": phase,
            "dominant_mode": di.get("dominant_mode", False),
            "dominance_top_signal": di.get("top_signal"),
            "dominance_runner_up_signal": di.get("runner_up_signal"),
            "dominance_ratio": di.get("ratio"),
            "concept_quotas_applied": di.get("concept_quotas_applied", []),
        }

    @staticmethod
    def _combine_llm_usage(
        usage1: Optional[Dict[str, int]],
        usage2: Optional[Dict[str, int]]
    ) -> Dict[str, int]:
        """Sum token counts from two LLM usage dicts."""
        combined: Dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v1 = (usage1 or {}).get(key, 0)
            v2 = (usage2 or {}).get(key, 0)
            combined[key] = v1 + v2
        return combined

    @staticmethod
    def _build_llm_fallback_parsed(reason: str) -> Dict[str, Any]:
        """Build a well-formed parsed dict when the LLM output is empty or incomplete."""
        return {
            "outcome": "blocked_missing_data",
            "outcome_reason": f"LLM response was empty or incomplete: {reason}",
            "response_to_participant": {
                "opening": "We were unable to generate a complete response for your inquiry. Please try again or contact Support.",
                "key_points": [],
                "steps": [],
                "warnings": []
            },
            "questions_to_ask": [],
            "escalation": {
                "needed": True,
                "reason": f"Automated response generation failed ({reason}). Please route to a human agent."
            },
            "guardrails_applied": [],
            "data_gaps": [reason],
            "coverage_gaps": []
        }

    @staticmethod
    def _build_llm_timeout_fallback(
        inquiry: str,
        selected_chunks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Build a degraded GR response when the LLM call times out.

        Summarises the retrieved context so the caller still gets
        actionable information instead of a blank failure.
        """
        key_points = []
        seen_articles = set()
        for chunk in selected_chunks[:6]:
            meta = chunk.get("metadata", {})
            article = meta.get("article_title", "")
            if article and article not in seen_articles:
                preview = meta.get("content", "")[:200].strip()
                if preview:
                    key_points.append(f"From \"{article}\": {preview}")
                    seen_articles.add(article)

        return {
            "outcome": "ambiguous_plan_rules",
            "outcome_reason": (
                "The response could not be fully generated because processing "
                "timed out. The information below is based on the retrieved "
                "knowledge-base context only."
            ),
            "response_to_participant": {
                "opening": (
                    "We found relevant information for your inquiry but were "
                    "unable to complete the full analysis in time. Below is a "
                    "summary of what the knowledge base contains. Please "
                    "contact ForUsAll Support for a complete answer."
                ),
                "key_points": key_points,
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Contact ForUsAll Support for a complete answer.",
                        "detail": "Email help@forusall.com or call 844-401-2253, Monday–Friday 7 AM–5 PM PT."
                    }
                ],
                "warnings": [
                    "This response was generated from retrieval context only "
                    "due to a processing timeout and may be incomplete."
                ]
            },
            "questions_to_ask": [],
            "escalation": {
                "needed": True,
                "reason": "LLM processing timed out; a human agent should review this inquiry."
            },
            "guardrails_applied": [
                "Response generated from retrieval context only due to processing timeout."
            ],
            "data_gaps": ["Full LLM analysis could not be completed within the time limit."],
            "coverage_gaps": []
        }

    def _build_uncertain_response(
        self,
        reason: str,
        confidence: float,
        error_type: Optional[str] = None,
        retrieval_failure_kind: Optional[str] = None,
        retrieval_retryable: bool = False,
    ) -> GenerateResponseResult:
        """Construye respuesta de fallback con el schema outcome-driven."""
        reason_messages = {
            "no_relevant_articles": (
                "No relevant knowledge-base articles were available."
            ),
            "generate_response_failed": (
                "Automated response generation could not be completed."
            ),
        }
        reason_code = (
            reason if reason in reason_messages else "generate_response_failed"
        )
        safe_reason = reason_messages[reason_code]
        metadata: Dict[str, Any] = {
            "error": reason_code,
            "chunks_used": 0,
            "sub_queries": [],
            "per_query_scores": {},
            "unique_articles": 0,
            "relevant_articles": 0,
            "coverage_gaps": [],
        }
        if error_type is not None:
            reviewed_error_types = {
                "PineconeRetrievalError", "PineconeCircuitOpen",
                "PineconeConnectionError", "PineconeTimeoutError",
                "TimeoutError", "ConnectionError", "RuntimeError",
                "ValueError", "TypeError", "Exception",
            }
            metadata["error_type"] = (
                error_type if error_type in reviewed_error_types else "Exception"
            )
        reviewed_failure_kinds = {
            "timeout", "transport", "rate_limit", "server_error",
            "circuit_open", "client_error", "unknown",
        }
        if retrieval_failure_kind in reviewed_failure_kinds:
            metadata["retrieval_failure_kind"] = retrieval_failure_kind
            metadata["retrieval_retryable"] = (
                retrieval_retryable is True
                and retrieval_failure_kind in {
                    "timeout", "transport", "rate_limit", "server_error",
                    "circuit_open",
                }
            )
        return GenerateResponseResult(
            decision="out_of_scope",
            confidence=confidence,
            response={
                "outcome": "blocked_missing_data",
                "outcome_reason": safe_reason,
                "response_to_participant": {
                    "opening": "We were unable to find sufficient information to address your inquiry.",
                    "key_points": [],
                    "steps": [],
                    "warnings": []
                },
                "questions_to_ask": [],
                "escalation": {
                    "needed": True,
                    "reason": "This inquiry may require human review."
                },
                "guardrails_applied": [],
                "data_gaps": [safe_reason],
                "coverage_gaps": []
            },
            source_articles=[],
            used_chunks=[],
            coverage_gaps=[],
            metadata=metadata,
        )


# ============================================================================
# Factory Function
# ============================================================================

def get_rag_engine(**kwargs) -> RAGEngine:
    """
    Factory function para obtener RAG engine.

    Args:
        **kwargs: Argumentos para RAGEngine

    Returns:
        RAGEngine instance
    """
    return RAGEngine(**kwargs)
