"""Role-specific application startup contracts.

These tests exercise the real FastAPI lifespan instead of inferring startup
behaviour from readiness state or configuration validation.
"""

from contextlib import ExitStack, asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI


class _StartupSpies:
    def __init__(self) -> None:
        self.pinecone = Mock()
        self.pinecone.get_index_stats.return_value = {"total_vectors": 0}
        self.pinecone.query_chunks.return_value = []
        self.llm_router = Mock()
        self.rag_engine = Mock()
        self.inquiry_router = Mock()
        self.forusbots = Mock(aclose=AsyncMock())
        self.repo = Mock()
        self.queue = Mock(aclose=AsyncMock())
        self.validator = Mock()
        self.execution_logger = Mock()

        self.pinecone_ctor = Mock(return_value=self.pinecone)
        self.llm_router_ctor = Mock(return_value=self.llm_router)
        self.rag_engine_ctor = Mock(return_value=self.rag_engine)
        self.inquiry_router_ctor = Mock(return_value=self.inquiry_router)
        self.forusbots_ctor = Mock(return_value=self.forusbots)
        self.backend_builder = Mock(return_value=Mock())
        self.repo_ctor = Mock(return_value=self.repo)
        self.queue_builder = Mock(return_value=self.queue)
        self.validator_builder = Mock(return_value=self.validator)
        self.execution_logger_ctor = Mock(return_value=self.execution_logger)

    def patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("api.main.validate_settings"))
        stack.enter_context(patch("api.main.PineconeUploader", self.pinecone_ctor))
        stack.enter_context(patch("api.main.LLMRouter", self.llm_router_ctor))
        stack.enter_context(patch("api.main.build_routes_from_settings", return_value={}))
        stack.enter_context(patch("api.main.RAGEngine", self.rag_engine_ctor))
        stack.enter_context(
            patch("api.main.InquiryRouterEngine", self.inquiry_router_ctor)
        )
        stack.enter_context(
            patch("api.main.ForusBotsClient.from_settings", self.forusbots_ctor)
        )
        stack.enter_context(
            patch("api.main._build_ticket_job_backend", self.backend_builder)
        )
        stack.enter_context(patch("api.main.TicketJobRepository", self.repo_ctor))
        stack.enter_context(patch("api.main._build_ticket_queue", self.queue_builder))
        stack.enter_context(
            patch("api.main.build_validator_from_settings", self.validator_builder)
        )
        stack.enter_context(
            patch("api.main.ExecutionLogger", self.execution_logger_ctor)
        )
        return stack


@asynccontextmanager
async def _run_lifespan(
    role: str,
    mode: str,
    monkeypatch,
    *,
    execution_logging: bool = False,
    firestore_database: str = "",
):
    from api.config import settings
    from api.main import lifespan

    monkeypatch.setattr(settings, "APP_ROLE", role)
    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", mode)
    monkeypatch.setattr(settings, "ENABLE_EXECUTION_LOGGING", execution_logging)
    monkeypatch.setattr(settings, "FIRESTORE_DATABASE", firestore_database)
    spies = _StartupSpies()
    application = FastAPI()
    with spies.patches():
        async with lifespan(application):
            yield application, spies


async def test_disabled_producer_initializes_core_and_polling_repository_only(
    monkeypatch,
):
    async with _run_lifespan(
        "producer", "disabled", monkeypatch
    ) as (application, spies):
        assert application.state.pinecone_uploader is spies.pinecone
        assert application.state.llm_router is spies.llm_router
        assert application.state.rag_engine is spies.rag_engine
        assert application.state.inquiry_router is spies.inquiry_router
        assert application.state.ticket_repo is spies.repo

        spies.forusbots_ctor.assert_not_called()
        spies.backend_builder.assert_called_once()
        spies.repo_ctor.assert_called_once()
        spies.queue_builder.assert_not_called()
        spies.validator_builder.assert_not_called()
        spies.llm_router.configure_pricing.assert_not_called()


async def test_active_producer_initializes_only_admission_dependencies(
    monkeypatch,
):
    async with _run_lifespan(
        "producer", "full", monkeypatch
    ) as (application, spies):
        assert application.state.rag_engine is spies.rag_engine
        assert application.state.ticket_repo is spies.repo
        assert application.state.ticket_queue is spies.queue
        assert application.state.participant_plan_validator is spies.validator
        assert not hasattr(application.state, "forusbots_client")
        assert not hasattr(application.state, "ticket_orchestrator_factory")
        assert not hasattr(application.state, "ticket_rate_limiter")

        spies.forusbots_ctor.assert_not_called()
        spies.backend_builder.assert_called_once()
        spies.queue_builder.assert_called_once_with(application)
        spies.validator_builder.assert_called_once()


