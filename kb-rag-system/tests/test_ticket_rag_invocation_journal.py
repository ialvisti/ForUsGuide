"""Durability contract for one platform record per real RAG invocation."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from data_pipeline.ticket_job_models import (
    TicketJobState,
    fingerprint_request,
    new_job_record,
    utcnow,
)


PAYLOAD = {
    "participant_id": "158948",
    "plan_id": "580",
    "company_name": "Example Inc.",
    "company_status": "Ongoing",
    "record_keeper": "LT Trust",
    "ticket": {
        "ticket_id": "TKT-INVOCATIONS",
        "username": "Ivan",
        "user_email": "ivan@example.test",
        "email_subject": "Rollover",
        "email_body": "What are the rollover rules?",
    },
}


def test_console_urls_accept_legacy_and_invocation_scoped_ids():
    from api.ticket_review_routes import validated_execution_id

    assert validated_execution_id("job123:0") == "job123:0"
    assert validated_execution_id("job123-e3-a2:0") == "job123-e3-a2:0"


def _execution_plan(route="knowledge_question"):
    return {
        "version": 1,
        "total_inquiries": 1,
        "unprocessed_inquiries": 0,
        "inquiries": [{
            "inquiry": "What are the rollover rules?",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "related_inquiries": [],
        }],
        "classifications": [{
            "route": route,
            "confidence": 0.93,
            "reasoning": "This requires grounded plan knowledge.",
            "user_message": None,
            "metadata": {},
        }],
        "gating": [{
            "route": route,
            "override_reason": None,
        }],
    }


async def _seed_claimed_job(
    repo, *, worker_id="worker-one", route="knowledge_question",
):
    fingerprint = fingerprint_request(PAYLOAD)
    record, _ = await repo.create_or_get(
        principal_id="n8n",
        idempotency_key=None,
        request_fingerprint=fingerprint,
        candidate=new_job_record(
            principal_id="n8n",
            tenant_id="tenant-a",
            ticket_id="TKT-INVOCATIONS",
            request_fingerprint=fingerprint,
            request_payload=PAYLOAD,
            execution_plan=_execution_plan(route),
            trace_id="trace-invocations",
        ),
    )
    epoch = await repo.claim(record.job_id, worker_id=worker_id, lease_s=90)
    assert epoch is not None
    return await repo.get(record.job_id), epoch


def _succeeded_checkpoint(route="knowledge_question"):
    knowledge_answer = {
        "answer": "A grounded answer.",
        "key_points": [],
        "source_articles": [],
        "used_chunks": [],
        "confidence_note": "well_covered",
        "metadata": {"model": "gpt-test"},
    }
    generated_response = {
        "decision": "respond",
        "confidence": 0.9,
        "response": {"response_to_participant": "A grounded response."},
        "source_articles": [],
        "used_chunks": [],
        "coverage_gaps": [],
        "metadata": {"model": "gpt-test"},
    }
    return {
        "route": route,
        "execution_status": "succeeded",
        "participant_reply_safe": True,
        "degraded": False,
        "result": {
            "inquiry": "What are the rollover rules?",
            "topic": "rollover",
            "route": route,
            "knowledge_answer": (
                knowledge_answer if route == "knowledge_question" else None
            ),
            "generate_response": (
                generated_response if route == "generate_response" else None
            ),
            "diagnostics": {},
        },
    }


@pytest.mark.parametrize(
    ("route", "expected_answer"),
    [
        ("knowledge_question", "A grounded answer."),
        ("generate_response", "A grounded response."),
    ],
)
async def test_intent_precedes_effect_and_completion_is_atomic_with_outbox(
    route, expected_answer,
):
    from data_pipeline.ticket_job_repository import (
        RAG_INVOCATIONS_COLLECTION,
        TICKET_EVALUATION_OUTBOX_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    record, epoch = await _seed_claimed_job(repo, route=route)

    invocation_id = await repo.begin_rag_invocation(
        record.job_id,
        0,
        route=route,
        worker_id="worker-one",
        lease_epoch=epoch,
    )

    intent = await backend.get_doc(RAG_INVOCATIONS_COLLECTION, invocation_id)
    assert invocation_id == f"{record.job_id}-e{epoch}-a1:0"
    assert intent["state"] == "started"
    assert intent["event_seed"]["classification"]["reasoning"] == \
        "This requires grounded plan knowledge."
    assert not backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION)

    await repo.record_inquiry_result(
        record.job_id,
        0,
        _succeeded_checkpoint(route),
        lease_epoch=epoch,
        invocation_id=invocation_id,
    )

    completed = await backend.get_doc(RAG_INVOCATIONS_COLLECTION, invocation_id)
    outbox = await backend.get_doc(
        TICKET_EVALUATION_OUTBOX_COLLECTION, invocation_id,
    )
    assert completed["state"] == "completed"
    assert completed["event_digest"] == outbox["event_digest"]
    assert outbox["event"]["execution_id"] == invocation_id
    assert outbox["event"]["invocation_id"] == invocation_id
    assert outbox["event"]["attempt"] == 1
    assert outbox["event"]["lease_epoch"] == epoch
    assert outbox["event"]["route"] == route
    assert outbox["event"]["answer"] == expected_answer


async def test_retry_has_two_invocation_ids_and_abandoned_one_has_no_answer():
    from data_pipeline.ticket_job_repository import (
        JOBS_COLLECTION,
        RAG_INVOCATIONS_COLLECTION,
        TICKET_EVALUATION_OUTBOX_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    record, first_epoch = await _seed_claimed_job(repo)
    first_id = await repo.begin_rag_invocation(
        record.job_id,
        0,
        route="knowledge_question",
        worker_id="worker-one",
        lease_epoch=first_epoch,
    )

    control = await backend.get_doc(JOBS_COLLECTION, record.job_id)
    control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
    backend._data[JOBS_COLLECTION][record.job_id] = control
    generation = await repo.fence_and_requeue(
        record.job_id,
        expected_lease_epoch=first_epoch,
        expected_lease_expires_at=control["lease_expires_at"],
        observed_at=utcnow(),
    )
    second_epoch = await repo.claim(
        record.job_id,
        worker_id="worker-two",
        expected_generation=generation,
    )
    second = await repo.get(record.job_id)
    second_id = await repo.begin_rag_invocation(
        record.job_id,
        0,
        route="knowledge_question",
        worker_id="worker-two",
        lease_epoch=second_epoch,
    )
    await repo.record_inquiry_result(
        record.job_id,
        0,
        _succeeded_checkpoint(),
        lease_epoch=second_epoch,
        invocation_id=second_id,
    )
    first_intent = await backend.get_doc(
        RAG_INVOCATIONS_COLLECTION, first_id,
    )
    recovered = await repo.recover_abandoned_rag_invocation(
        first_id,
        observed_at=first_intent["next_recovery_at"] + timedelta(seconds=1),
    )

    assert first_id != second_id
    assert set(backend._data[RAG_INVOCATIONS_COLLECTION]) == {
        first_id, second_id,
    }
    assert recovered is not None
    assert recovered["state"] == "recovered"
    events = backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION]
    assert set(events) == {first_id, second_id}
    assert events[first_id]["event"]["status"] == "failed"
    assert events[first_id]["event"]["answer"] is None
    assert events[first_id]["event"]["error"] == {
        "code": "RAG_INVOCATION_ABANDONED",
        "retryable": False,
    }
    assert events[second_id]["event"]["status"] == "succeeded"
    assert events[second_id]["event"]["answer"] == "A grounded answer."
    assert second.attempt == 2


async def test_recovery_reschedules_an_intent_while_its_lease_is_still_live():
    from data_pipeline.ticket_job_repository import (
        RAG_INVOCATIONS_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    record, epoch = await _seed_claimed_job(repo)
    invocation_id = await repo.begin_rag_invocation(
        record.job_id,
        0,
        route="knowledge_question",
        worker_id="worker-one",
        lease_epoch=epoch,
    )
    before = await backend.get_doc(RAG_INVOCATIONS_COLLECTION, invocation_id)

    recovered = await repo.recover_abandoned_rag_invocation(
        invocation_id,
        observed_at=before["started_at"] + timedelta(seconds=1),
    )

    after = await backend.get_doc(RAG_INVOCATIONS_COLLECTION, invocation_id)
    assert recovered is None
    assert after["state"] == "started"
    assert after["next_recovery_at"] > before["started_at"]


async def test_due_invocation_is_not_starved_by_future_intents():
    from data_pipeline.ticket_job_repository import (
        RAG_INVOCATIONS_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    now = utcnow()
    collection = backend._data.setdefault(RAG_INVOCATIONS_COLLECTION, {})
    for index in range(101):
        collection[f"a-future-{index:03d}:0"] = {
            "state": "started",
            "next_recovery_at": now + timedelta(hours=1),
        }
    collection["z-due:0"] = {
        "state": "started",
        "next_recovery_at": now - timedelta(seconds=1),
    }

    page = await repo.scan_due_rag_invocations(limit=1, observed_at=now)

    assert [invocation_id for invocation_id, _document in page] == ["z-due:0"]


class _SimulatedProcessCrash(BaseException):
    pass


class _CountingRagOrchestrator:
    def __init__(self):
        self.invocations = 0

    async def handle_inquiry(
        self, ext, req, *, total_inquiries, classification=None,
    ):
        from data_pipeline.ticket_orchestrator import InquiryOutcome

        self.invocations += 1
        return InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="knowledge_question",
            knowledge_result=SimpleNamespace(
                answer="Recovered retry answer",
                key_points=[],
                source_articles=[],
                used_chunks=[],
                confidence_note="well_covered",
                metadata={"model": "gpt-test"},
            ),
            diagnostics={},
        )


class _Queue:
    def __init__(self):
        self.enqueued = []

    async def ensure_enqueued(self, job_id, generation=0):
        self.enqueued.append((job_id, generation))
        return f"inline/{job_id}-g{generation}"

    async def task_exists(self, job_id, generation=0):
        return True

    async def aclose(self):
        pass


async def test_post_effect_process_crash_is_recovered_then_retry_is_distinct(
    monkeypatch,
):
    from api import ticket_worker
    from data_pipeline.ticket_job_repository import (
        JOBS_COLLECTION,
        RAG_INVOCATIONS_COLLECTION,
        TICKET_EVALUATION_OUTBOX_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )
    from data_pipeline.ticket_reconciler import TicketReconciler

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    fingerprint = fingerprint_request(PAYLOAD)
    record, _ = await repo.create_or_get(
        principal_id="n8n",
        idempotency_key=None,
        request_fingerprint=fingerprint,
        candidate=new_job_record(
            principal_id="n8n",
            tenant_id="tenant-a",
            ticket_id="TKT-INVOCATIONS",
            request_fingerprint=fingerprint,
            request_payload=PAYLOAD,
            execution_plan=_execution_plan(),
            trace_id="trace-invocations",
        ),
    )
    orchestrator = _CountingRagOrchestrator()
    app = SimpleNamespace(state=SimpleNamespace(
        ticket_repo=repo,
        ticket_orchestrator_factory=lambda: orchestrator,
        execution_logger=None,
    ))
    original_converter = ticket_worker._entry_from_outcome

    def crash_after_effect(_index, _outcome):
        raise _SimulatedProcessCrash()

    monkeypatch.setattr(ticket_worker, "_entry_from_outcome", crash_after_effect)
    with pytest.raises(_SimulatedProcessCrash):
        await ticket_worker.run_ticket_job(
            app, record.job_id, worker_id="worker-one",
        )

    assert orchestrator.invocations == 1
    journals = backend._data[RAG_INVOCATIONS_COLLECTION]
    assert len(journals) == 1
    first_id = next(iter(journals))
    assert journals[first_id]["state"] == "started"
    assert not backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION)

    control = await backend.get_doc(JOBS_COLLECTION, record.job_id)
    control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
    backend._data[JOBS_COLLECTION][record.job_id] = control
    backend._data[RAG_INVOCATIONS_COLLECTION][first_id][
        "next_recovery_at"
    ] = control["lease_expires_at"]
    queue = _Queue()
    counts = await TicketReconciler(repo, queue).run_once()

    assert counts["rag_invocations_recovered"] == 1
    assert backend._data[RAG_INVOCATIONS_COLLECTION][first_id]["state"] == \
        "recovered"
    assert backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION][first_id][
        "event"
    ]["answer"] is None

    monkeypatch.setattr(ticket_worker, "_entry_from_outcome", original_converter)
    refreshed = await repo.get(record.job_id)
    final = await ticket_worker.run_ticket_job(
        app,
        record.job_id,
        worker_id="worker-two",
        expected_generation=refreshed.enqueue_generation,
    )

    assert final.state == TicketJobState.SUCCEEDED
    assert orchestrator.invocations == 2
    invocation_ids = set(backend._data[RAG_INVOCATIONS_COLLECTION])
    assert len(invocation_ids) == 2
    assert first_id in invocation_ids
    assert set(backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION]) == \
        invocation_ids


async def test_nmi_and_unprocessed_never_create_invocation_intents():
    from data_pipeline.ticket_job_repository import (
        RAG_INVOCATIONS_COLLECTION,
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    record, epoch = await _seed_claimed_job(repo)

    with pytest.raises(ValueError, match="RAG route"):
        await repo.begin_rag_invocation(
            record.job_id,
            0,
            route="needs_more_info",
            worker_id="worker-one",
            lease_epoch=epoch,
        )

    await repo.record_inquiry_result(
        record.job_id,
        0,
        {
            "route": "knowledge_question",
            "execution_status": "unprocessed",
            "participant_reply_safe": False,
            "degraded": True,
        },
        lease_epoch=epoch,
    )
    assert not backend._data.get(RAG_INVOCATIONS_COLLECTION)
