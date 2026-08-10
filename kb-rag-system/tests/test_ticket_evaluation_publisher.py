"""Reliable delivery tests for the ticket-evaluation outbox."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from api.ticket_evaluation_models import TicketEvaluationEvent
from data_pipeline.ticket_job_repository import (
    TICKET_EVALUATION_OUTBOX_COLLECTION,
    InMemoryTicketJobBackend,
    TicketJobRepository,
)


def _event(*, execution_id="0123456789abcdef0123456789abcdef:0"):
    job_id, raw_index = execution_id.split(":")
    return TicketEvaluationEvent.model_validate({
        "execution_id": execution_id,
        "job_id": job_id,
        "inquiry_index": int(raw_index),
        "ticket_id": "TKT-9000",
        "route": "knowledge_question",
        "status": "succeeded",
        "occurred_at": datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc),
        "inquiry": "What are the rollover rules?",
        "topic": "rollover",
        "classification": {
            "route": "knowledge_question",
            "confidence": 0.9,
            "reasoning": "This is a plan knowledge question.",
        },
        "answer": "Generated answer",
        "structured_response": {"knowledge_answer": {"answer": "Generated answer"}},
    })


async def _seed_outbox(backend, event=None):
    event = event or _event()
    now = datetime.now(timezone.utc)
    backend._data.setdefault(TICKET_EVALUATION_OUTBOX_COLLECTION, {})[
        event.execution_id
    ] = {
        "execution_id": event.execution_id,
        "event_digest": event.canonical_digest(),
        "state": "pending",
        "attempt_count": 0,
        "created_at": now,
        "updated_at": now,
        "next_attempt_at": now,
        "delivered_at": None,
        "last_error_code": None,
        "event": event.to_document(),
    }
    return event


def _test_id_token(claims):
    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{encode({'alg': 'RS256', 'kid': 'test'})}.{encode(claims)}.sig"


def test_default_token_factory_fails_closed_when_adc_token_identity_differs(
    monkeypatch,
):
    from data_pipeline.ticket_evaluation_publisher import default_id_token_factory

    audience = "https://evaluation.internal"
    token = _test_id_token({
        "aud": audience,
        "email": "different@example.iam.gserviceaccount.com",
    })
    monkeypatch.setattr(
        "google.oauth2.id_token.fetch_id_token",
        lambda _request, observed_audience: (
            token if observed_audience == audience else "wrong-audience"
        ),
    )

    with pytest.raises(RuntimeError, match="configured service account") as error:
        default_id_token_factory(
            audience,
            expected_service_account=(
                "rag-publisher@example.iam.gserviceaccount.com"
            ),
        )

    assert token not in str(error.value)


def test_default_token_factory_accepts_only_matching_identity_and_audience(
    monkeypatch,
):
    from data_pipeline.ticket_evaluation_publisher import default_id_token_factory

    audience = "https://evaluation.internal"
    service_account = "rag-publisher@example.iam.gserviceaccount.com"
    token = _test_id_token({"aud": audience, "email": service_account})
    monkeypatch.setattr(
        "google.oauth2.id_token.fetch_id_token",
        lambda _request, _audience: token,
    )

    assert default_id_token_factory(
        audience,
        expected_service_account=service_account,
    ) == token


async def test_publisher_sends_oidc_in_both_headers_and_marks_exact_ack():
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)
    observed = {}

    async def handler(request):
        observed["request"] = request
        return httpx.Response(201, json={
            "accepted": True,
            "execution_id": event.execution_id,
            "replayed": False,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda audience: f"oidc-for-{audience}",
        )
        counts = await publisher.publish_pending(batch_size=10)

    request = observed["request"]
    assert request.method == "PUT"
    assert request.url.path == \
        f"/internal/v1/ticket-evaluations/{event.execution_id}"
    assert request.headers["authorization"] == \
        "Bearer oidc-for-https://evaluation.internal"
    assert request.headers["x-forus-workload-authorization"] == \
        request.headers["authorization"]
    assert json.loads(request.content)["execution_id"] == event.execution_id
    assert counts == {
        "evaluation_scanned": 1,
        "evaluation_delivered": 1,
        "evaluation_retried": 0,
        "evaluation_rejected": 0,
        "evaluation_errors": 0,
    }
    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert stored["state"] == "delivered"
    assert stored["attempt_count"] == 1
    assert stored["delivered_at"] is not None
    assert stored["expires_at"] > stored["delivered_at"]


@pytest.mark.parametrize("status_code", [200, 201])
async def test_idempotent_ack_accepts_first_delivery_or_replay(status_code):
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)

    async def handler(_request):
        return httpx.Response(status_code, json={
            "accepted": True,
            "execution_id": event.execution_id,
            "replayed": status_code == 200,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    assert counts["evaluation_delivered"] == 1


@pytest.mark.parametrize("status_code", [400, 409, 422])
async def test_permanent_validation_failure_is_durably_dead_lettered_without_body(
    status_code,
):
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)
    private_body = "participant-private-validation-detail"

    async def handler(_request):
        return httpx.Response(status_code, text=private_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert counts["evaluation_rejected"] == 1
    assert stored["state"] == "dead_letter"
    assert stored["last_error_code"] == f"HTTP_{status_code}"
    assert stored["dead_lettered_at"] == stored["updated_at"]
    assert "expires_at" not in stored
    assert private_body not in repr(stored)


@pytest.mark.parametrize("status_code", [401, 403])
async def test_receiver_auth_rejection_is_explicitly_dead_lettered(status_code):
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)

    async def handler(_request):
        return httpx.Response(status_code, text="private auth detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert counts["evaluation_rejected"] == 1
    assert counts["evaluation_retried"] == 0
    assert stored["state"] == "dead_letter"
    assert stored["last_error_code"] == f"HTTP_{status_code}"
    assert "next_attempt_at" in stored and stored["next_attempt_at"] is None
    assert "expires_at" not in stored


async def test_operator_can_replay_a_dead_letter_through_the_admin_command(
    caplog,
):
    from api.replay_ticket_evaluation import replay_dead_letter

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)
    await repo.record_ticket_evaluation_delivery_failure(
        event.execution_id,
        event_digest=event.canonical_digest(),
        error_code="HTTP_403",
        retryable=False,
    )

    result = await replay_dead_letter(
        repo,
        execution_id=event.execution_id,
        event_digest=event.canonical_digest(),
        authenticated_operator="oncall@example.test",
    )

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert result == 0
    assert stored["state"] == "pending"
    assert event.execution_id not in caplog.text
    assert "oncall@example.test" not in caplog.text


def test_admin_replay_derives_actor_from_refreshed_adc_and_fails_on_mismatch(
    monkeypatch,
):
    from api.replay_ticket_evaluation import resolve_authenticated_operator

    class Credentials:
        service_account_email = "different@example.iam.gserviceaccount.com"

        def refresh(self, _request):
            self.token = "private-access-token"

    credentials = Credentials()
    monkeypatch.setattr(
        "google.auth.default", lambda scopes: (credentials, "example")
    )

    with pytest.raises(RuntimeError, match="configured service account") as error:
        resolve_authenticated_operator(
            "rag-publisher@example.iam.gserviceaccount.com"
        )

    assert credentials.token not in str(error.value)


def test_admin_replay_uses_matching_refreshed_adc_service_account(monkeypatch):
    from api.replay_ticket_evaluation import resolve_authenticated_operator

    expected = "rag-publisher@example.iam.gserviceaccount.com"

    class Credentials:
        service_account_email = expected

        def __init__(self):
            self.refreshed = False

        def refresh(self, _request):
            self.refreshed = True

    credentials = Credentials()
    monkeypatch.setattr(
        "google.auth.default", lambda scopes: (credentials, "example")
    )

    assert resolve_authenticated_operator(expected) == expected
    assert credentials.refreshed is True


@pytest.mark.parametrize("failure", ["http_503", "transport", "bad_ack"])
async def test_transient_or_unacknowledged_delivery_stays_retryable(failure):
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)

    async def handler(request):
        if failure == "transport":
            raise httpx.ConnectError("private destination unavailable", request=request)
        if failure == "bad_ack":
            return httpx.Response(200, json={
                "accepted": True,
                "execution_id": "different:0",
                "replayed": False,
            })
        return httpx.Response(503, text="private upstream response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert counts["evaluation_retried"] == 1
    assert stored["state"] == "retry"
    assert stored["attempt_count"] == 1
    assert stored["next_attempt_at"] > stored["updated_at"]
    assert "expires_at" not in stored
    assert "private" not in repr(stored)


async def test_corrupt_outbox_digest_is_rejected_before_network_delivery():
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)
    backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION][event.execution_id][
        "event_digest"
    ] = "0" * 64
    network_calls = 0

    async def handler(_request):
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(201, json={
            "accepted": True,
            "execution_id": event.execution_id,
            "replayed": False,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert network_calls == 0
    assert counts["evaluation_rejected"] == 1
    assert stored["state"] == "dead_letter"
    assert stored["last_error_code"] == "EVENT_DIGEST_MISMATCH"


async def test_oversized_ack_content_length_is_not_read_or_accepted():
    from data_pipeline.ticket_evaluation_publisher import (
        MAX_ACK_BYTES,
        TicketEvaluationPublisher,
    )

    class NeverReadStream(httpx.AsyncByteStream):
        def __init__(self):
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            raise AssertionError("oversized response body must not be read")
            yield b""  # pragma: no cover

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    event = await _seed_outbox(backend)
    response_stream = NeverReadStream()

    async def handler(_request):
        return httpx.Response(
            200,
            headers={"Content-Length": str(MAX_ACK_BYTES + 1)},
            stream=response_stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            repo,
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda _audience: "token",
        )
        counts = await publisher.publish_pending(batch_size=1)

    stored = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, event.execution_id
    )
    assert response_stream.iterated is False
    assert counts["evaluation_retried"] == 1
    assert stored["last_error_code"] == "INVALID_ACK"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", ""),
        ("base_url", "http://evaluation.example.test"),
        ("audience", ""),
        ("service_account", "not-an-email"),
    ],
)
def test_publisher_rejects_unavailable_or_unsafe_destination(field, value):
    from data_pipeline.ticket_evaluation_publisher import (
        TicketEvaluationPublisher,
        TicketEvaluationPublisherConfigurationError,
    )

    kwargs = {
        "base_url": "https://evaluation.example.run.app",
        "audience": "https://evaluation.internal",
        "service_account": "rag-publisher@example.iam.gserviceaccount.com",
    }
    kwargs[field] = value
    with pytest.raises(TicketEvaluationPublisherConfigurationError):
        TicketEvaluationPublisher(
            TicketJobRepository(InMemoryTicketJobBackend()),
            client=httpx.AsyncClient(),
            token_factory=lambda _audience: "token",
            **kwargs,
        )


async def test_hydration_retry_runs_with_both_oidc_headers_when_outbox_is_empty():
    from data_pipeline.ticket_evaluation_publisher import TicketEvaluationPublisher

    observed = {}

    async def handler(request):
        observed["request"] = request
        return httpx.Response(200, json={"attempted": 3, "succeeded": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TicketEvaluationPublisher(
            TicketJobRepository(InMemoryTicketJobBackend()),
            base_url="https://evaluation.example.run.app",
            audience="https://evaluation.internal",
            service_account="rag-publisher@example.iam.gserviceaccount.com",
            client=client,
            token_factory=lambda audience: f"oidc-for-{audience}",
        )
        counts = await publisher.retry_due_hydrations(limit=20)

    request = observed["request"]
    assert request.method == "POST"
    assert request.url.path == "/internal/v1/ticket-evaluations:retry-hydration"
    assert request.url.params["limit"] == "20"
    assert request.headers["authorization"] == \
        "Bearer oidc-for-https://evaluation.internal"
    assert request.headers["x-forus-workload-authorization"] == \
        request.headers["authorization"]
    assert counts == {
        "evaluation_hydration_attempted": 3,
        "evaluation_hydration_succeeded": 2,
        "evaluation_hydration_errors": 0,
    }