async def test_worker_initializes_execution_dependencies_without_producer_dependencies(
    monkeypatch,
):
    # The global rollout flag only controls producer admission. A worker must
    # still finish durable jobs admitted by an earlier producer revision.
    async with _run_lifespan(
        "worker", "disabled", monkeypatch
    ) as (application, spies):
        assert application.state.rag_engine is spies.rag_engine
        assert application.state.inquiry_router is spies.inquiry_router
        assert application.state.llm_router is spies.llm_router
        assert application.state.forusbots_client is spies.forusbots
        assert application.state.ticket_repo is spies.repo
        assert callable(application.state.ticket_orchestrator_factory)

        spies.queue_builder.assert_not_called()
        spies.validator_builder.assert_not_called()
        spies.llm_router.configure_pricing.assert_called_once_with({})


async def test_reconciler_initializes_only_repository_and_queue(monkeypatch):
    async with _run_lifespan(
        "reconciler", "disabled", monkeypatch
    ) as (application, spies):
        assert application.state.ticket_repo is spies.repo
        assert application.state.ticket_queue is spies.queue

        spies.pinecone_ctor.assert_not_called()
        spies.llm_router_ctor.assert_not_called()
        spies.rag_engine_ctor.assert_not_called()
        spies.inquiry_router_ctor.assert_not_called()
        spies.forusbots_ctor.assert_not_called()
        spies.validator_builder.assert_not_called()


async def test_worker_execution_logger_uses_explicit_staging_database(monkeypatch):
    from api.config import settings

    monkeypatch.setattr(settings, "GCP_PROJECT", "rag-kb-system")
    async with _run_lifespan(
        "worker",
        "disabled",
        monkeypatch,
        execution_logging=True,
        firestore_database="ticket-staging",
    ) as (application, spies):
        assert application.state.execution_logger is spies.execution_logger
        spies.execution_logger_ctor.assert_called_once_with(
            project_id="rag-kb-system",
            database="ticket-staging",
            retention_days=settings.TICKET_IDEMPOTENCY_RETENTION_DAYS,
        )


async def test_producer_execution_logger_defaults_explicitly_to_default_database(
    monkeypatch,
):
    from api.config import settings

    monkeypatch.setattr(settings, "GCP_PROJECT", "rag-kb-system")
    async with _run_lifespan(
        "producer",
        "disabled",
        monkeypatch,
        execution_logging=True,
        firestore_database="",
    ) as (_application, spies):
        spies.execution_logger_ctor.assert_called_once_with(
            project_id="rag-kb-system",
            database="(default)",
            retention_days=settings.TICKET_IDEMPOTENCY_RETENTION_DAYS,
        )


def test_execution_logger_passes_explicit_database_to_firestore_client():
    from data_pipeline.execution_logger import ExecutionLogger

    with patch("data_pipeline.execution_logger.firestore.AsyncClient") as client_ctor:
        ExecutionLogger(
            project_id="rag-kb-system",
            database="ticket-staging",
            retention_days=90,
        )

    client_ctor.assert_called_once_with(
        project="rag-kb-system",
        database="ticket-staging",
    )


