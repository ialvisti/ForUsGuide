"""
Tests del endpoint interno del worker (Task 4 del plan).

La ejecución durable en sí (checkpoints, timeouts, agregación, claims) se
prueba en tests/test_handle_ticket_endpoint.py::TestRunTicketJob y
TestDurabilityRegressions; aquí se cubre la superficie HTTP que invoca
Cloud Tasks: autenticación OIDC fail-closed, 404 para jobs desconocidos y
tolerancia a delivery duplicado.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("PINECONE_API_KEY", "p")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    from api.config import settings as app_settings
    monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
    # Estos tests ejercitan la superficie HTTP del worker: el rol de proceso
    # debe ser `worker` para que /internal/tasks/ticket-job exista (Tarea 4
    # Paso 1a; el producer la oculta con 404).
    monkeypatch.setattr(app_settings, "APP_ROLE", "worker")

    mock_engine = Mock()
    mock_pinecone = Mock()
    mock_pinecone.get_index_stats.return_value = {"total_vectors": 0}
    mock_inquiry_router = Mock()

    with patch("api.main.validate_settings"), \
         patch("api.main.RAGEngine", return_value=mock_engine), \
         patch("api.main.PineconeUploader", return_value=mock_pinecone), \
         patch("api.main.InquiryRouterEngine", return_value=mock_inquiry_router):
        from api.main import app
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


class FakeOrch:
    async def extract_inquiries(self, req):
        return [ExtractedInquiry("q", "LT Trust", "401(k)", "general")]

    async def classify(self, inquiry):
        return SimpleNamespace(route="needs_more_info", confidence=0.9,
                               reasoning="r", user_message="msg")

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        return InquiryOutcome(inquiry=ext.inquiry, topic=ext.topic,
                              route="needs_more_info",
                              needs_more_info_message="msg")


async def _seed(client, mode="full"):
    from data_pipeline.ticket_job_models import fingerprint_request, new_job_record
    payload = dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": "401k", "email_body": "quiero retirar mi 401k"},
    )
    repo = client.app.state.ticket_repo
    fp = fingerprint_request(payload)
    rec, _ = await repo.create_or_get(
        principal_id="default", idempotency_key=None, request_fingerprint=fp,
        candidate=new_job_record(principal_id="default", request_fingerprint=fp,
                                 mode=mode, request_payload=payload),
    )
    return rec


class TestWorkerEndpointAuth:

    def test_worker_requires_oidc_when_enabled(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", True)
        r = client.post("/internal/tasks/ticket-job", json={"job_id": "x"})
        assert r.status_code == 401

    def test_worker_rejects_garbage_bearer(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", True)
        r = client.post("/internal/tasks/ticket-job", json={"job_id": "x"},
                        headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 403


class TestWorkerEndpointExecution:

    def test_unknown_job_is_404(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        r = client.post("/internal/tasks/ticket-job", json={"job_id": "nope"})
        assert r.status_code == 404

    def test_executes_job_and_duplicate_delivery_is_200(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        client.app.state.ticket_orchestrator_factory = lambda: FakeOrch()

        # sembrar el job dentro del loop de la app
        record = client.portal.call(_seed, client)

        r1 = client.post("/internal/tasks/ticket-job",
                         json={"job_id": record.job_id},
                         headers={"X-CloudTasks-TaskName": "t1",
                                  "X-CloudTasks-TaskRetryCount": "0"})
        assert r1.status_code == 200
        assert r1.json()["state"] == "succeeded"

        # re-entrega del MISMO task: 200 sin re-ejecutar (claim rechaza)
        r2 = client.post("/internal/tasks/ticket-job",
                         json={"job_id": record.job_id},
                         headers={"X-CloudTasks-TaskName": "t1",
                                  "X-CloudTasks-TaskRetryCount": "1"})
        assert r2.status_code == 200
        assert r2.json()["state"] == "duplicate_delivery"


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 2) — fallos del extractor/
# síntesis y reanudación sin repetir efectos. RED hasta Tareas 6/7.
# ---------------------------------------------------------------------------

import asyncio

from data_pipeline.ticket_job_models import TicketJobRecord


def _worker_app(repo, orch):
    return SimpleNamespace(state=SimpleNamespace(
        ticket_repo=repo,
        ticket_orchestrator_factory=lambda: orch,
        execution_logger=None,
    ))


async def _seed_repo_job(repo, mode="full"):
    from data_pipeline.ticket_job_models import fingerprint_request, new_job_record
    payload = dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": "401k", "email_body": "quiero retirar mi 401k"},
        record_keeper="LT Trust",
    )
    fp = fingerprint_request(payload)
    rec, _ = await repo.create_or_get(
        principal_id="default", idempotency_key=None, request_fingerprint=fp,
        candidate=new_job_record(principal_id="default", request_fingerprint=fp,
                                 mode=mode, request_payload=payload),
    )
    return rec


def _real_orchestrator(llm_router, inquiry_router=None):
    """TicketOrchestrator REAL (no FakeOrch): estas regresiones apuntan al
    manejo de fallos del propio orquestador, no al glue del worker."""
    from api.config import settings as app_settings
    from data_pipeline.ticket_orchestrator import (
        OrchestratorDeps, TicketOrchestrator,
    )
    deps = OrchestratorDeps(
        rag_engine=Mock(),
        inquiry_router=inquiry_router or Mock(),
        llm_router=llm_router,
        forusbots=Mock(),
    )
    return TicketOrchestrator(deps, app_settings)


class _FailingRouter:
    """Proveedor LLM caído: toda llamada lanza."""

    async def call(self, route, system, user, max_tokens=None):
        raise RuntimeError("LLM provider 500")


class _GarbageRouter:
    """Proveedor responde texto que no es JSON (sin ningún array embebido)."""

    async def call(self, route, system, user, max_tokens=None):
        return SimpleNamespace(content="sorry, I cannot help with that request")


class _ExtractOkSynthFailRouter:
    """extract_inquiries válido; kb_question_synthesis lanza."""

    async def call(self, route, system, user, max_tokens=None):
        if route == "extract_inquiries":
            return SimpleNamespace(content=(
                '[{"inquiry": "what is a 401k rollover?", "topic": "rollover"}]'
            ))
        raise RuntimeError("kq synthesis provider down")


class _KQClassifier:
    async def classify(self, inquiry):
        return SimpleNamespace(route="knowledge_question", confidence=0.9,
                               reasoning="r", user_message=None)


class TestExtractorAndSynthesisFailures:

    async def test_extract_llm_failure_is_failed_not_participant_nmi(self):
        """Bloqueo 4 del plan: un fallo del proveedor en la extracción hoy se
        convierte en [] → saludo succeeded+send_participant_reply. Un fallo
        técnico NUNCA es una respuesta al participante."""
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch = _real_orchestrator(_FailingRouter())

        final = await run_ticket_job(_worker_app(repo, orch), rec.job_id)

        assert final.state.value in ("failed", "timeout"), (
            f"fallo LLM del extractor terminó en {final.state.value!r}: se "
            "publicó un saludo en lugar de fallar"
        )
        assert final.next_action.value != "send_participant_reply"
        assert final.public_error_code is not None

    async def test_invalid_extract_json_is_not_publishable(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch = _real_orchestrator(_GarbageRouter())

        final = await run_ticket_job(_worker_app(repo, orch), rec.job_id)

        assert final.next_action.value != "send_participant_reply", (
            "JSON inválido del extractor produjo un resultado publicable"
        )
        assert final.state.value != "succeeded"

    async def test_kq_synthesis_failure_is_not_publishable(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch = _real_orchestrator(_ExtractOkSynthFailRouter(),
                                  inquiry_router=_KQClassifier())

        final = await run_ticket_job(_worker_app(repo, orch), rec.job_id)

        entries = final.per_inquiry_status
        assert entries, "no hubo checkpoints"
        assert not any(e.get("participant_reply_safe") for e in entries), (
            "un fallo de síntesis KQ quedó marcado participant_reply_safe=True"
        )
        assert final.next_action.value in ("use_legacy", "use_legacy_or_human"), (
            f"fallo de síntesis KQ produjo next_action={final.next_action.value!r}; "
            "debe degradar a legacy/humano, nunca un simple needs_more_info"
        )


class _ResumeOrch:
    """Orquestador con conteo por inquiry y crash opcional en un índice."""

    def __init__(self, inquiries, crash_on=None, forusbots_id="fb-x"):
        self._inquiries = inquiries
        self._crash_on = crash_on
        self._fb = forusbots_id
        self.extract_calls = 0
        self.handled = []

    async def extract_inquiries(self, req):
        self.extract_calls += 1
        return list(self._inquiries)

    async def classify(self, inquiry):
        return SimpleNamespace(route="knowledge_question", confidence=0.9,
                               reasoning="r", user_message=None)

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        if self._crash_on is not None and ext.inquiry == self._crash_on:
            raise asyncio.CancelledError()
        self.handled.append(ext.inquiry)
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="knowledge_question",
            knowledge_result=SimpleNamespace(
                answer="A", key_points=[], source_articles=[], used_chunks=[],
                confidence_note="well_covered", metadata={}),
            diagnostics={"forusbots_job_id": self._fb},
        )


def _two_inquiries():
    return [ExtractedInquiry("inquiry cero", "LT Trust", "401(k)", "rollover"),
            ExtractedInquiry("inquiry uno", "LT Trust", "401(k)", "hardship")]


class TestRetryResumesWithoutRepeatingEffects:

    async def test_retry_skips_completed_inquiry_checkpoint(self):
        """Bloqueo 3 del plan: un retry re-extrae y re-procesa inquiries ya
        completadas, repitiendo efectos LLM/ForusBots."""
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        # checkpoint terminal previo de la inquiry 0 (attempt anterior)
        await repo.record_inquiry_result(rec.job_id, 0, {
            "route": "knowledge_question", "execution_status": "succeeded",
            "participant_reply_safe": True, "degraded": False,
            "result": {"route": "knowledge_question"},
        })
        orch = _ResumeOrch(_two_inquiries())

        await run_ticket_job(_worker_app(repo, orch), rec.job_id)

        assert "inquiry cero" not in orch.handled, (
            "el retry volvió a procesar una inquiry con checkpoint terminal — "
            "efecto repetido"
        )
        assert "inquiry uno" in orch.handled

    async def test_retry_reuses_persisted_execution_plan(self):
        """El plan de ejecución (inquiries extraídas/clasificadas) debe
        persistirse una sola vez y reutilizarse en el retry; hoy ni siquiera
        existe el campo."""
        assert "execution_plan" in TicketJobRecord.model_fields, (
            "RED: TicketJobRecord.execution_plan no existe (Tarea 6 Paso 3)"
        )
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch_a = _ResumeOrch(_two_inquiries(), crash_on="inquiry uno")
        with pytest.raises(asyncio.CancelledError):
            await run_ticket_job(_worker_app(repo, orch_a), rec.job_id,
                                 worker_id="w-a")
        orch_b = _ResumeOrch(
            [ExtractedInquiry("OTRA extracción distinta", "LT", "401(k)", "x")])
        final = await run_ticket_job(_worker_app(repo, orch_b), rec.job_id,
                                     worker_id="w-b")
        assert orch_b.extract_calls == 0, (
            "el retry volvió a llamar extracción/clasificación en lugar de "
            "reutilizar el execution_plan persistido"
        )
        assert final is not None

    async def test_retry_preserves_prior_forusbots_job_ids(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch_a = _ResumeOrch(_two_inquiries(), crash_on="inquiry uno",
                             forusbots_id="fb-1")
        with pytest.raises(asyncio.CancelledError):
            await run_ticket_job(_worker_app(repo, orch_a), rec.job_id,
                                 worker_id="w-a")
        orch_b = _ResumeOrch(_two_inquiries(), forusbots_id="fb-2")
        final = await run_ticket_job(_worker_app(repo, orch_b), rec.job_id,
                                     worker_id="w-b")
        assert "fb-1" in (final.forusbots_job_ids or []), (
            f"IDs finales {final.forusbots_job_ids!r}: la agregación descartó "
            "los ForusBots job IDs persistidos por el attempt anterior"
        )

    async def test_crash_retry_completes_before_queue_attempts_exhaust(self):
        """Relaciones temporales de la Tarea 7 Paso 1: presupuesto de intento,
        lease/heartbeat, dispatch deadline y deadline absoluto del job deben
        existir y ser coherentes con el watch de n8n (2700s)."""
        from api.config import settings as app_settings
        for name in ("TICKET_ATTEMPT_BUDGET_S", "TICKET_JOB_DEADLINE_S",
                     "TICKET_WORKER_LEASE_S", "TICKET_WORKER_HEARTBEAT_S",
                     "TICKET_TASK_DISPATCH_DEADLINE_S"):
            assert hasattr(app_settings, name), (
                f"RED: settings.{name} no existe (Tarea 7 Paso 1)"
            )
        assert app_settings.TICKET_ATTEMPT_BUDGET_S == 480
        assert app_settings.TICKET_JOB_DEADLINE_S == 2400
        assert app_settings.TICKET_WORKER_LEASE_S == 90
        assert app_settings.TICKET_WORKER_HEARTBEAT_S == 30
        assert app_settings.TICKET_TASK_DISPATCH_DEADLINE_S == 540
        # heartbeat renueva al menos 3 veces dentro del lease
        assert app_settings.TICKET_WORKER_HEARTBEAT_S * 3 <= app_settings.TICKET_WORKER_LEASE_S
        # intento < Cloud Run worker timeout (520) < dispatch deadline
        assert (app_settings.TICKET_ATTEMPT_BUDGET_S < 520
                < app_settings.TICKET_TASK_DISPATCH_DEADLINE_S)
        # terminal ≤ deadline + poll(30s) con margen antes del watch n8n 2700s
        assert app_settings.TICKET_JOB_DEADLINE_S + 30 <= 2700 - 240


class TestStaleTaskGeneration:

    def test_stale_task_generation_returns_204_without_claim_or_effect(self, client, monkeypatch):
        """Tarea 7 Paso 3: el body OIDC incluye {job_id, enqueue_generation};
        una generación stale devuelve 204 SIN claim ni efecto (Cloud Tasks
        reintenta cualquier non-2xx, incluso 4xx)."""
        from data_pipeline.ticket_job_models import TicketJobRecord as _Rec
        assert "enqueue_generation" in _Rec.model_fields, (
            "RED: TicketJobRecord.enqueue_generation no existe (Tarea 7 Paso 3)"
        )
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        client.app.state.ticket_orchestrator_factory = lambda: FakeOrch()
        record = client.portal.call(_seed, client)

        async def _bump(repo, job_id):
            await repo.update(job_id, enqueue_generation=1)

        client.portal.call(_bump, client.app.state.ticket_repo, record.job_id)
        r = client.post("/internal/tasks/ticket-job",
                        json={"job_id": record.job_id, "enqueue_generation": 0},
                        headers={"X-CloudTasks-TaskName": "t-stale",
                                 "X-CloudTasks-TaskRetryCount": "0"})
        assert r.status_code == 204, (
            f"una task con generación stale devolvió {r.status_code}; debe ser "
            "204 sin efecto"
        )

        async def _get(repo, job_id):
            return await repo.get(job_id)

        current = client.portal.call(_get, client.app.state.ticket_repo,
                                     record.job_id)
        assert current.state.value == "queued", (
            "la task stale ejecutó/claimeó el job"
        )
