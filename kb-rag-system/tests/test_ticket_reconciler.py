"""
Tests del reconciliador automático de outbox/leases (plan Tarea 7 Paso 5).

Cubre: re-enqueue de outbox pending, fencing de lease vencido, terminalización
por deadline absoluto y payload ausente, concurrencia de dos reconciliadores
(recovery lock) y reejecución idempotente.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from data_pipeline.ticket_job_models import (
    TicketJobState,
    fingerprint_request,
    new_job_record,
    utcnow,
)
from data_pipeline.ticket_job_repository import (
    JOBS_COLLECTION,
    PAYLOADS_COLLECTION,
    InMemoryTicketJobBackend,
    TicketJobRepository,
)
from data_pipeline.ticket_reconciler import TicketReconciler

PAYLOAD = {"participant_id": "1", "plan_id": "2",
           "ticket": {"email_subject": "s", "email_body": "b"}}


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    async def ensure_enqueued(self, job_id, generation=0):
        self.enqueued.append((job_id, generation))
        return f"inline/ticket-{job_id}-g{generation}"

    async def aclose(self):
        pass


@pytest.fixture
def backend():
    return InMemoryTicketJobBackend()


@pytest.fixture
def repo(backend):
    return TicketJobRepository(backend, retention_days=90, max_outstanding=25)


async def _seed(repo, **over):
    payload = PAYLOAD
    rec, _ = await repo.create_or_get(
        principal_id="n8n", idempotency_key=None,
        request_fingerprint=fingerprint_request(payload),
        candidate=new_job_record(principal_id="n8n",
                                 request_fingerprint=fingerprint_request(payload),
                                 request_payload=payload, **over),
    )
    return rec


class TestReconcilerRepairs:

    async def test_reconciler_reenqueues_pending_outbox(self, repo):
        rec = await _seed(repo)   # queued, enqueue_state=pending
        queue = FakeQueue()
        counts = await TicketReconciler(repo, queue).run_once()
        assert counts["requeued_outbox"] == 1
        assert queue.enqueued and queue.enqueued[0][0] == rec.job_id
        refreshed = await repo.get(rec.job_id)
        assert refreshed.enqueue_state == "enqueued"

    async def test_reconciler_fences_expired_lease(self, repo, backend):
        rec = await _seed(repo)
        epoch = await repo.claim(rec.job_id, worker_id="w-old", lease_s=90)
        # forzar lease vencido en el pasado
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        queue = FakeQueue()
        counts = await TicketReconciler(repo, queue).run_once()
        assert counts["fenced_leases"] == 1
        refreshed = await repo.get(rec.job_id)
        assert refreshed.lease_epoch > epoch, "el fencing no incrementó el epoch"
        assert refreshed.state == TicketJobState.QUEUED
        assert refreshed.enqueue_generation > rec.enqueue_generation

    async def test_reconciler_terminalizes_expired_deadline(self, repo, backend):
        rec = await _seed(repo)
        await repo.claim(rec.job_id, worker_id="w")
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["job_deadline_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        counts = await TicketReconciler(repo, FakeQueue()).run_once()
        assert counts["deadline_terminalized"] == 1
        refreshed = await repo.get(rec.job_id)
        assert refreshed.state == TicketJobState.TIMEOUT
        assert refreshed.next_action.value == "use_legacy_or_human"

    async def test_reconciler_terminalizes_missing_payload(self, repo, backend):
        rec = await _seed(repo)
        await repo.claim(rec.job_id, worker_id="w")
        # payload desaparece (TTL) mientras el control sigue vivo
        backend._data[PAYLOADS_COLLECTION].pop(rec.job_id, None)

        counts = await TicketReconciler(repo, FakeQueue()).run_once()
        assert counts["payload_expired"] == 1
        refreshed = await repo.get(rec.job_id)
        assert refreshed.state == TicketJobState.FAILED

    async def test_two_concurrent_reconcilers_do_not_double_repair(self, repo):
        await _seed(repo)
        queue_a, queue_b = FakeQueue(), FakeQueue()
        rec_a = TicketReconciler(repo, queue_a, owner="rec-a")
        rec_b = TicketReconciler(repo, queue_b, owner="rec-b")
        # rec-a toma el recovery lock primero y repara; rec-b lo ve locked
        counts_a = await rec_a.run_once()
        counts_b = await rec_b.run_once()
        total_requeued = counts_a["requeued_outbox"] + counts_b["requeued_outbox"]
        assert total_requeued == 1, "el outbox se reparó dos veces"

    async def test_run_once_idempotent_when_no_work(self, repo):
        # job ya encolado y con lease vigente: nada que reparar
        rec = await _seed(repo)
        await repo.mark_enqueued(rec.job_id, "inline/ticket-x-g0")
        await repo.claim(rec.job_id, worker_id="w", lease_s=90)
        counts = await TicketReconciler(repo, FakeQueue()).run_once()
        assert counts["requeued_outbox"] == 0
        assert counts["fenced_leases"] == 0
        assert counts["errors"] == 0
