"""
Configuración de la API.

Maneja variables de entorno y settings de la aplicación.
Pydantic BaseSettings reads env vars automatically — no os.getenv needed.
"""

import logging
from typing import List
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Settings de la aplicación."""
    
    # API Configuration
    API_VERSION: str = "1.0.0"
    API_TITLE: str = "KB RAG System API"
    API_DESCRIPTION: str = "API para sistema RAG de Knowledge Base de Participant Advisory"
    
    # Server
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    ENVIRONMENT: str = "development"

    # Rol de proceso (plan de finalización, Tarea 4 Paso 1a). La MISMA imagen
    # se despliega con roles excluyentes; cada rol sólo sirve sus rutas:
    #   producer   → API completa existente (v1/v2/status + core no-ticket);
    #                nunca expone /internal/tasks/ticket-job
    #   worker     → health/readiness + ruta interna de Cloud Tasks; sin v1/v2
    #   reconciler → batch (python -m data_pipeline.ticket_reconciler);
    #                ninguna ruta pública salvo probes
    APP_ROLE: str = "producer"
    
    # Security
    API_KEY: str = ""
    ALLOWED_ORIGINS: List[str] = ["*"]
    
    # OpenAI
    OPENAI_API_KEY: str = ""
    # NOTE: OPENAI_MODEL / OPENAI_TEMPERATURE / OPENAI_REASONING_EFFORT remain
    # readable for backward compatibility, but runtime model selection now
    # flows through the LLM_ROUTE_* vars + LLMRouter.
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TEMPERATURE: float = 0.1
    OPENAI_REASONING_EFFORT: str = "medium"

    # Gemini / Vertex AI
    GEMINI_API_KEY: str = ""
    USE_VERTEX_AI: bool = False
    GCP_LOCATION: str = "us-central1"

    # LLM Routing (model name per task; provider is inferred from prefix).
    LLM_ROUTE_DECOMPOSE: str = "gpt-5.5"
    LLM_ROUTE_REQUIRED_DATA: str = "gpt-5.5"
    LLM_ROUTE_GR_OUTCOME: str = "gpt-5.5"
    LLM_ROUTE_GR_RESPONSE: str = "gpt-5.5"
    LLM_ROUTE_KNOWLEDGE: str = "gpt-5.5"
    LLM_ROUTE_CLASSIFY: str = "gemini-2.5-flash"

    # LLM Routing for the end-to-end ticket handler (LLM-first: the 4 n8n agents
    # become 4 internal LLM calls). See ticket-handler-planning/stage-3-*.md.
    LLM_ROUTE_EXTRACT_INQUIRIES: str = "gpt-5.5"
    LLM_ROUTE_KB_QUESTION_SYNTHESIS: str = "gpt-5.5"
    LLM_ROUTE_FORUSBOTS_FIELD_MAP: str = "gpt-5.5"
    LLM_ROUTE_GR_BODY_BUILD: str = "gpt-5.5"
    LLM_ROUTE_TICKET_FIELD_EXTRACT: str = "gpt-5.5"

    # Inquiry router rollout flag. Stage 4 reads this to decide whether the
    # /route-inquiry endpoint is exposed and how it behaves:
    #   disabled        → endpoint returns 503
    #   shadow          → classify but always return needs_more_info to caller
    #   knowledge_only  → only knowledge_question routes are honored
    #   full            → all routes honored
    # Default is "full" because the endpoint is generally available; operators
    # can downgrade per-environment via the ROUTER_MODE env var.
    ROUTER_MODE: str = "full"

    # ForusBots scraping service (end-to-end ticket handler).
    # Auth is the `x-auth-token` header. All scrape endpoints are async
    # (202 + jobId, then poll). See ticket-handler-planning/stage-1-*.md.
    FORUSBOTS_BASE_URL: str = "http://35.224.156.104:10000"
    FORUSBOTS_AUTH_TOKEN: str = ""
    FORUSBOTS_POLL_INTERVAL_S: float = 3.0
    FORUSBOTS_POLL_BACKOFF: float = 1.3
    FORUSBOTS_POLL_MAX_INTERVAL_S: float = 10.0
    FORUSBOTS_MAX_WAIT_S: float = 200.0
    FORUSBOTS_HTTP_READ_TIMEOUT_S: float = 15.0
    FORUSBOTS_MAX_INFLIGHT: int = 2
    FORUSBOTS_RESULT_CACHE_TTL_S: int = 180

    # Ticket handler orchestrator rollout flag. Mirrors ROUTER_MODE:
    #   disabled        → endpoint returns 503
    #   shadow          → runs the pipeline but tells n8n to use the legacy flow
    #   knowledge_only  → only knowledge_question tickets are handled end-to-end
    #   full            → full orchestration
    # Default "disabled": a brand-new orchestrator ships dark.
    TICKET_HANDLER_MODE: str = "disabled"
    TICKET_INQUIRY_BUDGET_S: float = 300.0
    TICKET_TOTAL_BUDGET_S: float = 480.0
    TICKET_JOB_TTL_S: int = 1800
    TICKET_MAX_RELATED: int = 3
    RATE_LIMIT_HANDLE_TICKET: int = 20

    # Presupuestos y relojes durables (plan Tarea 7 Paso 1). Relaciones:
    # heartbeat*3 <= lease; intento(480) < Cloud Run worker timeout(520) <
    # dispatch deadline(540); job deadline(2400) + poll(<=30) <= n8n
    # watch(2700) con margen. El retry config de Cloud Tasks NO es el reloj
    # autoritativo: job_deadline_at da la garantía.
    TICKET_ATTEMPT_BUDGET_S: float = 480.0
    TICKET_JOB_DEADLINE_S: int = 2400
    TICKET_WORKER_LEASE_S: float = 90.0
    TICKET_WORKER_HEARTBEAT_S: float = 30.0
    TICKET_TASK_DISPATCH_DEADLINE_S: int = 540
    TICKET_ADMISSION_QUEUE_DELAY_CEILING_S: int = 300

    # Durable ticket jobs (Task 3/4 del plan de remediación).
    #   TICKET_JOB_BACKEND: "memory" (dev/tests) | "firestore" (producción)
    #   TICKET_TASK_QUEUE:  "inline" (dev/tests) | "cloudtasks" (producción)
    # Producción con modo activo exige firestore + cloudtasks (fail-closed).
    TICKET_JOB_BACKEND: str = "memory"
    TICKET_TASK_QUEUE: str = "inline"
    TICKET_JOB_RETENTION_S: int = 86400
    FIRESTORE_TICKET_COLLECTION_PREFIX: str = ""
    # Base Firestore NOMBRADA (Tarea 5 Paso 3): la base —no un prefijo— es el
    # límite de aislamiento. Staging usa `ticket-staging`; producción usa
    # `(default)`. Obligatoria cuando TICKET_JOB_BACKEND=firestore.
    FIRESTORE_DATABASE: str = ""
    # Retención conjunta de receipts + control/tombstones (Tarea 5 Paso 2):
    # default 90d; nunca menor al horizonte máximo acordado en Tarea 1
    # (redelivery de la fuente, dedupe downstream, retención de rollback).
    TICKET_IDEMPOTENCY_RETENTION_DAYS: int = 90
    CLOUD_TASKS_QUEUE: str = "ticket-jobs"
    CLOUD_TASKS_LOCATION: str = "us-central1"
    TICKET_WORKER_URL: str = ""            # URL pública del worker (Cloud Tasks target)
    TICKET_WORKER_SERVICE_ACCOUNT: str = ""  # SA que firma el OIDC de Cloud Tasks
    TICKET_WORKER_REQUIRE_OIDC: bool = True
    # v1 adapter: espera corta para poder responder 200 inline en rutas rápidas
    # ya terminadas; si el job sigue vivo al vencer, responde 202 + poll.
    TICKET_V1_INLINE_WAIT_S: float = 3.0

    # Identidad de clientes: nombre de principal → API key. La API_KEY legacy
    # mapea al principal "default". Rotación: agregar la key nueva bajo el
    # mismo principal con sufijo (p.ej. "n8n" y "n8n_next"), migrar el caller
    # y retirar la vieja.
    API_CLIENT_KEYS: dict = {}
    # Tenant CANÓNICO por principal (Tarea 4 Paso 2): el tenant deriva de la
    # credencial autenticada, nunca del texto del ticket ni de un header sin
    # firmar. En producción este mapping vive en Secret Manager junto a
    # API_CLIENT_KEYS.
    API_CLIENT_TENANTS: dict = {}

    # Fuente canónica participant-plan (Tarea 4 Paso 1 / contrato Tarea 1).
    # "" = no configurada: un modo ACTIVO no puede arrancar así (fail-closed);
    # el adaptador concreto se cablea cuando el equipo owner entregue el
    # contrato (docs/verification/handle-ticket/01-external-contracts.md §1).
    PARTICIPANT_PLAN_SOURCE: str = ""
    PARTICIPANT_PLAN_TIMEOUT_S: float = 5.0

    # Identidad workload de v2 (Tarea 4 Paso 2a). v2 activo exige DOS
    # credenciales independientes: X-API-Key (cliente/tenant) y un ID token
    # Google-signed en X-ForUs-Workload-Authorization verificado en la app
    # (firma/issuer/audience/SA/exp). Cloud Run elimina la firma de
    # X-Serverless-Authorization antes de entregarlo: ese header se rechaza.
    # Vacíos = verificación desactivada (SÓLO dev/tests; producción activa
    # exige ambos configurados — validate_settings falla cerrado).
    TICKET_WIF_AUDIENCE: str = ""
    TICKET_WIF_EXPECTED_EMAIL: str = ""

    # Límites de recursos (Task 6, OWASP API4).
    MAX_REQUEST_BODY_BYTES: int = 1_048_576          # 1 MiB
    TICKET_MAX_OUTSTANDING_JOBS: int = 25            # por principal

    # Shadow real muestreado (Task 10): fracción de jobs shadow que ejecutan
    # el pipeline completo (sin publicar) para el differential harness.
    TICKET_SHADOW_SAMPLE_RATE: float = 0.0

    # Pinecone
    PINECONE_API_KEY: str = ""
    INDEX_NAME: str = "kb-articles-production"
    NAMESPACE: str = "kb_articles"
    
    # Logging
    LOG_LEVEL: str = "INFO"
    
    # Rate Limiting (requests per minute)
    RATE_LIMIT_REQUIRED_DATA: int = 60
    RATE_LIMIT_GENERATE_RESPONSE: int = 30
    
    # GCP
    GCP_PROJECT: str = ""
    GCS_BUCKET: str = ""
    ENABLE_EXECUTION_LOGGING: bool = False
    
    model_config = {
        "env_file": ".env",
        "case_sensitive": True,
        "extra": "ignore",
    }

    @property
    def cors_origins(self) -> List[str]:
        """Return CORS origins based on environment."""
        if self.ENVIRONMENT == "production":
            return [o for o in self.ALLOWED_ORIGINS if o != "*"] or [
                "https://forusguide.onrender.com"
            ]
        return self.ALLOWED_ORIGINS


# Singleton instance
settings = Settings()


def validate_settings():
    """Valida que todas las settings críticas estén configuradas."""
    errors = []

    if not settings.API_KEY:
        errors.append("API_KEY no está configurada")

    if not settings.PINECONE_API_KEY:
        errors.append("PINECONE_API_KEY no está configurada")

    has_openai = bool(settings.OPENAI_API_KEY)
    has_gemini = bool(settings.GEMINI_API_KEY) or (
        settings.USE_VERTEX_AI and bool(settings.GCP_PROJECT)
    )

    if not has_openai and not has_gemini:
        errors.append(
            "Debe configurarse al menos un proveedor LLM: "
            "OPENAI_API_KEY, o GEMINI_API_KEY, o USE_VERTEX_AI=true + GCP_PROJECT"
        )

    # Each route's model must have its provider's credentials available.
    route_models = {
        "LLM_ROUTE_DECOMPOSE": settings.LLM_ROUTE_DECOMPOSE,
        "LLM_ROUTE_REQUIRED_DATA": settings.LLM_ROUTE_REQUIRED_DATA,
        "LLM_ROUTE_GR_OUTCOME": settings.LLM_ROUTE_GR_OUTCOME,
        "LLM_ROUTE_GR_RESPONSE": settings.LLM_ROUTE_GR_RESPONSE,
        "LLM_ROUTE_KNOWLEDGE": settings.LLM_ROUTE_KNOWLEDGE,
        "LLM_ROUTE_CLASSIFY": settings.LLM_ROUTE_CLASSIFY,
        "LLM_ROUTE_EXTRACT_INQUIRIES": settings.LLM_ROUTE_EXTRACT_INQUIRIES,
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS": settings.LLM_ROUTE_KB_QUESTION_SYNTHESIS,
        "LLM_ROUTE_FORUSBOTS_FIELD_MAP": settings.LLM_ROUTE_FORUSBOTS_FIELD_MAP,
        "LLM_ROUTE_GR_BODY_BUILD": settings.LLM_ROUTE_GR_BODY_BUILD,
        "LLM_ROUTE_TICKET_FIELD_EXTRACT": settings.LLM_ROUTE_TICKET_FIELD_EXTRACT,
    }
    for var_name, model_name in route_models.items():
        model_lower = (model_name or "").strip().lower()
        if not model_lower:
            errors.append(f"{var_name} no puede estar vacío")
            continue
        if model_lower.startswith("gpt-") and not has_openai:
            errors.append(
                f"{var_name}={model_name} requiere OPENAI_API_KEY configurada"
            )
        elif model_lower.startswith("gemini-") and not has_gemini:
            errors.append(
                f"{var_name}={model_name} requiere GEMINI_API_KEY o "
                f"USE_VERTEX_AI=true + GCP_PROJECT"
            )
        elif not (model_lower.startswith("gpt-") or model_lower.startswith("gemini-")):
            errors.append(
                f"{var_name}={model_name} tiene un prefijo desconocido "
                f"(se esperaba 'gpt-*' o 'gemini-*')"
            )

    # Ticket handler rollout flag must be one of the known modes.
    valid_ticket_modes = {"disabled", "shadow", "knowledge_only", "full"}
    if settings.TICKET_HANDLER_MODE not in valid_ticket_modes:
        errors.append(
            f"TICKET_HANDLER_MODE={settings.TICKET_HANDLER_MODE} inválido "
            f"(se esperaba uno de {sorted(valid_ticket_modes)})"
        )

    # Rol de proceso cerrado (Tarea 4 Paso 1a). Un rol inválido impide el
    # arranque; cada rol valida sus dependencias específicas.
    valid_roles = {"producer", "worker", "reconciler"}
    if settings.APP_ROLE not in valid_roles:
        errors.append(
            f"APP_ROLE={settings.APP_ROLE} inválido "
            f"(se esperaba uno de {sorted(valid_roles)})"
        )

    active_mode = settings.TICKET_HANDLER_MODE in valid_ticket_modes - {"disabled"}

    # Autorización participant-plan fail-closed: un producer ACTIVO sin fuente
    # canónica configurada no puede arrancar. `None` nunca es autorización.
    if settings.APP_ROLE == "producer" and active_mode \
            and not settings.PARTICIPANT_PLAN_SOURCE:
        errors.append(
            f"TICKET_HANDLER_MODE={settings.TICKET_HANDLER_MODE} requiere "
            "PARTICIPANT_PLAN_SOURCE (fuente canónica participant-plan); "
            "sin validador la autorización queda abierta"
        )

    # Identidad workload de v2: producción activa exige la verificación
    # completa configurada (audiencia + SA esperada).
    if settings.ENVIRONMENT == "production" and active_mode \
            and settings.APP_ROLE == "producer":
        if not settings.TICKET_WIF_AUDIENCE or not settings.TICKET_WIF_EXPECTED_EMAIL:
            errors.append(
                "producción con ticket handler activo requiere "
                "TICKET_WIF_AUDIENCE y TICKET_WIF_EXPECTED_EMAIL (verificación "
                "del ID token workload de n8n en v2)"
            )

    # Base Firestore nombrada (Tarea 5 Paso 3): la base es el límite de
    # aislamiento; el rechazo cruzado staging/(default) es fail-closed.
    if settings.TICKET_JOB_BACKEND == "firestore":
        if not settings.FIRESTORE_DATABASE:
            errors.append(
                "TICKET_JOB_BACKEND=firestore requiere FIRESTORE_DATABASE "
                "explícita ((default) en producción, ticket-staging en staging)"
            )
        elif settings.ENVIRONMENT == "production" \
                and settings.FIRESTORE_DATABASE != "(default)":
            errors.append(
                f"producción sólo puede usar la base (default); "
                f"FIRESTORE_DATABASE={settings.FIRESTORE_DATABASE} está prohibido"
            )
        elif settings.ENVIRONMENT == "staging" \
                and settings.FIRESTORE_DATABASE != "ticket-staging":
            errors.append(
                f"staging sólo puede usar la base ticket-staging; "
                f"FIRESTORE_DATABASE={settings.FIRESTORE_DATABASE} está prohibido"
            )

    # Contención fail-closed del ticket handler: un modo activo no puede
    # arrancar sin token, y producción nunca habla con ForusBots por HTTP
    # plano (el token y PII del participante viajan en cada request).
    if settings.TICKET_HANDLER_MODE in valid_ticket_modes - {"disabled"}:
        if not settings.FORUSBOTS_AUTH_TOKEN:
            errors.append(
                f"TICKET_HANDLER_MODE={settings.TICKET_HANDLER_MODE} requiere "
                "FORUSBOTS_AUTH_TOKEN configurado"
            )
        is_tls = settings.FORUSBOTS_BASE_URL.lower().startswith("https://")
        if not is_tls and settings.ENVIRONMENT == "production":
            errors.append(
                f"TICKET_HANDLER_MODE={settings.TICKET_HANDLER_MODE} en producción "
                f"requiere FORUSBOTS_BASE_URL https:// "
                f"(actual: {settings.FORUSBOTS_BASE_URL.split('://')[0]}://…)"
            )
        elif not is_tls:
            logger.warning(
                "FORUSBOTS_BASE_URL no usa https:// — permitido sólo fuera de "
                "producción. El token y PII viajan sin cifrar."
            )

    # Ejecución durable fail-closed: producción con modo activo no puede
    # depender de memoria de proceso ni de asyncio local (HT-01).
    if settings.TICKET_JOB_BACKEND not in {"memory", "firestore"}:
        errors.append(
            f"TICKET_JOB_BACKEND={settings.TICKET_JOB_BACKEND} inválido "
            "(se esperaba memory|firestore)"
        )
    if settings.TICKET_TASK_QUEUE not in {"inline", "cloudtasks"}:
        errors.append(
            f"TICKET_TASK_QUEUE={settings.TICKET_TASK_QUEUE} inválido "
            "(se esperaba inline|cloudtasks)"
        )
    if (
        settings.TICKET_HANDLER_MODE in valid_ticket_modes - {"disabled"}
        and settings.ENVIRONMENT == "production"
    ):
        if settings.TICKET_JOB_BACKEND != "firestore":
            errors.append(
                "producción con ticket handler activo requiere "
                "TICKET_JOB_BACKEND=firestore (los jobs no pueden vivir en "
                "memoria de proceso)"
            )
        if settings.TICKET_TASK_QUEUE != "cloudtasks":
            errors.append(
                "producción con ticket handler activo requiere "
                "TICKET_TASK_QUEUE=cloudtasks"
            )
        if settings.TICKET_TASK_QUEUE == "cloudtasks" and not settings.TICKET_WORKER_URL:
            errors.append("TICKET_TASK_QUEUE=cloudtasks requiere TICKET_WORKER_URL")
        if settings.TICKET_TASK_QUEUE == "cloudtasks":
            # No existe una opción production sin OIDC ni sin SA firmante
            # (Tarea 7 Paso 6): Cloud Tasks debe firmar y el worker verificar.
            if not settings.TICKET_WORKER_SERVICE_ACCOUNT:
                errors.append(
                    "producción con cloudtasks requiere "
                    "TICKET_WORKER_SERVICE_ACCOUNT (SA firmante del OIDC)"
                )
            if not settings.TICKET_WORKER_REQUIRE_OIDC:
                errors.append(
                    "TICKET_WORKER_REQUIRE_OIDC=false está prohibido en "
                    "producción: el worker sólo acepta tasks OIDC-firmadas"
                )

    if errors:
        raise ValueError(f"Configuración inválida: {', '.join(errors)}")

    return True
