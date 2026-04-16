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
import hashlib
import logging
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from cachetools import TTLCache

from .pinecone_uploader import PineconeUploader
from .token_manager import TokenManager
from .llm_router import LLMRouter, LLMResponse, LLMEmptyResponseError
from collections import defaultdict

from .prompts import (
    build_required_data_prompt,
    build_generate_response_prompt,
    build_knowledge_question_prompt,
    build_decompose_question_prompt,
    build_gr_outcome_prompt,
    build_gr_response_prompt,
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
        logger.info(f"get_required_data() - Topic: {topic}, RK: {record_keeper}")
        
        try:
            # 1. Decompose inquiry into sub-queries
            sub_queries = await self._decompose_question(inquiry)
            enriched_queries = [f"{sq} {topic}" for sq in sub_queries]
            
            # Include original inquiry as fallback if decomposition didn't preserve it
            if inquiry not in sub_queries:
                enriched_queries.append(f"{inquiry} {topic}")
            
            logger.info(f"Decomposed into {len(sub_queries)} sub-queries: {sub_queries}")
            
            # 2. Parallel multi-query search with RK cascade
            chunks, per_query_scores = await self._search_for_required_data(
                enriched_queries=enriched_queries,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic
            )
            
            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_empty_required_data_response(
                    "No relevant articles found for this topic"
                )
            
            # 2b. Guard 1: Retrieval quality gate — skip LLM if no rdmh chunk is relevant
            best_rdmh_score = max(
                (c.get('score', 0) for c in chunks
                 if c['metadata'].get('chunk_type') == 'required_data_must_have'),
                default=0.0
            )
            if best_rdmh_score < self.RD_RETRIEVAL_MIN_SCORE:
                logger.info(
                    f"Retrieval quality gate: best rdmh score {best_rdmh_score:.4f} "
                    f"< threshold {self.RD_RETRIEVAL_MIN_SCORE}. Skipping LLM."
                )
                pqs = {sq: per_query_scores.get(eq, 0.0)
                       for sq, eq in zip(sub_queries, enriched_queries)}
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
            
            # 3. Build context with article diversity
            context, selected_chunks, tokens_used = self._build_context_with_diversity(
                chunks=chunks,
                budget=self.RD_CONTEXT_BUDGET,
                prioritize_types=['required_data_must_have', 'eligibility', 'business_rules'],
                max_per_article=self.RD_MAX_CHUNKS_PER_ARTICLE
            )
            
            logger.info(f"Context construido: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
            # 4. Generate prompts and call LLM
            system_prompt, user_prompt = build_required_data_prompt(
                context=context,
                inquiry=inquiry,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic
            )
            
            llm_usage = None
            llm_provider_used: Optional[str] = None
            llm_model_used: Optional[str] = None
            try:
                llm_result = await self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=800,
                    task_type="required_data",
                )
                llm_response = llm_result.content
                llm_usage = llm_result.usage
                llm_provider_used = llm_result.provider_used
                llm_model_used = llm_result.model_used
            except LLMEmptyResponseError as e:
                logger.error(f"LLM returned empty content in required_data: {e}")
                llm_response = json.dumps({"participant_data": [], "plan_data": [], "coverage_gaps": []})
            
            # 5. Parse LLM response + extract coverage gaps
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parseando JSON del LLM: {e}")
                logger.error(f"Respuesta: {llm_response[:500]}")
                parsed = {"participant_data": [], "plan_data": [], "coverage_gaps": []}
            
            coverage_gaps = parsed.get("coverage_gaps", [])
            if not isinstance(coverage_gaps, list):
                coverage_gaps = []
            coverage_gaps = [g for g in coverage_gaps if isinstance(g, str) and g.strip()]
            
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
                pqs = {sq: per_query_scores.get(eq, 0.0)
                       for sq, eq in zip(sub_queries, enriched_queries)}
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
            pqs = {sq: per_query_scores.get(eq, 0.0) for sq, eq in zip(sub_queries, enriched_queries)}
            
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
                    "coverage_gaps": coverage_gaps
                }
            )
        
        except Exception as e:
            logger.exception("Error en get_required_data")
            return self._build_empty_required_data_response(
                "An internal error occurred while determining required data"
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
        logger.info(f"generate_response() - Topic: {topic}, Budget: {max_response_tokens} tokens")

        try:
            # 1. Context budget with minimum floor
            context_budget = max(
                self.RESPONSE_MIN_CONTEXT_TOKENS,
                max_response_tokens - self.RESPONSE_MIN_TOKENS
            )
            
            logger.info(f"Context budget: {context_budget} tokens (de {max_response_tokens} total, reservando {self.RESPONSE_MIN_TOKENS} para response)")
            
            # 2. Decompose inquiry into sub-queries
            sub_queries = await self._decompose_question(inquiry)
            logger.info(f"Decomposed into {len(sub_queries)} sub-queries: {sub_queries}")
            
            enriched_queries = []
            for sq in sub_queries:
                parts = [sq, topic]
                if collected_data and "participant_data" in collected_data:
                    for key, value in list(collected_data["participant_data"].items())[:3]:
                        parts.append(f"{key}: {value}")
                enriched_queries.append(" ".join(parts))
            
            if inquiry not in sub_queries:
                fallback_parts = [inquiry, topic]
                enriched_queries.append(" ".join(fallback_parts))
            
            # 3. Simplified parallel search (KQ-style)
            chunks, per_query_scores = await self._search_for_response_simple(
                enriched_queries=enriched_queries,
                plan_type=plan_type,
            )
            
            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_uncertain_response(
                    "No relevant articles found for this topic",
                    confidence=0.0
                )
            
            # 4. Build context with diversity + tier priority
            context, selected_chunks, tokens_used = self._build_context_with_diversity_and_tiers(
                chunks=chunks,
                budget=context_budget,
                max_per_article=self.GR_MAX_CHUNKS_PER_ARTICLE
            )
            
            logger.info(f"Context: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
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
            )
            
            phase1_usage = None
            phase1_provider: Optional[str] = None
            phase1_model: Optional[str] = None
            outcome = None
            outcome_reason = None
            
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
                logger.info(f"Phase 1 outcome: {outcome} ({outcome_reason[:80]}...)")
            except asyncio.TimeoutError:
                logger.warning(f"Phase 1 timed out after {self.GR_PHASE1_TIMEOUT_SECONDS}s")
                return GenerateResponseResult(
                    decision="uncertain",
                    confidence=0.3,
                    response=self._build_llm_timeout_fallback(inquiry, selected_chunks),
                    source_articles=self._build_source_articles(selected_chunks),
                    used_chunks=self._serialize_used_chunks(selected_chunks),
                    coverage_gaps=[],
                    metadata=self._gr_metadata(
                        selected_chunks, tokens_used, "", None,
                        total_inquiries_in_ticket, sub_queries,
                        per_query_scores, enriched_queries,
                        phase="phase1_timeout",
                        llm_model=phase1_model,
                        llm_provider=phase1_provider,
                    ),
                )
            except (json.JSONDecodeError, LLMEmptyResponseError) as e:
                logger.error(f"Phase 1 failed ({type(e).__name__}): {e}")
                outcome = "ambiguous_plan_rules"
                outcome_reason = "Phase 1 outcome determination failed; proceeding with conservative outcome."
            
            phase1_elapsed = time.monotonic() - phase1_start
            logger.info(f"Phase 1 completed in {phase1_elapsed:.1f}s")
            
            # ── Fast path: out_of_scope_inquiry ──
            if outcome == "out_of_scope_inquiry":
                logger.info(f"Off-topic inquiry (fast path): {outcome_reason}")
                oos_parsed = {
                    "outcome": "out_of_scope_inquiry",
                    "outcome_reason": outcome_reason,
                    "response_to_participant": {
                        "opening": "I can only assist with retirement plan-related questions such as 401(k) distributions, rollovers, loans, and account access. Your inquiry appears to be outside this scope.",
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
                pqs = {sq: per_query_scores.get(eq, 0.0) for sq, eq in zip(sub_queries, enriched_queries)}
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
                        ),
                        "off_topic": True,
                    },
                )
            
            # ================================================================
            # 5b. Phase 2 — Response Generation
            # ================================================================
            completion_budget = max(self.RESPONSE_MIN_TOKENS, max_response_tokens - tokens_used)
            phase2_timeout = max(30, self.GR_LLM_TIMEOUT_SECONDS - phase1_elapsed - 5)
            
            p2_system, p2_user = build_gr_response_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                outcome=outcome,
                outcome_reason=outcome_reason,
            )
            
            phase2_usage = None
            phase2_provider: Optional[str] = None
            phase2_model: Optional[str] = None
            try:
                p2_result = await asyncio.wait_for(
                    self._call_llm(
                        system_prompt=p2_system,
                        user_prompt=p2_user,
                        max_tokens=completion_budget,
                        task_type="gr_response",
                    ),
                    timeout=phase2_timeout,
                )
                llm_response = p2_result.content
                phase2_usage = p2_result.usage
                phase2_provider = p2_result.provider_used
                phase2_model = p2_result.model_used
            except asyncio.TimeoutError:
                logger.warning(f"Phase 2 timed out after {phase2_timeout:.0f}s")
                llm_response = json.dumps(
                    self._build_llm_timeout_fallback(inquiry, selected_chunks)
                )
            except LLMEmptyResponseError as e:
                logger.error(f"Phase 2 LLM empty: {e}")
                llm_response = json.dumps(
                    self._build_llm_fallback_parsed(f"finish_reason={e.finish_reason}")
                )
            except Exception as e:
                logger.error(f"Phase 2 LLM error: {type(e).__name__}: {e}")
                llm_response = json.dumps(
                    self._build_llm_timeout_fallback(inquiry, selected_chunks)
                )
            
            # 6. Parse Phase 2 response
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Phase 2 JSON parse error: {e}")
                logger.error(f"Raw response: {llm_response[:500]}")
                parsed = {
                    "response_to_participant": {
                        "opening": "We were unable to generate a structured response for your inquiry.",
                        "key_points": [llm_response[:1000]] if llm_response else [],
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
            
            # Inject Phase 1 outcome into parsed response
            parsed["outcome"] = outcome
            parsed["outcome_reason"] = outcome_reason
            
            # Validate required structure
            missing_keys = self._LLM_RESPONSE_REQUIRED_KEYS - parsed.keys()
            if missing_keys:
                logger.error(
                    f"Phase 2 response missing required keys: {missing_keys}. "
                    f"Keys present: {list(parsed.keys())}"
                )
                parsed = self._build_llm_fallback_parsed(
                    f"missing keys: {', '.join(sorted(missing_keys))}"
                )
                parsed["outcome"] = outcome
                parsed["outcome_reason"] = outcome_reason
            
            coverage_gaps = parsed.get("coverage_gaps", [])
            if not isinstance(coverage_gaps, list):
                coverage_gaps = []
            coverage_gaps = [g for g in coverage_gaps if isinstance(g, str) and g.strip()]
            
            # 7. Hybrid confidence
            confidence = self._calculate_confidence(chunks, coverage_gaps=coverage_gaps)
            
            # Phase 1 outcome is a stronger signal than retrieval metrics alone.
            # When the LLM determined a concrete outcome and Phase 2 produced
            # a real response, ensure the decision aligns with the outcome.
            OUTCOME_CONFIDENCE_FLOORS = {
                "can_proceed": 0.65,
                "blocked_not_eligible": 0.65,
                "blocked_missing_data": 0.55,
                "ambiguous_plan_rules": 0.50,
            }
            floor = OUTCOME_CONFIDENCE_FLOORS.get(outcome, 0.0)
            if floor and confidence < floor:
                logger.info(f"Confidence floor applied: {confidence:.3f} -> {floor} (outcome={outcome})")
                confidence = floor
            
            decision = self._determine_decision(confidence)
            
            # 8. Source articles and used chunks
            source_articles = self._build_source_articles(selected_chunks)
            used_chunks = self._serialize_used_chunks(selected_chunks)
            total_articles = len(source_articles)
            relevant_articles = sum(
                1 for sa in source_articles if sa.get("used_info", False)
            )
            
            pqs = {sq: per_query_scores.get(eq, 0.0) for sq, eq in zip(sub_queries, enriched_queries)}
            
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
                    "phase1_outcome": outcome,
                }
            )
        
        except Exception as e:
            logger.exception("Error en generate_response")
            return self._build_uncertain_response(
                "An internal error occurred while generating the response",
                confidence=0.0
            )
    
    # ========================================================================
    # ENDPOINT 3: Knowledge Question
    # ========================================================================
    
    # Required Data settings
    RD_CONTEXT_BUDGET = 3500
    RD_TOP_K_PER_QUERY = 5
    RD_MAX_CHUNKS_PER_ARTICLE = 4
    RD_RETRIEVAL_MIN_SCORE = 0.25
    RD_NO_MATCH_CONFIDENCE = 0.40
    
    # Generate Response settings
    RESPONSE_MIN_CONTEXT_TOKENS = 4000
    GR_MAX_CHUNKS_PER_ARTICLE = 6
    GR_UNFILTERED_TOP_K = 15
    GR_LLM_TIMEOUT_SECONDS = 180
    GR_PHASE1_TIMEOUT_SECONDS = 45
    GR_PHASE1_MAX_TOKENS = 500
    GR_FALLBACK_MIN_CHUNKS = 6
    GR_FALLBACK_MIN_SCORE = 0.35
    
    # Knowledge Question settings
    KQ_CONTEXT_BUDGET = 4000
    KQ_TOP_K_PER_QUERY = 15
    KQ_MAX_CHUNKS_PER_ARTICLE = 6
    KQ_SOURCE_MIN_SCORE = 0.20
    KQ_PRIORITIZED_TYPES = [
        'business_rules', 'eligibility', 'steps', 'faqs',
        'guardrails', 'fees_details'
    ]

    async def ask_knowledge_question(
        self,
        question: str
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
        logger.info(f"ask_knowledge_question() - Question: {question[:80]}...")
        
        try:
            # 1. Decompose question into sub-queries
            sub_queries = await self._decompose_question(question)
            logger.info(f"Decomposed into {len(sub_queries)} sub-queries: {sub_queries}")
            
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
            for label, result_list in zip(query_labels, results):
                best = max((c.get('score', 0) for c in result_list), default=0)
                per_query_scores[label] = round(best, 4)
            
            # 3. Merge, deduplicate, rank by score
            chunks = self._merge_and_rank_chunks(*results)
            
            if not chunks:
                logger.warning("No chunks found for knowledge question")
                return KnowledgeQuestionResult(
                    answer="I couldn't find relevant information in the knowledge base to answer this question.",
                    key_points=[],
                    source_articles=[],
                    used_chunks=[],
                    confidence_note="limited_coverage",
                    metadata={"chunks_used": 0, "model": None, "provider": None, "sub_queries": sub_queries}
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
            except LLMEmptyResponseError as e:
                logger.error(f"LLM returned empty content in knowledge_question: {e}")
                llm_response = json.dumps({
                    "answer": "Unable to generate a response. Please try again or contact Support.",
                    "key_points": [],
                    "coverage_gaps": []
                })
            
            # 6. Parse response
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing knowledge question JSON: {e}")
                parsed = {
                    "answer": llm_response[:2000] if llm_response else "Unable to generate a structured answer.",
                    "key_points": [],
                    "coverage_gaps": []
                }
            
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
                    "per_query_scores": per_query_scores
                }
            )
        
        except Exception as e:
            logger.exception("Error in ask_knowledge_question")
            return KnowledgeQuestionResult(
                answer="An internal error occurred while processing your question.",
                key_points=[],
                source_articles=[],
                used_chunks=[],
                confidence_note="limited_coverage",
                metadata={"error": str(e), "chunks_used": 0, "model": None, "provider": None}
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
    
    # ── Cached Pinecone query wrapper ──
    
    def _cache_key(self, query_text: str, top_k: int, filter_dict: Optional[Dict]) -> str:
        """Build a deterministic cache key from query parameters."""
        raw = json.dumps({"q": query_text, "k": top_k, "f": filter_dict}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()
    
    async def _cached_query(
        self,
        query_text: str,
        top_k: int,
        filter_dict: Optional[Dict[str, Any]] = None
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
        key = self._cache_key(query_text, top_k, filter_dict)
        
        if key in self._search_cache:
            logger.debug("Cache HIT for Pinecone query")
            return self._search_cache[key]
        
        result = await asyncio.to_thread(
            self.pinecone.query_chunks,
            query_text=query_text,
            top_k=top_k,
            filter_dict=filter_dict
        )
        
        self._search_cache[key] = result
        return result
    
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
        topic: str
    ) -> tuple:
        """
        Parallel multi-query search for required_data endpoint.
        
        Runs all enriched sub-queries in parallel across the RK cascade,
        then merges, deduplicates, and ranks results. Tracks per-query
        best scores for observability.
        
        Returns:
            (merged_chunks, per_query_scores)
        """
        rk_cascade = self._build_rk_cascade(record_keeper)
        required_data_chunks: List[Dict[str, Any]] = []
        per_query_scores: Dict[str, float] = {eq: 0.0 for eq in enriched_queries}
        top_k = self.RD_TOP_K_PER_QUERY
        
        if record_keeper:
            # ── RK provided: run all sub-queries × first two cascade levels in parallel ──
            search_tasks = []
            for eq in enriched_queries:
                rk_filters = {
                    **rk_cascade[0]["filters"],
                    "plan_type": {"$in": [plan_type, "all"]},
                    "chunk_type": {"$eq": "required_data_must_have"}
                }
                global_filters = {
                    **rk_cascade[1]["filters"],
                    "plan_type": {"$in": [plan_type, "all"]},
                    "chunk_type": {"$eq": "required_data_must_have"}
                }
                search_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=rk_filters))
                search_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=global_filters))
            
            results = await asyncio.gather(*search_tasks)
            
            # Track per-query best scores from individual results before merging
            for i, eq in enumerate(enriched_queries):
                eq_chunks = results[2 * i] + results[2 * i + 1]
                best = max((c.get('score', 0) for c in eq_chunks), default=0)
                per_query_scores[eq] = round(best, 4)
            
            required_data_chunks = self._merge_and_rank_chunks(*results)
            
            logger.info(f"Phase 1 parallel ({len(search_tasks)} tasks): found {len(required_data_chunks)} unique chunks")
            
            # If parallel levels insufficient, continue cascade from level 2+
            if not self._rk_results_sufficient(required_data_chunks):
                for level in rk_cascade[2:]:
                    fallback_tasks = []
                    for eq in enriched_queries:
                        level_filters = {
                            **level["filters"],
                            "plan_type": {"$in": [plan_type, "all"]},
                            "chunk_type": {"$eq": "required_data_must_have"}
                        }
                        fallback_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=level_filters))
                    
                    level_results = await asyncio.gather(*fallback_tasks)
                    
                    for i, eq in enumerate(enriched_queries):
                        best = max((c.get('score', 0) for c in level_results[i]), default=0)
                        per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))
                    
                    level_chunks = self._merge_and_rank_chunks(*level_results)
                    logger.info(f"Cascade ({level['label']}): found {len(level_chunks)} chunks")
                    
                    if self._rk_results_sufficient(level_chunks):
                        required_data_chunks = self._merge_and_rank_chunks(
                            required_data_chunks, level_chunks
                        )
                        break
        else:
            # ── No RK: run all sub-queries at each cascade level sequentially ──
            for level in rk_cascade:
                level_tasks = []
                for eq in enriched_queries:
                    level_filters = {
                        **level["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"}
                    }
                    level_tasks.append(self._cached_query(eq, top_k=top_k, filter_dict=level_filters))
                
                level_results = await asyncio.gather(*level_tasks)
                
                for i, eq in enumerate(enriched_queries):
                    best = max((c.get('score', 0) for c in level_results[i]), default=0)
                    per_query_scores[eq] = max(per_query_scores[eq], round(best, 4))
                
                level_chunks = self._merge_and_rank_chunks(*level_results)
                logger.info(f"Cascade ({level['label']}): found {len(level_chunks)} chunks")
                
                if self._rk_results_sufficient(level_chunks):
                    required_data_chunks = self._merge_and_rank_chunks(
                        required_data_chunks, level_chunks
                    )
                    break
        
        if required_data_chunks:
            best = required_data_chunks[0]
            logger.info(
                f"Required data best match: article={best['metadata'].get('article_id')}, "
                f"topic={best['metadata'].get('topic')}, score={best['score']:.4f}"
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
        logger.info(f"Phase 2: focusing context on article_id={best_article_id}")
        
        context_chunks = await self._cached_query(
            enriched_queries[0], top_k=7, filter_dict=context_filters
        )
        logger.info(f"Phase 2 (context): found {len(context_chunks)} chunks")
        
        merged = self._merge_and_rank_chunks(required_data_chunks, context_chunks)
        
        logger.info(f"Total merged chunks for required_data: {len(merged)}")
        return merged, per_query_scores
    
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
    
    async def _search_for_response_simple(
        self,
        enriched_queries: List[str],
        plan_type: str,
    ) -> tuple:
        """
        KQ-style parallel search for generate_response endpoint.
        
        For each enriched query, fires two lanes in parallel:
        1. Unfiltered semantic search (top_k=15) — relies on embedding
           relevance; the enriched query text already contains topic +
           participant data for natural RK/topic matching.
        2. Plan-type filtered search (top_k=10) — safety net ensuring
           plan-specific articles surface even if embeddings are noisy.
        
        This replaces the RK cascade + topic-strategy approach, reducing
        Pinecone calls from ~24-28 to ~6-8 per request.
        
        Returns:
            (merged_chunks, per_query_scores)
        """
        per_query_scores: Dict[str, float] = {eq: 0.0 for eq in enriched_queries}
        tasks = []
        task_queries: List[str] = []

        for eq in enriched_queries:
            tasks.append(
                self._cached_query(eq, top_k=self.KQ_TOP_K_PER_QUERY, filter_dict=None)
            )
            task_queries.append(eq)
            tasks.append(
                self._cached_query(
                    eq,
                    top_k=10,
                    filter_dict={"plan_type": {"$in": [plan_type, "all"]}}
                )
            )
            task_queries.append(eq)

        results = await asyncio.gather(*tasks)

        for i, eq in enumerate(enriched_queries):
            unfiltered = results[2 * i]
            filtered = results[2 * i + 1]
            best = max(
                (c.get('score', 0) for c in unfiltered + filtered),
                default=0
            )
            per_query_scores[eq] = round(best, 4)

        chunks = self._merge_and_rank_chunks(*results)

        if not chunks:
            logger.warning("generate_response: no results from simplified search")
        else:
            unique_articles = len(set(
                c['metadata'].get('article_id') for c in chunks
            ))
            logger.info(
                f"generate_response (simple): {len(chunks)} chunks from "
                f"{unique_articles} articles ({len(tasks)} Pinecone calls)"
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
    
    async def _decompose_question(self, question: str) -> List[str]:
        """
        Decompose a multi-part question into focused sub-queries for parallel search.
        
        Uses a lightweight LLM call to identify distinct 401(k) concepts in the
        question. Falls back to the original question on any error.
        
        Returns:
            List of 1-3 focused sub-queries
        """
        try:
            system_prompt, user_prompt = build_decompose_question_prompt(question)
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
        except Exception as e:
            logger.warning(f"Question decomposition failed, using original: {e}")
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
    
    def _build_context_with_diversity(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        prioritize_types: Optional[List[str]] = None,
        max_per_article: int = 6
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
        
        # ── Phase 1: Best chunk from each article ──
        article_best: Dict[str, Dict[str, Any]] = {}
        for chunk in chunks:
            aid = chunk['metadata'].get('article_id', 'unknown')
            if aid not in article_best or chunk.get('score', 0) > article_best[aid].get('score', 0):
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
    
    def _build_context_with_diversity_and_tiers(
        self,
        chunks: List[Dict[str, Any]],
        budget: int,
        max_per_article: int = 6
    ) -> tuple:
        """
        Build context combining article diversity with tier-based priority.
        
        Phase 1 (diversity): Best chunk from each article, sorted by score.
        Phase 2 (tier fill): Fill remaining budget from critical -> high ->
        medium -> low, with a per-article cap.
        
        This prevents a single article from dominating while ensuring
        critical/high-tier chunks are prioritized for the response.
        
        Returns:
            (context_string, selected_chunks, tokens_used)
        """
        if not chunks:
            return "", [], 0
        
        # ── Phase 1: Best chunk from each article ──
        article_best: Dict[str, Dict[str, Any]] = {}
        for chunk in chunks:
            aid = chunk['metadata'].get('article_id', 'unknown')
            if aid not in article_best or chunk.get('score', 0) > article_best[aid].get('score', 0):
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
                article_counts[chunk['metadata'].get('article_id', 'unknown')] += 1
        
        logger.debug(
            f"Diversity+tiers phase 1: {len(selected)} chunks from "
            f"{len(article_counts)} articles, {tokens_used} tokens"
        )
        
        # ── Phase 2: Fill by tier priority with per-article cap ──
        by_tier = self._organize_chunks_by_tier(chunks)
        for tier in ['critical', 'high', 'medium', 'low']:
            for chunk in by_tier.get(tier, []):
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
        
        selected.sort(key=lambda c: c.get('score', 0), reverse=True)
        
        logger.debug(
            f"Diversity+tiers phase 2 total: {len(selected)} chunks from "
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
            f"Knowledge confidence: avg_score={avg_score:.3f}, "
            f"high_value_types={len(covered_high_value)}/{len(self._KQ_HIGH_VALUE_TYPES)}, "
            f"coverage_gaps={n_gaps} {coverage_gaps}"
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
    
    def _build_empty_required_data_response(self, reason: str) -> RequiredDataResponse:
        """Construye respuesta vacía para required_data."""
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
            metadata={
                "error": reason,
                "chunks_used": 0,
                "sub_queries": [],
                "per_query_scores": {},
                "unique_articles": 0,
                "relevant_articles": 0,
                "coverage_gaps": []
            }
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
    ) -> Dict[str, Any]:
        """Build a metadata dict for GenerateResponseResult."""
        source_articles = self._build_source_articles(selected_chunks)
        pqs = {sq: per_query_scores.get(eq, 0.0) for sq, eq in zip(sub_queries, enriched_queries)}
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

    def _build_uncertain_response(self, reason: str, confidence: float) -> GenerateResponseResult:
        """Construye respuesta de fallback con el schema outcome-driven."""
        return GenerateResponseResult(
            decision="out_of_scope",
            confidence=confidence,
            response={
                "outcome": "blocked_missing_data",
                "outcome_reason": f"Unable to generate response: {reason}",
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
                "data_gaps": [reason],
                "coverage_gaps": []
            },
            source_articles=[],
            used_chunks=[],
            coverage_gaps=[],
            metadata={
                "error": reason,
                "chunks_used": 0,
                "sub_queries": [],
                "per_query_scores": {},
                "unique_articles": 0,
                "relevant_articles": 0,
                "coverage_gaps": []
            }
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