def _pin_deployed_role_settings(monkeypatch, **overrides) -> None:
    from api.config import settings

    values = {
        "ENVIRONMENT": "production",
        "APP_ENV": "production",
        "APP_ROLE": "worker",
        "TICKET_HANDLER_MODE": "disabled",
        "API_KEY": "",
        "API_CLIENT_KEYS": {},
        "API_CLIENT_TENANTS": {},
        "PINECONE_API_KEY": "pinecone-test-key",
        "OPENAI_API_KEY": "openai-test-key",
        "GEMINI_API_KEY": "",
        "USE_VERTEX_AI": False,
        "LLM_ROUTE_CLASSIFY": "gpt-5.5",
        "TICKET_LLM_PRICING_JSON": (
            '{"pricing_as_of":"2026-07-21",'
            '"source":"openai-google-official-public-pricing","models":{'
            '"openai:gpt-5.5":{"input_usd_per_million":5.0,'
            '"output_usd_per_million":30.0},'
            '"gemini:gemini-2.5-pro":{"input_usd_per_million":1.25,'
            '"output_usd_per_million":10.0}}}'
        ),
        "FORUSBOTS_BASE_URL": "https://forusbots.example.com",
        "FORUSBOTS_AUTH_TOKEN": "forusbots-test-token",
        "TICKET_JOB_BACKEND": "firestore",
        "FIRESTORE_DATABASE": "(default)",
        "TICKET_TASK_QUEUE": "inline",
        "GCP_PROJECT": "rag-kb-system",
        "CLOUD_TASKS_LOCATION": "us-central1",
        "CLOUD_TASKS_QUEUE": "ticket-jobs",
        "TICKET_WORKER_URL": "",
        "TICKET_WORKER_AUDIENCE": (
            "https://kb-rag-ticket-worker."
            "rag-kb-system.ticket.internal"
        ),
        "TICKET_WORKER_SERVICE_ACCOUNT": (
            "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com"
        ),
        "TICKET_WORKER_REQUIRE_OIDC": True,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


def test_deployed_worker_requires_reviewed_exact_llm_pricing(monkeypatch):
    from api.config import validate_settings

    _pin_deployed_role_settings(monkeypatch, TICKET_LLM_PRICING_JSON="")

    with pytest.raises(ValueError, match="TICKET_LLM_PRICING_JSON"):
        validate_settings()


def test_deployed_worker_rejects_pricing_for_unknown_or_missing_model(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        TICKET_LLM_PRICING_JSON=(
            '{"pricing_as_of":"2026-07-21","source":"official","models":{'
            '"openai:gpt-unreviewed":{"input_usd_per_million":1,'
            '"output_usd_per_million":1}}}'
        ),
    )

    with pytest.raises(ValueError, match="exactamente"):
        validate_settings()


def test_deployed_reconciler_requires_strict_pricing_document(monkeypatch):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        APP_ROLE="reconciler",
        TICKET_TASK_QUEUE="cloudtasks",
        TICKET_WORKER_URL="https://worker.example.run.app",
        TICKET_LLM_PRICING_JSON="{}",
    )

    with pytest.raises(ValueError, match="TICKET_LLM_PRICING_JSON"):
        validate_settings()


def test_deployed_producer_core_ignores_ticket_pricing_document(monkeypatch):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        APP_ROLE="producer",
        API_KEY="legacy-core-key",
        TICKET_LLM_PRICING_JSON="not-json",
    )

    assert validate_settings() is True


def test_settings_exposes_workload_identity_email_allowlist():
    from api.config import settings

    assert hasattr(settings, "TICKET_WIF_ALLOWED_EMAILS")
    assert isinstance(settings.TICKET_WIF_ALLOWED_EMAILS, list)


@pytest.mark.parametrize("invalid_environment", ["prod", "stage", "", "Production"])
def test_settings_rejects_unknown_environment_fail_closed(
    monkeypatch,
    invalid_environment,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        ENVIRONMENT=invalid_environment,
        APP_ENV=invalid_environment,
    )

    with pytest.raises(ValueError, match="ENVIRONMENT"):
        validate_settings()


def test_settings_rejects_environment_and_app_env_mismatch(monkeypatch):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        ENVIRONMENT="production",
        APP_ENV="staging",
    )

    with pytest.raises(ValueError, match="APP_ENV"):
        validate_settings()


def _pin_active_producer_settings(monkeypatch, **overrides) -> None:
    values = {
        "APP_ROLE": "producer",
        "TICKET_HANDLER_MODE": "full",
        "API_CLIENT_KEYS": {"n8n": "mapped-test-key"},
        "API_CLIENT_TENANTS": {"n8n": "tenant-a"},
        "PARTICIPANT_PLAN_SOURCE": "https://participant-plan.example.com",
        "TICKET_WIF_AUDIENCE": "https://producer.example.run.app",
        "TICKET_WIF_ALLOWED_EMAILS": [
            "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com"
        ],
        "TICKET_WIF_EXPECTED_EMAIL": (
            "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com"
        ),
        "TICKET_TASK_QUEUE": "cloudtasks",
        "TICKET_WORKER_URL": "https://worker.example.run.app",
    }
    values.update(overrides)
    _pin_deployed_role_settings(monkeypatch, **values)


def test_active_producer_accepts_existing_n8n_auth_without_wif_or_directory(
    monkeypatch,
):
    """The deployed producer preserves the existing n8n IAM + API key contract."""
    from api.config import validate_settings

    credential = "existing-n8n-api-key"
    _pin_active_producer_settings(
        monkeypatch,
        API_KEY=credential,
        API_CLIENT_KEYS={},
        API_CLIENT_TENANTS={},
        PARTICIPANT_PLAN_SOURCE="",
        TICKET_WIF_AUDIENCE="",
        TICKET_WIF_ALLOWED_EMAILS=[],
        TICKET_WIF_EXPECTED_EMAIL="",
    )

    assert validate_settings() is True


