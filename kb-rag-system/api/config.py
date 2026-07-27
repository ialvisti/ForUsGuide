"""
Configuración de la API.

Maneja variables de entorno y settings de la aplicación.
Pydantic BaseSettings reads env vars automatically — no os.getenv needed.
"""

import logging
import math
from typing import List
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings

from data_pipeline.forusbots_client import (
    ForusBotsError,
    validate_forusbots_base_url,
)
from data_pipeline.llm_router import (
    build_routes_from_settings,
    parse_llm_pricing_json,
    required_pricing_keys,
)

logger = logging.getLogger(__name__)


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _is_canonical_https_origin(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class Settings(BaseSettings):
    """Settings de la aplicación."""

    # API Configuration
    API_VERSION: str = "1.0.0"
    API_TITLE: str = "KB RAG System API"
    API_DESCRIPTION: str = "API para sistema RAG de Knowledge Base de Participant Advisory"

    # Server
    # Cloud Run requires the container to listen on every interface.
    API_HOST: str = "0.0.0.0"  # noqa: S104
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
    # Reviewed USD-per-1M-token estimates for durable ticket telemetry. The
    # JSON contains pricing_as_of/source metadata plus exact provider:model
    # entries. Producer/core traffic never depends on this ticket-only gate.
    TICKET_LLM_PRICING_JSON: str = ""

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

    # Fault injection SÓLO staging (plan Tarea 7 Paso 7a). Producción rechaza
    # tanto el header de test como el fault_plan. APP_ENV distingue el entorno
    # de despliegue (independiente de ENVIRONMENT, que gobierna CORS/logging).
    APP_ENV: str = "development"
    TICKET_FAULT_SIGNING_SECRET: str = ""

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
    # Audiencia OIDC estable configurada como custom audience de Cloud Run. No
    # puede derivarse de TICKET_WORKER_URL dentro del template del propio worker.
    TICKET_WORKER_AUDIENCE: str = ""
    TICKET_WORKER_SERVICE_ACCOUNT: str = ""  # SA que firma el OIDC de Cloud Tasks
    TICKET_WORKER_REQUIRE_OIDC: bool = True
    # v1 adapter: espera corta para poder responder 200 inline en rutas rápidas
    # ya terminadas; si el job sigue vivo al vencer, responde 202 + poll.
    TICKET_V1_INLINE_WAIT_S: float = 3.0

    # Identidad de clientes: principal ESTABLE → una o varias API keys. La
    # lista permite rotación solapada sin cambiar owner/idempotencia/polling;
    # crear principals con sufijo para una key nueva está prohibido por diseño.
    # La API_KEY legacy mapea al principal "default".
    API_CLIENT_KEYS: dict[str, str | list[str]] = {}
    # Tenant CANÓNICO por principal (Tarea 4 Paso 2): el tenant deriva de la
    # credencial autenticada, nunca del texto del ticket ni de un header sin
    # firmar. En producción este mapping vive en Secret Manager junto a
    # API_CLIENT_KEYS.
    API_CLIENT_TENANTS: dict[str, str] = {}

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
    # Vacíos = verificación desactivada sólo en dev/tests; staging/production
    # activos exigen audiencia + allowlist (validate_settings falla cerrado).
    TICKET_WIF_AUDIENCE: str = ""
    # Allowlist exacta de service-account emails autorizados. Staging incluye
    # n8n + el runner E2E; producción sólo n8n. BaseSettings la recibe como
    # array JSON desde el entorno/Secret Manager.
    TICKET_WIF_ALLOWED_EMAILS: List[str] = []
    # Compatibilidad local temporal para fixtures/entornos anteriores. Los
    # entornos desplegados activos exigen la allowlist y no usan este fallback.
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


def validate_settings() -> bool:
    """Valida que todas las settings críticas estén configuradas."""
    errors = []

    positive_timings = {
        "TICKET_INQUIRY_BUDGET_S": settings.TICKET_INQUIRY_BUDGET_S,
        "TICKET_TOTAL_BUDGET_S": settings.TICKET_TOTAL_BUDGET_S,
        "TICKET_ATTEMPT_BUDGET_S": settings.TICKET_ATTEMPT_BUDGET_S,
        "TICKET_JOB_DEADLINE_S": settings.TICKET_JOB_DEADLINE_S,
        "TICKET_WORKER_LEASE_S": settings.TICKET_WORKER_LEASE_S,
        "TICKET_WORKER_HEARTBEAT_S": settings.TICKET_WORKER_HEARTBEAT_S,
        "TICKET_TASK_DISPATCH_DEADLINE_S": (
            settings.TICKET_TASK_DISPATCH_DEADLINE_S
        ),
        "TICKET_ADMISSION_QUEUE_DELAY_CEILING_S": (
            settings.TICKET_ADMISSION_QUEUE_DELAY_CEILING_S
        ),
        "TICKET_V1_INLINE_WAIT_S": settings.TICKET_V1_INLINE_WAIT_S,
        "PARTICIPANT_PLAN_TIMEOUT_S": settings.PARTICIPANT_PLAN_TIMEOUT_S,
        "FORUSBOTS_POLL_INTERVAL_S": settings.FORUSBOTS_POLL_INTERVAL_S,
        "FORUSBOTS_POLL_BACKOFF": settings.FORUSBOTS_POLL_BACKOFF,
        "FORUSBOTS_POLL_MAX_INTERVAL_S": settings.FORUSBOTS_POLL_MAX_INTERVAL_S,
        "FORUSBOTS_MAX_WAIT_S": settings.FORUSBOTS_MAX_WAIT_S,
        "FORUSBOTS_HTTP_READ_TIMEOUT_S": settings.FORUSBOTS_HTTP_READ_TIMEOUT_S,
        "FORUSBOTS_RESULT_CACHE_TTL_S": settings.FORUSBOTS_RESULT_CACHE_TTL_S,
        "FORUSBOTS_MAX_INFLIGHT": settings.FORUSBOTS_MAX_INFLIGHT,
    }
    invalid_timings = [
        name for name, value in positive_timings.items()
        if not _finite_positive(value)
    ]
    if invalid_timings:
        errors.append(
            "runtime timings must be finite positive: "
            + ", ".join(sorted(invalid_timings))
        )
    else:
        if settings.TICKET_WORKER_HEARTBEAT_S * 3 > settings.TICKET_WORKER_LEASE_S:
            errors.append("heartbeat*3 debe ser <= worker lease")
        if settings.TICKET_ATTEMPT_BUDGET_S >= \
                settings.TICKET_TASK_DISPATCH_DEADLINE_S:
            errors.append("attempt budget debe ser menor al dispatch deadline")
        if settings.TICKET_JOB_DEADLINE_S <= max(
            settings.TICKET_ATTEMPT_BUDGET_S,
            settings.TICKET_TASK_DISPATCH_DEADLINE_S,
        ):
            errors.append("job deadline debe superar attempt y dispatch")
        if settings.TICKET_TOTAL_BUDGET_S > settings.TICKET_ATTEMPT_BUDGET_S:
            errors.append("total budget debe caber en attempt budget")
        if settings.TICKET_INQUIRY_BUDGET_S > settings.TICKET_TOTAL_BUDGET_S:
            errors.append("inquiry budget debe caber en total budget")
        if settings.FORUSBOTS_POLL_INTERVAL_S > \
                settings.FORUSBOTS_POLL_MAX_INTERVAL_S:
            errors.append("ForUsBots poll interval debe ser <= poll max interval")
        if settings.FORUSBOTS_HTTP_READ_TIMEOUT_S >= settings.FORUSBOTS_MAX_WAIT_S:
            errors.append("ForUsBots read timeout debe ser menor al max wait")
        if settings.FORUSBOTS_POLL_MAX_INTERVAL_S > settings.FORUSBOTS_MAX_WAIT_S:
            errors.append("ForUsBots poll max interval debe ser <= max wait")
        if settings.FORUSBOTS_MAX_WAIT_S >= settings.TICKET_INQUIRY_BUDGET_S:
            errors.append("ForUsBots max wait debe caber en inquiry budget")
        if settings.FORUSBOTS_POLL_BACKOFF < 1:
            errors.append("ForUsBots poll backoff debe ser >= 1")

    valid_environments = {"development", "staging", "production"}
    if settings.ENVIRONMENT not in valid_environments:
        errors.append(
            f"ENVIRONMENT={settings.ENVIRONMENT!r} inválido (se esperaba uno "
            f"de {sorted(valid_environments)})"
        )
    if settings.APP_ENV not in valid_environments:
        errors.append(
            f"APP_ENV={settings.APP_ENV!r} inválido (se esperaba uno de "
            f"{sorted(valid_environments)})"
        )
    if settings.APP_ENV != settings.ENVIRONMENT:
        errors.append(
            f"APP_ENV={settings.APP_ENV!r} debe coincidir exactamente con "
            f"ENVIRONMENT={settings.ENVIRONMENT!r}"
        )

    valid_roles = {"producer", "worker", "reconciler"}
    role = settings.APP_ROLE
    if role not in valid_roles:
        errors.append(
            f"APP_ROLE={role} inválido (se esperaba uno de "
            f"{sorted(valid_roles)})"
        )
    needs_core_api = role == "producer"
    needs_rag_runtime = role in {"producer", "worker"}

    # Una credencial nunca puede resolver a dos principals. Además, una
    # rotación se representa como lista bajo el MISMO principal estable.
    observed_client_keys: dict[str, str] = {}
    for principal, configured in (settings.API_CLIENT_KEYS or {}).items():
        keys = [configured] if isinstance(configured, str) else configured
        if not principal or not keys or any(not key for key in keys):
            errors.append(
                "API_CLIENT_KEYS contiene un principal o credencial vacíos"
            )
            continue
        for key in keys:
            prior = observed_client_keys.get(key)
            if prior is not None:
                errors.append(
                    "API_CLIENT_KEYS contiene una credencial duplicada entre "
                    "principals o dentro de la rotación"
                )
                break
            observed_client_keys[key] = principal

    if needs_core_api and not (settings.API_KEY or settings.API_CLIENT_KEYS):
        errors.append("API_KEY/API_CLIENT_KEYS no está configurado")

    if needs_rag_runtime and not settings.PINECONE_API_KEY:
        errors.append("PINECONE_API_KEY no está configurada")

    has_openai = bool(settings.OPENAI_API_KEY)
    has_gemini = bool(settings.GEMINI_API_KEY) or (
        settings.USE_VERTEX_AI and bool(settings.GCP_PROJECT)
    )

    if needs_rag_runtime and not has_openai and not has_gemini:
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
    for var_name, model_name in (route_models.items() if needs_rag_runtime else ()):
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
    active_mode = settings.TICKET_HANDLER_MODE in valid_ticket_modes - {"disabled"}
    producer_ticket_active = role == "producer" and active_mode
    worker_runtime = role == "worker"
    reconciler_runtime = role == "reconciler"
    deployed_environment = settings.ENVIRONMENT in {"production", "staging"}

    # Cost alerts must be based on an explicit, dated and reviewed table.
    # Producer/core requests do not emit ticket metrics, so a ticket-pricing
    # document can never make that public/core process fail startup. Workers
    # require exact coverage of every primary and fallback model. Reconciler
    # validates the same deployment evidence even though it performs no LLM
    # calls itself.
    if deployed_environment and (worker_runtime or reconciler_runtime):
        try:
            llm_pricing = parse_llm_pricing_json(
                settings.TICKET_LLM_PRICING_JSON
            )
        except ValueError:
            errors.append(
                "TICKET_LLM_PRICING_JSON debe ser un documento de pricing "
                "estricto, fechado y revisado"
            )
        else:
            if not llm_pricing:
                errors.append(
                    "TICKET_LLM_PRICING_JSON debe contener modelos revisados"
                )
            elif worker_runtime:
                try:
                    expected_pricing = required_pricing_keys(
                        build_routes_from_settings(settings)
                    )
                except ValueError:
                    # The route validator already reports the invalid model;
                    # do not echo arbitrary configuration into this error.
                    errors.append(
                        "TICKET_LLM_PRICING_JSON no pudo vincularse a rutas "
                        "LLM válidas"
                    )
                else:
                    if frozenset(llm_pricing) != expected_pricing:
                        errors.append(
                            "TICKET_LLM_PRICING_JSON debe cubrir exactamente "
                            "cada provider:model primario y fallback"
                        )

    # El producer conserva el contrato ya desplegado de n8n: Cloud Run IAM
    # autentica a kb-rag-client y API_KEY autentica la aplicación. Los mapas
    # multi-tenant, el directorio participant-plan y la segunda identidad de
    # aplicación son extensiones opcionales; nunca requisitos de arranque.
    if settings.TICKET_WIF_AUDIENCE \
            or settings.TICKET_WIF_ALLOWED_EMAILS \
            or settings.TICKET_WIF_EXPECTED_EMAIL:
        allowed_wif_emails = settings.TICKET_WIF_ALLOWED_EMAILS or []
        if not settings.TICKET_WIF_AUDIENCE:
            errors.append(
                "TICKET_WIF_AUDIENCE es obligatoria al activar la identidad "
                "workload opcional"
            )
        if not allowed_wif_emails:
            errors.append(
                "TICKET_WIF_ALLOWED_EMAILS no puede estar vacía al activar "
                "la identidad workload opcional"
            )
        elif any(
            not isinstance(email, str)
            or not email
            or email != email.strip()
            for email in allowed_wif_emails
        ):
            errors.append(
                "TICKET_WIF_ALLOWED_EMAILS contiene un email vacío o con "
                "espacios; las identidades deben ser exactas"
            )
        elif len(set(allowed_wif_emails)) != len(allowed_wif_emails):
            errors.append(
                "TICKET_WIF_ALLOWED_EMAILS contiene identidades duplicadas"
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

    # ForusBots pertenece exclusivamente a ejecución durable. El producer
    # sólo autoriza/persiste/encola y no debe necesitar su token ni endpoint.
    needs_forusbots = worker_runtime
    if needs_forusbots:
        if not settings.FORUSBOTS_AUTH_TOKEN:
            errors.append(
                "APP_ROLE=worker requiere FORUSBOTS_AUTH_TOKEN configurado"
            )
        try:
            validate_forusbots_base_url(settings.FORUSBOTS_BASE_URL)
        except ForusBotsError:
            errors.append(
                f"APP_ROLE=worker en {settings.ENVIRONMENT} "
                "requiere FORUSBOTS_BASE_URL como origen canónico revisado"
            )

    # Ejecución durable fail-closed: los roles desplegados no pueden depender
    # de memoria de proceso ni de asyncio local (HT-01). El rollout flag sólo
    # controla admisión en producer; no desactiva worker/reconciler.
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
    # El rollout flag sólo detiene nuevas admisiones. Todo producer desplegado
    # sigue siendo endpoint de polling/rollback para jobs existentes y, por
    # tanto, jamás puede degradar ese estado a memoria de proceso.
    needs_durable_repo = deployed_environment and (
        role == "producer" or worker_runtime or reconciler_runtime
    )
    sends_cloud_tasks = deployed_environment and (
        producer_ticket_active or reconciler_runtime
    )
    verifies_task_oidc = deployed_environment and worker_runtime

    if needs_durable_repo:
        if settings.TICKET_JOB_BACKEND != "firestore":
            errors.append(
                f"APP_ROLE={role} en {settings.ENVIRONMENT} requiere "
                "TICKET_JOB_BACKEND=firestore (los jobs no pueden vivir en "
                "memoria de proceso)"
            )

    if sends_cloud_tasks:
        if settings.TICKET_TASK_QUEUE != "cloudtasks":
            errors.append(
                f"APP_ROLE={role} en {settings.ENVIRONMENT} requiere "
                "TICKET_TASK_QUEUE=cloudtasks"
            )
        for field_name, value in (
            ("GCP_PROJECT", settings.GCP_PROJECT),
            ("CLOUD_TASKS_LOCATION", settings.CLOUD_TASKS_LOCATION),
            ("CLOUD_TASKS_QUEUE", settings.CLOUD_TASKS_QUEUE),
            ("TICKET_WORKER_URL", settings.TICKET_WORKER_URL),
        ):
            if not value:
                errors.append(
                    f"APP_ROLE={role} con cloudtasks requiere {field_name}"
                )
        if settings.TICKET_WORKER_URL and not _is_canonical_https_origin(
            settings.TICKET_WORKER_URL
        ):
            errors.append(
                "TICKET_WORKER_URL debe ser un origen HTTPS canónico"
            )

    # Sender y receiver comparten audience/identidad exactas, pero el worker
    # no necesita proyecto, ubicación, nombre ni URL target de la cola.
    if sends_cloud_tasks or verifies_task_oidc:
        if not settings.TICKET_WORKER_AUDIENCE:
            errors.append(
                f"APP_ROLE={role} requiere TICKET_WORKER_AUDIENCE estable"
            )
        if not settings.TICKET_WORKER_SERVICE_ACCOUNT:
            errors.append(
                f"APP_ROLE={role} requiere TICKET_WORKER_SERVICE_ACCOUNT "
                "(SA firmante del OIDC)"
            )
        if not settings.TICKET_WORKER_REQUIRE_OIDC:
            errors.append(
                "TICKET_WORKER_REQUIRE_OIDC=false está prohibido en un "
                "entorno desplegado"
            )
        if settings.GCP_PROJECT and settings.ENVIRONMENT in {
            "staging", "production"
        }:
            service_name = (
                "kb-rag-ticket-worker-staging"
                if settings.ENVIRONMENT == "staging"
                else "kb-rag-ticket-worker"
            )
            expected_audience = (
                f"https://{service_name}.{settings.GCP_PROJECT}.ticket.internal"
            )
            if settings.TICKET_WORKER_AUDIENCE != expected_audience:
                errors.append(
                    "TICKET_WORKER_AUDIENCE debe ser la custom audience exacta "
                    "del worker/environment/project"
                )
            signer_suffix = (
                "stg" if settings.ENVIRONMENT == "staging" else "prod"
            )
            expected_signer = (
                f"ticket-task-signer-{signer_suffix}@{settings.GCP_PROJECT}."
                "iam.gserviceaccount.com"
            )
            if settings.TICKET_WORKER_SERVICE_ACCOUNT != expected_signer:
                errors.append(
                    "TICKET_WORKER_SERVICE_ACCOUNT debe ser la task signer "
                    "exacta del environment/project"
                )

    if errors:
        raise ValueError(f"Configuración inválida: {', '.join(errors)}")

    return True
