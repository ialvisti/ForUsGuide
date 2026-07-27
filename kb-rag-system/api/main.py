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
import math
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, Header, Request, HTTPException, status, Depends, Security
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
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
from data_pipeline.llm_router import (
    LLMRouter,
    build_routes_from_settings,
    parse_llm_pricing_json,
)
from data_pipeline.inquiry_router import (
    COVERAGE_TOP_K,
    CoveragePack,
    InquiryRouterEngine,
)
from data_pipeline.forusbots_client import ForusBotsClient
from data_pipeline.ticket_orchestrator import (
    OrchestratorDeps,
    TicketOrchestrator,
)
from data_pipeline.ticket_job_models import (
    TERMINAL_STATES,
    CreateOrGetOutcome,
    NextAction,
    TicketJobRecord,
    TicketJobState,
    fingerprint_request,
    hash_tenant_id,
    new_job_record,
    utcnow,
)
from data_pipeline.ticket_job_repository import (
    FirestoreTicketJobBackend,
    IdempotencyTenantMismatch,
    InMemoryTicketJobBackend,
    QuotaExceeded,
    RateWindowExceeded,
    TicketJobRepository,
)
from data_pipeline.ticket_task_queue import (
    CloudTasksTicketQueue,
    InlineTicketQueue,
)
from data_pipeline.staging_fault_injection import (
    FAULT_TEST_HEADER,
    FaultInjectionRejected,
    accept_fault_plan_from_request,
)
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
    HandleTicketV2Request,
    TicketHandleResponse,
    TicketJobHandle,
    TicketStatusResponse,
    TicketJobAcceptedV2,
    TicketJobStatusV2,
    InquiryStatusV2,
    InquiryResult,
)
from .config import settings, validate_settings
from .middleware import (
    _path_allowed_for_role,
    add_request_id,
    enforce_app_role,
    log_requests,
    handle_errors,
    limit_body_size,
)
from .participant_plan import (
    AuthorizedParticipantPlan,
    ParticipantPlanUnavailable,
    build_validator_from_settings,
)
from .auth import verify_workload_identity_token
from . import metrics as ticket_metrics

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
        logging.getLogger(__name__).error(
            "Cloud Logging setup failed; continuing with standard logging"
        )

logger = logging.getLogger(__name__)


def _emit_ticket_metric(metric: str, value: int | float, **labels: str) -> None:
    """Best-effort closed-schema telemetry for producer endpoints."""
    try:
        ticket_metrics.emit(metric, value, **labels)
    except (TypeError, ValueError):
        logger.error("ticket producer metric rejected by telemetry schema")


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

        role = settings.APP_ROLE
        ticket_active = settings.TICKET_HANDLER_MODE != "disabled"
        needs_rag_runtime = role in {"producer", "worker"}
        needs_ticket_execution = role == "worker"
        # El flag disabled detiene admisión, no lectura. Un productor que
        # actúa como rollback anchor debe conservar el repositorio durable
        # para que los clientes puedan terminar de sondear jobs ya admitidos
        # por una revisión anterior.
        needs_ticket_repo = role in {"producer", "worker", "reconciler"}
        needs_ticket_queue = role == "reconciler" or (
            role == "producer" and ticket_active
        )

        # Producer conserva toda la API core aunque tickets esté disabled.
        # Worker comparte el runtime RAG porque reanuda el execution_plan.
        # Reconciler es control-plane y no carga SDKs/modelos de ejecución.
        if needs_rag_runtime:
            app.state.pinecone_uploader = PineconeUploader(
                api_key=settings.PINECONE_API_KEY,
                index_name=settings.INDEX_NAME,
                namespace=settings.NAMESPACE,
            )
            logger.info("✅ Pinecone connection established")

            llm_router = LLMRouter(
                openai_api_key=settings.OPENAI_API_KEY or None,
                gemini_api_key=settings.GEMINI_API_KEY or None,
                use_vertex_ai=settings.USE_VERTEX_AI,
                gcp_project=settings.GCP_PROJECT or None,
                gcp_location=settings.GCP_LOCATION,
            )
            llm_router.configure_routes(build_routes_from_settings(settings))
            # Only durable worker execution owns ticket token/cost metrics.
            # Producer/core calls share this router but remain outside the
            # ticket ContextVar and do not depend on ticket pricing config.
            if role == "worker":
                llm_router.configure_pricing(
                    parse_llm_pricing_json(settings.TICKET_LLM_PRICING_JSON)
                )
            app.state.llm_router = llm_router
            logger.info("✅ LLM Router configured")

            app.state.rag_engine = RAGEngine(
                llm_router=llm_router,
                pinecone_uploader=app.state.pinecone_uploader,
            )
            logger.info("✅ RAG Engine initialized")

            app.state.inquiry_router = InquiryRouterEngine(
                llm_router=llm_router,
                coverage_pack_builder=_make_coverage_pack_builder(
                    app.state.rag_engine
                ),
            )
            logger.info("✅ Inquiry Router initialized")

            stats = app.state.pinecone_uploader.get_index_stats()
            logger.info(
                "📊 Total vectors in index: %s",
                stats.get("total_vectors", 0),
            )
            _log_pinecone_startup_diagnostics(
                app.state.pinecone_uploader, stats
            )

            if settings.ENABLE_EXECUTION_LOGGING:
                app.state.execution_logger = ExecutionLogger(
                    project_id=settings.GCP_PROJECT or None,
                    database=settings.FIRESTORE_DATABASE or "(default)",
                    retention_days=settings.TICKET_IDEMPOTENCY_RETENTION_DAYS,
                )
                logger.info("✅ Execution logger initialized (Firestore)")
            else:
                app.state.execution_logger = None

        if needs_ticket_repo:
            app.state.ticket_repo = TicketJobRepository(
                _build_ticket_job_backend(),
                retention_days=settings.TICKET_IDEMPOTENCY_RETENTION_DAYS,
                max_outstanding=settings.TICKET_MAX_OUTSTANDING_JOBS,
                rate_limit_per_minute=settings.RATE_LIMIT_HANDLE_TICKET,
            )

        # ForusBots y el orchestrator pertenecen exclusivamente al worker.
        # El producer sólo autoriza, persiste y encola trabajo durable.
        if needs_ticket_execution:
            app.state.forusbots_client = ForusBotsClient.from_settings(settings)
            app.state.ticket_orchestrator_factory = (
                lambda: _build_orchestrator_from_state(app)
            )

        # Producer y reconciler encolan; worker sólo recibe Cloud Tasks y no
        # debe requerir permisos ni configuración de la cola.
        if needs_ticket_queue:
            app.state.ticket_queue = _build_ticket_queue(app)

        if role == "producer" and ticket_active:
            # La autorización participant-plan ocurre sólo en admission. El
            # worker consume el payload durable ya autorizado.
            app.state.participant_plan_validator = (
                build_validator_from_settings(settings)
            )

        logger.info(
            "✅ Runtime wired (role=%s, ticket_mode=%s, backend=%s, queue=%s)",
            role,
            settings.TICKET_HANDLER_MODE,
            settings.TICKET_JOB_BACKEND,
            settings.TICKET_TASK_QUEUE,
        )

        logger.info("=" * 80)
        logger.info(f"🚀 API Ready on http://{settings.API_HOST}:{settings.API_PORT}")
        logger.info("=" * 80)

    except Exception as exc:
        logger.error("Startup failed (error_type=%s)", type(exc).__name__)
        raise

    yield

    # Shutdown
    logger.info("Shutting down API...")
    queue = getattr(app.state, "ticket_queue", None)
    if queue is not None:
        try:
            await queue.aclose()
        except Exception:
            logger.error("Error closing ticket queue")
    forusbots = getattr(app.state, "forusbots_client", None)
    if forusbots is not None:
        try:
            await forusbots.aclose()
        except Exception:
            logger.error("Error closing ForusBots client")


