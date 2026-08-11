"""Vertical contract for the private RAG evaluation ingestion plane."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.ticket_evaluation_models import TicketEvaluationEvent
from api.ticket_review_models import (
    MAX_REASON_LENGTH,
    DevRevAuthorizationStatus,
    DevRevTicketDetail,
)
from data_pipeline.devrev_client import (
    DevRevAuthenticationError,
    DevRevConfigurationError,
    DevRevNotFoundError,
    DevRevScopeError,
    DevRevTransientError,
)


T0 = datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc)
CURSOR_KEY = base64.b64decode(
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)
DON = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234"
AUDIENCE = "https://ticket-evaluation-ingest.example.run.app"
PUBLISHER = "ticket-evaluation-publisher@example.iam.gserviceaccount.com"


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _event(*, job_id: str = "job123", inquiry_index: int = 0, **overrides):
    values = {
        "execution_id": f"{job_id}:{inquiry_index}",
        "job_id": job_id,
        "inquiry_index": inquiry_index,
        "ticket_id": "TKT-1234",
        "tenant_id": "tenant-a",
        "route": "knowledge_question",
        "status": "succeeded",
        "occurred_at": T0,
        "inquiry": "What are the rollover rules?",
        "topic": "rollover",
        "classification": {
            "route": "knowledge_question",
            "confidence": 0.91,
            "reasoning": "The participant asked a knowledge question.",
        },
        "answer": "The generated RAG answer.",
        "structured_response": {"answer": "The generated RAG answer."},
        "diagnostics": {"retrieval": {"matches": 1}},
        "sources": [{"article_id": "article-1", "title": "Rollover Guide"}],
        "chunks": [{
            "chunk_id": "chunk-1",
            "article_id": "article-1",
            "content_hash": "a" * 64,
            "preview": "Bounded source evidence.",
            "score": 0.91,
        }],
        "retrieval_metadata": {"namespace": "tenant-a"},
        "correlation": {"trace_id": "trace-1"},
    }
    values.update(overrides)
    return TicketEvaluationEvent.model_validate(values)


def _ticket() -> DevRevTicketDetail:
    return DevRevTicketDetail(
        devrev_work_id=DON,
        devrev_display_id="TKT-1234",
        title="Synthetic ticket",
        stage="triage",
        state="open",
        applies_to_part="don:core:part/allowed",
        ticket_visibility=1,
        object_version=7,
        body="Synthetic participant question.",
        created_at=T0 - timedelta(days=1),
        modified_at=T0,
    )


class _DevRev:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.get_calls: list[str] = []
        self.list_calls: list[object] = []

    async def get_ticket(self, ticket_id: str) -> DevRevTicketDetail:
        self.get_calls.append(ticket_id)
        if self.error:
            raise self.error
        return _ticket()

    async def list_tickets(self, *args, **kwargs):  # pragma: no cover - must never run
        self.list_calls.append((args, kwargs))
        raise AssertionError("evaluation ingestion/listing must never call works.list")


class _BlockingDevRev(_DevRev):
    """Hold one real hydration call open so the competing claim is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_ticket(self, ticket_id: str) -> DevRevTicketDetail:
        self.get_calls.append(ticket_id)
        self.started.set()
        await self.release.wait()
        return _ticket()


@pytest.fixture
def stack():
    from data_pipeline.ticket_review_repository import (
        InMemoryTicketReviewBackend,
        TicketReviewRepository,
    )
    from data_pipeline.ticket_review_service import TicketEvaluationService

    clock = _Clock()
    backend = InMemoryTicketReviewBackend()
    repo = TicketReviewRepository(backend, cursor_key=CURSOR_KEY, clock=clock)
    devrev = _DevRev()
    service = TicketEvaluationService(
        devrev=devrev,
        repository=repo,
        clock=clock,
    )
    return backend, repo, devrev, service, clock


async def test_repository_is_idempotent_and_rejects_conflicting_replay(stack):
    from data_pipeline.ticket_review_repository import EvaluationReplayConflict

    _backend, repo, _devrev, _service, _clock = stack
    event = _event()

    first, created = await repo.persist_ticket_evaluation(event)
    replay, replay_created = await repo.persist_ticket_evaluation(event)

    assert created is True
    assert replay_created is False
    assert first.execution_id == replay.execution_id == event.execution_id
    with pytest.raises(EvaluationReplayConflict):
        await repo.persist_ticket_evaluation(
            _event(answer="A conflicting answer", structured_response={"answer": "different"})
        )


