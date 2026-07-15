"""Runtime safety regressions for Task 7 admission, polling, and task ACKs."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.models import HandleTicketRequest
from data_pipeline.ticket_job_models import (
    NextAction,
    TicketJobState,
    fingerprint_request,
    new_job_record,
    utcnow,
)
from data_pipeline.ticket_job_repository import (
    InMemoryTicketJobBackend,
    PAYLOADS_COLLECTION,
    TicketJobRepository,
)


def _request(*, principal: str = "client-a", tenant: str = "tenant-a") -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.principal_id = principal
    request.state.tenant_id = tenant
    return request


async def _seed_job(
    *,
    deadline_delta_s: float = 600,
) -> tuple[TicketJobRepository, InMemoryTicketJobBackend, object]:
    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    payload = {"participant_id": "synthetic", "plan_id": "synthetic-plan"}
    fingerprint = fingerprint_request(payload)
    candidate = new_job_record(
        principal_id="client-a",
        tenant_id="tenant-a",
        request_fingerprint=fingerprint,
        request_payload=payload,
        job_deadline_at=utcnow() + timedelta(seconds=deadline_delta_s),
    )
    record, _ = await repo.create_or_get(
        principal_id="client-a",
        idempotency_key="stable-logical-event",
        request_fingerprint=fingerprint,
        candidate=candidate,
    )
    assert record is not None
    return repo, backend, record


@pytest.mark.parametrize(
    ("endpoint_name", "expected_state_field"),
    (("get_ticket_status", "state"), ("get_ticket_job_v2", "state")),
)
async def test_poll_lazy_terminalizes_expired_deadline_and_releases_slot_once(
    endpoint_name: str,
    expected_state_field: str,
) -> None:
    from api import main as main_module

    repo, _backend, record = await _seed_job(deadline_delta_s=-1)
    endpoint = getattr(main_module, endpoint_name)
    request = _request()

    first = await endpoint(record.job_id, request, repo)
    second = await endpoint(record.job_id, request, repo)

    first_state = getattr(first, expected_state_field)
    second_state = getattr(second, expected_state_field)
    assert getattr(first_state, "value", first_state) == TicketJobState.TIMEOUT.value
    assert getattr(second_state, "value", second_state) == TicketJobState.TIMEOUT.value
    persisted = await repo.get(record.job_id)
    assert persisted is not None
    assert persisted.state == TicketJobState.TIMEOUT
    assert persisted.next_action == NextAction.USE_LEGACY_OR_HUMAN
    assert persisted.public_error_code == "TOTAL_JOB_TIMEOUT"
    assert await repo.count_active("client-a") == 0


@pytest.mark.parametrize("endpoint_name", ("get_ticket_status", "get_ticket_job_v2"))
async def test_poll_missing_payload_terminalizes_before_returning_410(
    endpoint_name: str,
) -> None:
    from api import main as main_module

    repo, backend, record = await _seed_job()

    async def _delete_payload(view) -> None:
        view.delete(PAYLOADS_COLLECTION, record.job_id)

    await backend.transact(_delete_payload)
    endpoint = getattr(main_module, endpoint_name)

    with pytest.raises(HTTPException) as exc:
        await endpoint(record.job_id, _request(), repo)

    assert exc.value.status_code == 410
    persisted = await repo.get(record.job_id)
    assert persisted is not None
    assert persisted.state == TicketJobState.FAILED
    assert persisted.public_error_code == "EXPIRED_PAYLOAD"
    assert persisted.next_action == NextAction.USE_LEGACY_OR_HUMAN
    assert await repo.count_active("client-a") == 0


@pytest.mark.parametrize("endpoint_name", ("get_ticket_status", "get_ticket_job_v2"))
async def test_unauthorized_poll_cannot_terminalize_another_tenants_job(
    endpoint_name: str,
) -> None:
    from api import main as main_module

    repo, _backend, record = await _seed_job(deadline_delta_s=-1)
    endpoint = getattr(main_module, endpoint_name)

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            record.job_id,
            _request(principal="attacker", tenant="other-tenant"),
            repo,
        )

    assert exc.value.status_code == 403
    persisted = await repo.get(record.job_id)
    assert persisted is not None and persisted.state == TicketJobState.QUEUED
    assert await repo.count_active("client-a") == 1


def _task_client(monkeypatch, *, oidc_required: bool) -> TestClient:
    from api.config import settings
    from api.main import app

    monkeypatch.setattr(settings, "APP_ROLE", "worker")
    monkeypatch.setattr(settings, "TICKET_WORKER_REQUIRE_OIDC", oidc_required)
    monkeypatch.setattr(
        settings,
        "TICKET_WORKER_SERVICE_ACCOUNT",
        "ticket-signer@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(settings, "TICKET_WORKER_URL", "https://worker.example.run.app")
    return TestClient(app)


def test_malformed_cloud_task_is_acked_204_when_task_auth_is_disabled_for_test(
    monkeypatch,
) -> None:
    client = _task_client(monkeypatch, oidc_required=False)

    response = client.post(
        "/internal/tasks/ticket-job",
        json={"enqueue_generation": 0},
    )

    assert response.status_code == 204
    assert response.content == b""


def test_malformed_cloud_task_without_oidc_is_not_acked(monkeypatch) -> None:
    client = _task_client(monkeypatch, oidc_required=True)

    response = client.post(
        "/internal/tasks/ticket-job",
        json={"enqueue_generation": 0},
    )

    assert response.status_code == 401


def test_malformed_cloud_task_with_valid_oidc_is_acked_204(monkeypatch) -> None:
    client = _task_client(monkeypatch, oidc_required=True)
    claims = {
        "email": "ticket-signer@example.iam.gserviceaccount.com",
        "email_verified": True,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
        response = client.post(
            "/internal/tasks/ticket-job",
            headers={"Authorization": "Bearer signed-token"},
            json={"enqueue_generation": 0},
        )

    assert response.status_code == 204
    assert response.content == b""


async def test_ticket_execution_audit_has_ttl_and_no_raw_external_ids() -> None:
    from data_pipeline.execution_logger import ExecutionLogger

    captured: dict = {}

    class _Collection:
        async def add(self, document):
            captured.update(document)

    class _Database:
        def collection(self, name: str):
            assert name == "ticket_executions"
            return _Collection()

    audit = ExecutionLogger.__new__(ExecutionLogger)
    audit.db = _Database()
    audit.retention_days = 30
    sentinel = "sensitive-participant-external-id"

    await audit.log_ticket_execution(
        request_id=f"request-{sentinel}",
        ticket_job_id=f"ticket-{sentinel}",
        mode="knowledge_only",
        route_summary=[{"route": "KNOWLEDGE", "execution_status": "succeeded"}],
        total_inquiries=1,
        forusbots_job_ids=[f"forusbots-{sentinel}"],
        duration_ms=10,
        error=f"upstream said {sentinel}",
        idempotency_key=f"idem-{sentinel}",
    )

    assert sentinel not in repr(captured)
    assert captured["forusbots_job_count"] == 1
    assert captured["failed"] is True
    assert captured["expires_at"] > utcnow() + timedelta(days=29)

    terraform = (
        Path(__file__).resolve().parents[2]
        / "infra/terraform/modules/ticket_environment/firestore.tf"
    ).read_text(encoding="utf-8")
    assert 'collection = "ticket_executions"' in terraform
    assert 'field      = "expires_at"' in terraform


class _AllowValidator:
    async def authorize(self, *, tenant_id: str, participant_id: str, plan_id: str):
        from api.participant_plan import AuthorizedParticipantPlan

        return AuthorizedParticipantPlan(
            tenant_id=tenant_id,
            participant_id=participant_id,
            plan_id=plan_id,
            record_keeper=None,
        )


class _QueueWithDelay:
    def __init__(self, *, delay: float | None = None, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.enqueued = []

    async def estimated_queue_delay_s(self) -> float:
        if self.error is not None:
            raise self.error
        assert self.delay is not None
        return self.delay

    async def ensure_enqueued(self, job_id: str, generation: int = 0) -> str:
        self.enqueued.append((job_id, generation))
        return f"task-{job_id}-g{generation}"


def _ticket_body() -> HandleTicketRequest:
    return HandleTicketRequest.model_validate({
        "participant_id": "synthetic-participant",
        "plan_id": "synthetic-plan",
        "company_name": "Synthetic Company",
        "company_status": "Ongoing",
        "ticket": {
            "username": "Synthetic User",
            "user_email": "synthetic@example.test",
            "email_subject": "Plan question",
            "email_body": "How do rollovers work?",
        },
        "record_keeper": "LT Trust",
    })


def _producer_request(queue) -> Request:
    from api.rate_limit import FixedWindowRateLimiter

    app = SimpleNamespace(state=SimpleNamespace())
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "app": app,
    })
    request.state.principal_id = "client-a"
    request.state.tenant_id = "tenant-a"
    request.app.state.participant_plan_validator = _AllowValidator()
    request.app.state.ticket_queue = queue
    request.app.state.ticket_rate_limiter = FixedWindowRateLimiter()
    return request


async def test_producer_rejects_new_job_when_estimated_queue_delay_exceeds_ceiling(
    monkeypatch,
) -> None:
    from api import main as main_module
    from api.config import settings

    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "full")
    monkeypatch.setattr(settings, "TICKET_ADMISSION_QUEUE_DELAY_CEILING_S", 300)
    repo = TicketJobRepository(InMemoryTicketJobBackend())
    queue = _QueueWithDelay(delay=301)

    with pytest.raises(HTTPException) as exc:
        await main_module._accept_ticket_job(
            _ticket_body(),
            _producer_request(queue),
            repo,
            api_version="v1",
        )

    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "QUEUE_DELAY_EXCEEDED"
    assert await repo.count_active("client-a") == 0
    assert queue.enqueued == []


async def test_producer_fails_closed_when_queue_delay_cannot_be_estimated(
    monkeypatch,
) -> None:
    from api import main as main_module
    from api.config import settings
    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "full")
    repo = TicketJobRepository(InMemoryTicketJobBackend())
    queue = _QueueWithDelay(error=RuntimeError("stats unavailable"))

    with pytest.raises(HTTPException) as exc:
        await main_module._accept_ticket_job(
            _ticket_body(),
            _producer_request(queue),
            repo,
            api_version="v1",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "QUEUE_DELAY_ESTIMATE_UNAVAILABLE"
    assert await repo.count_active("client-a") == 0


async def test_producer_fails_closed_on_non_finite_queue_delay(monkeypatch) -> None:
    from api import main as main_module
    from api.config import settings

    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "full")
    repo = TicketJobRepository(InMemoryTicketJobBackend())
    queue = _QueueWithDelay(delay=float("nan"))

    with pytest.raises(HTTPException) as exc:
        await main_module._accept_ticket_job(
            _ticket_body(),
            _producer_request(queue),
            repo,
            api_version="v1",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "QUEUE_DELAY_ESTIMATE_UNAVAILABLE"
    assert await repo.count_active("client-a") == 0