def _build_ticket_job_backend():
    """memory (dev/tests) | firestore (producción; validate_settings lo exige
    con el handler activo). La base NOMBRADA es obligatoria (Tarea 5 Paso 3):
    (default) en producción, ticket-staging en staging."""
    if settings.TICKET_JOB_BACKEND == "firestore":
        return FirestoreTicketJobBackend(
            project=settings.GCP_PROJECT or None,
            collection_prefix=settings.FIRESTORE_TICKET_COLLECTION_PREFIX,
            database=settings.FIRESTORE_DATABASE,
        )
    return InMemoryTicketJobBackend()


def _build_orchestrator_from_state(app: FastAPI) -> TicketOrchestrator:
    st = app.state
    deps = OrchestratorDeps(
        rag_engine=st.rag_engine,
        inquiry_router=st.inquiry_router,
        llm_router=st.llm_router,
        forusbots=st.forusbots_client,
        execution_logger=getattr(st, "execution_logger", None),
    )
    return TicketOrchestrator(deps, settings)


def _build_ticket_queue(app: FastAPI):
    """cloudtasks (producción) | inline (dev/tests, mismo worker durable)."""
    if settings.TICKET_TASK_QUEUE == "cloudtasks":
        queue = CloudTasksTicketQueue(
            project=settings.GCP_PROJECT,
            location=settings.CLOUD_TASKS_LOCATION,
            queue=settings.CLOUD_TASKS_QUEUE,
            worker_url=settings.TICKET_WORKER_URL,
            worker_audience=settings.TICKET_WORKER_AUDIENCE,
            service_account=settings.TICKET_WORKER_SERVICE_ACCOUNT,
            dispatch_deadline_s=settings.TICKET_TASK_DISPATCH_DEADLINE_S,
        )
        # el bump de generación ante tombstone es transaccional (repositorio)
        queue._generation_bumper = app.state.ticket_repo.bump_enqueue_generation
        return queue
    from api.ticket_worker import run_ticket_job

    return InlineTicketQueue(lambda job_id: run_ticket_job(app, job_id))


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


@app.exception_handler(RequestValidationError)
async def _request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
):
    """ACK permanent Cloud Tasks schema errors, but only after task auth.

    Cloud Tasks retries every non-2xx, including 4xx.  A malformed body can
    never become valid on retry, so the authenticated internal task endpoint
    acknowledges it with 204.  Authentication is deliberately repeated here
    because FastAPI validates the body before the route handler can call the
    normal OIDC guard.
    """
    if request.url.path != "/internal/tasks/ticket-job":
        return await request_validation_exception_handler(request, exc)

    from api.ticket_worker import verify_task_oidc

    try:
        await verify_task_oidc(request)
    except HTTPException as auth_error:
        return JSONResponse(
            status_code=auth_error.status_code,
            content={"detail": auth_error.detail},
            headers=auth_error.headers,
        )

    logger.warning(
        "authenticated Cloud Task rejected as permanent schema error "
        "(validation_errors=%d)",
        len(exc.errors()),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

# CORS Middleware — uses environment-aware origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom Middleware
# Starlette ejecuta el ÚLTIMO registrado como el más externo. Orden efectivo
# de ejecución: handle_errors → add_request_id → log_requests →
# limit_body_size → route. Así el request ID existe cuando log_requests
# escribe la línea de inicio (fix HT: logs con ID unknown).
# enforce_app_role se registra PRIMERO (más interno): corre después del
# request ID/logging, de modo que un 404 por rol queda trazado.
app.middleware("http")(enforce_app_role)
app.middleware("http")(limit_body_size)
app.middleware("http")(log_requests)
app.middleware("http")(add_request_id)
app.middleware("http")(handle_errors)

# Worker interno de ticket jobs (Cloud Tasks target; OIDC-protected)
from api.ticket_worker import router as _ticket_worker_router  # noqa: E402
app.include_router(_ticket_worker_router)


def _custom_openapi():
    """Document the deployed n8n authentication contract.

    Cloud Run IAM validates ``Authorization`` and the application validates
    ``X-API-Key``.  No second bearer header is required from n8n.
    """
    role = settings.APP_ROLE
    if app.openapi_schema and getattr(app.state, "openapi_role", None) == role:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        description=settings.API_DESCRIPTION,
        routes=[
            route for route in app.routes
            if _path_allowed_for_role(getattr(route, "path", ""), role)
        ],
    )
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["ApiKeyAuth"] = {
        "type": "apiKey", "in": "header", "name": "X-API-Key",
        "description": "Credencial de aplicación usada por el workflow n8n.",
    }
    schemes["CloudRunIAM"] = {
        "type": "http", "scheme": "bearer",
        "description": "Google ID token para invocar el servicio privado, "
                       "obtenido por el flujo OAuth2/IAM Credentials existente.",
    }
    for path, ops in schema.get("paths", {}).items():
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            if path.startswith(("/api/v1/handle-ticket",
                                "/api/v1/tickets/",
                                "/api/v2/handle-ticket",
                                "/api/v2/ticket-jobs/")):
                # Un único objeto OpenAPI expresa AND: el workflow existente
                # presenta ambos factores al servicio privado. No hay un
                # segundo bearer header propio de la aplicación.
                op["security"] = [{"ApiKeyAuth": [], "CloudRunIAM": []}]
    app.openapi_schema = schema
    app.state.openapi_role = role
    return schema


