"""
RAG Engine - Motor principal de búsqueda y generación.

Este módulo implementa el RAG engine con dos funciones principales:
1. get_required_data() - Determina qué datos se necesitan
2. generate_response() - Genera respuesta contextualizada
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from openai import OpenAI
from dotenv import load_dotenv

from .pinecone_uploader import PineconeUploader
from .token_manager import TokenManager
from .prompts import (
    build_required_data_prompt,
    build_generate_response_prompt
)

# Cargar variables de entorno
load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.INFO)
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
    
    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        reasoning_effort: Optional[str] = None
    ):
        """
        Inicializa el RAG engine.
        
        Args:
            openai_api_key: API key de OpenAI (usa .env si no se provee)
            model: Modelo de OpenAI a usar (default: gpt-4o-mini)
            temperature: Temperature para generación (default: 0.1)
            reasoning_effort: Esfuerzo de razonamiento para GPT-5.2 (none, low, medium, high, xhigh)
        """
        # OpenAI client
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY no está configurada")
        
        self.openai_client = OpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        
        # Detectar si es modelo GPT-5.2
        self.is_gpt5 = "gpt-5" in model.lower()
        
        # Pinecone uploader para búsquedas
        self.pinecone = PineconeUploader()
        
        # Token manager
        self.token_manager = TokenManager(model="gpt-4")
        
        logger.info(f"RAG Engine inicializado con modelo: {model}")
        if self.is_gpt5 and reasoning_effort:
            logger.info(f"  - Reasoning effort: {reasoning_effort}")
    
    # ========================================================================
    # ENDPOINT 1: Get Required Data
    # ========================================================================
    
    def get_required_data(
        self,
        inquiry: str,
        record_keeper: str,
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
            chunks = self._search_for_required_data(
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
            
            # 2. Construir contexto
            # Budget suficiente para incluir el chunk de required_data completo
            # (artículos con muchos campos pueden superar 1500 tokens fácilmente)
            context_budget = 2500  # Tokens para contexto
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
            
            # 4. Llamar LLM
            llm_response = self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=800  # Respuesta relativamente corta
            )
            
            # 5. Parsear respuesta
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError as e:
                logger.error(f"Error parseando JSON del LLM: {e}")
                logger.error(f"Respuesta: {llm_response[:500]}")
                parsed = {"participant_data": [], "plan_data": []}
            
            # 6. Calcular confidence (usa fórmula específica para required_data)
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
            logger.error(f"Error en get_required_data: {e}")
            import traceback
            traceback.print_exc()
            return self._build_empty_required_data_response(str(e))
    
    # ========================================================================
    # ENDPOINT 2: Generate Response
    # ========================================================================
    
    def generate_response(
        self,
        inquiry: str,
        record_keeper: str,
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
            # Budget combinado: maximizar contexto, reservar mínimo para respuesta.
            # Más contexto = más material de KB = respuesta más precisa y detallada.
            context_budget = max_response_tokens - self.RESPONSE_MIN_TOKENS
            
            logger.info(f"Context budget: {context_budget} tokens (de {max_response_tokens} total, reservando {self.RESPONSE_MIN_TOKENS} para response)")
            
            # 2. Buscar chunks relevantes
            chunks = self._search_for_response(
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
            
            # 3. Organizar chunks por tier
            chunks_by_tier = self._organize_chunks_by_tier(chunks)
            
            # 4. Construir contexto priorizando por tier
            context, selected_chunks, tokens_used = self.token_manager.build_context_with_tiers(
                chunks_by_tier=chunks_by_tier,
                budget=context_budget,
                tier_priority=['critical', 'high', 'medium', 'low']
            )
            
            logger.info(f"Context: {len(selected_chunks)} chunks, {tokens_used} tokens")
            
            # 5. Calcular tokens para completion
            # Lo que el contexto no usó, queda disponible para la respuesta.
            # Mínimo garantizado: RESPONSE_MIN_TOKENS.
            completion_budget = max(self.RESPONSE_MIN_TOKENS, max_response_tokens - tokens_used)
            
            # 6. Generar prompts (usa completion_budget para informar al LLM)
            system_prompt, user_prompt = build_generate_response_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                max_tokens=completion_budget
            )
            
            # 7. Llamar LLM
            llm_response = self._call_llm(
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
                # Fallback: construir respuesta mínima con el nuevo schema
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
            
            # 9. Calcular confidence y decision (calidad del retrieval RAG)
            confidence = self._calculate_confidence(chunks)
            decision = self._determine_decision(confidence)
            
            # 10. Construir respuesta
            # - decision/confidence: calidad del retrieval (calculado por el engine)
            # - response.outcome: determinación del caso (calculado por el LLM)
            # - guardrails viven SOLO dentro de response.guardrails_applied (sin duplicación)
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
            logger.error(f"Error en generate_response: {e}")
            import traceback
            traceback.print_exc()
            return self._build_uncertain_response(str(e), confidence=0.0)
    
    # ========================================================================
    # Helper Methods - Búsqueda
    # ========================================================================
    
    # Tokens mínimos reservados para la respuesta del LLM.
    # El resto del budget (max_response_tokens - este valor) va a contexto de KB.
    # 1200 tokens alcanza para una respuesta completa con el JSON schema.
    RESPONSE_MIN_TOKENS = 1200
    
    # Umbral mínimo para considerar que una búsqueda con filtro de topic
    # tuvo resultados suficientes. Si no se alcanza, se hace fallback sin topic.
    TOPIC_FILTER_MIN_CHUNKS = 3
    TOPIC_FILTER_MIN_SCORE = 0.20
    
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
    
    def _search_for_required_data(
        self,
        inquiry: str,
        record_keeper: str,
        plan_type: str,
        topic: str
    ) -> List[Dict[str, Any]]:
        """
        Busca chunks relevantes para el endpoint required_data.
        
        Usa una búsqueda en dos fases:
        
        Phase 1: Buscar chunks required_data_must_have (lo más crítico).
          Ejecuta DOS búsquedas paralelas y toma el mejor resultado por score:
          - 1A: Record-keeper específico (ej: "LT Trust")
          - 1B: Scope global (artículos que aplican a todos los RKs)
          
          La similitud semántica (el query incluye el topic) rankea
          el artículo correcto más alto automáticamente.
        
        Phase 2: Buscar chunks de contexto (eligibility, business_rules)
          del mismo artículo que ganó en Phase 1.
        """
        enriched_query = f"{inquiry} {topic}"
        
        # ── Phase 1A: Buscar must_have por record_keeper específico ──
        rk_filters = {
            "record_keeper": {"$eq": record_keeper},
            "plan_type": {"$eq": plan_type},
            "chunk_type": {"$eq": "required_data_must_have"}
        }
        rk_chunks = self.pinecone.query_chunks(
            query_text=enriched_query,
            top_k=3,
            filter_dict=rk_filters
        )
        logger.info(f"Phase 1A (RK={record_keeper}): found {len(rk_chunks)} chunks")
        
        # ── Phase 1B: Buscar must_have en artículos de scope global ──
        # Artículos con scope="global" no tienen record_keeper específico
        # y aplican a todos los record keepers.
        global_filters = {
            "scope": {"$eq": "global"},
            "plan_type": {"$eq": plan_type},
            "chunk_type": {"$eq": "required_data_must_have"}
        }
        global_chunks = self.pinecone.query_chunks(
            query_text=enriched_query,
            top_k=3,
            filter_dict=global_filters
        )
        logger.info(f"Phase 1B (scope=global): found {len(global_chunks)} chunks")
        
        # ── Merge 1A + 1B: deduplicar y ordenar por score ──
        # La similitud semántica determina qué artículo es más relevante
        # al topic del query. El mejor resultado queda primero.
        required_data_chunks = self._merge_and_rank_chunks(rk_chunks, global_chunks)
        
        if required_data_chunks:
            best = required_data_chunks[0]
            logger.info(
                f"Phase 1 best match: article={best['metadata'].get('article_id')}, "
                f"topic={best['metadata'].get('topic')}, score={best['score']:.4f}"
            )
        else:
            logger.warning("Phase 1: No required_data_must_have chunks found")
            return []
        
        # ── Phase 2: Buscar chunks de contexto del artículo ganador ──
        # Usamos article_id para focalizar en el mismo artículo.
        best_article_id = required_data_chunks[0]['metadata'].get('article_id')
        
        context_filters = {
            "article_id": {"$eq": best_article_id},
            "chunk_type": {"$in": ["eligibility", "business_rules"]}
        }
        logger.info(f"Phase 2: focusing context on article_id={best_article_id}")
        
        context_chunks = self.pinecone.query_chunks(
            query_text=enriched_query,
            top_k=7,
            filter_dict=context_filters
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
    
    def _search_for_response(
        self,
        inquiry: str,
        record_keeper: str,
        plan_type: str,
        topic: str,
        collected_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Busca chunks relevantes para generate_response endpoint."""
        # Construir query enriquecido con datos recolectados
        query_parts = [inquiry, topic]
        
        if collected_data:
            # Agregar snippets de datos relevantes al query
            if "participant_data" in collected_data:
                for key, value in list(collected_data["participant_data"].items())[:3]:
                    query_parts.append(f"{key}: {value}")
        
        enriched_query = " ".join(query_parts)
        
        # Filtros base (obligatorios)
        base_filters = {
            "record_keeper": {"$eq": record_keeper},
            "plan_type": {"$eq": plan_type}
        }
        
        # Strategy 1: Intentar con filtro de topic exacto
        if topic:
            topic_filters = {**base_filters, "topic": {"$eq": topic}}
            chunks = self.pinecone.query_chunks(
                query_text=enriched_query,
                top_k=30,
                filter_dict=topic_filters
            )
            
            if self._topic_results_sufficient(chunks):
                logger.info(f"Found {len(chunks)} chunks for generate_response (with topic filter: {topic})")
                return chunks
            
            logger.info(f"Topic filter '{topic}' returned insufficient results ({len(chunks)} chunks)")
            
            # Strategy 2: Buscar por tags (el topic puede estar en tags del artículo)
            topic_variations = self._get_topic_variations(topic)
            tags_filters = {
                "record_keeper": {"$eq": record_keeper},
                "plan_type": {"$eq": plan_type},
                "tags": {"$in": topic_variations}
            }
            chunks = self.pinecone.query_chunks(
                query_text=enriched_query,
                top_k=30,
                filter_dict=tags_filters
            )
            
            if self._topic_results_sufficient(chunks):
                logger.info(f"Found {len(chunks)} chunks for generate_response (with tags filter: {topic_variations})")
                return chunks
            
            logger.info(f"Tags filter also returned insufficient results ({len(chunks)} chunks), falling back without topic")
        
        # Strategy 3: Fallback sin filtro de topic
        chunks = self.pinecone.query_chunks(
            query_text=enriched_query,
            top_k=30,
            filter_dict=base_filters
        )
        
        logger.info(f"Found {len(chunks)} chunks for generate_response (without topic filter)")
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
    
    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int
    ) -> str:
        """
        Llama al LLM (OpenAI).
        
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
            # Preparar parámetros base
            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "response_format": {"type": "json_object"}  # Todos los modelos soportan esto
            }
            
            # Configuración específica por modelo
            if self.is_gpt5:
                # GPT-5.2: max_completion_tokens incluye TANTO reasoning como content.
                # Si pedimos 800 tokens y el modelo usa 780 en reasoning,
                # solo quedan 20 para el JSON → respuesta vacía.
                # Escalamos para dar suficiente headroom al reasoning.
                scaled_tokens = max(
                    max_tokens * self.GPT5_REASONING_MULTIPLIER,
                    self.GPT5_MIN_COMPLETION_TOKENS
                )
                params["max_completion_tokens"] = scaled_tokens
                logger.info(
                    f"Llamando GPT-5.2 | requested={max_tokens} "
                    f"| scaled max_completion_tokens={scaled_tokens}"
                )
                
                # Agregar reasoning_effort si está configurado
                if self.reasoning_effort:
                    params["reasoning_effort"] = self.reasoning_effort
                    logger.debug(f"Reasoning effort: {self.reasoning_effort}")
            else:
                # GPT-4.x: Usa max_tokens y temperature
                params["max_tokens"] = max_tokens
                params["temperature"] = self.temperature
                logger.debug(f"Llamando GPT-4 con max_tokens={max_tokens}, temperature={self.temperature}")
            
            # Llamar API
            response = self.openai_client.chat.completions.create(**params)
            
            content = response.choices[0].message.content
            
            # GPT-5.2 puede retornar content=None si agotó tokens en reasoning
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