async def test_persist_first_is_explicitly_quarantined_until_scope_is_validated(stack):
    from data_pipeline.ticket_review_repository import EvaluationRunNotFound

    _backend, repo, devrev, service, _clock = stack

    run, created = await repo.persist_ticket_evaluation(_event())
    page = await repo.list_ticket_evaluations()

    assert created is True
    assert run.authorization_status is DevRevAuthorizationStatus.QUARANTINED
    assert page.items == []
    with pytest.raises(EvaluationRunNotFound):
        await service.get_detail(run.execution_id)
    assert devrev.get_calls == []


@pytest.mark.parametrize(
    "error",
    [DevRevNotFoundError("missing"), DevRevScopeError("outside configured scope")],
)
async def test_only_scope_or_not_found_permanently_denies_a_durable_run(stack, error):
    from data_pipeline.ticket_review_repository import EvaluationRunNotFound

    _backend, repo, devrev, service, _clock = stack
    devrev.error = error

    result = await service.ingest(_event())

    assert result.run.authorization_status is DevRevAuthorizationStatus.DENIED
    assert result.run.hydration_retryable is False
    assert (await repo.list_ticket_evaluations()).items == []
    with pytest.raises(EvaluationRunNotFound):
        await service.get_detail(result.run.execution_id)
    assert devrev.get_calls == ["TKT-1234"]


async def test_two_runs_for_one_ticket_remain_two_rows_with_opaque_cursor(stack):
    _backend, repo, _devrev, service, _clock = stack
    await service.ingest(_event(job_id="job111"))
    await service.ingest(_event(job_id="job222"))

    first = await repo.list_ticket_evaluations(limit=1)
    second = await repo.list_ticket_evaluations(limit=1, cursor=first.next_cursor)

    assert len(first.items) == len(second.items) == 1
    assert first.next_cursor and "job" not in first.next_cursor
    assert {first.items[0].execution_id, second.items[0].execution_id} == {
        "job111:0",
        "job222:0",
    }


async def test_queue_pages_newest_occurrence_first_with_stable_ties(stack):
    _backend, repo, _devrev, service, _clock = stack
    cases = [
        ("aaa-old", T0 - timedelta(minutes=2)),
        ("bbb-new-low", T0),
        ("ccc-new-high", T0),
        ("zzz-mid", T0 - timedelta(minutes=1)),
    ]
    for job_id, occurred_at in cases:
        await service.ingest(_event(job_id=job_id, occurred_at=occurred_at))

    execution_ids: list[str] = []
    cursor = None
    for _ in cases:
        page = await repo.list_ticket_evaluations(limit=1, cursor=cursor)
        assert len(page.items) == 1
        execution_ids.append(page.items[0].execution_id)
        cursor = page.next_cursor

    assert execution_ids == [
        "ccc-new-high:0",
        "bbb-new-low:0",
        "zzz-mid:0",
        "aaa-old:0",
    ]
    assert cursor is None


async def test_two_attempts_of_same_job_and_inquiry_remain_distinct_rows(stack):
    _backend, repo, _devrev, service, _clock = stack
    first_id = "job123-e1-a1:0"
    second_id = "job123-e3-a2:0"

    await service.ingest(_event(
        execution_id=first_id,
        invocation_id=first_id,
        attempt=1,
        lease_epoch=1,
        status="failed",
        answer=None,
        structured_response={"knowledge_answer": {}},
        error={"code": "RAG_INVOCATION_ABANDONED", "retryable": False},
    ))
    await service.ingest(_event(
        execution_id=second_id,
        invocation_id=second_id,
        attempt=2,
        lease_epoch=3,
    ))

    page = await service.list_runs(limit=10)
    assert {item.execution_id for item in page.items} == {
        first_id, second_id,
    }
    attempts = {item.execution_id: item for item in page.items}
    assert attempts[first_id].invocation_id == first_id
    assert attempts[first_id].attempt == 1
    assert attempts[first_id].lease_epoch == 1
    assert attempts[second_id].invocation_id == second_id
    assert attempts[second_id].attempt == 2
    assert attempts[second_id].lease_epoch == 3
    first = await service.get_detail(first_id)
    second = await service.get_detail(second_id)
    assert first.execution.event.attempt == 1
    assert first.generated_answer is None
    assert second.execution.event.attempt == 2
    assert second.generated_answer == "The generated RAG answer."


