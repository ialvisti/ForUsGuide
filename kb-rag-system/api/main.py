"""
FastAPI Application - KB RAG System API.

API REST para el sistema RAG de Knowledge Base de Participant Advisory.

Endpoints:
- POST /api/v1/required-data - Determina qué datos se necesitan
- POST /api/v1/generate-response - Genera respuesta contextualizada
- GET /health - Health check
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import sys
from pathlib import Path

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.rag_engine import RAGEngine
from data_pipeline.pinecone_uploader import PineconeUploader
from .models import (
    RequiredDataRequest,
    RequiredDataResponse,
    GenerateResponseRequest,
    GenerateResponseResult,
    HealthResponse,
    ErrorResponse
)
from .config import settings, validate_settings
from .middleware import (
    authenticate_request,
    add_request_id,
    log_requests,
    handle_errors
)

# Configurar logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global instances (initialized on startup)
rag_engine: RAGEngine = None
pinecone_uploader: PineconeUploader = None


# ============================================================================
# Lifespan Context Manager
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager para startup y shutdown.
    """
    # Startup
    logger.info("=" * 80)
    logger.info("KB RAG System API - Starting Up")
    logger.info("=" * 80)
    
    try:
        # Validar configuración
        validate_settings()
        logger.info("✅ Configuration validated")
        
        # Inicializar RAG Engine
        global rag_engine
        rag_engine = RAGEngine(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODEL,
            temperature=settings.OPENAI_TEMPERATURE,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT if "gpt-5" in settings.OPENAI_MODEL.lower() else None
        )
        logger.info("✅ RAG Engine initialized")
        
        # Inicializar Pinecone Uploader
        global pinecone_uploader
        pinecone_uploader = PineconeUploader()
        logger.info("✅ Pinecone connection established")
        
        # Get stats
        stats = pinecone_uploader.get_index_stats()
        logger.info(f"📊 Total vectors in index: {stats.get('total_vectors', 0)}")
        
        logger.info("=" * 80)
        logger.info(f"🚀 API Ready on http://{settings.API_HOST}:{settings.API_PORT}")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}")
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down API...")


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title=settings.API_TITLE,
    description=settings.API_DESCRIPTION,
    version=settings.API_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom Middleware
app.middleware("http")(add_request_id)
app.middleware("http")(log_requests)
app.middleware("http")(handle_errors)


# ============================================================================
# Dependency Functions
# ============================================================================

async def verify_api_key(request: Request):
    """Dependency para verificar API key."""
    await authenticate_request(request)


def get_rag_engine() -> RAGEngine:
    """Dependency para obtener RAG engine."""
    if rag_engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG Engine not initialized"
        )
    return rag_engine


def get_pinecone() -> PineconeUploader:
    """Dependency para obtener Pinecone uploader."""
    if pinecone_uploader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pinecone not initialized"
        )
    return pinecone_uploader


# ============================================================================
# Routes
# ============================================================================

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.API_TITLE,
        "version": settings.API_VERSION,
        "status": "online",
        "docs": "/docs"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check(
    pinecone: PineconeUploader = Depends(get_pinecone)
):
    """
    Health check endpoint.
    
    Verifica el estado del servicio y sus dependencias.
    """
    try:
        # Check Pinecone connection
        stats = pinecone.get_index_stats()
        pinecone_connected = True
        total_vectors = stats.get('total_vectors', 0)
    except Exception as e:
        logger.error(f"Pinecone health check failed: {e}")
        pinecone_connected = False
        total_vectors = 0
    
    # Check OpenAI configuration
    openai_configured = bool(settings.OPENAI_API_KEY)
    
    return HealthResponse(
        status="healthy" if (pinecone_connected and openai_configured) else "degraded",
        version=settings.API_VERSION,
        pinecone_connected=pinecone_connected,
        openai_configured=openai_configured,
        total_vectors=total_vectors
    )


