"""
Contract tests del repositorio durable de ticket jobs (Task 3 del plan).

Definen la semántica que la implementación Firestore debe cumplir; se
ejecutan contra el backend in-memory que comparte la misma lógica
transaccional. La implementación real de Firestore es una capa delgada
sobre el mismo repositorio (documentado en el plan: lo NO verificado
localmente es el emulador/servicio Firestore real).
"""

from __future__ import annotations

import asyncio

import pytest

from data_pipeline.ticket_job_models import (
    CreateOrGetOutcome,
    TicketJobState,
    fingerprint_request,
    new_job_record,
    utcnow,
)
from data_pipeline.ticket_job_repository import (
    JOBS_COLLECTION,
    PAYLOADS_COLLECTION,
    InMemoryTicketJobBackend,
    IdempotencyReceiptOrphaned,
    InvalidStateTransition,
    TicketJobError,
    TicketJobRepository,
    split_record,
)


def test_idempotency_key_scope_is_shared_across_api_versions():
    """Fallback/migración v1↔v2 no puede duplicar el mismo evento lógico."""
    from data_pipeline.ticket_job_models import hash_idempotency_key

    assert hash_idempotency_key("n8n", "event-123", "v1") == \
        hash_idempotency_key("n8n", "event-123", "v2")


def test_request_fingerprint_is_wire_compatible_across_v1_and_v2():
    """La key compartida no sirve si el fingerprint considera el único
    campo de rollout que existe sólo en el wire v1."""
    logical = {
        "participant_id": "158948",
        "plan_id": "580",
        "ticket": {"email_subject": "401k", "email_body": "cash out"},
    }
    v1 = {**logical, "ticket_handler_mode": None}
    v2 = dict(logical)

    assert fingerprint_request(v1) == fingerprint_request(v2)


def test_control_document_contains_no_raw_tenant_ticket_or_forusbots_ids():
    record = _record(
        tenant_id="customer-tenant-raw",
        ticket_id="ticket-raw-123",
        forusbots_job_ids=["forusbots-raw-456"],
        request_payload=PAYLOAD_A,
    )

    control, payload = split_record(record)

    control_text = repr(control)
    assert "customer-tenant-raw" not in control_text
    assert "ticket-raw-123" not in control_text
    assert "forusbots-raw-456" not in control_text
    assert control["tenant_id_hash"]
    assert payload["tenant_id"] == "customer-tenant-raw"
    assert payload["ticket_id"] == "ticket-raw-123"
    assert payload["forusbots_job_ids"] == ["forusbots-raw-456"]


PAYLOAD_A = {"participant_id": "158948", "plan_id": "580",
             "ticket": {"email_subject": "401k", "email_body": "cash out"}}
PAYLOAD_B = {"participant_id": "999999", "plan_id": "111",
             "ticket": {"email_subject": "otro", "email_body": "different"}}


@pytest.fixture
def backend():
    return InMemoryTicketJobBackend()


@pytest.fixture
def repo(backend):
    return TicketJobRepository(backend)


def _record(principal="n8n", payload=None, **over):
    payload = payload if payload is not None else PAYLOAD_A
    return new_job_record(
        principal_id=principal,
        request_fingerprint=fingerprint_request(payload),
        total_inquiries=None,
        **over,
    )


async def _create(repo, key="key-1", principal="n8n", payload=None):
    payload = payload if payload is not None else PAYLOAD_A
    return await repo.create_or_get(
        principal_id=principal,
        idempotency_key=key,
        request_fingerprint=fingerprint_request(payload),
        candidate=_record(principal=principal, payload=payload),
    )