def test_worker_accepts_documented_forusbots_legacy_origin(monkeypatch):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        FORUSBOTS_BASE_URL="http://35.224.156.104:10000",
    )

    assert validate_settings() is True


@pytest.mark.parametrize(
    "allowed_emails",
    [
        [],
        [""],
        ["n8n@example.com", "n8n@example.com"],
        [" n8n@example.com"],
    ],
)
def test_deployed_active_producer_rejects_invalid_wif_email_allowlist(
    monkeypatch,
    allowed_emails,
):
    from api.config import validate_settings

    _pin_active_producer_settings(
        monkeypatch,
        TICKET_WIF_ALLOWED_EMAILS=allowed_emails,
    )

    with pytest.raises(ValueError, match="TICKET_WIF_ALLOWED_EMAILS"):
        validate_settings()


def test_staging_active_producer_accepts_exact_n8n_and_e2e_allowlist(monkeypatch):
    from api.config import validate_settings

    _pin_active_producer_settings(
        monkeypatch,
        ENVIRONMENT="staging",
        APP_ENV="staging",
        FIRESTORE_DATABASE="ticket-staging",
        TICKET_WORKER_AUDIENCE=(
            "https://kb-rag-ticket-worker-staging."
            "rag-kb-system.ticket.internal"
        ),
        TICKET_WORKER_SERVICE_ACCOUNT=(
            "ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com"
        ),
        TICKET_WIF_ALLOWED_EMAILS=[
            "n8n-ticket-invoker-stg@rag-kb-system.iam.gserviceaccount.com",
            "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com",
        ],
    )

    assert validate_settings() is True


def test_deployed_active_producer_rejects_duplicate_api_key_across_principals(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_active_producer_settings(
        monkeypatch,
        API_CLIENT_KEYS={"n8n": "same-key", "ops": ["same-key"]},
        API_CLIENT_TENANTS={"n8n": "tenant-a", "ops": "tenant-b"},
    )

    with pytest.raises(ValueError, match="API_CLIENT_KEYS"):
        validate_settings()


def test_production_worker_does_not_require_producer_queue_configuration(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(monkeypatch, TICKET_HANDLER_MODE="full")

    assert validate_settings() is True


def test_active_producer_does_not_require_forusbots_credentials(monkeypatch):
    """Admission enqueues durable work; only the worker owns ForUsBots."""
    from api.config import validate_settings

    _pin_active_producer_settings(
        monkeypatch,
        FORUSBOTS_AUTH_TOKEN="",
        FORUSBOTS_BASE_URL="http://unused.invalid/producer-must-not-read-this",
    )

    assert validate_settings() is True


def test_production_worker_requires_durable_store_even_when_admission_disabled(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        TICKET_JOB_BACKEND="memory",
        FIRESTORE_DATABASE="",
    )

    with pytest.raises(ValueError, match="TICKET_JOB_BACKEND=firestore"):
        validate_settings()


def test_production_disabled_producer_requires_durable_polling_store(
    monkeypatch,
):
    """A rollback anchor must still serve polls for already-admitted jobs."""
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        APP_ROLE="producer",
        API_KEY="legacy-core-test-key",
        TICKET_JOB_BACKEND="memory",
        FIRESTORE_DATABASE="",
    )

    with pytest.raises(ValueError, match="TICKET_JOB_BACKEND=firestore"):
        validate_settings()


def test_production_reconciler_requires_durable_store_and_queue_when_disabled(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        APP_ROLE="reconciler",
        PINECONE_API_KEY="",
        OPENAI_API_KEY="",
        FORUSBOTS_AUTH_TOKEN="",
        TICKET_JOB_BACKEND="memory",
        FIRESTORE_DATABASE="",
        TICKET_TASK_QUEUE="inline",
    )

    with pytest.raises(ValueError, match="TICKET_JOB_BACKEND=firestore"):
        validate_settings()


def test_production_reconciler_accepts_only_control_plane_dependencies(
    monkeypatch,
):
    from api.config import validate_settings

    _pin_deployed_role_settings(
        monkeypatch,
        APP_ROLE="reconciler",
        PINECONE_API_KEY="",
        OPENAI_API_KEY="",
        FORUSBOTS_AUTH_TOKEN="",
        TICKET_TASK_QUEUE="cloudtasks",
        TICKET_WORKER_URL="https://worker.example.run.app",
    )

    assert validate_settings() is True
