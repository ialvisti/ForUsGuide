"""
Integración del repositorio contra el emulador REAL de Firestore
(plan Tarea 5 Paso 4). Se ejecuta vía scripts/run_firestore_emulator_tests.sh
(emulador fijado por digest de ci/tool-images.env); si el emulador no está
disponible, los casos que hacen RPC se saltan con la razón explícita — nunca
se simula. El contrato puro de la fixture GR sigue ejecutándose localmente.

El emulador prueba la semántica transaccional real del cliente
google-cloud-firestore (retries de transacción, tipos nativos, borrados),
pero NO demuestra IAM, TTL efectivo ni disponibilidad de índices: eso se
valida contra staging real (Tarea 14).
"""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

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
from data_pipeline.durable_document import DurableDocumentValidationError

if TYPE_CHECKING:
    from data_pipeline.ticket_orchestrator import InquiryOutcome

pytestmark = [
    pytest.mark.integration,
]

PAYLOAD_A = {"participant_id": "158948", "plan_id": "580",
             "ticket": {"email_subject": "401k", "email_body": "cash out"}}
PAYLOAD_B = {"participant_id": "999999", "plan_id": "111",
             "ticket": {"email_subject": "otro", "email_body": "different"}}


@pytest.fixture
def require_firestore_emulator():
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip(
            "requiere el emulador de Firestore "
            "(scripts/run_firestore_emulator_tests.sh)"
        )


@pytest.fixture
def backend(require_firestore_emulator):
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


async def _realistic_gr_fixture() -> tuple[InquiryOutcome, dict[str, Any]]:
    """Build a synthetic GR through the production mapping/conversion path."""
    from api.ticket_worker import _entry_from_outcome
    from data_pipeline.ticket_orchestrator import (
        InquiryOutcome,
        OrchestratorDeps,
        TicketOrchestrator,
    )

    orchestrator = TicketOrchestrator(
        OrchestratorDeps(
            rag_engine=None,
            inquiry_router=None,
            llm_router=None,
            forusbots=None,
        ),
        SimpleNamespace(
            TICKET_MAX_RELATED=3,
            TICKET_INQUIRY_BUDGET_S=300.0,
        ),
    )
    diagnostics: dict[str, Any] = {}
    modules, _ = await orchestrator._map_fields(
        [
            {"field": "account_balance", "required": True},
            {"field": "termination_date", "required": True},
        ],
        diagnostics,
    )
    diagnostics["mapped_modules"] = modules

    source_articles = [
        {
            "article_id": f"synthetic-article-{index:02d}",
            "article_title": f"Synthetic retirement guidance {index:02d}",
            "chunk_types_used": "business_rules, steps",
            "relevance": "Synthetic contract coverage",
            "used_info": True,
            "max_score": round(0.99 - index * 0.01, 2),
        }
        for index in range(10)
    ]
    used_chunks = [
        {
            "chunk_id": f"synthetic-chunk-{index:02d}",
            "score": round(0.99 - index * 0.01, 2),
            "chunk_type": "business_rules" if index % 2 == 0 else "steps",
            "chunk_tier": "high",
            "article_id": f"synthetic-article-{index % 10:02d}",
            "article_title": (
                f"Synthetic retirement guidance {index % 10:02d}"
            ),
            "content_preview": f"Synthetic preview {index:02d}",
            "content": (
                f"Synthetic retirement-plan content {index:02d}; "
                "contains no participant data"
            ),
        }
        for index in range(21)
    ]
    outcome = InquiryOutcome(
        inquiry="Synthetic distribution eligibility request",
        topic="distribution",
        route="generate_response",
        record_keeper="Synthetic Record Keeper",
        plan_type="401(k)",
        scrape_status="ok",
        generate_result=SimpleNamespace(
            decision="can_proceed",
            confidence=0.91,
            response={
                "outcome": "can_proceed",
                "response_to_participant": "Synthetic safe response",
            },
            source_articles=source_articles,
            used_chunks=used_chunks,
            coverage_gaps=[],
            metadata={"synthetic": True, "chunks_used": len(used_chunks)},
        ),
        diagnostics=diagnostics,
    )
    return outcome, _entry_from_outcome(0, outcome)


async def test_realistic_gr_fixture_uses_async_production_pipeline() -> None:
    pending_fixture = _realistic_gr_fixture()

    assert inspect.isawaitable(pending_fixture), (
        "la fixture debe atravesar el mapper asíncrono de producción"
    )
    built = await pending_fixture
    assert isinstance(built, tuple) and len(built) == 2, (
        "la fixture debe conservar el outcome previo a su conversión durable"
    )
    outcome, entry = built

    assert len(outcome.generate_result.source_articles) == 10
    assert len(outcome.generate_result.used_chunks) == 21
    assert outcome.diagnostics["field_mapping"]["deterministic_mapped"] == {
        "account_balance": [
            {"module": "savings_rate", "field": "Account Balance"}
        ],
        "termination_date": [
            {"module": "census", "field": "Termination Date"}
        ],
    }
    durable_gr = entry["result"]["generate_response"]
    assert len(durable_gr["source_articles"]) == 10
    assert durable_gr["used_chunks"] == []
    assert durable_gr["metadata"]["chunks_used"] == 21


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
        # una escritura tardía queda fenced y tampoco vuelve a liberar cuota
        with pytest.raises(StaleLeaseEpoch):
            await repo.record_inquiry_result(rec.job_id, 0, {
                "execution_status": "succeeded",
                "participant_reply_safe": True,
            })
        counter = await backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal))
        assert counter is None, (
            "el contador debe eliminarse al volver atómicamente a cero"
        )