class TestCreateOrGet:

    async def test_create_or_get_creates_then_replays(self, repo):
        rec1, outcome1 = await _create(repo)
        assert outcome1 == CreateOrGetOutcome.CREATED
        assert rec1.state == TicketJobState.QUEUED

        rec2, outcome2 = await _create(repo)
        assert outcome2 == CreateOrGetOutcome.REPLAYED
        assert rec2.job_id == rec1.job_id

    async def test_same_key_different_fingerprint_conflicts(self, repo):
        _, outcome1 = await _create(repo, payload=PAYLOAD_A)
        assert outcome1 == CreateOrGetOutcome.CREATED
        rec2, outcome2 = await _create(repo, payload=PAYLOAD_B)
        assert outcome2 == CreateOrGetOutcome.CONFLICT
        assert rec2 is None or rec2.job_id is not None  # nunca cruza payloads

    async def test_idempotency_is_scoped_by_principal(self, repo):
        rec_a, out_a = await _create(repo, principal="n8n")
        rec_b, out_b = await _create(repo, principal="ops")
        assert out_a == CreateOrGetOutcome.CREATED
        assert out_b == CreateOrGetOutcome.CREATED, (
            "la misma key de otro principal reutilizó el job ajeno"
        )
        assert rec_a.job_id != rec_b.job_id

    async def test_idempotency_replay_is_bound_to_tenant(self, repo):
        """Un remapeo de principal no puede entregar el job del tenant previo.

        ``create_or_get`` cubre la carrera entre el peek optimista del API y
        la transacción: incluso ahí el receipt debe quedar tenant-bound.
        """
        fingerprint = fingerprint_request(PAYLOAD_A)
        candidate_a = _record(payload=PAYLOAD_A, tenant_id="tenant-a")
        created, created_outcome = await repo.create_or_get(
            principal_id="n8n",
            idempotency_key="tenant-bound-key",
            request_fingerprint=fingerprint,
            candidate=candidate_a,
        )
        assert created_outcome == CreateOrGetOutcome.CREATED

        candidate_b = _record(payload=PAYLOAD_A, tenant_id="tenant-b")
        replay, replay_outcome = await repo.create_or_get(
            principal_id="n8n",
            idempotency_key="tenant-bound-key",
            request_fingerprint=fingerprint,
            candidate=candidate_b,
        )

        assert replay_outcome == CreateOrGetOutcome.CONFLICT
        assert replay is None
        assert created.tenant_id == "tenant-a"

    async def test_concurrent_create_or_get_single_creation(self, repo):
        results = await asyncio.gather(*[_create(repo) for _ in range(50)])
        outcomes = [o for _, o in results]
        assert outcomes.count(CreateOrGetOutcome.CREATED) == 1
        assert outcomes.count(CreateOrGetOutcome.REPLAYED) == 49
        assert len({r.job_id for r, _ in results}) == 1

    async def test_raw_idempotency_key_is_never_stored(self, repo, backend):
        await _create(repo, key="super-secret-idem-key")
        dump = repr(await backend.dump_all())
        assert "super-secret-idem-key" not in dump

    async def test_orphan_receipt_fails_closed_without_creating_another_job(
            self, repo, backend):
        original, _ = await _create(repo, key="orphan-key")
        backend._data[JOBS_COLLECTION].pop(original.job_id)

        with pytest.raises(IdempotencyReceiptOrphaned):
            await _create(repo, key="orphan-key")
        with pytest.raises(IdempotencyReceiptOrphaned):
            await repo.peek_idempotent(
                principal_id="n8n",
                idempotency_key="orphan-key",
                api_version=original.api_version,
                request_fingerprint=fingerprint_request(PAYLOAD_A),
                tenant_id=None,
            )

        dump = await backend.dump_all()
        assert len(dump[JOBS_COLLECTION]) == 0
        assert await repo.count_active("n8n") == 1


class TestDurabilityAcrossInstances:

    async def test_poll_from_second_repository_instance_finds_job(self, backend):
        repo_a = TicketJobRepository(backend)
        repo_b = TicketJobRepository(backend)     # "otra instancia de Cloud Run"
        rec, _ = await _create(repo_a)
        found = await repo_b.get(rec.job_id)
        assert found is not None
        assert found.job_id == rec.job_id

    async def test_get_authorized_enforces_principal(self, repo):
        rec, _ = await _create(repo, principal="n8n")
        assert await repo.get_authorized(rec.job_id, "n8n") is not None
        assert await repo.get_authorized(rec.job_id, "ops") is None


class TestStateMachine:

    async def test_state_transitions_are_validated(self, repo):
        rec, _ = await _create(repo)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.update(rec.job_id, state=TicketJobState.SUCCEEDED)
        with pytest.raises(InvalidStateTransition):
            await repo.update(rec.job_id, state=TicketJobState.RUNNING)

    async def test_terminal_job_freezes_completed_at(self, repo):
        rec, _ = await _create(repo)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.update(rec.job_id, state=TicketJobState.SUCCEEDED)
        first = await repo.get(rec.job_id)
        assert first.completed_at is not None
        again = await repo.get(rec.job_id)
        assert again.completed_at == first.completed_at

    async def test_per_inquiry_checkpoint_persists_immediately(self, repo, backend):
        rec, _ = await _create(repo)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.record_inquiry_result(rec.job_id, 0, {
            "index": 0, "route": "generate_response",
            "execution_status": "succeeded", "participant_reply_safe": True,
        })
        # otra instancia ve el checkpoint aunque el job siga running
        other = TicketJobRepository(backend)
        found = await other.get(rec.job_id)
        assert found.state == TicketJobState.RUNNING
        assert len(found.per_inquiry_status) == 1
        assert found.per_inquiry_status[0]["execution_status"] == "succeeded"

    async def test_worker_claim_is_exclusive(self, repo):
        rec, _ = await _create(repo)
        claim1 = await repo.claim(rec.job_id, worker_id="task-attempt-1")
        claim2 = await repo.claim(rec.job_id, worker_id="task-attempt-2")
        # claim devuelve el lease_epoch nuevo (fencing, Tarea 6) o None
        assert claim1, "el primer claim debe otorgar un lease_epoch"
        assert claim2 is None, "delivery at-least-once ejecutó el job dos veces"


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 3) — timestamps nativos,
# cuotas atómicas, fencing por lease epoch, deadline absoluto y reconciliador.
# RED hasta cerrar las Tareas 5/6/7.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta

from data_pipeline.ticket_job_models import TicketJobRecord
from data_pipeline.ticket_job_repository import _record_to_doc
from data_pipeline.ticket_job_repository import StaleLeaseEpoch


class TestNativeTimestamps:

    def test_firestore_documents_keep_native_timestamps(self):
        """Bloqueo 2 del plan: los docs se serializan con mode='json' y los
        timestamps llegan a Firestore como strings — el TTL nunca eliminará
        la PII. Deben preservarse datetime nativos."""
        rec = _record()
        doc = _record_to_doc(rec)
        for field_name in ("created_at", "updated_at", "expires_at"):
            value = doc.get(field_name)
            assert isinstance(value, datetime), (
                f"{field_name} se serializó como {type(value).__name__} "
                f"({value!r}); Firestore TTL exige timestamp nativo"
            )

    async def test_idempotency_document_keeps_native_timestamps(self, repo, backend):
        await _create(repo, key="native-ts-key")
        dump = await backend.dump_all()
        idem_docs = [
            doc for coll, docs in dump.items()
            if "idempotency" in coll for doc in docs.values()
        ]
        assert idem_docs, "no se creó documento de idempotencia"
        for doc in idem_docs:
            for field_name in ("created_at", "expires_at"):
                value = doc.get(field_name)
                assert isinstance(value, datetime), (
                    f"idempotency.{field_name} es {type(value).__name__} "
                    f"({value!r}); .isoformat() rompe el TTL de Firestore"
                )


class TestAtomicQuotas:

    async def test_50_concurrent_reservations_consume_one_quota_slot(self, repo, backend):
        """Tarea 5 Paso 2: la cuota de jobs activos debe ser un contador
        transaccional durable (ticket_active_counters), no un count() no
        atómico en el endpoint."""
        results = await asyncio.gather(*[_create(repo) for _ in range(50)])
        assert len({r.job_id for r, _ in results if r is not None}) == 1
        dump = await backend.dump_all()
        counters = {
            coll: docs for coll, docs in dump.items()
            if "active_counter" in coll or coll == "ticket_active_counters"
        }
        assert counters, (
            "RED: no existe la colección ticket_active_counters — la reserva "
            "de cuota no es atómica ni durable (Tarea 5 Paso 2)"
        )
        totals = [
            doc.get("active_jobs") for docs in counters.values()
            for doc in docs.values()
        ]
        assert totals == [1], (
            f"50 reservas concurrentes de la misma key consumieron {totals!r} "
            "slots; deben consumir exactamente uno"
        )


