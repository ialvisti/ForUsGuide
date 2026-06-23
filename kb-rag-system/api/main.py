"""
FastAPI Application - KB RAG System API.

API REST para el sistema RAG de Knowledge Base de Participant Advisory.

Endpoints:
- POST /api/v1/required-data - Determina qué datos se necesitan
- POST /api/v1/generate-response - Genera respuesta contextualizada
- POST /api/v1/knowledge-question - Responde preguntas generales de KB (sin datos requeridos)
- POST /api/v1/route-inquiry - Clasifica una inquiry hacia el endpoint downstream
- GET /health - Health check
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from cachetools import TTLCache
import sys
from pathlib import Path

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env into os.environ so vars consumed by SDKs (e.g. Google ADC reading
# GOOGLE_APPLICATION_CREDENTIALS) are visible. pydantic-settings only populates
# the Settings object, it does not export to os.environ. No-op in Cloud Run
# where .env is absent and ADC comes from the metadata server.
from dotenv import load_dotenv
load_dotenv()

from data_pipeline.rag_engine import RAGEngine
from data_pipeline.pinecone_uploader import PineconeUploader
from data_pipeline.execution_logger import ExecutionLogger
from data_pipeline.llm_router import LLMRouter, build_routes_from_settings
from data_pipeline.inquiry_router import (
    COVERAGE_TOP_K,
    CoveragePack,
    InquiryRouterEngine,
)
from data_pipeline.forusbots_client import ForusBotsClient
from data_pipeline.ticket_orchestrator import (
    InquiryOutcome,
    OrchestratorDeps,
    TicketOrchestrator,
)
from data_pipeline.ticket_jobs import TicketJobStore
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
    RouteInquiryRequest,
    RouteInquiryResponse,
    SourceArticle,
    UsedChunk,
    HandleTicketRequest,
    TicketHandleResponse,
    TicketJobHandle,
    TicketStatusResponse,
    InquiryResult,
    RouteDecision,
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


def _hit_count(value: Any) -> int:
    """Best-effort hit count for diagnostics without assuming concrete type."""
    try:
        return len(value)
    except TypeError:
        return 1 if value else 0


def _log_pinecone_startup_diagnostics(
    pinecone_uploader: PineconeUploader,
    stats: Dict[str, Any],
) -> None:
    """Emit safe Pinecone diagnostics and a one-hit search smoke test."""
    try:
        import pinecone as _pinecone

        sdk_version = getattr(_pinecone, "__version__", "unknown")
    except Exception:
        sdk_version = "unknown"

    index_name = getattr(pinecone_uploader, "index_name", settings.INDEX_NAME)
    namespace = getattr(pinecone_uploader, "namespace", settings.NAMESPACE)
    total_vectors = stats.get("total_vectors", "unknown")

    logger.info(
        "Pinecone diagnostics | sdk_version=%s | index=%s | namespace=%s | "
        "total_vectors=%s",
        sdk_version,
        index_name,
        namespace,
        total_vectors,
    )

    try:
        smoke_chunks = pinecone_uploader.query_chunks(
            query_text="knowledge base article content",
            top_k=1,
            filter_dict=None,
        )
    except Exception as exc:
        logger.warning(
            "Pinecone smoke search failed | index=%s | namespace=%s | "
            "error_type=%s",
            index_name,
            namespace,
            type(exc).__name__,
        )
        return

    hits = _hit_count(smoke_chunks)
    if hits < 1:
        logger.warning(
            "Pinecone smoke search returned 0 hits | index=%s | namespace=%s | "
            "total_vectors=%s",
            index_name,
            namespace,
            total_vectors,
        )
        return

    logger.info(
        "Pinecone smoke search ok | index=%s | namespace=%s | hits=%s",
        index_name,
        namespace,
        hits,
    )


def _make_coverage_pack_builder(rag_engine: RAGEngine):
    """Build an async callable that retrieves the top-K KB chunks for an
    inquiry and packages them into a :class:`CoveragePack` for the classifier.

    The pack carries enough structure (chunk_type, chunk_tier, topic,
    article_title, excerpt, score) for the LLM to decide — by looking at the
    actual content — whether the chunks directly answer the question (KQ),
    point to an eligibility flow (GR), or only match topically (NMI).

    Pinecone exceptions and empty results are converted into
    ``CoveragePack.failed`` / ``CoveragePack.empty``; both states steer the
    LLM toward NMI via the prompt.
    """

    async def _builder(inquiry: str) -> CoveragePack:
        try:
            chunks = await rag_engine._cached_query(
                query_text=inquiry, top_k=COVERAGE_TOP_K, filter_dict=None
            )
        except Exception as exc:
            logger.warning(
                "Coverage retrieval failed (%s); returning failed pack.",
                type(exc).__name__,
            )
            return CoveragePack.failed(type(exc).__name__)

        if not chunks:
            logger.info("Coverage retrieval returned 0 chunks.")
            return CoveragePack.empty()

        top_score = max(
            (float(c.get("score", 0.0) or 0.0) for c in chunks), default=0.0
        )

        # Preserve order of first appearance — the LLM uses position as a
        # secondary signal of relevance after score.
        distinct_articles: List[str] = []
        chunk_types_present: List[str] = []
        for c in chunks:
            md = c.get("metadata", {}) or {}
            title = md.get("article_title") or md.get("title")
            if title and title not in distinct_articles:
                distinct_articles.append(title)
            chunk_type = md.get("chunk_type")
            if chunk_type and chunk_type not in chunk_types_present:
                chunk_types_present.append(chunk_type)

        return CoveragePack(
            retrieval_status="ok",
            top_score=top_score,
            chunk_count=len(chunks),
            distinct_articles=distinct_articles,
            chunk_types_present=chunk_types_present,
            chunks=chunks,
        )

    return _builder


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

        # Inicializar Inquiry Router → app.state. Shares the LLM Router and
        # a coverage_pack_builder that retrieves the top-K KB chunks via
        # Pinecone before each classification, so the LLM reasons about
        # actual KB content rather than just surface text patterns.
        app.state.inquiry_router = InquiryRouterEngine(
            llm_router=llm_router,
            coverage_pack_builder=_make_coverage_pack_builder(
                app.state.rag_engine
            ),
        )
        logger.info("✅ Inquiry Router initialized")
        
        # Get stats
        stats = app.state.pinecone_uploader.get_index_stats()
        logger.info(f"📊 Total vectors in index: {stats.get('total_vectors', 0)}")
        _log_pinecone_startup_diagnostics(app.state.pinecone_uploader, stats)
        
        # Inicializar Execution Logger → app.state (Firestore, optional)
        if settings.ENABLE_EXECUTION_LOGGING:
            app.state.execution_logger = ExecutionLogger(
                project_id=settings.GCP_PROJECT or None
            )
            logger.info("✅ Execution logger initialized (Firestore)")
        else:
            app.state.execution_logger = None

        # End-to-end ticket handler wiring: ForusBots client + in-process job
        # store + idempotency cache + background-task registry. The orchestrator
        # itself is built per-request (cheap) from these on app.state.
        app.state.forusbots_client = ForusBotsClient.from_settings(settings)
        app.state.ticket_jobs = TicketJobStore(ttl_s=settings.TICKET_JOB_TTL_S)
        app.state.ticket_idem = TTLCache(maxsize=2048, ttl=settings.TICKET_JOB_TTL_S)
        app.state.bg_tasks = set()
        logger.info(
            "✅ Ticket handler wired (mode=%s)", settings.TICKET_HANDLER_MODE
        )

        logger.info("=" * 80)
        logger.info(f"🚀 API Ready on http://{settings.API_HOST}:{settings.API_PORT}")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}")
        raise
    
    yield

    # Shutdown
    logger.info("Shutting down API...")
    forusbots = getattr(app.state, "forusbots_client", None)
    if forusbots is not None:
        try:
            await forusbots.aclose()
        except Exception:
            logger.exception("Error closing ForusBots client")


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


def get_inquiry_router(request: Request) -> InquiryRouterEngine:
    """Dependency para obtener Inquiry Router engine from app.state."""
    engine = getattr(request.app.state, "inquiry_router", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inquiry Router not initialized"
        )
    return engine


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


@app.get("/ui/router")
async def router_ui():
    """Serve the inquiry router interface."""
    router_file = Path(__file__).parent.parent / "ui" / "router.html"
    if router_file.exists():
        return FileResponse(router_file)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Router UI not found"
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
        total_vectors=total_vectors,
        router_mode=settings.ROUTER_MODE,
        ticket_handler_mode=settings.TICKET_HANDLER_MODE,
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


# ============================================================================
# Route Inquiry (Endpoint 4)
# ============================================================================

def _build_suggested_call(
    inquiry: str,
    route: str,
) -> Tuple[str, Dict[str, Any]]:
    """Build the downstream endpoint path + ready-to-send payload for ``route``.

    The slim ``RouteInquiryRequest`` only carries the inquiry, so for the
    ``generate_response`` route we return a TEMPLATE with ``record_keeper``,
    ``plan_type``, ``topic``, and ``collected_data`` as placeholder ``None`` /
    ``{}``. The caller is expected to fill those in before invoking the
    downstream ``/api/v1/generate-response`` endpoint (whose own request model
    declares them as required).
    """
    if route == "knowledge_question":
        return "/api/v1/knowledge-question", {"question": inquiry}
    if route == "generate_response":
        return "/api/v1/generate-response", {
            "inquiry": inquiry,
            "record_keeper": None,
            "plan_type": None,
            "topic": None,
            "collected_data": {},
        }
    # needs_more_info → caller should run the existing required-data flow first
    return "/api/v1/required-data", {"inquiry": inquiry}


def _apply_router_mode(route: str, mode: str) -> Tuple[str, Optional[str]]:
    """Apply per-request/global rollout gating to the classifier output.

    Returns ``(effective_route, override_reason)``. ``override_reason`` is non-
    None only when the mode coerced the original route — useful for metadata
    observability. Caller is responsible for raising 503 on ``disabled``.
    """
    if mode == "shadow" and route != "needs_more_info":
        return "needs_more_info", f"router_mode=shadow coerced route from {route!r}"
    if mode == "knowledge_only" and route == "generate_response":
        return "needs_more_info", "router_mode=knowledge_only coerced generate_response"
    return route, None


@app.post(
    "/api/v1/route-inquiry",
    response_model=RouteInquiryResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"],
)
async def route_inquiry_endpoint(
    request: RouteInquiryRequest,
    http_request: Request,
    router_engine: InquiryRouterEngine = Depends(get_inquiry_router),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger),
):
    """
    Endpoint 4: Classify an inquiry to choose the right downstream endpoint.

    Accepts only ``inquiry`` and an optional ``router_mode`` override. Returns
    the routing decision plus a ``suggested_endpoint``/``suggested_payload``
    template the caller invokes next. When ``route == 'needs_more_info'``,
    ``user_message`` is populated with a participant-ready prompt asking for
    the missing detail.

    **Routes:**
    - ``knowledge_question`` → punctual KB lookup (`/api/v1/knowledge-question`)
    - ``generate_response`` → eligibility/outcome (`/api/v1/generate-response`)
    - ``needs_more_info`` → ambiguous; fall back to today's `required-data` flow

    **Autenticación:** Requiere header ``X-API-Key``.
    """
    start = time.monotonic()
    effective_mode = request.router_mode or settings.ROUTER_MODE

    if effective_mode == "disabled":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inquiry router is disabled.",
        )

    try:
        logger.info(
            f"Route inquiry request | "
            f"router_mode={effective_mode} | "
            f"len={len(request.inquiry)}"
        )

        result = await router_engine.classify(inquiry=request.inquiry)

        effective_route, override_reason = _apply_router_mode(
            result.route, effective_mode
        )

        suggested_endpoint, suggested_payload = _build_suggested_call(
            request.inquiry, effective_route
        )

        # If the override forced the route to needs_more_info, the LLM never
        # produced a user_message for that bucket — fall back to the engine's
        # default so the response contract holds.
        if effective_route == "needs_more_info" and not result.user_message:
            user_message = (
                "Could you share a bit more detail about what you'd like help with?"
            )
        elif effective_route != "needs_more_info":
            user_message = None
        else:
            user_message = result.user_message

        # ``result.metadata`` already carries ``coverage_signals`` (the
        # retrieval_status / top_score / chunk_count / distinct_articles /
        # chunk_types_present summary), ``coverage_basis`` (the LLM's reading
        # of why this route was chosen), and the legacy
        # ``kb_coverage_top_score`` / ``kb_coverage_reasoning`` fields for
        # backwards compatibility with downstream consumers.
        metadata: Dict[str, Any] = {
            **result.metadata,
            "fast_path_hit": result.fast_path_hit,
            "router_mode": effective_mode,
        }
        if override_reason is not None:
            metadata["router_mode_override"] = override_reason
            metadata["original_route"] = result.route

        response = RouteInquiryResponse(
            route=effective_route,
            confidence=result.confidence,
            reasoning=result.reasoning,
            signals=result.signals,
            suggested_endpoint=suggested_endpoint,
            suggested_payload=suggested_payload,
            user_message=user_message,
            metadata=metadata,
        )

        logger.info(
            f"Route inquiry completed | "
            f"Route: {effective_route} | "
            f"Confidence: {result.confidence:.2f} | "
            f"Fast path: {result.fast_path_hit}"
        )

        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="route_inquiry",
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
                endpoint="route_inquiry",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data={},
                error=str(e),
            )
        logger.exception("Error in route_inquiry endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while routing the inquiry.",
        )


# ============================================================================
# Handle Ticket — end-to-end orchestrator (Endpoint 5)
# ============================================================================

_TICKET_GREETING = "Could you share a bit more detail about what you'd like help with?"


def get_ticket_orchestrator(request: Request) -> TicketOrchestrator:
    """Build a per-request orchestrator from the engines on app.state."""
    st = request.app.state
    if (
        getattr(st, "rag_engine", None) is None
        or getattr(st, "inquiry_router", None) is None
        or getattr(st, "forusbots_client", None) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket handler not initialized",
        )
    deps = OrchestratorDeps(
        rag_engine=st.rag_engine,
        inquiry_router=st.inquiry_router,
        llm_router=st.llm_router,
        forusbots=st.forusbots_client,
        execution_logger=getattr(st, "execution_logger", None),
    )
    return TicketOrchestrator(deps, settings)


def get_ticket_jobs(request: Request) -> TicketJobStore:
    jobs = getattr(request.app.state, "ticket_jobs", None)
    if jobs is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket jobs store not initialized",
        )
    return jobs


def _apply_ticket_handler_mode(route: str, mode: str) -> Tuple[str, Optional[str]]:
    """Gating mirror of ``_apply_router_mode`` for the ticket handler."""
    if mode == "knowledge_only" and route == "generate_response":
        return "needs_more_info", "ticket_handler_mode=knowledge_only coerced generate_response"
    return route, None


def _knowledge_answer_model(r: Any) -> KnowledgeQuestionResponse:
    return KnowledgeQuestionResponse(
        answer=r.answer,
        key_points=r.key_points,
        source_articles=[SourceArticle(**sa) for sa in r.source_articles],
        used_chunks=[UsedChunk(**uc) for uc in r.used_chunks],
        confidence_note=r.confidence_note,
        metadata=r.metadata,
    )


def _generate_result_model(r: Any) -> GenerateResponseResult:
    return GenerateResponseResult(
        decision=r.decision,
        confidence=r.confidence,
        response=r.response,
        source_articles=[SourceArticle(**sa) for sa in r.source_articles],
        used_chunks=[UsedChunk(**uc) for uc in r.used_chunks],
        coverage_gaps=r.coverage_gaps,
        metadata=r.metadata,
    )


def _outcome_to_inquiry_result(o: InquiryOutcome) -> InquiryResult:
    """Convert the orchestrator's raw-dataclass outcome to the Pydantic model,
    reusing the exact conversions the per-endpoint handlers use."""
    return InquiryResult(
        inquiry=o.inquiry,
        topic=o.topic,
        record_keeper=o.record_keeper,
        plan_type=o.plan_type,
        route=RouteDecision(o.route),
        scrape_status=o.scrape_status,
        knowledge_answer=_knowledge_answer_model(o.knowledge_result) if o.knowledge_result is not None else None,
        generate_response=_generate_result_model(o.generate_result) if o.generate_result is not None else None,
        needs_more_info_message=o.needs_more_info_message,
        diagnostics=o.diagnostics,
    )


def _nmi_outcome(ext: Any, message: str, diagnostics: Optional[Dict[str, Any]] = None) -> InquiryOutcome:
    return InquiryOutcome(
        inquiry=ext.inquiry,
        topic=ext.topic,
        route="needs_more_info",
        record_keeper=ext.record_keeper,
        plan_type=ext.plan_type,
        needs_more_info_message=message,
        diagnostics=diagnostics or {},
    )


async def _handle_one_gated(
    orch: TicketOrchestrator, ext: Any, req: HandleTicketRequest, total: int,
    classification: Any, override_reason: Optional[str],
) -> InquiryOutcome:
    """Run one inquiry, honoring a mode coercion (knowledge_only) without
    re-classifying."""
    if override_reason is not None:
        message = getattr(classification, "user_message", None) or _TICKET_GREETING
        return _nmi_outcome(ext, message, {
            "classifier": {"route": getattr(classification, "route", None),
                           "confidence": getattr(classification, "confidence", None)},
            "ticket_handler_override": override_reason,
        })
    return await orch.handle_inquiry(
        ext, req, total_inquiries=total, classification=classification
    )


def _aggregate_job_state(outcomes: List[InquiryOutcome]) -> str:
    degraded = any(
        o.route == "generate_response" and o.scrape_status in ("failed", "timeout")
        for o in outcomes
    )
    return "partial" if degraded else "succeeded"


async def _log_ticket_safe(
    exec_logger: Optional[ExecutionLogger], http_request: Request,
    req: HandleTicketRequest, start: float, mode: str,
    outcomes: List[InquiryOutcome], error: Optional[str],
    ticket_job_id: Optional[str] = None,
) -> None:
    if not exec_logger:
        return
    try:
        duration_ms = (time.monotonic() - start) * 1000
        route_summary = [
            {"topic": o.topic, "route": o.route, "scrape_status": o.scrape_status}
            for o in outcomes
        ]
        fb_ids = [
            o.diagnostics.get("forusbots_job_id")
            for o in outcomes if o.diagnostics.get("forusbots_job_id")
        ]
        await exec_logger.log_ticket_execution(
            request_id=getattr(http_request.state, "request_id", "unknown"),
            ticket_job_id=ticket_job_id,
            mode=mode,
            route_summary=route_summary,
            total_inquiries=len(outcomes),
            forusbots_job_ids=fb_ids,
            duration_ms=duration_ms,
            error=error,
            idempotency_key=req.idempotency_key,
        )
    except Exception:
        logger.exception("ticket execution logging failed")


async def _run_ticket_job(
    app: FastAPI, job_id: str, orch: TicketOrchestrator,
    capped: List[Any], classifications: List[Any], gated: List[Tuple[str, Optional[str]]],
    req: HandleTicketRequest, total: int,
    exec_logger: Optional[ExecutionLogger], http_request: Request,
    mode: str, start: float,
) -> None:
    """Background runner for the slow (generate_response) path."""
    jobs: TicketJobStore = app.state.ticket_jobs
    jobs.set_state(job_id, state="running")
    try:
        async def _process() -> List[InquiryOutcome]:
            outs: List[InquiryOutcome] = []
            for ext, c, (_er, reason) in zip(capped, classifications, gated):
                out = await asyncio.wait_for(
                    _handle_one_gated(orch, ext, req, total, c, reason),
                    settings.TICKET_INQUIRY_BUDGET_S,
                )
                outs.append(out)
            return outs

        outcomes = await asyncio.wait_for(_process(), settings.TICKET_TOTAL_BUDGET_S)
        fb_ids = [
            o.diagnostics.get("forusbots_job_id")
            for o in outcomes if o.diagnostics.get("forusbots_job_id")
        ]
        jobs.set_state(
            job_id, state=_aggregate_job_state(outcomes), outcomes=outcomes,
            forusbots_job_ids=fb_ids, total_inquiries=total,
        )
        await _log_ticket_safe(exec_logger, http_request, req, start, mode, outcomes, None, ticket_job_id=job_id)
    except asyncio.TimeoutError:
        logger.warning("ticket job %s exceeded total budget", job_id)
        jobs.set_state(job_id, state="timeout", error="ticket_total_budget_exceeded")
        await _log_ticket_safe(exec_logger, http_request, req, start, mode, [], "timeout", ticket_job_id=job_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("ticket job %s failed", job_id)
        jobs.set_state(job_id, state="failed", error=str(e))
        await _log_ticket_safe(exec_logger, http_request, req, start, mode, [], str(e), ticket_job_id=job_id)


@app.post(
    "/api/v1/handle-ticket",
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"],
    responses={200: {"model": TicketHandleResponse}, 202: {"model": TicketJobHandle}},
)
async def handle_ticket_endpoint(
    request: HandleTicketRequest,
    http_request: Request,
    orchestrator: TicketOrchestrator = Depends(get_ticket_orchestrator),
    jobs: TicketJobStore = Depends(get_ticket_jobs),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger),
):
    """
    Endpoint 5: end-to-end ticket handler. One call runs the whole flow that
    n8n used to orchestrate (extract → classify → knowledge_question OR
    required-data → ForusBots scrape → generate-response).

    **Hybrid contract:** fast routes (knowledge_question / needs_more_info)
    return inline (``200`` ``TicketHandleResponse``). The slow data path returns
    ``202`` ``TicketJobHandle`` immediately; poll ``GET /api/v1/tickets/{id}``.

    **Rollout:** gated by ``TICKET_HANDLER_MODE`` (or per-request override):
    ``disabled`` → 503; ``shadow`` → classify only and tell the caller to use
    the legacy flow; ``knowledge_only`` → only knowledge questions are handled
    end-to-end; ``full`` → full orchestration.
    """
    start = time.monotonic()
    effective_mode = request.ticket_handler_mode or settings.TICKET_HANDLER_MODE
    if effective_mode == "disabled":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket handler is disabled.",
        )

    idem = request.idempotency_key or http_request.headers.get("Idempotency-Key")
    if idem:
        existing_id = http_request.app.state.ticket_idem.get(idem)
        if existing_id:
            job = jobs.get(existing_id)
            if job is not None:
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=TicketJobHandle(
                        ticket_job_id=job.ticket_job_id, state=job.state,
                        poll_url=f"/api/v1/tickets/{job.ticket_job_id}", estimate={},
                    ).model_dump(),
                )

    try:
        extracted = await orchestrator.extract_inquiries(request)
        if not extracted:
            primary = InquiryResult(
                inquiry=(request.ticket.email_body or request.ticket.email_subject or "(empty)")[:1000],
                topic="general", plan_type="401(k)",
                route=RouteDecision.NEEDS_MORE_INFO,
                needs_more_info_message=_TICKET_GREETING,
            )
            await _log_ticket_safe(exec_logger, http_request, request, start, effective_mode, [], None)
            return TicketHandleResponse(
                route_taken=RouteDecision.NEEDS_MORE_INFO, primary=primary,
                total_inquiries_in_ticket=0,
                metadata={"ticket_handler_mode": effective_mode, "reason": "no_actionable_inquiry"},
            )

        total = len(extracted)
        capped = extracted[: 1 + settings.TICKET_MAX_RELATED]
        classifications = [await orchestrator.classify(e.inquiry) for e in capped]

        # shadow: don't act — classify, report what we WOULD do, defer to legacy.
        if effective_mode == "shadow":
            results = [
                _outcome_to_inquiry_result(_nmi_outcome(
                    e, getattr(c, "user_message", None) or _TICKET_GREETING,
                    {"classifier": {"route": getattr(c, "route", None),
                                    "confidence": getattr(c, "confidence", None)}},
                ))
                for e, c in zip(capped, classifications)
            ]
            await _log_ticket_safe(exec_logger, http_request, request, start, "shadow", [], None)
            return TicketHandleResponse(
                route_taken=RouteDecision.NEEDS_MORE_INFO, primary=results[0],
                related=results[1:], total_inquiries_in_ticket=total,
                metadata={"ticket_handler_mode": "shadow", "fallback": True,
                          "shadow_routes": [getattr(c, "route", None) for c in classifications]},
            )

        gated = [
            _apply_ticket_handler_mode(getattr(c, "route", "needs_more_info"), effective_mode)
            for c in classifications
        ]
        slow = any(getattr(c, "route", None) == "generate_response" and reason is None
                   for c, (_er, reason) in zip(classifications, gated))

        if not slow:
            outcomes: List[InquiryOutcome] = []
            for ext, c, (_er, reason) in zip(capped, classifications, gated):
                outcomes.append(await _handle_one_gated(orchestrator, ext, request, total, c, reason))
            results = [_outcome_to_inquiry_result(o) for o in outcomes]
            await _log_ticket_safe(exec_logger, http_request, request, start, effective_mode, outcomes, None)
            return TicketHandleResponse(
                route_taken=results[0].route, primary=results[0], related=results[1:],
                total_inquiries_in_ticket=total,
                metadata={"ticket_handler_mode": effective_mode},
            )

        # slow path → background job + 202
        job = jobs.create()
        if idem:
            http_request.app.state.ticket_idem[idem] = job.ticket_job_id
        task = asyncio.create_task(_run_ticket_job(
            http_request.app, job.ticket_job_id, orchestrator,
            capped, classifications, gated, request, total,
            exec_logger, http_request, effective_mode, start,
        ))
        http_request.app.state.bg_tasks.add(task)
        task.add_done_callback(http_request.app.state.bg_tasks.discard)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=TicketJobHandle(
                ticket_job_id=job.ticket_job_id, state="queued",
                poll_url=f"/api/v1/tickets/{job.ticket_job_id}",
                estimate={"avg_seconds": int(settings.FORUSBOTS_MAX_WAIT_S)},
            ).model_dump(),
        )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error in handle_ticket endpoint")
        await _log_ticket_safe(exec_logger, http_request, request, start, effective_mode, [], str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while handling the ticket.",
        )


@app.get(
    "/api/v1/tickets/{ticket_job_id}",
    response_model=TicketStatusResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"],
)
async def get_ticket_status(
    ticket_job_id: str,
    jobs: TicketJobStore = Depends(get_ticket_jobs),
):
    """Poll a slow (data-path) ticket job started by ``POST /api/v1/handle-ticket``."""
    job = jobs.get(ticket_job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket job not found or expired.",
        )
    results = [_outcome_to_inquiry_result(o) for o in job.outcomes]
    primary = results[0] if results else None
    return TicketStatusResponse(
        ticket_job_id=job.ticket_job_id,
        state=job.state,
        route_taken=primary.route if primary else None,
        primary=primary,
        related=results[1:] if results else [],
        total_inquiries_in_ticket=job.total_inquiries,
        forusbots_job_ids=job.forusbots_job_ids,
        elapsed_s=round(time.monotonic() - job.created_monotonic, 2),
        error=job.error,
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
