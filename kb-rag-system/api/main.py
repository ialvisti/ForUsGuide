"""
FastAPI Application - KB RAG System API.

API REST para el sistema RAG de Knowledge Base de Participant Advisory.

Endpoints:
- POST /api/v1/required-data - Determina qué datos se necesitan
- POST /api/v1/generate-response - Genera respuesta contextualizada
- POST /api/v1/knowledge-question - Responde preguntas generales de KB (sin datos requeridos)
- GET /health - Health check
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import sys
from pathlib import Path

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.rag_engine import RAGEngine
from data_pipeline.pinecone_uploader import PineconeUploader
from data_pipeline.execution_logger import ExecutionLogger
from data_pipeline.llm_router import LLMRouter, build_routes_from_settings
from .models import (
    RequiredDataRequest,
    RequiredDataResponse,
    GenerateResponseRequest,
    GenerateResponseResult,
    HealthResponse,
    ErrorResponse,
    ListChunksRequest,
    ListChunksResponse,
    Chunk,
    ChunkMetadata,
    IndexStatsResponse,
    KnowledgeQuestionRequest,
    KnowledgeQuestionResponse,
    SourceArticle,
    UsedChunk
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

if settings.ENVIRONMENT == "production":
    try:
        import google.cloud.logging as cloud_logging
        cloud_logging.Client().setup_logging()
    except Exception:
        pass

logger = logging.getLogger(__name__)


# ============================================================================
# Lifespan Context Manager
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager para startup y shutdown.
    Stores instances on app.state instead of module-level globals.
    """
    # Startup
    logger.info("=" * 80)
    logger.info("KB RAG System API - Starting Up")
    logger.info("=" * 80)
    
    try:
        # Validar configuración
        validate_settings()
        logger.info("✅ Configuration validated")
        
        # Inicializar Pinecone Uploader → app.state (explicit settings)
        app.state.pinecone_uploader = PineconeUploader(
            api_key=settings.PINECONE_API_KEY,
            index_name=settings.INDEX_NAME,
            namespace=settings.NAMESPACE
        )
        logger.info("✅ Pinecone connection established")

        # Build hybrid LLM Router. In production (GCP) USE_VERTEX_AI=true
        # authenticates via ADC with no API key. Locally either OPENAI_API_KEY
        # or GEMINI_API_KEY (or both) must be set.
        llm_router = LLMRouter(
            openai_api_key=settings.OPENAI_API_KEY or None,
            gemini_api_key=settings.GEMINI_API_KEY or None,
            use_vertex_ai=settings.USE_VERTEX_AI,
            gcp_project=settings.GCP_PROJECT or None,
            gcp_location=settings.GCP_LOCATION,
        )
        llm_router.configure_routes(build_routes_from_settings(settings))
        app.state.llm_router = llm_router
        logger.info("✅ LLM Router configured")

        # Inicializar RAG Engine → app.state (shares Pinecone + LLM Router)
        app.state.rag_engine = RAGEngine(
            llm_router=llm_router,
            pinecone_uploader=app.state.pinecone_uploader,
        )
        logger.info("✅ RAG Engine initialized")
        
        # Get stats
        stats = app.state.pinecone_uploader.get_index_stats()
        logger.info(f"📊 Total vectors in index: {stats.get('total_vectors', 0)}")
        
        # Inicializar Execution Logger → app.state (Firestore, optional)
        if settings.ENABLE_EXECUTION_LOGGING:
            app.state.execution_logger = ExecutionLogger(
                project_id=settings.GCP_PROJECT or None
            )
            logger.info("✅ Execution logger initialized (Firestore)")
        else:
            app.state.execution_logger = None
        
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

# CORS Middleware — uses environment-aware origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom Middleware
app.middleware("http")(add_request_id)
app.middleware("http")(log_requests)
app.middleware("http")(handle_errors)

# Mount UI static files
UI_DIR = Path(__file__).parent.parent / "ui"
if UI_DIR.exists():
    app.mount("/ui/static", StaticFiles(directory=UI_DIR), name="ui-static")


# ============================================================================
# Dependency Functions (read from app.state, not globals)
# ============================================================================

async def verify_api_key(request: Request):
    """Dependency para verificar API key."""
    await authenticate_request(request)


def get_rag_engine(request: Request) -> RAGEngine:
    """Dependency para obtener RAG engine from app.state."""
    engine = getattr(request.app.state, "rag_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG Engine not initialized"
        )
    return engine