class TestLeaseFencing:

    async def test_forusbots_submit_intent_survives_lease_loss_and_blocks_resubmit(
            self, repo, backend):
        """El lease fencea escrituras, pero no puede deshacer un POST que ya
        salió. El intent durable debe sobrevivir al cambio de epoch y hacer
        que el nuevo worker prefiera reconciliación manual a otro submit.
        """
        from datetime import timedelta

        rec, _ = await _create(repo, key="forusbots-intent-fence")
        old_epoch = await repo.claim(rec.job_id, worker_id="worker-old")
        assert await repo.reserve_forusbots_submit_intent(
            rec.job_id,
            0,
            worker_id="worker-old",
            lease_epoch=old_epoch,
            route="generate_response",
        ) is True

        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control
        new_epoch = await repo.claim(rec.job_id, worker_id="worker-new")
        assert new_epoch > old_epoch

        assert await repo.reserve_forusbots_submit_intent(
            rec.job_id,
            0,
            worker_id="worker-new",
            lease_epoch=new_epoch,
            route="generate_response",
        ) is False
        current = await repo.get(rec.job_id)
        intent = next(e for e in current.per_inquiry_status
                      if e.get("index") == 0)
        assert intent["forusbots_submit_intent"] is True
        assert intent["execution_status"] == "running"

    async def test_heartbeat_cannot_resurrect_an_expired_lease(
            self, repo, backend):
        """Un heartbeat tardío debe quedar fenced. Renovar un lease ya
        vencido abre una ventana donde el worker viejo vuelve a ejecutar
        efectos externos antes de que el reconciliador lo observe."""
        rec, _ = await _create(repo, key="expired-heartbeat")
        epoch = await repo.claim(rec.job_id, worker_id="w-old")
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        expired_at = utcnow() - timedelta(seconds=1)
        control["lease_expires_at"] = expired_at
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        renewed = await repo.renew_lease(
            rec.job_id,
            worker_id="w-old",
            lease_epoch=epoch,
        )

        assert renewed is False
        current = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        assert current["lease_expires_at"] == expired_at

    async def test_stale_task_confirmation_cannot_mark_new_generation_enqueued(
            self, repo):
        rec, _ = await _create(repo)
        assert await repo.bump_enqueue_generation(rec.job_id) == 1
        assert await repo.bump_enqueue_generation(rec.job_id) == 2

        with pytest.raises(TicketJobError, match="generación"):
            await repo.mark_enqueued(
                rec.job_id,
                f"inline/ticket-{rec.job_id}-g1",
            )

        current = await repo.get(rec.job_id)
        assert current.enqueue_generation == 2
        assert current.enqueue_state == "pending"
        assert current.task_name is None

    async def test_stale_lease_epoch_cannot_checkpoint_or_publish(self, repo):
        """Tarea 6 Paso 4a: cada claim incrementa lease_epoch y toda escritura
        condicional lo incluye; un worker viejo queda fenced."""
        assert "lease_epoch" in TicketJobRecord.model_fields, (
            "RED: TicketJobRecord.lease_epoch no existe (Tarea 6 Paso 4a)"
        )
        rec, _ = await _create(repo)
        assert await repo.claim(rec.job_id, worker_id="w-old")
        old = await repo.get(rec.job_id)
        old_epoch = old.lease_epoch
        # el reconciliador/nuevo worker fencea al viejo con otro epoch
        await repo.update(rec.job_id, state=TicketJobState.QUEUED,
                          claimed_by=None, claimed_at=None)
        assert await repo.claim(rec.job_id, worker_id="w-new")
        fresh = await repo.get(rec.job_id)
        assert fresh.lease_epoch > old_epoch, "el claim no incrementó el epoch"
        with pytest.raises(Exception):
            await repo.record_inquiry_result(
                rec.job_id, 0,
                {"execution_status": "succeeded",
                 "participant_reply_safe": True},
                lease_epoch=old_epoch,
            )

    async def test_matching_epoch_cannot_checkpoint_terminal_job(self, repo):
        rec, _ = await _create(repo, key="terminal-checkpoint")
        epoch = await repo.claim(rec.job_id, worker_id="w")
        await repo.update(
            rec.job_id,
            state=TicketJobState.SUCCEEDED,
            expected_lease_epoch=epoch,
        )

        with pytest.raises(StaleLeaseEpoch, match="terminal"):
            await repo.record_inquiry_result(
                rec.job_id,
                0,
                {"execution_status": "succeeded"},
                lease_epoch=epoch,
            )

    async def test_matching_epoch_cannot_checkpoint_expired_lease(
            self, repo, backend):
        rec, _ = await _create(repo, key="expired-checkpoint")
        epoch = await repo.claim(rec.job_id, worker_id="w")
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        with pytest.raises(StaleLeaseEpoch, match="vencido"):
            await repo.record_inquiry_result(
                rec.job_id,
                0,
                {"execution_status": "succeeded"},
                lease_epoch=epoch,
            )

    async def test_active_lease_assertion_rejects_terminal_and_expired(
            self, repo, backend):
        expired, _ = await _create(repo, key="assert-expired")
        expired_epoch = await repo.claim(expired.job_id, worker_id="w-expired")
        control = await backend.get_doc(JOBS_COLLECTION, expired.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][expired.job_id] = control
        with pytest.raises(StaleLeaseEpoch, match="vencido"):
            await repo.assert_active_lease(
                expired.job_id,
                worker_id="w-expired",
                lease_epoch=expired_epoch,
            )

        terminal, _ = await _create(repo, key="assert-terminal")
        terminal_epoch = await repo.claim(
            terminal.job_id, worker_id="w-terminal",
        )
        await repo.update(
            terminal.job_id,
            state=TicketJobState.SUCCEEDED,
            expected_lease_epoch=terminal_epoch,
        )
        with pytest.raises(StaleLeaseEpoch, match="terminal"):
            await repo.assert_active_lease(
                terminal.job_id,
                worker_id="w-terminal",
                lease_epoch=terminal_epoch,
            )

    async def test_conditional_update_rejects_terminal_or_expired_lease(
            self, repo, backend):
        expired, _ = await _create(repo, key="update-expired")
        expired_epoch = await repo.claim(expired.job_id, worker_id="w-expired")
        control = await backend.get_doc(JOBS_COLLECTION, expired.job_id)
        control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][expired.job_id] = control
        with pytest.raises(StaleLeaseEpoch, match="vencido"):
            await repo.update(
                expired.job_id,
                expected_lease_epoch=expired_epoch,
                current_step="publish",
            )

        terminal, _ = await _create(repo, key="update-terminal")
        terminal_epoch = await repo.claim(
            terminal.job_id, worker_id="w-terminal",
        )
        await repo.update(
            terminal.job_id,
            state=TicketJobState.SUCCEEDED,
            expected_lease_epoch=terminal_epoch,
        )
        with pytest.raises(StaleLeaseEpoch, match="terminal"):
            await repo.update(
                terminal.job_id,
                expected_lease_epoch=terminal_epoch,
                current_step="publish-again",
            )

    async def test_recovery_lock_requeues_without_owning_worker_lease(self):
        """Tarea 7 Paso 5: el reconciliador usa un recovery_lock separado y
        NUNCA conserva el lease de ejecución del worker."""
        try:
            from data_pipeline import ticket_reconciler  # noqa: F401
        except ImportError:
            pytest.fail(
                "RED: data_pipeline.ticket_reconciler no existe (Tarea 7 "
                "Paso 5) — no hay reconciliación automática de outbox/leases"
            )


