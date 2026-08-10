"""Contract tests for durable RAG ticket-evaluation events."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


def _models():
    try:
        from api.ticket_evaluation_models import (  # noqa: PLC0415
            ClassificationRationale,
            EvaluationRoute,
            EvaluationStatus,
            TicketEvaluationEvent,
        )
    except ModuleNotFoundError:
        pytest.fail("ticket evaluation event contract is not implemented")
    return (
        ClassificationRationale,
        EvaluationRoute,
        EvaluationStatus,
        TicketEvaluationEvent,
    )


def _valid_event(**overrides):
    (
        ClassificationRationale,
        EvaluationRoute,
        EvaluationStatus,
        TicketEvaluationEvent,
    ) = _models()
    values = {
        "schema_version": "1.0",
        "execution_id": "0123456789abcdef0123456789abcdef:0",
        "job_id": "0123456789abcdef0123456789abcdef",
        "inquiry_index": 0,
        "ticket_id": "TKT-1234",
        "tenant_id": "tenant-a",
        "route": EvaluationRoute.KNOWLEDGE_QUESTION,
        "status": EvaluationStatus.SUCCEEDED,
        "occurred_at": datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc),
        "inquiry": "What are the rollover rules?",
        "topic": "rollover",
        "classification": ClassificationRationale(
            route=EvaluationRoute.KNOWLEDGE_QUESTION,
            confidence=0.91,
            reasoning="The participant asked for plan knowledge.",
        ),
        "answer": "A direct, generated RAG answer.",
        "structured_response": {
            "knowledge_answer": {
                "answer": "A direct, generated RAG answer.",
                "confidence_note": "well_covered",
            }
        },
        "diagnostics": {"classifier": {"parse_ok": True}},
        "sources": [{
            "article_id": "article-1",
            "title": "Rollover Guide",
            "url": "https://example.test/rollover",
        }],
        "chunks": [{
            "chunk_id": "chunk-1",
            "source_id": "article-1",
            "content_hash": "a" * 64,
            "preview": "Bounded evidence preview.",
            "score": 0.88,
        }],
        "retrieval_metadata": {"namespace": "tenant-a", "match_count": 1},
        "correlation": {"trace_id": "trace-1", "request_fingerprint": "b" * 64},
    }
    values.update(overrides)
    return TicketEvaluationEvent.model_validate(values)


def test_contract_accepts_a_bounded_ticket_associated_rag_execution():
    event = _valid_event()

    assert event.execution_id == f"{event.job_id}:{event.inquiry_index}"
    assert event.answer == "A direct, generated RAG answer."
    assert event.sources[0].article_id == "article-1"
    assert event.chunks[0].content_hash == "a" * 64


def test_contract_accepts_invocation_scoped_identity_and_keeps_legacy_ids():
    job_id = "0123456789abcdef0123456789abcdef"
    invocation_id = f"{job_id}-e3-a2:0"

    invocation = _valid_event(
        execution_id=invocation_id,
        invocation_id=invocation_id,
        lease_epoch=3,
        attempt=2,
    )
    legacy = _valid_event()

    assert invocation.event_id == invocation_id
    assert invocation.invocation_id == invocation_id
    assert legacy.invocation_id == legacy.execution_id
    assert '"invocation_id"' not in legacy.canonical_json()
    assert "invocation_id" not in legacy.to_document()
    assert '"invocation_id"' in invocation.canonical_json()


@pytest.mark.parametrize("route", ["needs_more_info", "classification"])
def test_contract_rejects_non_rag_routes(route):
    with pytest.raises(ValidationError):
        _valid_event(route=route)


@pytest.mark.parametrize("status", ["queued", "running", "unprocessed"])
def test_contract_rejects_non_auditable_statuses(status):
    with pytest.raises(ValidationError):
        _valid_event(status=status)


def test_contract_rejects_non_deterministic_execution_id():
    with pytest.raises(ValidationError, match="deterministic invocation identity"):
        _valid_event(execution_id="different:0")

    job_id = "0123456789abcdef0123456789abcdef"
    with pytest.raises(ValidationError, match="deterministic invocation identity"):
        _valid_event(
            execution_id=f"{job_id}-e4-a2:0",
            invocation_id=f"{job_id}-e4-a2:0",
            lease_epoch=3,
            attempt=2,
        )


def test_contract_rejects_route_that_disagrees_with_classification():
    ClassificationRationale, EvaluationRoute, _, _ = _models()
    classification = ClassificationRationale(
        route=EvaluationRoute.GENERATE_RESPONSE,
        confidence=0.8,
        reasoning="Requires participant-specific plan data.",
    )

    with pytest.raises(ValidationError, match="classification route"):
        _valid_event(classification=classification)


def test_contract_requires_a_ticket_identity_and_aware_timestamp():
    with pytest.raises(ValidationError):
        _valid_event(ticket_id=" ")
    with pytest.raises(ValidationError):
        _valid_event(occurred_at=datetime(2026, 8, 5, 15, 30))


def test_contract_bounds_evidence_and_forbids_secret_shaped_fields():
    with pytest.raises(ValidationError):
        _valid_event(sources=[{"article_id": f"article-{i}"} for i in range(51)])
    with pytest.raises(ValidationError):
        _valid_event(chunks=[{
            "chunk_id": "chunk-1",
            "content_hash": "a" * 64,
            "preview": "x" * 2001,
        }])
    with pytest.raises(ValidationError):
        _valid_event(api_key="must-never-be-accepted")
    with pytest.raises(ValidationError, match="sensitive key"):
        _valid_event(diagnostics={"authorization": "Bearer secret"})


@pytest.mark.parametrize(
    "secret_key",
    [
        "openaiApiKey",
        "access_token",
        "refreshToken",
        "devrev_pat",
        "token",
        "client-secret",
        "privateKey",
    ],
)
def test_contract_rejects_vendor_and_camel_case_secret_keys(secret_key):
    with pytest.raises(ValidationError, match="sensitive key"):
        _valid_event(
            diagnostics={"provider": {"request": {secret_key: "must-not-persist"}}}
        )


def test_failed_checkpoint_preserves_safe_execution_diagnostics():
    from api.ticket_evaluation_models import build_ticket_evaluation_event

    record = SimpleNamespace(
        job_id="job123",
        ticket_id="TKT-1234",
        tenant_id="tenant-a",
        request_fingerprint="f" * 64,
        trace_id="trace-1",
        principal_id=None,
        forusbots_job_ids=[],
        execution_plan={
            "classifications": [{
                "route": "generate_response",
                "reasoning": "A grounded response was attempted.",
            }],
            "inquiries": [{"inquiry": "Answer the ticket", "topic": "plan"}],
        },
    )
    entry = {
        "route": "generate_response",
        "execution_status": "timeout",
        "degraded": True,
        "participant_reply_safe": False,
        "manual_reconciliation_required": True,
        "scrape_status": "timeout",
        "diagnostics": {"failure_phase": "handle_inquiry"},
        "error": {"code": "INQUIRY_TIMEOUT", "retryable": False},
    }

    event = build_ticket_evaluation_event(
        record,
        0,
        entry,
        observed_at=datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc),
    )

    assert event is not None
    assert event.diagnostics["failure_phase"] == "handle_inquiry"
    assert event.diagnostics["checkpoint"] == {
        "degraded": True,
        "execution_status": "timeout",
        "manual_reconciliation_required": True,
        "participant_reply_safe": False,
        "scrape_status": "timeout",
    }


def test_canonical_json_is_stable_and_uses_utc_wire_timestamps():
    first = _valid_event()
    second = _valid_event(
        diagnostics={"classifier": {"parse_ok": True}},
        retrieval_metadata={"match_count": 1, "namespace": "tenant-a"},
    )

    assert first.canonical_json() == second.canonical_json()
    assert '"occurred_at":"2026-08-05T15:30:00Z"' in first.canonical_json()