app.openapi = _custom_openapi

# Mount UI static files
UI_DIR = Path(__file__).parent.parent / "ui"
if UI_DIR.exists():
    app.mount("/ui/static", StaticFiles(directory=UI_DIR), name="ui-static")


# ============================================================================
# Dependency Functions (read from app.state, not globals)
# ============================================================================

from fastapi.security import APIKeyHeader

# Declarado como security scheme para que OpenAPI documente la auth (HT-23).
_API_KEY_SCHEME = APIKeyHeader(
    name="X-API-Key", auto_error=False,
    description="Credencial de cliente (principal). Además, Cloud Run IAM "
                "exige un identity token en Authorization.",
)


async def verify_api_key(
    request: Request, api_key: Optional[str] = Security(_API_KEY_SCHEME)
):
    """Dependency de auth: valida X-API-Key y resuelve el principal del
    caller (API_CLIENT_KEYS o la API_KEY legacy → "default"). El principal
    queda en request.state.principal_id para autorización de objetos."""
    from api.auth import authenticate_principal
    await authenticate_principal(request)


async def verify_v2_api_key(
    request: Request, api_key: Optional[str] = Security(_API_KEY_SCHEME)
):
    """Alias de compatibilidad para overrides de tests/integraciones antiguas.

    Las rutas v2 desplegadas usan ``verify_api_key`` y aceptan la ``API_KEY``
    existente, exactamente igual que v1.
    """
    await verify_api_key(request, api_key)


async def verify_workload_identity(http_request: Request) -> None:
    """Dependency de v1/v2: identidad workload independiente de X-API-Key
    (Tarea 4 Paso 2a). No-op mientras WIF no esté configurado (ventana de
    migración); una vez activo, cierra el bypass de v1 (P1 review). La
    verificación completa vive en api.auth."""
    # google-auth verifica firma/claims de forma síncrona y puede refrescar
    # los certificados públicos de Google por red. Sacarlo del event loop
    # evita que una renovación de JWKS bloquee todas las requests del worker.
    await asyncio.to_thread(verify_workload_identity_token, http_request)


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
        # Check Pinecone connection. get_index_stats() ya no traga errores:
        # {"error": ...} significa dependencia caída, no índice vacío (HT-24).
        stats = pinecone.get_index_stats()
        pinecone_connected = "error" not in stats and "total_vectors" in stats
        total_vectors = stats.get('total_vectors', 0)
    except Exception as exc:
        logger.error("Pinecone health check failed (error_type=%s)",
                     type(exc).__name__)
        pinecone_connected = False
        total_vectors = 0

    # Un proveedor LLM utilizable, no sólo OpenAI (HT-24): las rutas pueden
    # correr enteramente sobre Gemini/Vertex.
    openai_configured = bool(settings.OPENAI_API_KEY) or bool(
        settings.GEMINI_API_KEY
    ) or (settings.USE_VERTEX_AI and bool(settings.GCP_PROJECT))

    return HealthResponse(
        status="healthy" if (pinecone_connected and openai_configured) else "degraded",
        version=settings.API_VERSION,
        pinecone_connected=pinecone_connected,
        openai_configured=openai_configured,
        total_vectors=total_vectors,
        router_mode=settings.ROUTER_MODE,
        ticket_handler_mode=settings.TICKET_HANDLER_MODE,
    )


@app.get("/livez", include_in_schema=False)
async def livez():
    """Liveness: proceso vivo. SIN I/O externo (Task 11)."""
    return {"status": "ok"}


_READINESS_PROBE_TIMEOUT_S = 3.0
# Firestore reserva IDs que coinciden con ``__.*__``. El formato productivo
# de job es hex de 32 caracteres, así que la sonda usa un ID legal e inerte.
_READINESS_SENTINEL_JOB_ID = "0" * 32


async def _readiness_probe(
    dependency: str,
    probe,
    *,
    role: str,
) -> Optional[str]:
    """Ejecuta una sonda read-only acotada y sanitiza cualquier fallo."""
    try:
        await asyncio.wait_for(probe(), timeout=_READINESS_PROBE_TIMEOUT_S)
        return None
    except Exception as exc:  # noqa: BLE001 - readiness falla cerrado
        # El mensaje puede contener URLs, IDs o contexto de credenciales.
        logger.warning(
            "readiness dependency failed role=%s dependency=%s error_type=%s",
            role,
            dependency,
            type(exc).__name__,
        )
        return dependency


async def _probe_pinecone(pinecone) -> None:
    """Sonda SDK read-only; nunca query/upsert y nunca bloquea el event loop."""
    stats = await asyncio.to_thread(pinecone.get_index_stats)
    if not isinstance(stats, dict) or "error" in stats or "total_vectors" not in stats:
        raise RuntimeError("pinecone stats unavailable")


async def _probe_ticket_repo(repo) -> None:
    # ID deliberadamente imposible: ejercita el backend sin leer datos reales.
    await repo.get(_READINESS_SENTINEL_JOB_ID)


async def _probe_ticket_queue(queue) -> None:
    # Cloud Tasks usa GetQueue; la cola inline de desarrollo devuelve 0.
    await queue.estimated_queue_delay_s()


async def _probe_forusbots(client) -> None:
    # GET health dedicado, sin datos de participante.
    await client.health()