async def test_ingest_persists_before_devrev_and_links_a_system_created_review(stack):
    _backend, repo, devrev, service, _clock = stack

    result = await service.ingest(_event())
    stored = await repo.get_ticket_evaluation("job123:0")
    review = await repo.get_review(stored.review_id)

    assert result.created is True
    assert result.run.authorization_status is DevRevAuthorizationStatus.AUTHORIZED
    assert result.run.hydration_status.value == "succeeded"
    assert stored.devrev_work_id == DON
    assert review.devrev_work_id == DON
    assert review.ticket_job_ids == ["job123"]
    assert devrev.get_calls == ["TKT-1234"]
    assert devrev.list_calls == []


async def test_second_run_for_same_ticket_merges_parent_correlation(stack):
    _backend, repo, _devrev, service, _clock = stack
    first = await service.ingest(_event(job_id="job111"))
    second = await service.ingest(_event(job_id="job222"))

    assert first.run.review_id == second.run.review_id
    review = await repo.get_review(first.run.review_id)
    assert review.ticket_job_ids == ["job111", "job222"]


async def test_same_ticket_evidence_does_not_audit_only_a_new_sync_timestamp(stack):
    backend, repo, _devrev, service, clock = stack
    first = await service.ingest(_event(inquiry_index=0))
    initial = await repo.get_review(first.run.review_id)
    initial_events = await backend.dump_subcollection(
        "ticket_reviews", initial.review_id, "audit_events"
    )

    clock.advance(seconds=1)
    second = await service.ingest(_event(inquiry_index=1))
    repeated = await repo.get_review(second.run.review_id)
    repeated_events = await backend.dump_subcollection(
        "ticket_reviews", repeated.review_id, "audit_events"
    )

    assert repeated.version == initial.version
    assert repeated.last_devrev_sync_at == initial.last_devrev_sync_at
    assert set(repeated_events) == set(initial_events)


async def test_devrev_outage_acknowledges_durable_run_and_records_retry(stack):
    _backend, repo, devrev, service, _clock = stack
    devrev.error = DevRevTransientError("synthetic timeout")

    result = await service.ingest(_event())
    stored = await repo.get_ticket_evaluation("job123:0")

    assert result.created is True
    assert stored.authorization_status is DevRevAuthorizationStatus.QUARANTINED
    assert stored.hydration_status.value == "failed"
    assert stored.hydration_retryable is True
    assert stored.next_hydration_attempt_at is not None
    assert stored.hydration_error_code == "devrev_transient"
    assert (await repo.list_ticket_evaluations()).items == []


async def test_ingest_replay_and_private_retry_honor_next_attempt_at(stack):
    _backend, repo, devrev, service, clock = stack
    devrev.error = DevRevTransientError("synthetic timeout")

    first = await service.ingest(_event())
    replay = await service.ingest(_event())
    early_attempted, early_succeeded = await service.retry_due_hydrations()

    assert first.created is True
    assert replay.created is False
    assert devrev.get_calls == ["TKT-1234"]
    assert (early_attempted, early_succeeded) == (0, 0)

    clock.now = first.run.next_hydration_attempt_at
    devrev.error = None
    attempted, succeeded = await service.retry_due_hydrations()
    stored = await repo.get_ticket_evaluation(first.run.execution_id)

    assert (attempted, succeeded) == (1, 1)
    assert devrev.get_calls == ["TKT-1234", "TKT-1234"]
    assert stored.authorization_status is DevRevAuthorizationStatus.AUTHORIZED


@pytest.mark.parametrize(
    "error",
    [
        DevRevAuthenticationError("configured token was rejected"),
        DevRevConfigurationError("configured client is unavailable"),
    ],
)
async def test_auth_and_configuration_failures_stay_quarantined_and_retryable(stack, error):
    _backend, _repo, devrev, service, clock = stack
    devrev.error = error

    result = await service.ingest(_event())

    assert result.run.authorization_status is DevRevAuthorizationStatus.QUARANTINED
    assert result.run.hydration_retryable is True
    assert result.run.next_hydration_attempt_at is not None
    assert result.run.next_hydration_attempt_at - clock.now >= timedelta(minutes=5)