def get_pinecone(request: Request) -> PineconeUploader:
    """Dependency para obtener Pinecone uploader from app.state."""
    uploader = getattr(request.app.state, "pinecone_uploader", None)
    if uploader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pinecone not initialized"
        )
    return uploader


def get_execution_logger(request: Request) -> Optional[ExecutionLogger]:
    """Dependency para obtener execution logger from app.state (may be None)."""
    return getattr(request.app.state, "execution_logger", None)


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
        "docs": "/docs",
        "ui": "/ui"
    }


@app.get("/ui")
async def ui():
    """Serve the UI interface."""
    ui_file = Path(__file__).parent.parent / "ui" / "index.html"
    if ui_file.exists():
        return FileResponse(ui_file)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="UI not found"
        )


@app.get("/ui/chunks")
async def chunks_ui():
    """Serve the chunks viewer interface."""
    chunks_file = Path(__file__).parent.parent / "ui" / "chunks.html"
    if chunks_file.exists():
        return FileResponse(chunks_file)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chunks viewer not found"
        )


@app.get("/ui/knowledge")
async def knowledge_ui():
    """Serve the knowledge question interface."""
    knowledge_file = Path(__file__).parent.parent / "ui" / "knowledge.html"
    if knowledge_file.exists():
        return FileResponse(knowledge_file)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge question UI not found"
        )


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
    http_request: Request,
    engine: RAGEngine = Depends(get_rag_engine),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger)
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
    start = time.monotonic()
    try:
        logger.info(f"Required data request | Topic: {request.topic} | RK: {request.record_keeper}")
        
        result = await engine.get_required_data(
            inquiry=request.inquiry,
            record_keeper=request.record_keeper,
            plan_type=request.plan_type,
            topic=request.topic,
            related_inquiries=request.related_inquiries
        )
        
        logger.info(f"Required data completed | Confidence: {result.confidence}")
        
        response = RequiredDataResponse(
            article_reference=result.article_reference,
            required_fields=result.required_fields,
            confidence=result.confidence,
            source_articles=[
                SourceArticle(**sa) for sa in result.source_articles
            ],
            used_chunks=[
                UsedChunk(**uc) for uc in result.used_chunks
            ],
            coverage_gaps=result.coverage_gaps,
            metadata=result.metadata
        )
        
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="required_data",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data=response.model_dump(),
            )
        
        return response
    
    except Exception as e:
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="required_data",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data={},
                error=str(e),
            )
        logger.exception("Error in required_data endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the required-data request."
        )


