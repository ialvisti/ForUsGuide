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
    
    record_keeper: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Record keeper (ej: 'LT Trust', 'Vanguard')",
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
    
    record_keeper: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Record keeper"
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
        default=1500,
        ge=500,
        le=3000,
        description="Máximo de tokens para la respuesta"
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
    data_type: str = Field(..., description="Tipo de dato: text, currency, date, boolean, number, list")
    required: bool = Field(..., description="Si el campo es obligatorio")


class ArticleReference(BaseModel):
    """Referencia al artículo fuente."""
    
    article_id: Optional[str] = Field(None, description="ID del artículo")
    title: Optional[str] = Field(None, description="Título del artículo")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")


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
    
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata del procesamiento"
    )


class ResponseStep(BaseModel):
    """Un paso en la respuesta."""
    
    step_number: int = Field(..., description="Número del paso")
    action: str = Field(..., description="Acción a realizar")
    note: Optional[str] = Field(None, description="Nota adicional o warning")


class ResponseSection(BaseModel):
    """Una sección de la respuesta."""
    
    topic: str = Field(..., description="Tema de esta sección")
    answer_components: List[str] = Field(..., description="Componentes de la respuesta")
    steps: List[ResponseStep] = Field(default_factory=list, description="Pasos a seguir")
    warnings: List[str] = Field(default_factory=list, description="Warnings importantes")
    outcomes: Optional[List[str]] = Field(default=None, description="Posibles resultados")


class Guardrails(BaseModel):
    """Guardrails aplicados."""
    
    must_not_say: List[str] = Field(
        default_factory=list,
        description="Cosas que se evitaron decir"
    )
    must_verify: List[str] = Field(
        default_factory=list,
        description="Cosas que deben verificarse"
    )


class GenerateResponseResult(BaseModel):
    """Response del endpoint /generate-response."""
    
    decision: DecisionType = Field(..., description="Decisión: can_proceed, uncertain, out_of_scope")
    
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score"
    )
    
    response: Dict[str, Any] = Field(
        ...,
        description="Respuesta estructurada con sections"
    )
    
    guardrails: Guardrails = Field(..., description="Guardrails aplicados")
    
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata del procesamiento"
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
    
    article_id: str
    article_title: str
    record_keeper: str
    plan_type: str
    topic: str
    chunk_tier: str
    chunk_type: str
    chunk_category: str
    content: str
    specific_topics: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    subtopics: Optional[List[str]] = None
    scope: Optional[str] = None


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