async def test_hydration_claim_lease_prevents_two_simultaneous_devrev_calls(stack):
    from data_pipeline.ticket_review_service import TicketEvaluationService

    _backend, repo, _devrev, _service, clock = stack
    devrev = _BlockingDevRev()
    service = TicketEvaluationService(devrev=devrev, repository=repo, clock=clock)

    first_task = asyncio.create_task(service.ingest(_event()))
    await devrev.started.wait()
    competing = await service.ingest(_event())

    assert competing.created is False
    assert competing.run.authorization_status is DevRevAuthorizationStatus.QUARANTINED
    assert devrev.get_calls == ["TKT-1234"]

    devrev.release.set()
    first = await first_task
    assert first.run.authorization_status is DevRevAuthorizationStatus.AUTHORIZED
    assert devrev.get_calls == ["TKT-1234"]


async def test_expired_hydration_lease_returns_an_interrupted_attempt_to_due_retry(stack):
    _backend, repo, _devrev, _service, clock = stack
    run, _created = await repo.persist_ticket_evaluation(_event())

    abandoned = await repo.claim_ticket_evaluation_hydration(run.execution_id)
    assert abandoned is not None
    assert (await repo.list_due_ticket_evaluation_hydrations()) == []

    clock.now = abandoned.lease_expires_at
    due = await repo.list_due_ticket_evaluation_hydrations()
    reclaimed = await repo.claim_ticket_evaluation_hydration(run.execution_id)

    assert [candidate.execution_id for candidate in due] == [run.execution_id]
    assert reclaimed is not None
    assert reclaimed.lease_token != abandoned.lease_token


async def test_hydration_success_is_monotonic_against_a_stale_failure(stack):
    _backend, repo, _devrev, service, clock = stack
    succeeded = (await service.ingest(_event())).run

    result = await repo.record_ticket_evaluation_hydration_failure(
        succeeded.execution_id,
        error_code="stale_timeout",
        retryable=True,
        next_attempt_at=clock.now + timedelta(minutes=1),
        lease_token="stale-lease-result",
    )

    assert result.hydration_status.value == "succeeded"
    assert result.authorization_status is DevRevAuthorizationStatus.AUTHORIZED
    assert result.hydration_error_code is None


async def test_filtered_scan_returns_a_continuation_before_matches_after_one_thousand(stack):
    _backend, repo, _devrev, service, _clock = stack
    for index in range(1_001):
        await repo.persist_ticket_evaluation(_event(job_id=f"zzz{index:04d}"))
    await service.ingest(_event(job_id="aaa0000"))

    first = await repo.list_ticket_evaluations(limit=1)
    second = await repo.list_ticket_evaluations(limit=1, cursor=first.next_cursor)

    assert first.items == []
    assert first.next_cursor is not None
    assert [run.execution_id for run in second.items] == ["aaa0000:0"]
    assert second.next_cursor is None


async def test_detail_requires_a_persisted_run_before_devrev(stack):
    from data_pipeline.ticket_review_repository import EvaluationRunNotFound

    _backend, _repo, devrev, service, _clock = stack
    with pytest.raises(EvaluationRunNotFound):
        await service.get_detail("missing:0")
    assert devrev.get_calls == []


async def test_detail_combines_execution_review_and_latest_devrev_without_list(stack):
    _backend, _repo, devrev, service, _clock = stack
    await service.ingest(_event())

    detail = await service.get_detail("job123:0")

    assert detail.execution.event.answer == "The generated RAG answer."
    assert detail.review is not None
    assert detail.ticket is not None and detail.ticket.title == "Synthetic ticket"
    assert detail.ticket.body == "Synthetic participant question."
    assert devrev.get_calls == ["TKT-1234"]
    assert devrev.list_calls == []