class TestPollingAndDurability:

    async def test_realistic_gr_checkpoint_with_multiple_mappings_roundtrips(
            self, repo, unique_key):
        rec, _ = await _create(repo, unique_key)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)
        outcome, entry = await _realistic_gr_fixture()

        await repo.record_inquiry_result(rec.job_id, 0, entry)
        found = await repo.get(rec.job_id)

        assert len(outcome.generate_result.source_articles) == 10
        assert len(outcome.generate_result.used_chunks) == 21
        persisted = found.per_inquiry_status[0]
        assert persisted["result"]["generate_response"]["response"][
            "response_to_participant"
        ]
        assert len(
            persisted["result"]["diagnostics"]["field_mapping"][
                "deterministic_mapped"
            ]
        ) == 2
        assert len(
            persisted["result"]["generate_response"]["source_articles"]
        ) == 10
        # `_entry_from_outcome` deliberately minimizes bulky chunks before
        # Firestore, while metadata preserves the synthetic input cardinality.
        assert persisted["result"]["generate_response"]["used_chunks"] == []
        assert persisted["result"]["generate_response"]["metadata"][
            "chunks_used"
        ] == 21

    async def test_twenty_consecutive_synthetic_gr_executions_terminalize(
            self, repo, unique_key):
        terminal_states = []
        for case_number in range(20):
            principal = f"synthetic-gr-{unique_key[:12]}-{case_number}"
            rec, _ = await _create(
                repo,
                f"{unique_key}-gr-{case_number}",
                principal=principal,
            )
            epoch = await repo.claim(rec.job_id, worker_id="synthetic-worker")
            assert epoch is not None

            for operation, fingerprint_seed in (
                ("participant", "a"),
                ("plan", "b"),
            ):
                decision = await repo.prepare_forusbots_operation(
                    rec.job_id,
                    0,
                    operation=operation,
                    request_fingerprint=fingerprint_seed * 64,
                    worker_id="synthetic-worker",
                    lease_epoch=epoch,
                    route="generate_response",
                )
                assert decision.action == "submit"
                await repo.record_forusbots_external_job(
                    rec.job_id,
                    0,
                    operation=operation,
                    external_job_id=(
                        f"synthetic-{operation}-{case_number:02d}"
                    ),
                    worker_id="synthetic-worker",
                    lease_epoch=epoch,
                )

            _outcome, entry = await _realistic_gr_fixture()
            await repo.record_inquiry_result(
                rec.job_id,
                0,
                entry,
                lease_epoch=epoch,
            )
            await repo.update(
                rec.job_id,
                state=TicketJobState.SUCCEEDED,
                expected_lease_epoch=epoch,
            )
            found = await repo.get(rec.job_id)
            assert found is not None
            assert len(found.forusbots_job_ids) == 2
            assert found.public_error_code is None
            terminal_states.append(found.state)

        assert terminal_states == [TicketJobState.SUCCEEDED] * 20

    async def test_legacy_nested_array_shape_is_rejected_before_firestore_write(
            self, repo, unique_key):
        rec, _ = await _create(repo, unique_key)
        await repo.update(rec.job_id, state=TicketJobState.RUNNING)

        with pytest.raises(DurableDocumentValidationError):
            await repo.record_inquiry_result(rec.job_id, 0, {
                "route": "generate_response",
                "execution_status": "succeeded",
                "participant_reply_safe": True,
                "result": {
                    "diagnostics": {
                        "field_mapping": {
                            "deterministic_mapped": {
                                "account_balance": [
                                    ["savings_rate", "Account Balance"]
                                ]
                            }
                        }
                    }
                },
            })

        found = await repo.get(rec.job_id)
        assert found.per_inquiry_status == []

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


class TestReconcilerCursorLive:

    async def test_state_in_document_id_cursor_paginates_without_starvation(
            self, unique_key, require_firestore_emulator):
        """Exercise the exact reconciler query against the emulator.

        Firestore must accept ``state in [queued, running]`` combined with
        ``order_by(__name__)`` and a document-ID cursor.  The terminal record
        is deliberately interleaved by random ID and must never enter either
        page.
        """
        backend = FirestoreTicketJobBackend(
            project=os.environ.get(
                "FIRESTORE_PROJECT_ID", "handle-ticket-emulator"
            ),
            database="(default)",
            collection_prefix=f"cursor_{unique_key}_",
        )
        repo = TicketJobRepository(backend, rate_limit_per_minute=0)
        created = []
        for index in range(5):
            record, outcome = await _create(
                repo,
                f"{unique_key}-{index}",
                principal=f"cursor-{unique_key}",
            )
            assert outcome == CreateOrGetOutcome.CREATED
            created.append(record.job_id)

        await repo.update(created[1], state=TicketJobState.RUNNING)
        await repo.update(created[3], state=TicketJobState.RUNNING)
        await repo.update(created[4], state=TicketJobState.RUNNING)
        await repo.update(created[4], state=TicketJobState.SUCCEEDED)
        expected_active = sorted(created[:4])

        first = await repo.scan_control_docs(limit=2)
        second_process = TicketJobRepository(
            backend, rate_limit_per_minute=0
        )
        second = await second_process.scan_control_docs(limit=2)

        assert [job_id for job_id, _ in first] == expected_active[:2]
        assert [job_id for job_id, _ in second] == expected_active[2:]
        assert {
            document["state"] for _, document in first + second
        } == {TicketJobState.QUEUED.value, TicketJobState.RUNNING.value}
        assert created[4] not in {
            job_id for job_id, _ in first + second
        }


class TestDatabaseSelection:

    def test_named_database_is_mandatory(self):
        with pytest.raises(ValueError, match="base nombrada"):
            FirestoreTicketJobBackend(project="handle-ticket-emulator",
                                      database="")