async def _probe_participant_plan_validator(validator) -> None:
    # No inventar IDs para llamar authorize. El adaptador real debe exponer
    # una sonda sin efectos propia.
    health = getattr(validator, "health", None)
    if not callable(health):
        raise RuntimeError("validator health probe unavailable")
    await health()


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request):
    """Readiness role-aware con sondas read-only de las dependencias.

    `/livez` es la sonda de proceso sin I/O. Aquí cada sonda queda acotada a
    tres segundos y la respuesta nunca incluye detalles del proveedor.

    - producer disabled: core + repositorio durable de polling; NO depende de
      validador/cola/ForusBots de admisión/ejecución.
    - producer activo: además cola y, si está configurado, el directorio
      participant-plan opcional; el contrato n8n existente no depende de él.
    - worker: repo + dependencias de ejecución (LLM/Pinecone).
    - reconciler: repo + cola (sin LLM/Pinecone/ForusBots)."""
    st = request.app.state
    role = settings.APP_ROLE
    active = settings.TICKET_HANDLER_MODE != "disabled"
    missing: list = []
    probes = []

    def _need(attr: str) -> None:
        if getattr(st, attr, None) is None:
            missing.append(attr)

    provider_ok = bool(settings.OPENAI_API_KEY) or bool(settings.GEMINI_API_KEY) \
        or (settings.USE_VERTEX_AI and bool(settings.GCP_PROJECT))

    if role == "producer":
        # core existente: SIEMPRE (Pinecone/Vertex/buckets son core de otras
        # rutas aunque tickets esté disabled)
        for attr in (
            "rag_engine", "pinecone_uploader", "inquiry_router", "llm_router",
        ):
            _need(attr)
        pinecone = getattr(st, "pinecone_uploader", None)
        if pinecone is not None:
            probes.append(("pinecone", lambda: _probe_pinecone(pinecone)))
        # Incluso disabled debe poder servir GET de jobs existentes durante
        # rollback/cambio de revisión; la cola y los integradores sí quedan
        # fuera hasta que la admisión esté activa.
        _need("ticket_repo")
        repo = getattr(st, "ticket_repo", None)
        if repo is not None:
            probes.append(("ticket_repo", lambda: _probe_ticket_repo(repo)))
        if active:
            _need("ticket_queue")
            validator = getattr(st, "participant_plan_validator", None)
            if validator is not None:
                probes.append((
                    "participant_plan_validator",
                    lambda: _probe_participant_plan_validator(validator),
                ))
            queue = getattr(st, "ticket_queue", None)
            if queue is not None:
                probes.append(("ticket_queue", lambda: _probe_ticket_queue(queue)))
    elif role == "worker":
        _need("ticket_repo")
        for attr in (
            "rag_engine", "pinecone_uploader", "inquiry_router", "llm_router",
            "forusbots_client",
        ):
            _need(attr)
        repo = getattr(st, "ticket_repo", None)
        pinecone = getattr(st, "pinecone_uploader", None)
        forusbots = getattr(st, "forusbots_client", None)
        if repo is not None:
            probes.append(("ticket_repo", lambda: _probe_ticket_repo(repo)))
        if pinecone is not None:
            probes.append(("pinecone", lambda: _probe_pinecone(pinecone)))
        if forusbots is not None:
            probes.append(("forusbots", lambda: _probe_forusbots(forusbots)))
    elif role == "reconciler":
        _need("ticket_repo")
        _need("ticket_queue")
        provider_ok = True  # el reconciliador no usa LLM/Pinecone
        repo = getattr(st, "ticket_repo", None)
        queue = getattr(st, "ticket_queue", None)
        if repo is not None:
            probes.append(("ticket_repo", lambda: _probe_ticket_repo(repo)))
        if queue is not None:
            probes.append(("ticket_queue", lambda: _probe_ticket_queue(queue)))

    provider_needed = role in ("producer", "worker")
    probe_results = await asyncio.gather(*(
        _readiness_probe(name, probe, role=role) for name, probe in probes
    ))
    unhealthy = sorted(result for result in probe_results if result is not None)

    if missing or unhealthy or (provider_needed and not provider_ok):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "role": role,
                "missing": sorted(missing),
                "unhealthy": unhealthy,
                "llm_provider_configured": provider_ok,
            },
        )
    return {"status": "ready", "role": role}


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
        logger.info(
            "Required data request | inquiry_length=%d", len(request.inquiry)
        )

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
        logger.error("Error in required_data endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the required-data request."
        ) from e


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
            "Generate response request | inquiry_length=%d | max_tokens=%d",
            len(request.inquiry),
            request.max_response_tokens,
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
        logger.error("Error in generate_response endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while generating the response."
        ) from e


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
        logger.info(
            "Knowledge question request | question_length=%d",
            len(request.question),
        )

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
        logger.error("Error in knowledge_question endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the knowledge question."
        ) from e


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
    # El override del body sólo puede RESTRINGIR el modo del servidor (mismo
    # clamp que el ticket handler; hallazgo adyacente a HT-10).
    effective_mode = _effective_ticket_mode(settings.ROUTER_MODE, request.router_mode)

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
        logger.error("Error in route_inquiry endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while routing the inquiry.",
        ) from e


# ============================================================================
# Handle Ticket — end-to-end orchestrator (Endpoint 5)
# ============================================================================

_TICKET_GREETING = "Could you share a bit more detail about what you'd like help with?"
_TICKET_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


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


def get_ticket_repo(request: Request) -> TicketJobRepository:
    repo = getattr(request.app.state, "ticket_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket job repository not initialized",
        )
    return repo


# Orden de restrictividad del rollout: el body sólo puede movernos hacia la
# izquierda (más restrictivo), nunca hacia la derecha.
_TICKET_MODE_RANK = {"disabled": 0, "shadow": 1, "knowledge_only": 2, "full": 3}


def _effective_ticket_mode(server_mode: str, requested: Optional[str]) -> str:
    """El override per-request sólo puede restringir el modo del servidor.

    Un caller nunca puede expandir el rollout (p.ej. servidor=disabled +
    body=full sigue siendo disabled). Modos desconocidos se tratan como el
    modo del servidor.
    """
    if requested is None or requested not in _TICKET_MODE_RANK:
        return server_mode
    if _TICKET_MODE_RANK[requested] < _TICKET_MODE_RANK.get(server_mode, 0):
        return requested
    return server_mode


def _request_principal(http_request: Request) -> str:
    return getattr(http_request.state, "principal_id", None) or "default"


