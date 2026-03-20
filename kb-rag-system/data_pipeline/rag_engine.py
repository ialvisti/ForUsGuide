"""
RAG Engine - Motor principal de búsqueda y generación.

Este módulo implementa el RAG engine con dos funciones principales:
1. get_required_data() - Determina qué datos se necesitan
2. generate_response() - Genera respuesta contextualizada

All public methods are async to avoid blocking FastAPI's event loop.
Pinecone SDK calls (synchronous) are wrapped with asyncio.to_thread().
OpenAI calls use the native AsyncOpenAI client.
"""

import os
import json
import asyncio
import hashlib
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from openai import AsyncOpenAI
from cachetools import TTLCache

from .pinecone_uploader import PineconeUploader
from .token_manager import TokenManager
from .prompts import (
    build_required_data_prompt,
    build_generate_response_prompt,
    build_knowledge_question_prompt
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
        metadata: Info de diagnóstico (chunks_used, tokens, modelo).
    """
    decision: str  # "can_proceed", "uncertain", "out_of_scope"
    confidence: float
    response: Dict[str, Any]
    metadata: Dict[str, Any]


@dataclass
class KnowledgeQuestionResult:
    """Respuesta del endpoint /knowledge-question."""
    answer: str
    key_points: List[str]
    source_articles: List[Dict[str, Any]]
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
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        reasoning_effort: Optional[str] = None,
        pinecone_uploader: Optional['PineconeUploader'] = None
    ):
        """
        Inicializa el RAG engine.
        
        Args:
            openai_api_key: API key de OpenAI (usa .env si no se provee)
            model: Modelo de OpenAI a usar (default: gpt-4o-mini)
            temperature: Temperature para generación (default: 0.1)
            reasoning_effort: Esfuerzo de razonamiento para GPT-5.2 (none, low, medium, high, xhigh)
            pinecone_uploader: Pre-configured PineconeUploader instance.
                               If None, creates a new one (standalone usage).
        """
        # Async OpenAI client — non-blocking in FastAPI's event loop
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY no está configurada")
        
        self.openai_client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        
        # Detectar si es modelo GPT-5.2
        self.is_gpt5 = "gpt-5" in model.lower()
        
        # Pinecone uploader para búsquedas (sync SDK, wrapped with asyncio.to_thread).
        # Reuse a shared instance when provided to avoid duplicate connections.
        self.pinecone = pinecone_uploader or PineconeUploader()
        
        # Token manager
        self.token_manager = TokenManager(model="gpt-4")
        
        # TTL cache for Pinecone search results
        self._search_cache: TTLCache = TTLCache(
            maxsize=self.CACHE_MAX_SIZE,
            ttl=self.CACHE_TTL_SECONDS
        )
        # Lock to prevent duplicate Pinecone calls when concurrent coroutines
        # check the cache before either has written back.
        self._cache_lock = asyncio.Lock()
        
        logger.info(f"RAG Engine inicializado con modelo: {model}")
        if self.is_gpt5 and reasoning_effort:
            logger.info(f"  - Reasoning effort: {reasoning_effort}")
    
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
        
        Este es el Endpoint 1 del sistema. Busca en la KB chunks relevantes
        de tipo 'required_data', 'eligibility', y 'business_rules', y usa
        el LLM para extraer campos específicos necesarios.
        
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
            # 1. Buscar chunks relevantes con filtros
            chunks = await self._search_for_required_data(
                inquiry=inquiry,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic
            )
            
            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_empty_required_data_response(
                    "No relevant articles found for this topic"
                )
            
            # 2. Construir contexto (CPU-bound, no async needed)
            context_budget = 2500
            context, selected_chunks, tokens_used = self._build_context_from_chunks(
                chunks=chunks,
                budget=context_budget,
                prioritize_types=['required_data_must_have', 'eligibility', 'business_rules']
            )
            
            logger.info(f"Context construido: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
            # 3. Generar prompts
            system_prompt, user_prompt = build_required_data_prompt(
                context=context,
                inquiry=inquiry,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic
            )
            
            # 4. Llamar LLM (async)
            llm_response = await self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=800
            )
            
            # 5. Parsear respuesta
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parseando JSON del LLM: {e}")
                logger.error(f"Respuesta: {llm_response[:500]}")
                parsed = {"participant_data": [], "plan_data": []}
            
            # 6. Calcular confidence
            confidence = self._calculate_required_data_confidence(chunks, query_topic=topic)
            
            # 7. Construir respuesta
            return RequiredDataResponse(
                article_reference={
                    "article_id": chunks[0]['metadata'].get('article_id'),
                    "title": chunks[0]['metadata'].get('article_title'),
                    "confidence": confidence
                },
                required_fields=parsed,
                confidence=confidence,
                metadata={
                    "chunks_used": len(selected_chunks),
                    "tokens_used": tokens_used,
                    "model": self.model
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
        Genera respuesta contextualizada usando la KB y datos recolectados.
        
        Este es el Endpoint 2 del sistema. Busca chunks relevantes, construye
        contexto respetando token budget, y genera respuesta estructurada.
        
        Args:
            inquiry: La consulta del participante
            record_keeper: Record keeper (ej: "LT Trust")
            plan_type: Tipo de plan (ej: "401(k)")
            topic: Tema principal
            collected_data: Datos recolectados del participante/plan
            max_response_tokens: Máximo de tokens para la respuesta
            total_inquiries_in_ticket: Total de inquiries en el ticket
        
        Returns:
            GenerateResponseResult con respuesta estructurada
        """
        logger.info(f"generate_response() - Topic: {topic}, Budget: {max_response_tokens} tokens")
        
        try:
            # 1. Calcular presupuesto de contexto
            context_budget = max_response_tokens - self.RESPONSE_MIN_TOKENS
            
            logger.info(f"Context budget: {context_budget} tokens (de {max_response_tokens} total, reservando {self.RESPONSE_MIN_TOKENS} para response)")
            
            # 2. Buscar chunks relevantes (async)
            chunks = await self._search_for_response(
                inquiry=inquiry,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                collected_data=collected_data
            )
            
            if not chunks:
                logger.warning("No se encontraron chunks relevantes")
                return self._build_uncertain_response(
                    "No relevant articles found for this topic",
                    confidence=0.0
                )
            
            # 3. Organizar chunks por tier (CPU-bound, no async needed)
            chunks_by_tier = self._organize_chunks_by_tier(chunks)
            
            # 4. Construir contexto priorizando por tier
            context, selected_chunks, tokens_used = self.token_manager.build_context_with_tiers(
                chunks_by_tier=chunks_by_tier,
                budget=context_budget,
                tier_priority=['critical', 'high', 'medium', 'low']
            )
            
            logger.info(f"Context: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
            # 5. Calcular tokens para completion
            completion_budget = max(self.RESPONSE_MIN_TOKENS, max_response_tokens - tokens_used)
            
            # 6. Generar prompts
            system_prompt, user_prompt = build_generate_response_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                max_tokens=completion_budget
            )
            
            # 7. Llamar LLM (async)
            llm_response = await self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=completion_budget
            )
            
            # 8. Parsear respuesta
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parseando JSON del LLM: {e}")
                logger.error(f"Respuesta: {llm_response[:500]}")
                parsed = {
                    "outcome": "blocked_missing_data",
                    "outcome_reason": "Response parsing failed — raw LLM output could not be parsed as JSON.",
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
                    "data_gaps": ["LLM response was not valid JSON"]
                }
            
            # 9. Calcular confidence y decision
            confidence = self._calculate_confidence(chunks)
            decision = self._determine_decision(confidence)
            
            # 10. Construir respuesta
            return GenerateResponseResult(
                decision=decision,
                confidence=confidence,
                response=parsed,
                metadata={
                    "chunks_used": len(selected_chunks),
                    "context_tokens": tokens_used,
                    "response_tokens": self.token_manager.count_tokens(llm_response),
                    "model": self.model,
                    "total_inquiries": total_inquiries_in_ticket
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
    
    async def ask_knowledge_question(
        self,
        question: str
    ) -> KnowledgeQuestionResult:
        """
        Answer a general knowledge question using the KB — no participant data required.
        
        Performs a broad semantic search across all articles, builds context,
        and generates a comprehensive answer via the LLM.
        
        Args:
            question: The knowledge question to answer
        
        Returns:
            KnowledgeQuestionResult with answer, key points, and sources
        """
        logger.info(f"ask_knowledge_question() - Question: {question[:80]}...")
        
        try:
            # 1. Broad semantic search (no RK/topic/plan filters)
            chunks = await self._cached_query(
                query_text=question,
                top_k=20,
                filter_dict=None
            )
            
            if not chunks:
                logger.warning("No chunks found for knowledge question")
                return KnowledgeQuestionResult(
                    answer="I couldn't find relevant information in the knowledge base to answer this question.",
                    key_points=[],
                    source_articles=[],
                    confidence_note="limited_coverage",
                    metadata={"chunks_used": 0, "model": self.model}
                )
            
            # 2. Build context (generous budget for knowledge answers)
            context_budget = 3500
            context, selected_chunks, tokens_used = self._build_context_from_chunks(
                chunks=chunks,
                budget=context_budget,
                prioritize_types=['business_rules', 'eligibility', 'steps', 'faqs']
            )
            
            logger.info(f"Context built: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
            # 3. Build prompts
            system_prompt, user_prompt = build_knowledge_question_prompt(
                context=context,
                question=question
            )
            
            # 4. Call LLM
            llm_response = await self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=2000
            )
            
            # 5. Parse response
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing knowledge question JSON: {e}")
                parsed = {
                    "answer": llm_response[:2000] if llm_response else "Unable to generate a structured answer.",
                    "key_points": [],
                    "source_articles": [],
                    "confidence_note": "limited_coverage"
                }
            
            # 6. Extract source articles from chunks if not provided by LLM
            source_articles = parsed.get("source_articles", [])
            if not source_articles:
                seen_articles = set()
                for chunk in selected_chunks:
                    aid = chunk['metadata'].get('article_id')
                    if aid and aid not in seen_articles:
                        seen_articles.add(aid)
                        source_articles.append({
                            "article_id": aid,
                            "title": chunk['metadata'].get('article_title'),
                            "relevance": f"Contains {chunk['metadata'].get('chunk_type', 'content')} about {chunk['metadata'].get('topic', 'this topic')}"
                        })
            
            return KnowledgeQuestionResult(
                answer=parsed.get("answer", ""),
                key_points=parsed.get("key_points", []),
                source_articles=source_articles,
                confidence_note=parsed.get("confidence_note", "partially_covered"),
                metadata={
                    "chunks_used": len(selected_chunks),
                    "context_tokens": tokens_used,
                    "model": self.model
                }
            )
        
        except Exception as e:
            logger.exception("Error in ask_knowledge_question")
            return KnowledgeQuestionResult(
                answer="An internal error occurred while processing your question.",
                key_points=[],
                source_articles=[],
                confidence_note="limited_coverage",
                metadata={"error": str(e), "chunks_used": 0, "model": self.model}
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
        
        Uses asyncio.Lock to prevent duplicate Pinecone calls when
        concurrent coroutines generate the same cache key.
        """
        key = self._cache_key(query_text, top_k, filter_dict)
        
        async with self._cache_lock:
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
        inquiry: str,
        record_keeper: Optional[str],
        plan_type: str,
        topic: str
    ) -> List[Dict[str, Any]]:
        """
        Busca chunks relevantes para el endpoint required_data.
        
        Uses a cascading record-keeper strategy:
        
        When RK provided:
          Phase 1: Parallel queries — RK-specific + global scope
                   RK results are prioritized; global supplements.
          Phase 1 fallback: If neither returned results, cascade to
                   LT Trust → any.
        
        When RK NOT provided:
          Phase 1: Global scope first.
          Phase 1 fallback: If insufficient, cascade to LT Trust → any.
        
        Phase 2 (always): Context chunks (eligibility, business_rules) from
          the winning article.
        """
        enriched_query = f"{inquiry} {topic}"
        rk_cascade = self._build_rk_cascade(record_keeper)
        required_data_chunks: List[Dict[str, Any]] = []
        
        if record_keeper:
            # ── RK provided: run first two cascade levels in parallel ──
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
            
            rk_chunks, global_chunks = await asyncio.gather(
                self._cached_query(enriched_query, top_k=3, filter_dict=rk_filters),
                self._cached_query(enriched_query, top_k=3, filter_dict=global_filters),
            )
            
            logger.info(f"Phase 1A ({rk_cascade[0]['label']}): found {len(rk_chunks)} chunks")
            logger.info(f"Phase 1B ({rk_cascade[1]['label']}): found {len(global_chunks)} chunks")
            
            if rk_chunks:
                required_data_chunks = self._merge_and_rank_chunks(rk_chunks, global_chunks)
            elif global_chunks:
                required_data_chunks = global_chunks
            
            # If parallel levels insufficient, continue cascade from level 2+
            if not self._rk_results_sufficient(required_data_chunks):
                for level in rk_cascade[2:]:
                    level_filters = {
                        **level["filters"],
                        "plan_type": {"$in": [plan_type, "all"]},
                        "chunk_type": {"$eq": "required_data_must_have"}
                    }
                    level_chunks = await self._cached_query(
                        enriched_query, top_k=3, filter_dict=level_filters
                    )
                    logger.info(f"Cascade ({level['label']}): found {len(level_chunks)} chunks")
                    
                    if self._rk_results_sufficient(level_chunks):
                        required_data_chunks = self._merge_and_rank_chunks(
                            required_data_chunks, level_chunks
                        )
                        break
        else:
            # ── No RK: sequential cascade through all levels ──
            for level in rk_cascade:
                level_filters = {
                    **level["filters"],
                    "plan_type": {"$in": [plan_type, "all"]},
                    "chunk_type": {"$eq": "required_data_must_have"}
                }
                level_chunks = await self._cached_query(
                    enriched_query, top_k=3, filter_dict=level_filters
                )
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
            return []
        
        # ── Phase 2: Context chunks from the winning article ──
        best_article_id = required_data_chunks[0]['metadata'].get('article_id')
        
        context_filters = {
            "article_id": {"$eq": best_article_id},
            "chunk_type": {"$in": ["eligibility", "business_rules"]}
        }
        logger.info(f"Phase 2: focusing context on article_id={best_article_id}")
        
        context_chunks = await self._cached_query(
            enriched_query, top_k=7, filter_dict=context_filters
        )
        logger.info(f"Phase 2 (context): found {len(context_chunks)} chunks")
        
        # ── Merge: required_data primero, luego contexto (deduplicado) ──
        seen_ids = set()
        merged = []
        
        for chunk in required_data_chunks + context_chunks:
            chunk_id = chunk.get('id')
            if chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                merged.append(chunk)
        
        logger.info(f"Total merged chunks for required_data: {len(merged)}")
        return merged
    
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
    
    async def _search_for_response(
        self,
        inquiry: str,
        record_keeper: Optional[str],
        plan_type: str,
        topic: str,
        collected_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Busca chunks relevantes para generate_response endpoint.
        
        Uses a cascading record-keeper strategy combined with topic strategies:
        
        For each RK cascade level (ordered by priority):
          1. Try topic exact-match filter
          2. Try tags filter (with case variations)
          3. Try no-topic filter
          If any strategy returns sufficient results, stop cascading.
        
        RK cascade order depends on whether record_keeper was provided
        (see _build_rk_cascade for details).
        """
        query_parts = [inquiry, topic]
        
        if collected_data:
            if "participant_data" in collected_data:
                for key, value in list(collected_data["participant_data"].items())[:3]:
                    query_parts.append(f"{key}: {value}")
        
        enriched_query = " ".join(query_parts)
        rk_cascade = self._build_rk_cascade(record_keeper)
        
        for level in rk_cascade:
            base_filters = {
                **level["filters"],
                "plan_type": {"$in": [plan_type, "all"]}
            }
            
            chunks = await self._search_with_topic_strategies(
                enriched_query, base_filters, topic
            )
            
            if self._rk_results_sufficient(
                chunks,
                min_chunks=self.TOPIC_FILTER_MIN_CHUNKS,
                min_score=self.TOPIC_FILTER_MIN_SCORE
            ):
                logger.info(
                    f"generate_response: found {len(chunks)} chunks "
                    f"at cascade level '{level['label']}'"
                )
                return chunks
            
            logger.info(
                f"generate_response: cascade level '{level['label']}' "
                f"insufficient ({len(chunks)} chunks), trying next level"
            )
        
        logger.warning("generate_response: no sufficient results across all cascade levels")
        return chunks if chunks else []
    
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
    
    # Multiplicador para GPT-5.2: los reasoning tokens se consumen dentro
    # de max_completion_tokens, así que necesitamos presupuesto extra.
    # Con reasoning_effort="medium", el modelo puede usar ~60-70% en reasoning.
    GPT5_REASONING_MULTIPLIER = 4
    GPT5_MIN_COMPLETION_TOKENS = 2000
    
    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int
    ) -> str:
        """
        Llama al LLM (OpenAI) de forma asíncrona.
        
        Args:
            system_prompt: System prompt
            user_prompt: User prompt
            max_tokens: Máximo de tokens para el contenido de la respuesta.
                        Para GPT-5.2, se escala automáticamente para incluir
                        headroom para reasoning tokens.
        
        Returns:
            Respuesta del LLM (string)
        """
        try:
            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "response_format": {"type": "json_object"}
            }
            
            if self.is_gpt5:
                scaled_tokens = max(
                    max_tokens * self.GPT5_REASONING_MULTIPLIER,
                    self.GPT5_MIN_COMPLETION_TOKENS
                )
                params["max_completion_tokens"] = scaled_tokens
                logger.info(
                    f"Llamando GPT-5.2 | requested={max_tokens} "
                    f"| scaled max_completion_tokens={scaled_tokens}"
                )
                
                if self.reasoning_effort:
                    params["reasoning_effort"] = self.reasoning_effort
                    logger.debug(f"Reasoning effort: {self.reasoning_effort}")
            else:
                params["max_tokens"] = max_tokens
                params["temperature"] = self.temperature
                logger.debug(f"Llamando GPT-4 con max_tokens={max_tokens}, temperature={self.temperature}")
            
            # Async call — does not block the event loop
            response = await self.openai_client.chat.completions.create(**params)
            
            content = response.choices[0].message.content
            
            if content is None or content.strip() == "":
                logger.warning(
                    f"LLM retornó contenido vacío/None. "
                    f"Finish reason: {response.choices[0].finish_reason}. "
                    f"Usage: {response.usage}"
                )
                return "{}"
            
            logger.info(f"LLM response: {len(content)} characters")
            
            return content
        
        except Exception as e:
            logger.error(f"Error llamando LLM: {e}")
            raise
    
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
        query_topic: str
    ) -> float:
        """
        Calcula confidence para el endpoint /required-data.
        
        Combina tres tipos de señales:
        
        1. Retrieval + Topic (55%): ¿Encontramos el chunk correcto Y del topic correcto?
           - must_have encontrado + topic coincide = 50% (match perfecto)
           - must_have encontrado + topic NO coincide = 15% (artículo equivocado)
           - must_have NO encontrado = 0%
           
        2. Soporte contextual (10%): ¿Hay chunks critical y suficiente contexto?
        
        3. Similitud semántica (35%): ¿Qué tan bien alinea el query con los chunks?
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
                # Match perfecto: artículo correcto con must_have chunk
                retrieval_score += 0.50
            else:
                # Must have encontrado pero del topic equivocado.
                # Bonus reducido — puede ser el artículo incorrecto.
                retrieval_score += 0.15
        
        # === Componente 2: Soporte Contextual (10%) ===
        # Señal B: Cobertura de chunks critical (5%)
        critical_count = sum(
            1 for c in chunks
            if c['metadata'].get('chunk_tier') == 'critical'
        )
        retrieval_score += 0.05 * min(1.0, critical_count / 3)
        
        # Señal C: Profundidad de contexto (5%)
        retrieval_score += 0.05 * min(1.0, len(chunks) / 5)
        
        # === Componente 3: Similitud Semántica (35%) ===
        top_scores = [c['score'] for c in chunks[:3]]
        avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
        similarity_score = avg_score * 0.35
        
        confidence = retrieval_score + similarity_score
        
        logger.info(
            f"Required data confidence: {confidence:.3f} "
            f"(retrieval={retrieval_score:.3f}, similarity={similarity_score:.3f}, "
            f"must_have={'yes' if has_must_have else 'no'}, "
            f"topic_matched={'yes' if topic_matched else 'no'}, "
            f"critical_chunks={critical_count}, total_chunks={len(chunks)})"
        )
        
        return round(min(1.0, confidence), 3)
    
    def _calculate_confidence(self, chunks: List[Dict[str, Any]]) -> float:
        """
        Calcula confidence score basado en chunks encontrados.
        
        Considera:
        - Scores de similitud de Pinecone
        - Presencia de chunks CRITICAL
        - Número total de chunks
        """
        if not chunks:
            return 0.0
        
        # Promedio de top 3 scores
        top_scores = [chunk['score'] for chunk in chunks[:3]]
        avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
        
        # Boost si hay chunks CRITICAL
        critical_count = sum(
            1 for chunk in chunks
            if chunk['metadata'].get('chunk_tier') == 'critical'
        )
        
        confidence = avg_score
        if critical_count >= 2:
            confidence = min(1.0, confidence * 1.15)
        elif critical_count >= 1:
            confidence = min(1.0, confidence * 1.08)
        
        return round(confidence, 3)
    
    def _determine_decision(self, confidence: float) -> str:
        """
        Determina decision basado en confidence score.
        
        Returns:
            "can_proceed", "uncertain", o "out_of_scope"
        """
        if confidence >= 0.70:
            return "can_proceed"
        elif confidence >= 0.50:
            return "uncertain"
        else:
            return "out_of_scope"
    
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
            metadata={
                "error": reason,
                "chunks_used": 0
            }
        )
    
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
                "data_gaps": [reason]
            },
            metadata={
                "error": reason,
                "chunks_used": 0
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
