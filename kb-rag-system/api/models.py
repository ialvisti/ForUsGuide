"""
Modelos Pydantic para request/response de la API.

Define la estructura de datos para los endpoints:
- /api/v1/required-data
- /api/v1/generate-response
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator
from enum import Enum


# ============================================================================
# Enums
# ============================================================================

class DecisionType(str, Enum):
    """Tipos de decisión para generate_response."""
    CAN_PROCEED = "can_proceed"
    UNCERTAIN = "uncertain"
    OUT_OF_SCOPE = "out_of_scope"


class PlanType(str, Enum):
    """Tipos de plan soportados."""
    PLAN_401K = "401(k)"
    PLAN_403B = "403(b)"
    PLAN_457 = "457"


# ============================================================================
# Request Models
# ============================================================================

class RequiredDataRequest(BaseModel):
    """Request para el endpoint /required-data."""
    
    inquiry: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="La consulta del participante"
    )
    
    record_keeper: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Record keeper (ej: 'LT Trust', 'Vanguard'). "
            "When provided, RK-specific articles are prioritized. "
            "When omitted, global articles are searched first."
        ),
        examples=["LT Trust", "Vanguard", "Fidelity"]
    )
    
    plan_type: str = Field(
        ...,
        description="Tipo de plan",
        examples=["401(k)", "403(b)", "457"]
    )
    
    topic: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Tema principal de la consulta",
        examples=["rollover", "distribution", "loan", "hardship"]
    )
    
    related_inquiries: Optional[List[str]] = Field(
        default=None,
        description="Otras inquiries relacionadas en el mismo ticket"
    )
    
    @field_validator('inquiry')
    @classmethod
    def validate_inquiry(cls, v: str) -> str:
        """Valida que la inquiry no esté vacía."""
        if not v.strip():
            raise ValueError("Inquiry cannot be empty")
        return v.strip()
    
    @field_validator('record_keeper')
    @classmethod
    def validate_record_keeper(cls, v: Optional[str]) -> Optional[str]:
        """Normaliza record_keeper: strip whitespace, treat empty string as None."""
        if v is None:
            return None
        v = v.strip()
        return v if v else None
    
    @field_validator('topic')
    @classmethod
    def validate_topic(cls, v: str) -> str:
        """Normaliza el topic a lowercase."""
        return v.lower().strip()


class GenerateResponseRequest(BaseModel):
    """Request para el endpoint /generate-response."""
    
    inquiry: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="La consulta del participante"
    )
    
    record_keeper: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Record keeper. When provided, RK-specific articles are prioritized. "
            "When omitted, global articles are searched first."
        )
    )
    
    plan_type: str = Field(
        ...,
        description="Tipo de plan"
    )
    
    topic: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Tema principal"
    )
    
    collected_data: Dict[str, Any] = Field(
        ...,
        description="Datos recolectados del participante y plan"
    )
    
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Contexto adicional del ticket"
    )
    
    max_response_tokens: Optional[int] = Field(
        default=5500,
        ge=500,
        le=5500,
        description="Máximo de tokens para la respuesta (default: 5500)"
    )
    
    total_inquiries_in_ticket: Optional[int] = Field(
        default=1,
        ge=1,
        le=10,
        description="Total de inquiries en el ticket"
    )
    
    @field_validator('inquiry')
    @classmethod
    def validate_inquiry(cls, v: str) -> str:
        """Valida que la inquiry no esté vacía."""
        if not v.strip():
            raise ValueError("Inquiry cannot be empty")
        return v.strip()
    
    @field_validator('record_keeper')
    @classmethod
    def validate_record_keeper(cls, v: Optional[str]) -> Optional[str]:
        """Normaliza record_keeper: strip whitespace, treat empty string as None."""
        if v is None:
            return None
        v = v.strip()
        return v if v else None
    
    @field_validator('topic')
    @classmethod
    def validate_topic(cls, v: str) -> str:
        """Normaliza el topic."""
        return v.lower().strip()


# ============================================================================
# Response Models
# ============================================================================

class RequiredField(BaseModel):
    """Campo de datos requerido."""
    
    field: str = Field(..., description="Nombre del campo")
    description: str = Field(..., description="Descripción del campo")
    why_needed: str = Field(..., description="Por qué se necesita este campo")
    data_type: str = Field(..., description="Tipo de dato: text, currency, date, boolean, number, or list[<element_type>] (e.g. list[text], list[currency])")
    required: bool = Field(..., description="Si el campo es obligatorio")


class ArticleReference(BaseModel):
    """Referencia al artículo fuente."""
    
    article_id: Optional[str] = Field(None, description="ID del artículo")
    title: Optional[str] = Field(None, description="Título del artículo")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")


class SourceArticle(BaseModel):
    """Artículo fuente referenciado en la respuesta (deduplicated by article_id)."""
    
    article_id: Optional[str] = Field(None, description="ID del artículo")
    article_title: Optional[str] = Field(None, description="Título del artículo")
    chunk_types_used: Optional[str] = Field(
        None,
        description="Comma-separated chunk types that contributed (e.g. 'business_rules, steps, faqs')"
    )
    relevance: Optional[str] = Field(None, description="Por qué este artículo es relevante")
    used_info: bool = Field(
        True,
        description="Whether the article had chunks with scores above the relevance threshold"
    )
    max_score: Optional[float] = Field(
        None,
        description="Highest chunk score for this article"
    )


class UsedChunk(BaseModel):
    """Individual chunk used by the LLM to generate the answer."""
    
    chunk_id: str = Field(..., description="Pinecone vector ID")
    score: float = Field(..., description="Pinecone similarity score")
    chunk_type: str = Field(..., description="Chunk type (faqs, business_rules, steps, etc.)")
    chunk_tier: str = Field(..., description="Chunk tier (critical, high, medium, low)")
    article_id: str = Field(..., description="Parent article ID")
    article_title: str = Field(..., description="Parent article title")
    content_preview: str = Field(..., description="First ~200 characters of content")
    content: str = Field(..., description="Full chunk content")


class RequiredDataResponse(BaseModel):
    """Response del endpoint /required-data."""
    
    article_reference: ArticleReference = Field(..., description="Artículo de referencia")
    
    required_fields: Dict[str, List[RequiredField]] = Field(
        ...,
        description="Campos requeridos organizados por categoría"
    )
    
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score general"
    )
    
    source_articles: List[SourceArticle] = Field(
        default_factory=list,
        description="Artículos fuente consultados para la respuesta"
    )
    
    used_chunks: List[UsedChunk] = Field(
        default_factory=list,
        description="Individual chunks fed to the LLM, ordered by score descending"
    )
    
    coverage_gaps: List[str] = Field(
        default_factory=list,
        description="Core topics the inquiry asks about that are entirely absent from KB context"
    )
    
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata del procesamiento"
    )


class ResponseStep(BaseModel):
    """Un paso en la respuesta."""
    
    step_number: int = Field(..., description="Número del paso")
    action: str = Field(..., description="Acción a realizar")
    detail: Optional[str] = Field(None, description="Contexto adicional o sub-instrucciones")


class QuestionToAsk(BaseModel):
    """Pregunta para el participante cuando faltan datos."""
    
    question: str = Field(..., description="Texto de la pregunta")
    why: str = Field(..., description="Por qué se necesita esta información")


class Escalation(BaseModel):
    """Info de escalación a Support."""
    
    needed: bool = Field(..., description="Si se requiere escalación a Support")
    reason: Optional[str] = Field(None, description="Razón de la escalación")


class ResponseToParticipant(BaseModel):
    """Contenido de la respuesta al participante."""
    
    opening: str = Field(..., description="Resumen personalizado en 1-2 oraciones")
    key_points: List[str] = Field(
        default_factory=list,
        description="Hechos esenciales, cada uno único y sin superposición"
    )
    steps: List[ResponseStep] = Field(
        default_factory=list,
        description="Pasos secuenciales que el participante debe seguir"
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Advertencias consolidadas (cada una única)"
    )


class OutcomeType(str, Enum):
    """Tipos de outcome determinados por el LLM basado en reglas del artículo."""
    CAN_PROCEED = "can_proceed"
    BLOCKED_NOT_ELIGIBLE = "blocked_not_eligible"
    BLOCKED_MISSING_DATA = "blocked_missing_data"
    AMBIGUOUS_PLAN_RULES = "ambiguous_plan_rules"


class GenerateResponseResult(BaseModel):
    """
    Response del endpoint /generate-response.
    
    Dos niveles de determinación:
    - decision/confidence: Calidad del retrieval RAG (calculado por el engine).
    - response.outcome: Determinación del caso del participante (calculado por el LLM).
    """
    
    decision: DecisionType = Field(
        ...,
        description="Calidad del retrieval RAG: can_proceed, uncertain, out_of_scope"
    )
    
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score del retrieval"
    )
    
    response: Dict[str, Any] = Field(
        ...,
        description=(
            "Respuesta outcome-driven del LLM. Contiene: outcome, outcome_reason, "
            "response_to_participant, questions_to_ask, escalation, "
            "guardrails_applied, data_gaps"
        )
    )
    
    source_articles: List[SourceArticle] = Field(
        default_factory=list,
        description="Artículos fuente consultados para la respuesta"
    )
    
    used_chunks: List[UsedChunk] = Field(
        default_factory=list,
        description="Individual chunks fed to the LLM, ordered by score descending"
    )
    
    coverage_gaps: List[str] = Field(
        default_factory=list,
        description="Core topics the inquiry asks about that are entirely absent from KB context"
    )
    
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata del procesamiento (chunks_used, tokens, modelo)"
    )


# ============================================================================
# Health & Error Models
# ============================================================================

class HealthResponse(BaseModel):
    """Response del health check."""
    
    status: str = Field(..., description="Estado del servicio")
    version: str = Field(..., description="Versión de la API")
    pinecone_connected: bool = Field(..., description="Si Pinecone está conectado")
    openai_configured: bool = Field(..., description="Si OpenAI está configurado")
    total_vectors: int = Field(..., description="Total de vectores en Pinecone")


class ErrorResponse(BaseModel):
    """Response de error estándar."""
    
    error: str = Field(..., description="Tipo de error")
    message: str = Field(..., description="Mensaje de error")
    detail: Optional[str] = Field(None, description="Detalles adicionales")
    request_id: Optional[str] = Field(None, description="ID de la request para tracking")


# ============================================================================
# Chunks Models
# ============================================================================

class ChunkMetadata(BaseModel):
    """Metadata de un chunk."""
    
    # Metadata del artículo
    article_id: str
    article_title: str
    description: Optional[str] = None
    record_keeper: Optional[str] = None
    plan_type: Optional[str] = None
    scope: Optional[str] = None
    tags: Optional[List[str]] = None
    
    # Topics
    topic: Optional[str] = None
    subtopics: Optional[List[str]] = None
    
    # Metadata del chunk
    chunk_tier: str
    chunk_type: str
    chunk_category: str
    chunk_index: Optional[int] = None
    content: str
    content_hash: Optional[str] = None
    specific_topics: Optional[List[str]] = None
    
    # Fechas (disponibles cuando el artículo las incluye)
    source_last_updated: Optional[str] = None
    transformed_at: Optional[str] = None


class Chunk(BaseModel):
    """Modelo de un chunk."""
    
    id: str
    score: float
    metadata: ChunkMetadata


class ListChunksRequest(BaseModel):
    """Request para listar chunks."""
    
    article_id: Optional[str] = Field(
        None,
        description="Filtrar por article_id específico"
    )
    
    tier: Optional[str] = Field(
        None,
        description="Filtrar por tier: critical, high, medium, low"
    )
    
    chunk_type: Optional[str] = Field(
        None,
        description="Filtrar por tipo de chunk"
    )
    
    limit: Optional[int] = Field(
        default=100,
        ge=1,
        le=1000,
        description="Máximo número de chunks a retornar"
    )


class ListChunksResponse(BaseModel):
    """Response del endpoint /chunks."""
    
    chunks: List[Chunk] = Field(..., description="Lista de chunks encontrados")
    total: int = Field(..., description="Total de chunks retornados")
    filters_applied: Dict[str, Any] = Field(..., description="Filtros aplicados")


class IndexStatsResponse(BaseModel):
    """Response de estadísticas del índice."""
    
    total_vectors: int = Field(..., description="Total de vectores en el índice")
    namespaces: Dict[str, Any] = Field(..., description="Información de namespaces")


# ============================================================================
# Knowledge Question Models
# ============================================================================

class KnowledgeQuestionRequest(BaseModel):
    """Request para el endpoint /knowledge-question."""
    
    question: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="General knowledge question about 401(k) plans, processes, or rules"
    )
    
    @field_validator('question')
    @classmethod
    def validate_question(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Question cannot be empty")
        return v.strip()


class KnowledgeQuestionResponse(BaseModel):
    """Response del endpoint /knowledge-question."""
    
    answer: str = Field(..., description="Respuesta completa basada en la KB")
    
    key_points: List[str] = Field(
        default_factory=list,
        description="Puntos clave extraídos de la respuesta"
    )
    
    source_articles: List[SourceArticle] = Field(
        default_factory=list,
        description="Artículos fuente usados para la respuesta"
    )
    
    used_chunks: List[UsedChunk] = Field(
        default_factory=list,
        description="Individual chunks fed to the LLM, ordered by score descending"
    )
    
    confidence_note: str = Field(
        ...,
        description="Nivel de cobertura: well_covered, partially_covered, limited_coverage"
    )
    
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata del procesamiento (chunks_used, tokens, modelo)"
    )