def _poll_owner_matches(record: TicketJobRecord, http_request: Request) -> bool:
    """Authorize from control-only ownership data before any mutation.

    Raw tenant IDs live in the TTL-bound payload.  A missing/expired payload
    must therefore be authorized using the stable hash retained in control;
    legacy records without that hash fall back to their raw tenant field.
    """
    if record.principal_id != _request_principal(http_request):
        return False
    tenant_id = getattr(http_request.state, "tenant_id", None)
    if tenant_id is None:
        return False
    if record.tenant_id_hash is not None:
        return record.tenant_id_hash == hash_tenant_id(tenant_id)
    return record.tenant_id == tenant_id


async def _lazy_terminalize_poll_record(
    repo: TicketJobRepository,
    record: TicketJobRecord,
    payload_present: bool,
) -> Tuple[Optional[TicketJobRecord], bool]:
    """Revalidate deadline/payload and terminalize atomically after auth.

    The repository performs the condition check, lease fence, terminal state,
    receipt/TTL update, and exactly-once slot release in one transaction.  The
    endpoint calls this only *after* principal+tenant authorization, so an
    object probe cannot mutate another tenant's job.
    """
    if record.state in TERMINAL_STATES:
        return record, payload_present
    await repo.terminalize_if_unrecoverable(record.job_id)
    return await repo.get_with_payload_state(record.job_id)


def _extract_idempotency_key(
    request: HandleTicketRequest, http_request: Request, *, allow_body: bool = True
) -> Optional[str]:
    """Key del header (preferida) o del body (v1 legacy). Conflictos y
    tamaños fuera de [1, 128] se rechazan (HT-05/HT-06)."""
    header_key = http_request.headers.get("Idempotency-Key")
    body_key = getattr(request, "idempotency_key", None)
    if body_key and not allow_body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "IDEMPOTENCY_KEY_IN_BODY",
                    "message": "v2 sólo acepta el header Idempotency-Key"},
        )
    if header_key and body_key and header_key != body_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "IDEMPOTENCY_KEY_CONFLICT",
                    "message": "header y body traen keys distintas"},
        )
    key = header_key or body_key
    if key is not None and not (1 <= len(key) <= 128):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "IDEMPOTENCY_KEY_INVALID",
                    "message": "Idempotency-Key debe tener entre 1 y 128 caracteres"},
        )
    return key