@app.post(
    "/api/v1/generate-response",
    response_model=GenerateResponseResult,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"]
)
async def generate_response_endpoint(
    request: GenerateResponseRequest,
    http_request: Request,
    engine: RAGEngine = Depends(get_rag_engine),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger)
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
    - Default: 5000 tokens (siempre disponibles)
    - Se puede reducir vía `max_response_tokens` si se necesita
    
    **Autenticación:** Requiere header `X-API-Key`
    """
    start = time.monotonic()
    try:
        logger.info(
            f"Generate response request | "
            f"Topic: {request.topic} | "
            f"RK: {request.record_keeper} | "
            f"Max tokens: {request.max_response_tokens}"
        )
        
        result = await engine.generate_response(
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
        
        response = GenerateResponseResult(
            decision=result.decision,
            confidence=result.confidence,
            response=result.response,
            source_articles=[
                SourceArticle(**sa) for sa in result.source_articles
            ],
            used_chunks=[
                UsedChunk(**uc) for uc in result.used_chunks
            ],
            coverage_gaps=result.coverage_gaps,
            metadata=result.metadata
        )
        
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="generate_response",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data=response.model_dump(),
            )
        
        return response
    
    except Exception as e:
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="generate_response",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data={},
                error=str(e),
            )
        logger.exception("Error in generate_response endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while generating the response."
        )


@app.post(
    "/api/v1/knowledge-question",
    response_model=KnowledgeQuestionResponse,
    tags=["RAG Endpoints"]
)
async def knowledge_question_endpoint(
    request: KnowledgeQuestionRequest,
    http_request: Request,
    engine: RAGEngine = Depends(get_rag_engine),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger)
):
    """
    Endpoint 3: Answer a general knowledge question using the KB.
    
    This endpoint takes a plain question and returns an answer based on
    the knowledge base articles. No participant data, record keeper, or
    plan type is required — it performs a broad semantic search.
    
    **Use cases:**
    - Support agents looking up general 401(k) rules or processes
    - Quick knowledge base lookups via the UI
    - Testing KB coverage for a given topic
    
    **No autenticación requerida** (endpoint público para UI)
    """
    start = time.monotonic()
    try:
        logger.info(f"Knowledge question request | Q: {request.question[:80]}...")
        
        result = await engine.ask_knowledge_question(
            question=request.question
        )
        
        logger.info(f"Knowledge question completed | Coverage: {result.confidence_note}")
        
        response = KnowledgeQuestionResponse(
            answer=result.answer,
            key_points=result.key_points,
            source_articles=[
                SourceArticle(**sa) for sa in result.source_articles
            ],
            used_chunks=[
                UsedChunk(**uc) for uc in result.used_chunks
            ],
            confidence_note=result.confidence_note,
            metadata=result.metadata
        )
        
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="knowledge_question",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data=response.model_dump(),
            )
        
        return response
    
    except Exception as e:
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="knowledge_question",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data={},
                error=str(e),
            )
        logger.exception("Error in knowledge_question endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the knowledge question."
        )


@app.post(
    "/api/v1/chunks",
    response_model=ListChunksResponse,
    tags=["Chunks Management"]
)
async def list_chunks_endpoint(
    request: ListChunksRequest,
    pinecone: PineconeUploader = Depends(get_pinecone)
):
    """
    Lista chunks de Pinecone con filtros opcionales.
    
    Uses Pinecone's list + fetch API (no semantic search) when an article_id
    is provided. Falls back to semantic search only when no article_id is given.
    
    **No requiere autenticación** (endpoint público para UI)
    """
    try:
        logger.info(f"List chunks request | Filters: article_id={request.article_id}, tier={request.tier}, type={request.chunk_type}")
        
        if request.article_id:
            # Preferred path: list + fetch (no semantic search, deterministic)
            raw_chunks = pinecone.list_and_fetch_chunks(
                prefix=request.article_id,
                limit=request.limit,
                tier=request.tier,
                chunk_type=request.chunk_type
            )
        else:
            # Fallback: semantic search with contextual query
            query_parts = []
            if request.tier:
                query_parts.append(f"{request.tier} priority")
            if request.chunk_type:
                query_parts.append(f"{request.chunk_type}")
            query_parts.append("knowledge base article content")
            query_text = " ".join(query_parts)
            
            filter_dict = {}
            if request.tier:
                filter_dict["chunk_tier"] = {"$eq": request.tier}
            if request.chunk_type:
                filter_dict["chunk_type"] = {"$eq": request.chunk_type}
            
            raw_chunks = pinecone.query_chunks(
                query_text=query_text,
                top_k=request.limit,
                filter_dict=filter_dict if filter_dict else None
            )
        
        # Convertir a modelo Pydantic
        chunks = []
        for raw_chunk in raw_chunks:
            try:
                chunk = Chunk(
                    id=raw_chunk['id'],
                    score=raw_chunk['score'],
                    metadata=ChunkMetadata(**raw_chunk['metadata'])
                )
                chunks.append(chunk)
            except Exception as e:
                logger.warning(f"Error parsing chunk {raw_chunk.get('id')}: {e}")
                continue
        
        logger.info(f"List chunks completed | Found: {len(chunks)} chunks")
        
        return ListChunksResponse(
            chunks=chunks,
            total=len(chunks),
            filters_applied={
                "article_id": request.article_id,
                "tier": request.tier,
                "chunk_type": request.chunk_type,
                "limit": request.limit
            }
        )
    
    except Exception as e:
        logger.exception("Error in list_chunks endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while listing chunks."
        )


@app.get(
    "/api/v1/index-stats",
    response_model=IndexStatsResponse,
    tags=["Chunks Management"]
)
async def index_stats_endpoint(
    pinecone: PineconeUploader = Depends(get_pinecone)
):
    """
    Obtiene estadísticas del índice de Pinecone.
    
    **No requiere autenticación** (endpoint público para UI)
    """
    try:
        stats = pinecone.get_index_stats()
        
        return IndexStatsResponse(
            total_vectors=stats.get('total_vectors', 0),
            namespaces=stats.get('namespaces', {})
        )
    
    except Exception as e:
        logger.exception("Error getting index stats")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while retrieving index stats."
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
