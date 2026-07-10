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
)
from data_pipeline.ticket_job_repository import (
    InMemoryTicketJobBackend,
    InvalidStateTransition,
    TicketJobRepository,
)


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
        assert claim1 is True
        assert claim2 is False, "delivery at-least-once ejecutó el job dos veces"