async def _accept_ticket_job(
    request: HandleTicketRequest,
    http_request: Request,
    repo: TicketJobRepository,
    *,
    api_version: str,
    allow_body_idem: bool = True,
) -> Tuple[TicketJobRecord, bool]:
    """Productor común v1/v2: autoriza, valida, reserva idempotencia en
    transacción ANTES de cualquier LLM, y confirma record + task encolado.

    Devuelve (record, idempotency_replayed). Un crash entre record y enqueue
    se cierra reasegurando el enqueue en el retry (task name determinístico).
    """
    principal = _request_principal(http_request)
    fault_header = http_request.headers.get(FAULT_TEST_HEADER)
    if fault_header is not None \
            and getattr(http_request.state, "principal_id", None) is None:
        # The test contract is never accepted through the legacy fallback of
        # this internal helper: the public route must have authenticated and
        # attached an explicit principal first.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FAULT_INJECTION_REJECTED"},
        )
    try:
        fault_plan = accept_fault_plan_from_request(
            app_env=settings.APP_ENV,
            header_value=fault_header,
            principal_id=principal,
            secret=settings.TICKET_FAULT_SIGNING_SECRET,
        )
    except FaultInjectionRejected as exc:
        ticket_metrics.increment("ticket_fault_plan_rejected")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FAULT_INJECTION_REJECTED"},
        ) from exc
    if fault_plan is not None:
        ticket_metrics.increment("ticket_fault_plan_accepted")

    effective_mode = _effective_ticket_mode(
        settings.TICKET_HANDLER_MODE,
        getattr(request, "ticket_handler_mode", None),
    )
    if effective_mode == "disabled":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket handler is disabled.",
        )

    tenant = getattr(http_request.state, "tenant_id", None) or "default"

    # Preserve the deployed n8n contract: Cloud Run authenticates
    # kb-rag-client and X-API-Key authenticates the application caller.  An
    # optional participant directory may further canonicalize these fields,
    # but its absence cannot break the existing trusted integration.
    validator = getattr(http_request.app.state, "participant_plan_validator", None)
    if validator is None:
        authorized = AuthorizedParticipantPlan(
            tenant_id=tenant,
            participant_id=request.participant_id,
            plan_id=request.plan_id,
            record_keeper=request.record_keeper,
        )
    else:
        try:
            authorized = await asyncio.wait_for(
                validator.authorize(
                    tenant_id=tenant,
                    participant_id=request.participant_id,
                    plan_id=request.plan_id,
                ),
                timeout=settings.PARTICIPANT_PLAN_TIMEOUT_S,
            )
        except ParticipantPlanUnavailable as exc:
            ticket_metrics.increment("ticket_participant_plan_unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "PARTICIPANT_PLAN_VALIDATION_UNAVAILABLE"},
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("participant-plan validator failed")
            ticket_metrics.increment("ticket_participant_plan_unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "PARTICIPANT_PLAN_VALIDATION_UNAVAILABLE"},
            ) from exc
    if authorized is None or (
        authorized.tenant_id != tenant
        or authorized.participant_id != request.participant_id
        or authorized.plan_id != request.plan_id
    ):
        ticket_metrics.increment("ticket_participant_plan_mismatch")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PARTICIPANT_PLAN_MISMATCH"},
        )

    idem_key = _extract_idempotency_key(
        request, http_request, allow_body=allow_body_idem
    )
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    # Si existe un directorio opcional, sus valores canónicos sustituyen el
    # body. Sin directorio conservamos intactos los campos del workflow n8n
    # autenticado, incluido record_keeper.
    payload["record_keeper"] = authorized.record_keeper
    payload["participant_id"] = authorized.participant_id
    payload["plan_id"] = authorized.plan_id
    fingerprint = fingerprint_request(payload)

    # Resolución de idempotencia ANTES de las cuotas de jobs nuevos (Tarea 4
    # Paso 5): un replay no consume rate limit ni slots y repara un enqueue
    # pendiente; sólo un job lógico recién creado paga cuota.
    if idem_key is not None:
        try:
            peek_outcome, peeked = await repo.peek_idempotent(
                principal_id=principal,
                idempotency_key=idem_key,
                api_version=api_version,
                request_fingerprint=fingerprint,
                tenant_id=tenant,
            )
        except IdempotencyTenantMismatch as exc:
            ticket_metrics.increment("ticket_jobs_conflicted")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "IDEMPOTENCY_TENANT_MISMATCH",
                        "message": "la Idempotency-Key pertenece a otro "
                                   "tenant"},
            ) from exc
        if peek_outcome == "conflict":
            ticket_metrics.increment("ticket_jobs_conflicted")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                        "message": "la misma Idempotency-Key ya se usó con "
                                   "otro payload"},
            )
        if peek_outcome == "replay" and peeked is not None:
            ticket_metrics.increment("ticket_jobs_replayed")
            record = peeked
            if record.enqueue_state != "enqueued" \
                    and record.state not in TERMINAL_STATES:
                queue = http_request.app.state.ticket_queue
                task_name = await queue.ensure_enqueued(
                    record.job_id, record.enqueue_generation)
                record = await repo.mark_enqueued(record.job_id, task_name)
            return record, True

    # Admission applies only to a NEW logical job. Replays have already paid
    # admission and must retain their durable recovery semantics.
    queue = http_request.app.state.ticket_queue
    try:
        estimated_delay_s = float(await queue.estimated_queue_delay_s())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "ticket queue delay estimate unavailable (error_type=%s)",
            type(exc).__name__,
        )
        _emit_ticket_metric(
            "ticket_queue_delay_seconds", 0, code="unavailable"
        )
        ticket_metrics.increment("ticket_queue_delay_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_DELAY_ESTIMATE_UNAVAILABLE",
                    "retryable": True},
            headers={"Retry-After": "30"},
        ) from exc
    if not math.isfinite(estimated_delay_s) or estimated_delay_s < 0:
        _emit_ticket_metric(
            "ticket_queue_delay_seconds", 0, code="unavailable"
        )
        ticket_metrics.increment("ticket_queue_delay_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_DELAY_ESTIMATE_UNAVAILABLE",
                    "retryable": True},
            headers={"Retry-After": "30"},
        )
    delay_code = (
        "rejected"
        if estimated_delay_s > settings.TICKET_ADMISSION_QUEUE_DELAY_CEILING_S
        else "observed"
    )
    _emit_ticket_metric(
        "ticket_queue_delay_seconds", estimated_delay_s, code=delay_code
    )
    if delay_code == "rejected":
        ticket_metrics.increment("ticket_queue_delay_rejected")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "QUEUE_DELAY_EXCEEDED", "retryable": True},
            headers={"Retry-After": "30"},
        )

    from datetime import timedelta as _timedelta
    candidate = new_job_record(
        principal_id=principal,
        request_fingerprint=fingerprint,
        retention_s=settings.TICKET_JOB_RETENTION_S,
        mode=effective_mode,
        api_version=api_version,
        tenant_id=authorized.tenant_id,
        ticket_id=request.ticket.ticket_id,
        request_payload=payload,
        fault_plan=fault_plan,
        trace_id=getattr(http_request.state, "request_id", None),
        # deadline ABSOLUTO del job (Tarea 7 Paso 1): worker/reconciliador/
        # GET terminalizan después de vencido; Cloud Tasks no es el reloj.
        job_deadline_at=utcnow() + _timedelta(
            seconds=settings.TICKET_JOB_DEADLINE_S),
    )
    try:
        record, outcome = await repo.create_or_get(
            principal_id=principal,
            idempotency_key=idem_key,
            request_fingerprint=fingerprint,
            candidate=candidate,
        )
    except RateWindowExceeded as exc:
        # Única capa autoritativa de tasa: transacción durable compartida por
        # todas las instancias (sin precheck in-memory/check-then-act).
        ticket_metrics.increment("ticket_rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "RATE_LIMITED", "retryable": True},
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from exc
    except QuotaExceeded as exc:
        ticket_metrics.increment("ticket_outstanding_capped")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "TOO_MANY_OUTSTANDING_JOBS", "retryable": True,
                    "outstanding": exc.outstanding},
            headers={"Retry-After": "30"},
        ) from exc
    if outcome == CreateOrGetOutcome.CONFLICT or record is None:
        ticket_metrics.increment("ticket_jobs_conflicted")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "message": "la misma Idempotency-Key ya se usó con otro payload"},
        )
    replayed = outcome == CreateOrGetOutcome.REPLAYED
    ticket_metrics.increment(
        "ticket_jobs_replayed" if replayed else "ticket_jobs_accepted"
    )

    # Cerrar la ventana record/task: un crash entre create_or_get y enqueue se
    # repara en el retry (task name determinístico). Un job ya terminal no se
    # re-encola (el claim lo rechazaría igualmente, pero no gastamos el task).
    if record.enqueue_state != "enqueued" and record.state not in TERMINAL_STATES:
        queue = http_request.app.state.ticket_queue
        task_name = await queue.ensure_enqueued(
            record.job_id, record.enqueue_generation)
        record = await repo.mark_enqueued(record.job_id, task_name)

    return record, replayed


async def _wait_for_terminal(
    repo: TicketJobRepository, job_id: str, budget_s: float
) -> Optional[TicketJobRecord]:
    """Espera corta del adapter v1 para responder 200 inline en rutas rápidas
    ya terminadas. Nunca bloquea más allá del budget."""
    deadline = time.monotonic() + max(budget_s, 0.0)
    while True:
        record = await repo.get(job_id)
        if record is not None and record.state in TERMINAL_STATES:
            return record
        if time.monotonic() >= deadline:
            return record
        await asyncio.sleep(0.05)


def _record_results(record: TicketJobRecord) -> List[InquiryResult]:
    return [
        InquiryResult.model_validate(e["result"])
        for e in record.per_inquiry_status
        if e.get("result")
    ]


def _record_has_generate_response(record: TicketJobRecord) -> bool:
    return any(
        e.get("route") == "generate_response" for e in record.per_inquiry_status
    )