async def test_detail_surfaces_coverage_and_response_data_gaps(stack):
    _backend, _repo, _devrev, service, _clock = stack
    oversized_gap = "x" * (MAX_REASON_LENGTH + 50)
    event = _event(
        route="generate_response",
        classification={
            "route": "generate_response",
            "confidence": 0.84,
            "reasoning": "A grounded participant response was requested.",
        },
        structured_response={
            "generate_response": {
                "coverage_gaps": [
                    "The KB did not cover vesting.",
                    "   ",
                    {"unexpected": "shape"},
                    42,
                    oversized_gap,
                ],
                "response": {
                    "response_to_participant": "A bounded answer.",
                    "data_gaps": [
                        "The participant's plan year is unknown.",
                        "The KB did not cover vesting.",
                    ],
                },
            }
        },
    )
    await service.ingest(event)

    detail = await service.get_detail("job123:0")

    assert detail.gaps == [
        "The KB did not cover vesting.",
        "x" * MAX_REASON_LENGTH,
        "The participant's plan year is unknown.",
    ]


def _verifier(*, email: str = PUBLISHER, audience: str = AUDIENCE):
    def verify(_token: str, requested_audience: str):
        assert requested_audience == AUDIENCE
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": email,
            "email_verified": True,
        }

    return verify


def _ingest_client(service, *, verifier=None) -> TestClient:
    from api.ticket_evaluation_ingest_app import build_ingest_app

    return TestClient(
        build_ingest_app(
            service=service,
            oidc_audience=AUDIENCE,
            allowed_service_accounts=(PUBLISHER,),
            token_verifier=verifier or _verifier(),
        ),
        raise_server_exceptions=False,
    )


def _headers() -> dict[str, str]:
    return {"X-ForUs-Workload-Authorization": "Bearer signed.synthetic.token"}


def test_private_put_checks_exact_oidc_audience_and_service_account(stack):
    _backend, _repo, _devrev, service, _clock = stack
    wrong_audience = _ingest_client(
        service, verifier=_verifier(audience="https://wrong.example.run.app")
    )
    wrong_caller = _ingest_client(
        service, verifier=_verifier(email="other@example.iam.gserviceaccount.com")
    )

    assert wrong_audience.put(
        "/internal/v1/ticket-evaluations/job123:0",
        headers=_headers(),
        json=_event().model_dump(mode="json"),
    ).status_code == 403
    assert wrong_caller.put(
        "/internal/v1/ticket-evaluations/job123:0",
        headers=_headers(),
        json=_event().model_dump(mode="json"),
    ).status_code == 403


def test_private_put_rejects_path_body_mismatch_and_acks_replay(stack):
    _backend, _repo, _devrev, service, _clock = stack
    client = _ingest_client(service)
    payload = _event().model_dump(mode="json")

    mismatch = client.put(
        "/internal/v1/ticket-evaluations/another:0", headers=_headers(), json=payload
    )
    first = client.put(
        "/internal/v1/ticket-evaluations/job123:0", headers=_headers(), json=payload
    )
    replay = client.put(
        "/internal/v1/ticket-evaluations/job123:0", headers=_headers(), json=payload
    )

    assert mismatch.status_code == 422
    assert first.status_code == 201
    assert first.json() == {
        "accepted": True,
        "execution_id": "job123:0",
        "replayed": False,
        "hydration_status": "succeeded",
    }
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.headers["Cache-Control"] == "no-store"


def test_private_app_bounds_body_before_json_materialization(stack):
    _backend, _repo, _devrev, service, _clock = stack
    client = _ingest_client(service)
    response = client.put(
        "/internal/v1/ticket-evaluations/job123:0",
        headers={**_headers(), "Content-Type": "application/json"},
        content=b"x" * (400 * 1024 + 1),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert client.get("/readyz").json() == {"ready": True}


def test_private_app_redacts_invalid_payload_values(stack):
    _backend, _repo, _devrev, service, _clock = stack
    client = _ingest_client(service)
    payload = _event().model_dump(mode="json")
    payload["diagnostics"] = {
        "provider": {"openaiApiKey": "SENTINEL-MUST-NEVER-ECHO"}
    }

    response = client.put(
        "/internal/v1/ticket-evaluations/job123:0",
        headers=_headers(),
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "Request validation failed",
        }
    }
    assert "SENTINEL-MUST-NEVER-ECHO" not in response.text


