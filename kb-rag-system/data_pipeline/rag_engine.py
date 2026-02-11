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
class ResponseSection:
    """Sección de respuesta."""
    topic: str
    answer_components: List[str]
    steps: List[Dict[str, Any]]
    warnings: List[str]
    outcomes: Optional[List[str]] = None


@dataclass
class GenerateResponseResult:
    """Respuesta del endpoint /generate-response."""
    decision: str  # "can_proceed", "uncertain", "out_of_scope"
    confidence: float
    response: Dict[str, Any]
    guardrails: Dict[str, List[str]]
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
            
            # 2. Construir contexto (budget pequeño para este endpoint)
            context_budget = 1500  # Tokens para contexto
            context, selected_chunks, tokens_used = self._build_context_from_chunks(
                chunks=chunks,
                budget=context_budget,
                prioritize_types=['required_data', 'eligibility', 'business_rules']
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
            
            # 6. Calcular confidence
            confidence = self._calculate_confidence(chunks)
            
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
            context_budget = self.token_manager.calculate_context_budget(
                max_response_tokens,
                reserve_for_response=0.35  # 35% para respuesta del LLM
            )
            
            logger.info(f"Context budget: {context_budget} tokens")
            
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
            
            # 5. Generar prompts
            system_prompt, user_prompt = build_generate_response_prompt(
                context=context,
                inquiry=inquiry,
                collected_data=collected_data,
                record_keeper=record_keeper,
                plan_type=plan_type,
                topic=topic,
                max_tokens=max_response_tokens
            )
            
            # 6. Calcular tokens para completion
            completion_budget = max_response_tokens - tokens_used
            completion_budget = max(500, completion_budget)  # Mínimo 500 tokens
            
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
                # Fallback a respuesta básica
                parsed = {
                    "sections": [{
                        "topic": topic,
                        "answer_components": [llm_response[:1000]],
                        "steps": [],
                        "warnings": ["Response parsing failed"]
                    }],
                    "guardrails_applied": [],
                    "data_gaps": []
                }
            
            # 9. Calcular confidence y decision
            confidence = self._calculate_confidence(chunks)
            decision = self._determine_decision(confidence)
            
            # 10. Construir respuesta
            return GenerateResponseResult(
                decision=decision,
                confidence=confidence,
                response=parsed,
                guardrails={
                    "must_not_say": parsed.get("guardrails_applied", []),
                    "must_verify": []
                },
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
    
    # Umbral mínimo para considerar que una búsqueda con filtro de topic
    # tuvo resultados suficientes. Si no se alcanza, se hace fallback sin topic.
    TOPIC_FILTER_MIN_CHUNKS = 3
    TOPIC_FILTER_MIN_SCORE = 0.20
    
    def _search_for_required_data(
        self,
        inquiry: str,
        record_keeper: str,
        plan_type: str,
        topic: str
    ) -> List[Dict[str, Any]]:
        """Busca chunks relevantes para required_data endpoint."""
        # Filtros base (obligatorios)
        base_filters = {
            "record_keeper": {"$eq": record_keeper},
            "plan_type": {"$eq": plan_type},
            "chunk_type": {"$in": ["required_data", "eligibility", "business_rules"]}
        }
        
        # Intentar primero con filtro de topic para mayor precisión
        if topic:
            topic_filters = {**base_filters, "topic": {"$eq": topic}}
            chunks = self.pinecone.query_chunks(
                query_text=inquiry,
                top_k=10,
                filter_dict=topic_filters
            )
            
            if self._topic_results_sufficient(chunks):
                logger.info(f"Found {len(chunks)} chunks for required_data (with topic filter: {topic})")
                return chunks
            
            logger.info(f"Topic filter '{topic}' returned insufficient results ({len(chunks)} chunks), falling back without topic")
        
        # Fallback: buscar sin filtro de topic
        chunks = self.pinecone.query_chunks(
            query_text=inquiry,
            top_k=10,
            filter_dict=base_filters
        )
        
        logger.info(f"Found {len(chunks)} chunks for required_data (without topic filter)")
        return chunks
    
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
        query_parts = [inquiry]
        
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
        
        # Intentar primero con filtro de topic para mayor precisión
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
            
            logger.info(f"Topic filter '{topic}' returned insufficient results ({len(chunks)} chunks), falling back without topic")
        
        # Fallback: buscar sin filtro de topic
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
        selected = []
        tokens_used = 0
        
        for chunk in ordered_chunks:
            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)
            
            if tokens_used + chunk_tokens <= budget:
                selected.append(chunk)
                tokens_used += chunk_tokens
            else:
                break
        
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
            max_tokens: Máximo de tokens para completion
        
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
                # GPT-5.2: Usa max_completion_tokens en lugar de max_tokens
                params["max_completion_tokens"] = max_tokens
                logger.debug(f"Llamando GPT-5.2 con max_completion_tokens={max_tokens}")
                
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
            logger.debug(f"LLM response: {len(content)} characters")
            
            return content
        
        except Exception as e:
            logger.error(f"Error llamando LLM: {e}")
            raise
    
    # ========================================================================
    # Helper Methods - Confidence & Decision
    # ========================================================================
    
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
        """Construye respuesta de fallback."""
        return GenerateResponseResult(
            decision="out_of_scope",
            confidence=confidence,
            response={
                "sections": [{
                    "topic": "error",
                    "answer_components": [
                        f"Unable to generate response: {reason}"
                    ],
                    "steps": [],
                    "warnings": ["This inquiry may require human review"]
                }]
            },
            guardrails={
                "must_not_say": [],
                "must_verify": []
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