def _record_elapsed_s(record: TicketJobRecord) -> Optional[float]:
    """Congelado en terminal (HT-26); en vivo sólo mientras corre."""
    if record.state in TERMINAL_STATES:
        return record.elapsed_s
    if record.created_at is None:
        return None
    return round((utcnow() - record.created_at).total_seconds(), 2)


def _job_handle_response(record: TicketJobRecord, replayed: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={
            "Location": f"/api/v1/tickets/{record.job_id}",
            "Retry-After": "3",
            "Cache-Control": "no-store",
        },
        content=TicketJobHandle(
            ticket_job_id=record.job_id,
            state=record.state.value,
            poll_url=f"/api/v1/tickets/{record.job_id}",
            estimate={"avg_seconds": int(settings.FORUSBOTS_MAX_WAIT_S),
                      "idempotency_replayed": replayed},
        ).model_dump(),
    )


@app.post(
    "/api/v1/handle-ticket",
    dependencies=[
        Depends(verify_api_key),
        Depends(verify_workload_identity),
    ],
    tags=["RAG Endpoints"],
    responses={200: {"model": TicketHandleResponse}, 202: {"model": TicketJobHandle}},
)
async def handle_ticket_endpoint(
    request: HandleTicketRequest,
    http_request: Request,
    repo: TicketJobRepository = Depends(get_ticket_repo),
):
    """
    Endpoint 5: end-to-end ticket handler (adapter v1 sobre el job durable).

    **Contrato:** todo request aceptado crea un job DURABLE y encola su
    ejecución. Si el job termina dentro de una espera corta y no contiene
    rutas ``generate_response``, responde ``200`` inline (compat con el
    contrato híbrido); en cualquier otro caso responde ``202`` y n8n hace
    poll de ``GET /api/v1/tickets/{id}``. La idempotencia se reserva en
    transacción ANTES de cualquier LLM: misma key + mismo payload replaya el
    job; misma key + otro payload es ``409``.

    **Auth:** conserva el flujo existente de n8n: Cloud Run IAM valida
    ``Authorization`` y la aplicación valida ``X-API-Key``. La comprobación
    workload adicional es opcional y queda desactivada si no tiene audiencia.

    **Rollout:** gated por ``TICKET_HANDLER_MODE``; el override del body sólo
    puede RESTRINGIR el modo del servidor.
    """
    record, replayed = await _accept_ticket_job(
        request, http_request, repo, api_version="v1"
    )

    record = await _wait_for_terminal(
        repo, record.job_id, settings.TICKET_V1_INLINE_WAIT_S
    ) or record

    # Un 200 inline de v1 SÓLO es válido cuando el resultado es publicable
    # (Tarea 4 Paso 6): succeeded + send_participant_reply + todas las
    # inquiries seguras + sin fallback. Cualquier otra cosa (shadow, coerción
    # de modo, degradación) va por 202/poll para que n8n lea el next_action
    # explícito; nunca se oculta use_legacy dentro de un 200.
    metadata = (record.public_result or {}).get("metadata", {})
    all_safe = bool(record.per_inquiry_status) and all(
        e.get("participant_reply_safe") for e in record.per_inquiry_status
    )
    fast_inline = (
        record.state == TicketJobState.SUCCEEDED
        and record.next_action == NextAction.SEND_PARTICIPANT_REPLY
        and metadata.get("fallback") is not True
        and all_safe
        and not _record_has_generate_response(record)
    )
    if fast_inline:
        results = _record_results(record)
        if results:
            return TicketHandleResponse(
                route_taken=results[0].route,
                primary=results[0],
                related=results[1:],
                total_inquiries_in_ticket=record.total_inquiries or 0,
                metadata=metadata,
            )
    return _job_handle_response(record, replayed)


@app.get(
    "/api/v1/tickets/{ticket_job_id}",
    response_model=TicketStatusResponse,
    dependencies=[
        Depends(verify_api_key),
        Depends(verify_workload_identity),
    ],
    tags=["RAG Endpoints"],
)
async def get_ticket_status(
    ticket_job_id: str,
    http_request: Request,
    repo: TicketJobRepository = Depends(get_ticket_repo),
):
    """Poll de un ticket job. ``404`` = ID inexistente; ``410`` = el
    control/tombstone sigue vigente pero el payload expiró (no reintentar);
    ``403`` = job de otro principal (invariante 10)."""
    if _TICKET_JOB_ID_RE.fullmatch(ticket_job_id) is None:
        ticket_metrics.increment("ticket_poll_not_found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket job not found or expired.",
        )
    record, payload_present = await repo.get_with_payload_state(ticket_job_id)
    if record is None:
        ticket_metrics.increment("ticket_poll_not_found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket job not found or expired.",
        )
    if not _poll_owner_matches(record, http_request):
        ticket_metrics.increment("ticket_poll_forbidden")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "TICKET_JOB_FORBIDDEN",
                    "message": "el job pertenece a otro principal"},
        )
    record, payload_present = await _lazy_terminalize_poll_record(
        repo, record, payload_present
    )
    if record is None:
        ticket_metrics.increment("ticket_poll_not_found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket job not found or expired.",
        )
    if not payload_present:
        ticket_metrics.increment("ticket_poll_gone")
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "TICKET_JOB_EXPIRED",
                    "message": "el resultado expiró; el receipt impide "
                               "recrear el job con la misma key"},
        )
    _emit_ticket_metric(
        "ticket_n8n_poll_count", 1, state=record.state.value
    )
    results = _record_results(record)
    primary = results[0] if results else None
    return TicketStatusResponse(
        ticket_job_id=record.job_id,
        state=record.state.value,
        route_taken=primary.route if primary else None,
        primary=primary,
        related=results[1:] if results else [],
        total_inquiries_in_ticket=record.total_inquiries,
        forusbots_job_ids=record.forusbots_job_ids,
        elapsed_s=_record_elapsed_s(record),
        error=record.public_error_code,
        # metadata visible también en el poll: un job shadow (fallback=true)
        # nunca debe parecer publicable aunque llegue por 202+poll (HT-11)
        metadata=(record.public_result or {}).get("metadata", {}),
        next_action=record.next_action.value,
    )


# ============================================================================
# v2: contrato uniforme 202 + polling (plan §6)
# ============================================================================