def test_admin_collection_contains_only_persisted_rag_runs_and_never_works_list(monkeypatch):
    from api.ticket_review_routes import API_PREFIX
    from tests.test_ticket_review_routes import _auth_headers, _harness

    harness = _harness(monkeypatch)
    with harness.client as client:
        client.portal.call(harness.service._evaluation_service.ingest, _event(job_id="job111"))
        client.portal.call(harness.service._evaluation_service.ingest, _event(job_id="job222"))

        response = client.get(f"{API_PREFIX}/tickets", headers=_auth_headers())

    assert response.status_code == 200, response.text
    assert {item["execution_id"] for item in response.json()["items"]} == {
        "job111:0",
        "job222:0",
    }
    assert harness.devrev.list_calls == []


def test_admin_detail_is_execution_keyed_and_a_devrev_only_ticket_is_absent(monkeypatch):
    from api.ticket_review_routes import API_PREFIX
    from tests.test_ticket_review_routes import _auth_headers, _harness

    harness = _harness(monkeypatch)
    with harness.client as client:
        client.portal.call(harness.service._evaluation_service.ingest, _event())
        known = client.get(f"{API_PREFIX}/tickets/job123:0", headers=_auth_headers())
        before_missing = len(harness.devrev.get_calls)
        missing = client.get(f"{API_PREFIX}/tickets/unknown:0", headers=_auth_headers())

    assert known.status_code == 200, known.text
    assert known.json()["execution"]["event"]["answer"] == "The generated RAG answer."
    assert missing.status_code == 404
    assert len(harness.devrev.get_calls) == before_missing
    assert harness.devrev.list_calls == []


def test_manual_ticket_review_creation_route_is_absent(monkeypatch):
    from api.ticket_review_routes import API_PREFIX
    from tests.test_ticket_review_routes import _harness, _write_headers

    harness = _harness(monkeypatch)
    response = harness.client.post(
        f"{API_PREFIX}/tickets/TKT-1234/review",
        headers=_write_headers(harness.client),
        json={},
    )
    assert response.status_code == 404


def test_admin_collection_filters_the_execution_ledger_and_binds_the_cursor(monkeypatch):
    from api.ticket_review_routes import API_PREFIX, CODE_CURSOR_REJECTED
    from tests.test_ticket_review_routes import _auth_headers, _harness

    harness = _harness(monkeypatch)
    failed = _event(
        job_id="jobfailed",
        route="generate_response",
        status="failed",
        classification={
            "route": "generate_response",
            "confidence": 0.87,
            "reasoning": "The route attempted a grounded response.",
        },
        answer=None,
        structured_response={"error": "synthetic failure"},
    )
    with harness.client as client:
        client.portal.call(harness.service._evaluation_service.ingest, _event(job_id="job111"))
        client.portal.call(harness.service._evaluation_service.ingest, _event(job_id="job222"))
        client.portal.call(harness.service._evaluation_service.ingest, failed)

        filtered = client.get(
            f"{API_PREFIX}/tickets",
            headers=_auth_headers(),
            params={"status": "succeeded", "review_status": "unreviewed", "page_size": 1},
        )
        assert filtered.status_code == 200, filtered.text
        payload = filtered.json()
        assert len(payload["items"]) == 1
        assert payload["items"][0]["status"] == "succeeded"
        assert payload["items"][0]["review"]["status"] == "unreviewed"
        assert payload["next_cursor"]

        wrong_filter = client.get(
            f"{API_PREFIX}/tickets",
            headers={**_auth_headers(), "X-Tickets-Cursor": payload["next_cursor"]},
            params={"status": "failed", "page_size": 1},
        )

    assert wrong_filter.status_code == 422
    assert wrong_filter.json()["error"]["code"] == CODE_CURSOR_REJECTED
    assert harness.devrev.list_calls == []


def test_admin_timeline_cursor_round_trips_for_the_same_execution(monkeypatch):
    from api.ticket_review_routes import API_PREFIX
    from tests.test_ticket_review_routes import _auth_headers, _harness

    harness = _harness(monkeypatch)
    with harness.client as client:
        client.portal.call(harness.service._evaluation_service.ingest, _event())
        first = client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline",
            headers=_auth_headers(),
            params={"page_size": 1},
        )
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor and "remote-timeline-next" not in cursor

        second = client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline",
            headers={**_auth_headers(), "X-Tickets-Cursor": cursor},
            params={"page_size": 1},
        )

    assert second.status_code == 200, second.text
    assert harness.devrev.timeline_calls[-1][1] == "remote-timeline-next"