class TestAbsoluteDeadline:

    async def test_absolute_job_deadline_terminalizes_late_deliveries(self, repo):
        """Tarea 7 Paso 1: job_deadline_at=accepted_at+2400s; un GET/worker
        tardío terminaliza por CAS y libera cuota exactamente una vez."""
        assert "job_deadline_at" in TicketJobRecord.model_fields, (
            "RED: TicketJobRecord.job_deadline_at no existe (Tarea 7 Paso 1)"
        )

    async def test_reconciler_repairs_pending_outbox_and_stale_lease(self):
        try:
            from data_pipeline.ticket_reconciler import TicketReconciler  # noqa: F401
        except ImportError:
            pytest.fail(
                "RED: data_pipeline.ticket_reconciler.TicketReconciler no "
                "existe (Tarea 7 Paso 5) — outbox pending y leases vencidos "
                "no se reparan sin CLI manual"
            )


class TestAtomicLazyTerminalization:

    async def test_deadline_and_missing_payload_terminalize_atomically(
            self, repo, backend):
        deadline, _ = await _create(repo, key="lazy-deadline")
        deadline_epoch = await repo.claim(
            deadline.job_id, worker_id="w-deadline",
        )
        control = await backend.get_doc(JOBS_COLLECTION, deadline.job_id)
        control["job_deadline_at"] = utcnow() - timedelta(seconds=1)
        backend._data[JOBS_COLLECTION][deadline.job_id] = control

        timed_out = await repo.terminalize_if_unrecoverable(
            deadline.job_id, now=utcnow(),
        )
        assert timed_out.state == TicketJobState.TIMEOUT
        assert timed_out.public_error_code == "TOTAL_JOB_TIMEOUT"
        assert timed_out.lease_epoch > deadline_epoch
        assert timed_out.lease_owner is None

        missing, _ = await _create(repo, key="lazy-missing")
        await repo.claim(missing.job_id, worker_id="w-missing")
        backend._data[PAYLOADS_COLLECTION].pop(missing.job_id)

        failed = await repo.terminalize_if_unrecoverable(
            missing.job_id, now=utcnow(),
        )
        assert failed.state == TicketJobState.FAILED
        assert failed.public_error_code == "EXPIRED_PAYLOAD"
        assert failed.lease_owner is None
        assert await repo.count_active("n8n") == 0

    async def test_healthy_job_is_returned_without_mutation(self, repo):
        rec, _ = await _create(repo, key="lazy-healthy")
        current = await repo.terminalize_if_unrecoverable(
            rec.job_id, now=utcnow(),
        )
        assert current.state == TicketJobState.QUEUED
        assert current.lease_epoch == rec.lease_epoch
        assert await repo.count_active("n8n") == 1