@app.post(
    "/api/v2/handle-ticket",
    dependencies=[
        Depends(verify_api_key),
        Depends(verify_workload_identity),
    ],
    tags=["RAG Endpoints"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketJobAcceptedV2,
    responses={
        401: {"model": ErrorResponse,
              "description": "Falta X-API-Key o la identidad de Cloud Run"},
        403: {"model": ErrorResponse,
              "description": "Credencial inválida o "
                             "PARTICIPANT_PLAN_MISMATCH"},
        409: {"model": ErrorResponse,
              "description": "IDEMPOTENCY_PAYLOAD_MISMATCH: la misma "
                             "Idempotency-Key con otro payload"},
        413: {"model": ErrorResponse, "description": "Body > 1 MiB"},
        422: {"description": "Falta Idempotency-Key o el body contiene campos "
                             "prohibidos (idempotency_key/ticket_handler_mode)"},
        429: {"model": ErrorResponse,
              "description": "RATE_LIMITED o TOO_MANY_OUTSTANDING_JOBS "
                             "(con Retry-After)"},
        503: {"model": ErrorResponse,
              "description": "Handler disabled o dependencia opcional "
                             "no disponible"},
    },
)
async def handle_ticket_v2(
    request: HandleTicketV2Request,
    http_request: Request,
    repo: TicketJobRepository = Depends(get_ticket_repo),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description="OBLIGATORIO: key estable por evento lógico de ticket; "
                    "el replay devuelve el mismo job.",
    ),
):
    """Contrato v2: SIEMPRE ``202 + polling`` sobre el job durable.

    Auth: el mismo contrato existente de n8n: ``Authorization`` para Cloud Run
    IAM y ``X-API-Key`` para la aplicación. El body es estricto: no acepta
    ``idempotency_key`` ni ``ticket_handler_mode`` — la key viaja SÓLO en el
    header y el rollout es exclusivamente server-side."""
    record, replayed = await _accept_ticket_job(
        request, http_request, repo, api_version="v2", allow_body_idem=False
    )
    status_url = f"/api/v2/ticket-jobs/{record.job_id}"
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={
            "Location": status_url,
            "Retry-After": "3",
            "Cache-Control": "no-store",
        },
        content=TicketJobAcceptedV2(
            ticket_job_id=record.job_id,
            state=record.state,
            status_url=status_url,
            retry_after_seconds=3,
            idempotency_replayed=replayed,
        ).model_dump(mode="json"),
    )


@app.get(
    "/api/v2/ticket-jobs/{ticket_job_id}",
    dependencies=[
        Depends(verify_api_key),
        Depends(verify_workload_identity),
    ],
    tags=["RAG Endpoints"],
    response_model=TicketJobStatusV2,
    responses={
        401: {"model": ErrorResponse, "description": "Falta X-API-Key"},
        403: {"model": ErrorResponse,
              "description": "TICKET_JOB_FORBIDDEN: el job es de otro principal"},
        404: {"model": ErrorResponse,
              "description": "TICKET_JOB_NOT_FOUND: ID desconocido"},
        410: {"model": ErrorResponse,
              "description": "TICKET_JOB_EXPIRED: el receipt/tombstone sigue "
                             "vigente pero el payload/resultado ya expiró; no "
                             "reintentar con la misma key"},
    },
)
async def get_ticket_job_v2(
    ticket_job_id: str,
    http_request: Request,
    repo: TicketJobRepository = Depends(get_ticket_repo),
):
    if _TICKET_JOB_ID_RE.fullmatch(ticket_job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TICKET_JOB_NOT_FOUND"},
        )
    record, payload_present = await repo.get_with_payload_state(ticket_job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TICKET_JOB_NOT_FOUND"},
        )
    if not _poll_owner_matches(record, http_request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "TICKET_JOB_FORBIDDEN"},
        )
    record, payload_present = await _lazy_terminalize_poll_record(
        repo, record, payload_present
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TICKET_JOB_NOT_FOUND"},
        )
    if not payload_present:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "TICKET_JOB_EXPIRED",
                    "message": "el resultado expiró; el receipt impide "
                               "recrear el job con la misma key"},
        )
    _emit_ticket_metric(
        "ticket_n8n_poll_count", 1, state=record.state.value
    )
    inquiries = [
        InquiryStatusV2(
            index=e.get("index", i),
            route=e.get("route"),
            execution_status=e.get("execution_status", "pending"),
            participant_reply_safe=bool(e.get("participant_reply_safe")),
            manual_reconciliation_required=bool(
                e.get("manual_reconciliation_required")
            ),
            result=e.get("result"),
            error=e.get("error"),
        )
        for i, e in enumerate(record.per_inquiry_status)
    ]
    return TicketJobStatusV2(
        ticket_job_id=record.job_id,
        state=record.state.value,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        elapsed_s=_record_elapsed_s(record),
        total_inquiries=record.total_inquiries,
        processed_inquiries=record.processed_inquiries,
        unprocessed_inquiries=record.unprocessed_inquiries,
        forusbots_job_ids=record.forusbots_job_ids,
        inquiries=inquiries,
        next_action=record.next_action.value,
        error={"code": record.public_error_code,
               "retryable": record.retryable,
               "trace_id": record.trace_id}
        if record.public_error_code else None,
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
        logger.info(
            "List chunks request | article_filter=%s | tier_filter=%s | "
            "type_filter=%s | limit=%d",
            request.article_id is not None,
            request.tier is not None,
            request.chunk_type is not None,
            request.limit,
        )

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
            except Exception as exc:
                logger.warning(
                    "Error parsing chunk (error_type=%s)", type(exc).__name__
                )
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

    except Exception as exc:
        logger.error("Error in list_chunks endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while listing chunks."
        ) from exc


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

    except Exception as exc:
        logger.error("Error getting index stats")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while retrieving index stats."
        ) from exc


# ============================================================================
# Error Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handler para HTTPException. Los details estructurados (dict con
    ``code`` machine-readable) se preservan bajo ``detail`` además del shape
    legacy ``error/message/request_id``."""
    request_id = getattr(request.state, "request_id", "unknown")

    content = {
        "error": "http_error",
        "message": exc.detail,
        "request_id": request_id,
    }
    if isinstance(exc.detail, dict):
        content["detail"] = exc.detail
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handler para excepciones generales."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error("Unhandled exception | Request ID: %s", request_id)

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