@app.post(
    "/api/v1/required-data",
    response_model=RequiredDataResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"]
)
async def required_data_endpoint(
    request: RequiredDataRequest,
    engine: RAGEngine = Depends(get_rag_engine)
):
    """
    Endpoint 1: Determina qué datos se necesitan para responder una inquiry.
    
    Este endpoint analiza la inquiry y el contexto de la KB para identificar
    qué campos específicos de datos del participante y plan se necesitan
    recolectar antes de poder generar una respuesta.
    
    **Flujo:**
    1. n8n detecta inquiry en ticket
    2. Llama este endpoint con inquiry + metadata
    3. API retorna lista de campos requeridos
    4. n8n → AI Mapper → ForUsBots para recolectar datos
    
    **Autenticación:** Requiere header `X-API-Key`
    """
    try:
        logger.info(f"Required data request | Topic: {request.topic} | RK: {request.record_keeper}")
        
        # Llamar RAG engine
        result = engine.get_required_data(
            inquiry=request.inquiry,
            record_keeper=request.record_keeper,
            plan_type=request.plan_type,
            topic=request.topic,
            related_inquiries=request.related_inquiries
        )
        
        logger.info(f"Required data completed | Confidence: {result.confidence}")
        
        # Convertir dataclass a dict para Pydantic
        return RequiredDataResponse(
            article_reference=result.article_reference,
            required_fields=result.required_fields,
            confidence=result.confidence,
            metadata=result.metadata
        )
    
    except Exception as e:
        logger.error(f"Error in required_data endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing request: {str(e)}"
        )


@app.post(
    "/api/v1/generate-response",
    response_model=GenerateResponseResult,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"]
)
async def generate_response_endpoint(
    request: GenerateResponseRequest,
    engine: RAGEngine = Depends(get_rag_engine)
):
    """
    Endpoint 2: Genera respuesta contextualizada usando datos recolectados.
    
    Este endpoint toma la inquiry, los datos recolectados del participante/plan,
    y genera una respuesta estructurada con steps, warnings, y guardrails.
    
    **Flujo:**
    1. ForUsBots recolectó datos requeridos
    2. n8n llama este endpoint con inquiry + collected_data
    3. API genera respuesta contextualizada
    4. n8n empaqueta y envía a DevRev AI
    
    **Token Budget:**
    - Dinámico basado en `total_inquiries_in_ticket`
    - 1 inquiry: ~3000 tokens
    - 2 inquiries: ~1500 tokens cada una
    - 3+ inquiries: ~1200 tokens cada una
    
    **Autenticación:** Requiere header `X-API-Key`
    """
    try:
        logger.info(
            f"Generate response request | "
            f"Topic: {request.topic} | "
            f"RK: {request.record_keeper} | "
            f"Max tokens: {request.max_response_tokens}"
        )
        
        # Llamar RAG engine
        result = engine.generate_response(
            inquiry=request.inquiry,
            record_keeper=request.record_keeper,
            plan_type=request.plan_type,
            topic=request.topic,
            collected_data=request.collected_data,
            max_response_tokens=request.max_response_tokens,
            total_inquiries_in_ticket=request.total_inquiries_in_ticket
        )
        
        logger.info(
            f"Generate response completed | "
            f"Decision: {result.decision} | "
            f"Confidence: {result.confidence}"
        )
        
        # Convertir dataclass a dict para Pydantic
        return GenerateResponseResult(
            decision=result.decision,
            confidence=result.confidence,
            response=result.response,
            guardrails=result.guardrails,
            metadata=result.metadata
        )
    
    except Exception as e:
        logger.error(f"Error in generate_response endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing request: {str(e)}"
        )


# ============================================================================
# Error Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handler para HTTPException."""
    request_id = getattr(request.state, "request_id", "unknown")
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "http_error",
            "message": exc.detail,
            "request_id": request_id
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handler para excepciones generales."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(f"Unhandled exception | Request ID: {request_id}")
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred",
            "request_id": request_id
        }
    )


# ============================================================================
# Run Server (for development)
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True,  # Only for development
        log_level=settings.LOG_LEVEL.lower()
    )
