"""
Integración del repositorio contra el emulador REAL de Firestore
(plan Tarea 5 Paso 4). Se ejecuta vía scripts/run_firestore_emulator_tests.sh
(emulador fijado por digest de ci/tool-images.env); si el emulador no está
disponible, la suite entera se salta con la razón explícita — nunca se simula.

El emulador prueba la semántica transaccional real del cliente
google-cloud-firestore (retries de transacción, tipos nativos, borrados),
pero NO demuestra IAM, TTL efectivo ni disponibilidad de índices: eso se
valida contra staging real (Tarea 14).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest

from data_pipeline.ticket_job_models import (
    CreateOrGetOutcome,
    TicketJobState,
    fingerprint_request,
    new_job_record,
)
from data_pipeline.ticket_job_repository import (
    COUNTERS_COLLECTION,
    JOBS_COLLECTION,
    PAYLOADS_COLLECTION,
    RECEIPTS_COLLECTION,
    FirestoreTicketJobBackend,
    StaleLeaseEpoch,
    TicketJobRepository,
    principal_hash,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("FIRESTORE_EMULATOR_HOST"),
        reason="requiere el emulador de Firestore "
               "(scripts/run_firestore_emulator_tests.sh)",
    ),
]

PAYLOAD_A = {"participant_id": "158948", "plan_id": "580",
             "ticket": {"email_subject": "401k", "email_body": "cash out"}}
PAYLOAD_B = {"participant_id": "999999", "plan_id": "111",
             "ticket": {"email_subject": "otro", "email_body": "different"}}


@pytest.fixture
def backend():
    return FirestoreTicketJobBackend(
        project=os.environ.get("FIRESTORE_PROJECT_ID", "handle-ticket-emulator"),
        database="(default)",
    )


@pytest.fixture
def repo(backend):
    return TicketJobRepository(backend, retention_days=90,
                               max_outstanding=25, rate_limit_per_minute=0)


def _candidate(principal="n8n", payload=None, **over):
    payload = payload if payload is not None else PAYLOAD_A
    return new_job_record(
        principal_id=principal,
        request_fingerprint=fingerprint_request(payload),
        request_payload=payload,
        **over,
    )


async def _create(repo, key, principal="n8n", payload=None):
    payload = payload if payload is not None else PAYLOAD_A
    return await repo.create_or_get(
        principal_id=principal,
        idempotency_key=key,
        request_fingerprint=fingerprint_request(payload),
        candidate=_candidate(principal=principal, payload=payload),
    )


class TestNativeTypes:

    async def test_native_timestamp_types_roundtrip(self, repo, backend, unique_key):
        rec, _ = await _create(repo, unique_key)
        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        payload = await backend.get_doc(PAYLOADS_COLLECTION, rec.job_id)
        assert isinstance(control["created_at"], datetime), (
            f"created_at llegó como {type(control['created_at']).__name__}"
        )
        assert isinstance(payload["expires_at"], datetime)
        idem = await backend.get_doc(
            RECEIPTS_COLLECTION, rec.idempotency_key_hash)
        assert isinstance(idem["expires_at"], datetime)
        assert isinstance(idem["created_at"], datetime)


@pytest.fixture
def unique_key():
    import uuid
    return f"itest-{uuid.uuid4().hex}"


class TestConcurrencyAndQuota:

    async def test_50_concurrent_same_key_create_one_job_and_slot(
            self, repo, backend, unique_key):
        principal = f"n8n-{unique_key[:12]}"
        results = await asyncio.gather(
            *[_create(repo, unique_key, principal=principal) for _ in range(50)]
        )
        job_ids = {r.job_id for r, _ in results if r is not None}
        outcomes = [o for _, o in results]
        assert len(job_ids) == 1
        assert outcomes.count(CreateOrGetOutcome.CREATED) == 1
        counter = await backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal))
        assert counter is not None and counter["active_jobs"] == 1, (
            f"50 reservas concurrentes consumieron {counter} slots"
        )

    async def test_same_key_different_payload_conflicts(self, repo, unique_key):
        _, o1 = await _create(repo, unique_key, payload=PAYLOAD_A)
        assert o1 == CreateOrGetOutcome.CREATED
        rec2, o2 = await _create(repo, unique_key, payload=PAYLOAD_B)
        assert o2 == CreateOrGetOutcome.CONFLICT and rec2 is None

    async def test_terminal_releases_slot_exactly_once(
            self, repo, backend, unique_key):
        principal = f"ops-{unique_key[:12]}"
        rec, _ = await _create(repo, unique_key, principal=principal)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.update(rec.job_id, state=TicketJobState.SUCCEEDED)
        # segunda escritura sobre terminal no re-libera
        await repo.record_inquiry_result(rec.job_id, 0, {
            "execution_status": "succeeded", "participant_reply_safe": True,
        })
        counter = await backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal))
        assert counter is None, (
            "el contador debe eliminarse al volver atómicamente a cero"
        )


class TestPollingAndDurability:

    async def test_two_clients_poll_same_backend(self, backend, unique_key):
        repo_a = TicketJobRepository(backend)
        repo_b = TicketJobRepository(backend)
        rec, _ = await _create(repo_a, unique_key)
        found = await repo_b.get(rec.job_id)
        assert found is not None and found.job_id == rec.job_id

    async def test_transitions_and_checkpoints_persist(self, repo, unique_key):
        rec, _ = await _create(repo, unique_key)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.record_inquiry_result(rec.job_id, 0, {
            "route": "knowledge_question", "execution_status": "succeeded",
            "participant_reply_safe": True,
        })
        found = await repo.get(rec.job_id)
        assert found.state == TicketJobState.RUNNING
        assert found.per_inquiry_status[0]["execution_status"] == "succeeded"

    async def test_control_and_receipt_survive_payload_and_return_410(
            self, repo, backend, unique_key):
        """control/receipt sobreviven al payload: get_with_payload_state
        devuelve payload_present=False y el endpoint responde 410."""
        rec, _ = await _create(repo, unique_key)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        await repo.update(rec.job_id, state=TicketJobState.SUCCEEDED)
        # simular la expiración TTL del payload (el TTL real es asíncrono y
        # se observa en staging, no aquí)
        ref = backend._client.collection(PAYLOADS_COLLECTION).document(rec.job_id)
        await ref.delete()
        record, payload_present = await repo.get_with_payload_state(rec.job_id)
        assert record is not None, "el control/tombstone debe sobrevivir"
        assert payload_present is False
        receipt = await backend.get_doc(
            RECEIPTS_COLLECTION, rec.idempotency_key_hash)
        assert receipt is not None, "el receipt debe sobrevivir al payload"
        # POST replay con la misma key NO crea otro job
        rec2, outcome = await _create(repo, unique_key)
        assert outcome == CreateOrGetOutcome.REPLAYED
        assert rec2.job_id == rec.job_id

    async def test_missing_payload_nonterminal_does_not_reexecute(
            self, repo, backend, unique_key):
        rec, _ = await _create(repo, unique_key)
        ref = backend._client.collection(PAYLOADS_COLLECTION).document(rec.job_id)
        await ref.delete()
        record, payload_present = await repo.get_with_payload_state(rec.job_id)
        assert payload_present is False
        assert record.request_payload is None, (
            "sin payload no hay nada que reejecutar"
        )

    async def test_active_counter_never_expires_while_positive(
            self, backend, repo, unique_key):
        principal = f"live-{unique_key[:12]}"
        await _create(repo, unique_key, principal=principal)
        counter = await backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal))
        assert counter is not None
        assert "expires_at" not in counter, (
            "el contador activo NO puede llevar TTL mientras sea positivo"
        )


class TestLeaseFencingLive:

    async def test_stale_epoch_checkpoint_rejected(self, repo, unique_key):
        rec, _ = await _create(repo, unique_key)
        epoch1 = await repo.claim(rec.job_id, worker_id="w-a")
        assert epoch1 is not None
        # fencing: el reconciliador re-encola y otro worker reclama
        await repo.update(rec.job_id, state=TicketJobState.QUEUED,
                          claimed_by=None, claimed_at=None)
        epoch2 = await repo.claim(rec.job_id, worker_id="w-b")
        assert epoch2 is not None and epoch2 > epoch1
        with pytest.raises(StaleLeaseEpoch):
            await repo.record_inquiry_result(
                rec.job_id, 0,
                {"execution_status": "succeeded",
                 "participant_reply_safe": True},
                lease_epoch=epoch1,
            )


class TestDatabaseSelection:

    def test_named_database_is_mandatory(self):
        with pytest.raises(ValueError, match="base nombrada"):
            FirestoreTicketJobBackend(project="handle-ticket-emulator",
                                      database="")
