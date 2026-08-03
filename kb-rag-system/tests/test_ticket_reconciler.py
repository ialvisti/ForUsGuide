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
    def __init__(self, *, task_alive=True, task_exists_error=None):
        self.enqueued = []
        self.task_alive = task_alive
        self.task_exists_error = task_exists_error
        self.exists_checks = []

    async def ensure_enqueued(self, job_id, generation=0):
        self.enqueued.append((job_id, generation))
        return f"inline/ticket-{job_id}-g{generation}"

    async def task_exists(self, job_id, generation=0):
        self.exists_checks.append((job_id, generation))
        if self.task_exists_error is not None:
            raise self.task_exists_error
        return self.task_alive

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

    async def test_run_emits_sanitized_application_duration(
        self, repo, monkeypatch,
    ):
        emitted = []
        monotonic_values = iter((100.0, 100.75))
        monkeypatch.setattr(
            "data_pipeline.ticket_reconciler._monotonic",
            lambda: next(monotonic_values),
        )
        monkeypatch.setattr(
            "data_pipeline.ticket_reconciler.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )

        await TicketReconciler(repo, FakeQueue()).run_once()

        assert (
            "ticket_reconciler_duration_seconds", 0.75, {}
        ) in emitted

    async def test_run_emits_exact_global_active_job_gauges(
        self, repo, monkeypatch,
    ):
        now = utcnow()
        await _seed(repo, created_at=now - timedelta(seconds=37))
        await _seed(repo, created_at=now - timedelta(seconds=11))
        terminal = await _seed(repo, created_at=now - timedelta(seconds=90))
        await repo.update(terminal.job_id, state=TicketJobState.RUNNING)
        await repo.update(terminal.job_id, state=TicketJobState.SUCCEEDED)
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.ticket_reconciler.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )

        await TicketReconciler(repo, FakeQueue()).run_once()

        active = [event for event in emitted if event[0] == "ticket_jobs_active"]
        oldest = [
            event for event in emitted
            if event[0] == "ticket_jobs_oldest_age_seconds"
        ]
        assert active == [("ticket_jobs_active", 2, {})]
        assert len(oldest) == 1
        assert 37 <= oldest[0][1] < 40

    async def test_reconciler_reenqueues_pending_outbox(self, repo):
        rec = await _seed(repo)   # queued, enqueue_state=pending
        queue = FakeQueue()
        counts = await TicketReconciler(repo, queue).run_once()
        assert counts["requeued_outbox"] == 1
        assert queue.enqueued and queue.enqueued[0][0] == rec.job_id
        refreshed = await repo.get(rec.job_id)
        assert refreshed.enqueue_state == "enqueued"

    async def test_stale_enqueued_job_with_missing_task_bumps_generation(
        self, repo, backend,
    ):
        rec = await _seed(repo)
        await repo.mark_enqueued(
            rec.job_id, f"inline/ticket-{rec.job_id}-g0",
        )
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["updated_at"] = utcnow() - timedelta(minutes=2)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        queue = FakeQueue(task_alive=False)

        counts = await TicketReconciler(repo, queue).run_once()

        refreshed = await repo.get(rec.job_id)
        assert counts["requeued_outbox"] == 1
        assert queue.exists_checks == [(rec.job_id, 0)]
        assert queue.enqueued == [(rec.job_id, 1)]
        assert refreshed.enqueue_generation == 1
        assert refreshed.enqueue_state == "enqueued"

    async def test_stale_enqueued_job_with_live_task_is_not_duplicated(
        self, repo, backend,
    ):
        rec = await _seed(repo)
        await repo.mark_enqueued(
            rec.job_id, f"inline/ticket-{rec.job_id}-g0",
        )
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["updated_at"] = utcnow() - timedelta(minutes=2)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        queue = FakeQueue(task_alive=True)

        counts = await TicketReconciler(repo, queue).run_once()

        refreshed = await repo.get(rec.job_id)
        assert counts["requeued_outbox"] == 0
        assert queue.exists_checks == [(rec.job_id, 0)]
        assert queue.enqueued == []
        assert refreshed.enqueue_generation == 0

    async def test_task_lookup_failure_does_not_bump_or_false_confirm(
        self, repo, backend,
    ):
        rec = await _seed(repo)
        await repo.mark_enqueued(
            rec.job_id, f"inline/ticket-{rec.job_id}-g0",
        )
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["updated_at"] = utcnow() - timedelta(minutes=2)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        queue = FakeQueue(task_exists_error=TimeoutError("unavailable"))

        counts = await TicketReconciler(repo, queue).run_once()

        refreshed = await repo.get(rec.job_id)
        assert counts["errors"] == 1
        assert queue.enqueued == []
        assert refreshed.enqueue_generation == 0
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
        epoch = await repo.claim(rec.job_id, worker_id="w")
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["job_deadline_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        counts = await TicketReconciler(repo, FakeQueue()).run_once()
        assert counts["deadline_terminalized"] == 1
        refreshed = await repo.get(rec.job_id)
        assert refreshed.state == TicketJobState.TIMEOUT
        assert refreshed.next_action.value == "use_legacy_or_human"
        assert refreshed.lease_epoch > epoch
        assert refreshed.lease_owner is None
        assert refreshed.claimed_by is None
        assert await repo.count_active("n8n") == 0

    async def test_reconciler_terminalizes_missing_payload(self, repo, backend):
        rec = await _seed(repo)
        await repo.claim(rec.job_id, worker_id="w")
        # payload desaparece (TTL) mientras el control sigue vivo
        backend._data[PAYLOADS_COLLECTION].pop(rec.job_id, None)

        counts = await TicketReconciler(repo, FakeQueue()).run_once()
        assert counts["payload_expired"] == 1
        refreshed = await repo.get(rec.job_id)
        assert refreshed.state == TicketJobState.FAILED
        assert refreshed.lease_owner is None
        assert refreshed.claimed_by is None
        assert await repo.count_active("n8n") == 0

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

    async def test_terminal_tombstones_cannot_starve_pending_jobs(self, repo):
        for _ in range(25):
            terminal = await _seed(repo)
            await repo.update(terminal.job_id, state=TicketJobState.RUNNING)
            await repo.update(terminal.job_id, state=TicketJobState.SUCCEEDED)
        pending = await _seed(repo)

        queue = FakeQueue()
        counts = await TicketReconciler(
            repo, queue, batch_size=25,
        ).run_once()

        assert counts["requeued_outbox"] == 1
        assert queue.enqueued == [(pending.job_id, 0)]

    async def test_scan_cursor_reaches_jobs_after_a_full_healthy_page(
            self, repo, backend):
        for job_id in ("a-healthy", "b-healthy"):
            healthy = await _seed(repo, job_id=job_id)
            await repo.mark_enqueued(
                healthy.job_id, f"inline/ticket-{healthy.job_id}-g0",
            )
            await repo.claim(healthy.job_id, worker_id=f"worker-{job_id}")
        pending = await _seed(repo, job_id="z-pending")

        first_queue = FakeQueue()
        first = await TicketReconciler(
            repo, first_queue, batch_size=2, owner="reconciler-first",
        ).run_once()
        next_process_repo = TicketJobRepository(
            backend, retention_days=90, max_outstanding=25,
        )
        second_queue = FakeQueue()
        second = await TicketReconciler(
            next_process_repo, second_queue,
            batch_size=2, owner="reconciler-second",
        ).run_once()

        assert first["requeued_outbox"] == 0
        assert second["requeued_outbox"] == 1
        assert second_queue.enqueued == [(pending.job_id, 0)]

    async def test_heartbeat_after_scan_prevents_stale_lease_fencing(
            self, repo, backend, monkeypatch):
        rec = await _seed(repo)
        epoch = await repo.claim(rec.job_id, worker_id="w-live", lease_s=90)
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        acquire = repo.acquire_recovery_lock

        async def acquire_then_heartbeat(job_id, *, owner, lock_s=120.0):
            acquired = await acquire(job_id, owner=owner, lock_s=lock_s)
            if acquired:
                assert await repo.renew_lease(
                    job_id,
                    worker_id="w-live",
                    lease_epoch=epoch,
                    lease_s=90,
                )
            return acquired

        monkeypatch.setattr(repo, "acquire_recovery_lock",
                            acquire_then_heartbeat)
        queue = FakeQueue()
        counts = await TicketReconciler(repo, queue).run_once()

        current = await repo.get(rec.job_id)
        assert counts["fenced_leases"] == 0
        assert current.state == TicketJobState.RUNNING
        assert current.lease_epoch == epoch
        assert current.lease_owner == "w-live"
        assert queue.enqueued == []

    async def test_new_claim_after_scan_is_not_terminalized_from_stale_snapshot(
            self, repo, backend, monkeypatch):
        rec = await _seed(repo)
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["job_deadline_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        acquire = repo.acquire_recovery_lock

        async def acquire_then_claim(job_id, *, owner, lock_s=120.0):
            acquired = await acquire(job_id, owner=owner, lock_s=lock_s)
            if acquired:
                assert await repo.claim(job_id, worker_id="w-new")
            return acquired

        monkeypatch.setattr(repo, "acquire_recovery_lock", acquire_then_claim)
        counts = await TicketReconciler(repo, FakeQueue()).run_once()

        current = await repo.get(rec.job_id)
        assert counts["deadline_terminalized"] == 0
        assert current.state == TicketJobState.RUNNING
        assert current.lease_owner == "w-new"

    async def test_terminalization_has_no_claimable_gap_after_fencing(
            self, repo, backend, monkeypatch):
        rec = await _seed(repo)
        await repo.claim(rec.job_id, worker_id="w-old")
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["job_deadline_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        fence = repo.fence_and_requeue

        async def fence_then_claim(job_id, **kwargs):
            generation = await fence(job_id, **kwargs)
            assert await repo.claim(job_id, worker_id="w-between")
            return generation

        monkeypatch.setattr(repo, "fence_and_requeue", fence_then_claim)
        await TicketReconciler(repo, FakeQueue()).run_once()

        current = await repo.get(rec.job_id)
        assert not (
            current.state in {
                TicketJobState.SUCCEEDED,
                TicketJobState.PARTIAL,
                TicketJobState.FAILED,
                TicketJobState.TIMEOUT,
                TicketJobState.CANCELLED,
            }
            and (current.lease_owner is not None or current.claimed_by is not None)
        ), "un worker reclamó el job entre fence y terminalización"
