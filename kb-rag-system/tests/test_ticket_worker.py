"""
Tests del endpoint interno del worker (Task 4 del plan).

La ejecución durable en sí (checkpoints, timeouts, agregación, claims) se
prueba en tests/test_handle_ticket_endpoint.py::TestRunTicketJob y
TestDurabilityRegressions; aquí se cubre la superficie HTTP que invoca
Cloud Tasks: autenticación OIDC fail-closed, 404 para jobs desconocidos y
tolerancia a delivery duplicado.
"""

from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from data_pipeline.ticket_job_models import TicketJobRecord
from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome


def _request_with_bearer(token: str | None = None):
    from starlette.requests import Request

    token = token or secrets.token_hex(16)
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/internal/tasks/ticket-job",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("PINECONE_API_KEY", "p")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    from api.config import settings as app_settings
    monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
    monkeypatch.setattr(
        app_settings, "TICKET_WORKER_URL", "https://worker.example.run.app"
    )
    monkeypatch.setattr(
        app_settings,
        "TICKET_WORKER_SERVICE_ACCOUNT",
        "task-signer@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        app_settings,
        "FORUSBOTS_BASE_URL",
        "https://forusbots.example.test",
    )
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

    async def test_task_oidc_verification_runs_off_event_loop(
            self, monkeypatch):
        import threading
        from api.config import settings as app_settings
        from api.ticket_worker import verify_task_oidc
        from google.oauth2 import id_token as gid

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", True)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_URL", "https://worker")
        monkeypatch.setattr(
            app_settings,
            "TICKET_WORKER_AUDIENCE",
            "https://ticket-worker-staging.internal",
        )
        monkeypatch.setattr(
            app_settings, "TICKET_WORKER_SERVICE_ACCOUNT",
            "task-signer@example.iam.gserviceaccount.com",
        )
        event_loop_thread = threading.get_ident()
        verifier_threads = []

        verified_audiences = []

        def fake_verify(*_args, **kwargs):
            verifier_threads.append(threading.get_ident())
            verified_audiences.append(kwargs["audience"])
            return {
                "email": "task-signer@example.iam.gserviceaccount.com",
                "email_verified": True,
            }

        monkeypatch.setattr(gid, "verify_oauth2_token", fake_verify)

        await verify_task_oidc(_request_with_bearer())

        assert verifier_threads[0] != event_loop_thread
        assert verified_audiences == ["https://ticket-worker-staging.internal"]

    async def test_worker_rejects_unverified_email_claim(self, monkeypatch):
        from fastapi import HTTPException
        from api.config import settings as app_settings
        from api.ticket_worker import verify_task_oidc
        from google.oauth2 import id_token as gid

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", True)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_URL", "https://worker")
        monkeypatch.setattr(
            app_settings, "TICKET_WORKER_SERVICE_ACCOUNT",
            "task-signer@example.iam.gserviceaccount.com",
        )
        monkeypatch.setattr(
            gid,
            "verify_oauth2_token",
            lambda *_a, **_k: {
                "email": "task-signer@example.iam.gserviceaccount.com",
                "email_verified": False,
            },
        )

        with pytest.raises(HTTPException) as exc:
            await verify_task_oidc(_request_with_bearer())
        assert exc.value.status_code == 403

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

    @pytest.mark.parametrize(
        "body",
        (
            {"job_id": "not-a-job", "enqueue_generation": 0},
            {"job_id": "a" * 32, "enqueue_generation": -1},
            {"job_id": "a" * 32, "enqueue_generation": 2_147_483_648},
        ),
    )
    def test_authenticated_permanent_task_schema_errors_are_acked_204(
        self, client, monkeypatch, body
    ):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        response = client.post("/internal/tasks/ticket-job", json=body)
        assert response.status_code == 204

    def test_task_headers_are_validated_then_hashed_before_worker_claim(
        self, client, monkeypatch
    ):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        record = client.portal.call(_seed, client)
        observed = {}

        async def fake_run(_app, _job_id, **kwargs):
            observed.update(kwargs)
            return record

        monkeypatch.setattr("api.ticket_worker.run_ticket_job", fake_run)
        raw_name = "projects/p/locations/l/queues/q/tasks/private-task-name"
        response = client.post(
            "/internal/tasks/ticket-job",
            json={"job_id": record.job_id, "enqueue_generation": 0},
            headers={
                "X-CloudTasks-TaskName": raw_name,
                "X-CloudTasks-TaskRetryCount": "7",
            },
        )

        assert response.status_code == 200
        assert raw_name not in observed["worker_id"]
        assert observed["worker_id"].startswith("task-")
        assert len(observed["worker_id"]) == 37

    @pytest.mark.parametrize(
        "headers",
        (
            {"X-CloudTasks-TaskName": "x" * 1025,
             "X-CloudTasks-TaskRetryCount": "0"},
            {"X-CloudTasks-TaskName": "valid/task",
             "X-CloudTasks-TaskRetryCount": "-1"},
            {"X-CloudTasks-TaskName": "valid/task",
             "X-CloudTasks-TaskRetryCount": "not-a-number"},
        ),
    )
    def test_invalid_task_headers_are_acked_without_lookup(
        self, client, monkeypatch, headers
    ):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        repo = client.app.state.ticket_repo
        get_spy = AsyncMock(wraps=repo.get)
        monkeypatch.setattr(repo, "get", get_spy)

        response = client.post(
            "/internal/tasks/ticket-job",
            json={"job_id": "a" * 32, "enqueue_generation": 0},
            headers=headers,
        )

        assert response.status_code == 204
        get_spy.assert_not_awaited()

    def test_unknown_job_is_204_not_retried(self, client, monkeypatch):
        """Contrato Tarea 7 Paso 3: un job desconocido devuelve 204 sin
        efecto. Cloud Tasks reintenta CUALQUIER non-2xx (también 404), así
        que un job que no debe ejecutarse debe responder 2xx."""
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        r = client.post("/internal/tasks/ticket-job", json={"job_id": "nope"})
        assert r.status_code == 204

    def test_executes_job_and_duplicate_delivery_is_2xx(self, client, monkeypatch):
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

        # re-entrega del MISMO task sobre un job ya terminal: 204 sin efecto
        # (Cloud Tasks no reintenta un 2xx; el corto-circuito de terminal
        # gana antes del claim)
        r2 = client.post("/internal/tasks/ticket-job",
                         json={"job_id": record.job_id},
                         headers={"X-CloudTasks-TaskName": "t1",
                                  "X-CloudTasks-TaskRetryCount": "1"})
        assert r2.status_code == 204


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 2) — fallos del extractor/
# síntesis y reanudación sin repetir efectos. RED hasta Tareas 6/7.
# ---------------------------------------------------------------------------

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


class _HeartbeatBlockingOrchestrator:

    def __init__(self, delay_s=1.0):
        self.delay_s = delay_s
        self.cancelled = asyncio.Event()
        self.completed_effect = False

    async def extract_inquiries(self, req):
        return [ExtractedInquiry(
            "What are rollover rules?", "LT Trust", "401(k)", "rollover"
        )]

    async def classify(self, inquiry):
        return SimpleNamespace(
            route="knowledge_question", confidence=0.9,
            reasoning="knowledge", user_message=None,
        )

    async def handle_inquiry(
        self, ext, req, *, total_inquiries, classification=None
    ):
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed_effect = True
        return InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="knowledge_question",
            knowledge_result=SimpleNamespace(
                answer="Safe answer",
                key_points=[],
                source_articles=[],
                used_chunks=[],
                confidence_note="well_covered",
                metadata={},
            ),
        )


async def _wait_for_supervised_attempt(task, timeout_s=0.5):
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    completed_in_time = task in done
    if not completed_in_time:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return completed_in_time


class TestHeartbeatSupervision:

    async def test_transient_renewal_error_recovers_without_cancelling_attempt(
        self, monkeypatch
    ):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        monkeypatch.setattr(app_settings, "TICKET_WORKER_HEARTBEAT_S", 0.005)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_LEASE_S", 1.0)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orchestrator = _HeartbeatBlockingOrchestrator(delay_s=0.04)
        original_renew = repo.renew_lease
        renew_calls = 0

        async def flaky_renew(*args, **kwargs):
            nonlocal renew_calls
            renew_calls += 1
            if renew_calls == 1:
                raise RuntimeError("transient firestore transport failure")
            return await original_renew(*args, **kwargs)

        monkeypatch.setattr(repo, "renew_lease", flaky_renew)

        final = await asyncio.wait_for(
            run_ticket_job(
                _worker_app(repo, orchestrator), rec.job_id,
                worker_id="heartbeat-recovery-worker",
            ),
            timeout=0.5,
        )

        assert renew_calls >= 2, "el heartbeat no reintentó el fallo transitorio"
        assert orchestrator.cancelled.is_set() is False
        assert orchestrator.completed_effect is True
        assert final is not None
        assert final.state.value == "succeeded"

    async def test_sustained_renewal_uncertainty_cancels_and_requeues_attempt(
        self, monkeypatch
    ):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        monkeypatch.setattr(app_settings, "TICKET_WORKER_HEARTBEAT_S", 0.005)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_LEASE_S", 1.0)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orchestrator = _HeartbeatBlockingOrchestrator()
        renew_calls = 0

        async def hanging_renew(*args, **kwargs):
            nonlocal renew_calls
            renew_calls += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(repo, "renew_lease", hanging_renew)
        attempt = asyncio.create_task(run_ticket_job(
            _worker_app(repo, orchestrator), rec.job_id,
            worker_id="heartbeat-uncertain-worker",
        ))

        completed_in_time = await _wait_for_supervised_attempt(attempt)
        current = await repo.get(rec.job_id)

        assert completed_in_time, "la incertidumbre dejó _execute corriendo"
        assert attempt.cancelled()
        assert renew_calls >= 2
        assert orchestrator.cancelled.is_set() is True
        assert orchestrator.completed_effect is False
        assert current.state.value == "queued"
        assert current.public_error_code == "WORKER_CANCELLED"
        assert current.lease_owner is None

    async def test_definitive_lease_loss_cancels_without_overwriting_new_owner(
        self, monkeypatch
    ):
        from datetime import timedelta

        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_models import utcnow
        from data_pipeline.ticket_job_repository import (
            JOBS_COLLECTION,
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        monkeypatch.setattr(app_settings, "TICKET_WORKER_HEARTBEAT_S", 0.005)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_LEASE_S", 1.0)
        backend = InMemoryTicketJobBackend()
        repo = TicketJobRepository(backend)
        rec = await _seed_repo_job(repo)
        orchestrator = _HeartbeatBlockingOrchestrator()
        replacement_epoch = None

        async def lose_lease(*args, **kwargs):
            nonlocal replacement_epoch
            control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
            control["lease_expires_at"] = utcnow() - timedelta(seconds=1)
            backend._data[JOBS_COLLECTION][rec.job_id] = control
            replacement_epoch = await repo.claim(
                rec.job_id, worker_id="replacement-worker", lease_s=1.0,
            )
            return False

        monkeypatch.setattr(repo, "renew_lease", lose_lease)
        attempt = asyncio.create_task(run_ticket_job(
            _worker_app(repo, orchestrator), rec.job_id,
            worker_id="stale-heartbeat-worker",
        ))

        completed_in_time = await _wait_for_supervised_attempt(attempt)
        current = await repo.get(rec.job_id)

        assert completed_in_time, "el worker siguió tras perder su lease"
        assert attempt.cancelled()
        assert orchestrator.cancelled.is_set() is True
        assert orchestrator.completed_effect is False
        assert replacement_epoch is not None
        assert current.lease_epoch == replacement_epoch
        assert current.lease_owner == "replacement-worker"
        assert current.state.value == "running"

    async def test_false_renewal_with_unavailable_followup_read_cancels_attempt(
        self, monkeypatch
    ):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        monkeypatch.setattr(app_settings, "TICKET_WORKER_HEARTBEAT_S", 0.005)
        monkeypatch.setattr(app_settings, "TICKET_WORKER_LEASE_S", 1.0)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orchestrator = _HeartbeatBlockingOrchestrator()
        original_get = repo.get

        async def unavailable_get(*args, **kwargs):
            await asyncio.Event().wait()

        async def false_then_unavailable_read(*args, **kwargs):
            monkeypatch.setattr(repo, "get", unavailable_get)
            return False

        monkeypatch.setattr(repo, "renew_lease", false_then_unavailable_read)
        attempt = asyncio.create_task(run_ticket_job(
            _worker_app(repo, orchestrator), rec.job_id,
            worker_id="heartbeat-followup-read-worker",
        ))

        completed_in_time = await _wait_for_supervised_attempt(attempt)
        monkeypatch.setattr(repo, "get", original_get)
        current = await repo.get(rec.job_id)

        assert completed_in_time, "el follow-up read bloqueó el supervisor"
        assert attempt.cancelled()
        assert orchestrator.cancelled.is_set() is True
        assert orchestrator.completed_effect is False
        assert current.state.value == "queued"
        assert current.public_error_code == "WORKER_CANCELLED"


class _ForusBotsLeaseRaceState:
    def __init__(self):
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.submit_calls = 0


class _ForusBotsLeaseRaceOrchestrator:
    """Simula el punto exacto posterior al intent y anterior al checkpoint.

    Cada worker recibe una instancia separada, como dos instancias de Cloud
    Run; el contador/eventos representan el upstream compartido.
    """

    def __init__(self, shared):
        self.shared = shared
        self._intent_guard = None

    def set_forusbots_intent_guard(self, guard):
        self._intent_guard = guard

    async def extract_inquiries(self, req):
        return [ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")]

    async def classify(self, inquiry):
        return SimpleNamespace(route="generate_response", confidence=0.9,
                               reasoning="r", user_message=None)

    async def handle_inquiry(self, ext, req, *, total_inquiries,
                             classification=None):
        assert self._intent_guard is not None
        await self._intent_guard()
        self.shared.submit_calls += 1
        self.shared.submit_started.set()
        await self.shared.release_submit.wait()
        return InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="generate_response",
            record_keeper=ext.record_keeper,
            plan_type=ext.plan_type,
            scrape_status="ok",
            generate_result=SimpleNamespace(
                decision="can_proceed",
                confidence=0.9,
                response="safe",
                source_articles=[],
                used_chunks=[],
                coverage_gaps=[],
                metadata={},
            ),
            diagnostics={"forusbots_job_id": "job-upstream-once"},
        )


class TestForusBotsDurableSubmitIntent:

    async def test_dedupe_scope_is_isolated_per_durable_job(self):
        from api.ticket_worker import _install_forusbots_intent_guard
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        class HookCapture:
            def set_forusbots_operation_hooks(
                self, prepare, submitted, *, dedupe_scope
            ):
                self.dedupe_scope = dedupe_scope

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        first = HookCapture()
        second = HookCapture()
        common = dict(
            repo=repo,
            inquiry_index=0,
            worker_id="worker",
            lease_epoch=1,
            route="generate_response",
            job_request_fingerprint="a" * 64,
        )

        _install_forusbots_intent_guard(first, job_id="job-a", **common)
        _install_forusbots_intent_guard(second, job_id="job-b", **common)

        assert first.dedupe_scope != second.dedupe_scope

    async def test_restart_after_confirmed_id_resumes_without_second_submit(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        shared = SimpleNamespace(
            submit_calls=0,
            resume_calls=0,
            cancel_after_receipt=True,
        )

        class ResumeAwareOrchestrator:
            def set_forusbots_operation_hooks(
                self, prepare, submitted, *, dedupe_scope
            ):
                self.prepare = prepare
                self.submitted = submitted
                self.dedupe_scope = dedupe_scope

            async def extract_inquiries(self, req):
                return [ExtractedInquiry(
                    "cash out", "LT Trust", "401(k)", "rollover"
                )]

            async def classify(self, inquiry):
                return SimpleNamespace(
                    route="generate_response",
                    confidence=0.9,
                    reasoning="synthetic",
                    user_message=None,
                    metadata={},
                )

            async def handle_inquiry(
                self, ext, req, *, total_inquiries, classification=None
            ):
                decision = await self.prepare("participant")
                if decision.action == "submit":
                    shared.submit_calls += 1
                    await self.submitted(
                        "participant", "external-job-synthetic-restart"
                    )
                    if shared.cancel_after_receipt:
                        shared.cancel_after_receipt = False
                        raise asyncio.CancelledError()
                elif decision.action == "resume":
                    shared.resume_calls += 1
                    assert decision.external_job_id == \
                        "external-job-synthetic-restart"
                else:
                    raise AssertionError(decision.action)
                return InquiryOutcome(
                    inquiry=ext.inquiry,
                    topic=ext.topic,
                    route="generate_response",
                    record_keeper=ext.record_keeper,
                    plan_type=ext.plan_type,
                    scrape_status="ok",
                    generate_result=SimpleNamespace(
                        decision="can_proceed",
                        confidence=0.9,
                        response={"response_to_participant": "Safe response"},
                        source_articles=[],
                        used_chunks=[],
                        coverage_gaps=[],
                        metadata={},
                    ),
                    diagnostics={},
                )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        app = SimpleNamespace(state=SimpleNamespace(
            ticket_repo=repo,
            ticket_orchestrator_factory=ResumeAwareOrchestrator,
            execution_logger=None,
        ))

        with pytest.raises(asyncio.CancelledError):
            await run_ticket_job(app, rec.job_id, worker_id="worker-first")

        final = await run_ticket_job(
            app, rec.job_id, worker_id="worker-second"
        )

        assert shared.submit_calls == 1
        assert shared.resume_calls == 1
        assert final.state.value == "succeeded"
        assert final.forusbots_job_ids == [
            "external-job-synthetic-restart"
        ]

    async def test_lost_lease_double_worker_never_submits_twice(self):
        """Worker A reserva intent y entra al POST. Tras perder el lease,
        worker B debe ver ese intent durable y cerrar para reconciliación sin
        ejecutar un segundo POST; A queda fenced al intentar checkpointear.
        """
        from datetime import timedelta

        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            JOBS_COLLECTION,
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        backend = InMemoryTicketJobBackend()
        repo = TicketJobRepository(backend)
        rec = await _seed_repo_job(repo)
        shared = _ForusBotsLeaseRaceState()
        app = SimpleNamespace(state=SimpleNamespace(
            ticket_repo=repo,
            ticket_orchestrator_factory=lambda: (
                _ForusBotsLeaseRaceOrchestrator(shared)
            ),
            execution_logger=None,
        ))

        old_attempt = asyncio.create_task(
            run_ticket_job(app, rec.job_id, worker_id="worker-old")
        )
        await asyncio.wait_for(shared.submit_started.wait(), timeout=2)

        control = await backend.get_doc(JOBS_COLLECTION, rec.job_id)
        control["lease_expires_at"] = (
            control["lease_expires_at"] -
            timedelta(seconds=app_settings.TICKET_WORKER_LEASE_S + 1)
        )
        backend._data[JOBS_COLLECTION][rec.job_id] = control

        new_final = await asyncio.wait_for(
            run_ticket_job(app, rec.job_id, worker_id="worker-new"),
            timeout=2,
        )
        shared.release_submit.set()
        old_final = await asyncio.wait_for(old_attempt, timeout=2)

        assert shared.submit_calls == 1
        assert old_final is None
        assert new_final.public_error_code == "FORUSBOTS_NEEDS_RECONCILIATION"
        assert new_final.next_action.value == "use_legacy_or_human"
        assert new_final.per_inquiry_status[0][
            "manual_reconciliation_required"
        ] is True


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

    def test_generation_bumped_between_read_and_claim_is_204_without_effect(
            self, client, monkeypatch):
        """El pre-check HTTP no es el fence: la generación debe volver a
        comprobarse dentro de la misma transacción que adquiere el lease.

        La barrera reproduce de forma determinista:
        task g0 lee g0 -> reconciliador quema g1 -> task g0 intenta claim.
        """
        import asyncio
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WORKER_REQUIRE_OIDC", False)
        client.app.state.ticket_orchestrator_factory = lambda: FakeOrch()
        record = client.portal.call(_seed, client)
        repo = client.app.state.ticket_repo
        original_claim = repo.claim
        claim_entered = threading.Event()
        allow_claim = threading.Event()

        async def claim_after_generation_bump(*args, **kwargs):
            claim_entered.set()
            released = await asyncio.to_thread(allow_claim.wait, 5)
            assert released, "la barrera del test no liberó el claim"
            return await original_claim(*args, **kwargs)

        monkeypatch.setattr(repo, "claim", claim_after_generation_bump)
        with ThreadPoolExecutor(max_workers=1) as pool:
            response_future = pool.submit(
                client.post,
                "/internal/tasks/ticket-job",
                json={"job_id": record.job_id, "enqueue_generation": 0},
                headers={"X-CloudTasks-TaskName": "t-racing-g0",
                         "X-CloudTasks-TaskRetryCount": "0"},
            )
            assert claim_entered.wait(5), "la task no llegó al claim"
            client.portal.call(repo.bump_enqueue_generation, record.job_id)
            allow_claim.set()
            response = response_future.result(timeout=5)

        assert response.status_code == 204
        current = client.portal.call(repo.get, record.job_id)
        assert current.enqueue_generation == 1
        assert current.state.value == "queued"
        assert current.attempt == 0


# ---------------------------------------------------------------------------
# Revisión adversarial (plan de finalización, Tarea 15 Paso 5) — P0/P1/P2
# confirmados. RED hasta corregir.
# ---------------------------------------------------------------------------

class _ScrapeThenTimeoutOrch:
    """Primera inquiry: scrape ForusBots OK (produce job_id) pero luego el
    outcome se marca timeout/failed en el checkpoint (efecto ya ocurrido)."""

    def __init__(self, fb_id="fb-scraped-1"):
        self._fb = fb_id
        self.handle_calls = 0

    async def extract_inquiries(self, req):
        return [ExtractedInquiry("q0", "LT Trust", "401(k)", "rollover")]

    async def classify(self, inquiry):
        return SimpleNamespace(route="generate_response", confidence=0.9,
                               reasoning="r", user_message=None)

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        self.handle_calls += 1
        # el scrape YA ocurrió (job_id real) pero el paso posterior degrada:
        # se lanza timeout DESPUÉS de tener el job_id
        import asyncio
        raise asyncio.TimeoutError()


class TestForusBotsIdTraceabilityOnDegraded:

    async def test_generate_response_timeout_is_manual_and_non_retryable(self):
        """A GR timeout may hide an accepted upstream effect.

        It must therefore require reconciliation and must never invite a
        consumer to retry the whole ticket blindly.
        """
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        final = await run_ticket_job(
            _worker_app(repo, _ScrapeThenTimeoutOrch()), rec.job_id
        )

        entry = final.per_inquiry_status[0]
        assert entry["manual_reconciliation_required"] is True
        assert entry["error"] == {
            "code": "INQUIRY_TIMEOUT",
            "retryable": False,
        }
        assert final.public_error_code == "FORUSBOTS_NEEDS_RECONCILIATION"
        assert final.retryable is False

    async def test_forusbots_ids_preserved_when_inquiry_times_out(self):
        """P1 (review): un scrape que produjo job_id pero cuya inquiry terminó
        en timeout DEBE conservar el ID en forusbots_job_ids (reconciliación).
        Hoy el checkpoint timeout no tiene 'result' y el ID se pierde."""
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        # sembramos un checkpoint previo con un scrape exitoso + timeout
        await repo.record_inquiry_result(rec.job_id, 0, {
            "route": "generate_response",
            "execution_status": "timeout",
            "participant_reply_safe": False,
            "degraded": True,
            "forusbots_job_ids": ["fb-scraped-1"],  # efecto real ocurrido
            "error": {"code": "INQUIRY_TIMEOUT", "retryable": True},
        })
        from api.ticket_worker import _collect_forusbots_ids_from_entries
        current = await repo.get(rec.job_id)
        ids = _collect_forusbots_ids_from_entries(current.per_inquiry_status)
        assert "fb-scraped-1" in ids, (
            "un job_id de ForusBots de una inquiry degradada se perdió: "
            "reconciliación imposible (P1 review)"
        )


class TestAggregatePublicationSafety:

    def test_manual_reconciliation_cannot_be_masked_by_retryable_error(self):
        from api.ticket_worker import _aggregate_public_error

        entries = [
            {
                "error": {
                    "code": "PINECONE_TRANSIENT_FAILURE",
                    "retryable": True,
                }
            },
            {
                "manual_reconciliation_required": True,
                "error": {"code": "INQUIRY_TIMEOUT", "retryable": False},
            },
        ]

        assert _aggregate_public_error(entries, unprocessed=0) == (
            "FORUSBOTS_NEEDS_RECONCILIATION", False
        )

    def test_mixed_error_aggregation_keeps_code_and_retryability_coherent(self):
        from api.ticket_worker import _aggregate_public_error

        entries = [
            {
                "error": {
                    "code": "UNSAFE_RETRIEVAL_QUERY",
                    "retryable": False,
                }
            },
            {
                "error": {
                    "code": "PINECONE_TRANSIENT_FAILURE",
                    "retryable": True,
                }
            },
        ]

        assert _aggregate_public_error(entries, unprocessed=0) == (
            "UNSAFE_RETRIEVAL_QUERY", False
        )

        entries.append({
            "error": {"code": "INTERNAL_ERROR", "retryable": True}
        })
        assert _aggregate_public_error(entries, unprocessed=0) == (
            "INTERNAL_ERROR", False
        )

    def test_succeeded_but_unsafe_inquiry_is_never_publishable(self):
        from api.ticket_worker import aggregate_states

        state, next_action = aggregate_states([{
            "execution_status": "succeeded",
            "participant_reply_safe": False,
            "degraded": False,
        }], unprocessed=0)

        assert state.value != "succeeded"
        assert next_action.value == "use_legacy_or_human"

    def test_unparseable_classifier_fallback_is_never_publishable(self):
        from api.ticket_worker import _entry_from_outcome, aggregate_states
        from data_pipeline.ticket_orchestrator import TicketOrchestrator

        classification = SimpleNamespace(
            route="needs_more_info",
            confidence=0.0,
            reasoning="Classifier output unparseable",
            metadata={"classifier_parse_ok": False},
        )
        diagnostics = TicketOrchestrator._classifier_diag(classification)
        outcome = InquiryOutcome(
            inquiry="ambiguous request",
            topic="general",
            route="needs_more_info",
            needs_more_info_message="Could you share more detail?",
            diagnostics=diagnostics,
        )

        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)

        assert entry["participant_reply_safe"] is False
        assert entry["error"]["code"] == "LLM_FAILURE"
        assert state.value != "succeeded"
        assert next_action.value == "use_legacy_or_human"

    @pytest.mark.parametrize(
        ("outcome", "expected_code", "result_key"),
        [
            (
                InquiryOutcome(
                    inquiry="What are rollover rules?",
                    topic="rollover",
                    route="knowledge_question",
                    knowledge_result=SimpleNamespace(
                        answer="Temporary fallback answer",
                        key_points=[],
                        source_articles=[],
                        used_chunks=[],
                        confidence_note="uncertain",
                        metadata={
                            "error": "vector retrieval unavailable",
                            "error_type": "PineconeException",
                            "retrieval_failure_kind": "server_error",
                            "retrieval_retryable": True,
                        },
                    ),
                ),
                "PINECONE_TRANSIENT_FAILURE",
                "knowledge_answer",
            ),
            (
                InquiryOutcome(
                    inquiry="Can I take a distribution?",
                    topic="distribution",
                    route="generate_response",
                    scrape_status="ok",
                    generate_result=SimpleNamespace(
                        decision="uncertain",
                        confidence=0.0,
                        response={"response_to_participant": "Temporary fallback"},
                        source_articles=[],
                        used_chunks=[],
                        coverage_gaps=[],
                        metadata={
                            "error": "generation provider unavailable",
                            "error_type": "TimeoutError",
                        },
                    ),
                ),
                "LLM_FAILURE",
                "generate_response",
            ),
            (
                InquiryOutcome(
                    inquiry="Can I take a distribution?",
                    topic="distribution",
                    route="generate_response",
                    scrape_status="ok",
                    generate_result=SimpleNamespace(
                        decision="out_of_scope",
                        confidence=0.0,
                        response={"outcome": "blocked_missing_data"},
                        source_articles=[],
                        used_chunks=[],
                        coverage_gaps=[],
                        metadata={
                            "error": "generate_response_failed",
                            "retrieval_failure_kind": "transport",
                            "retrieval_retryable": True,
                        },
                    ),
                ),
                "PINECONE_TRANSIENT_FAILURE",
                "generate_response",
            ),
        ],
    )
    def test_rag_metadata_error_is_structured_and_never_publishable(
        self, outcome, expected_code, result_key
    ):
        from api.ticket_worker import _entry_from_outcome, aggregate_states

        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)

        assert entry["participant_reply_safe"] is False
        assert entry["degraded"] is True
        assert entry["error"]["code"] == expected_code
        assert entry["result"][result_key]["metadata"]["error"]
        assert state.value != "succeeded"
        assert next_action.value == "use_legacy_or_human"

    def test_field_mapping_llm_failure_is_never_publishable(self):
        from api.ticket_worker import _entry_from_outcome, aggregate_states

        outcome = InquiryOutcome(
            inquiry="Please process my request",
            topic="general",
            route="needs_more_info",
            needs_more_info_message="Please provide the missing fields.",
            diagnostics={"field_mapping": {"llm_failed": True}},
        )

        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)

        assert entry["participant_reply_safe"] is False
        assert entry["error"]["code"] == "LLM_FAILURE"
        assert state.value != "succeeded"
        assert next_action.value == "use_legacy_or_human"

    def test_failed_classifier_retrieval_is_preserved_and_never_publishable(self):
        from api.ticket_worker import _entry_from_outcome, aggregate_states
        from data_pipeline.ticket_orchestrator import TicketOrchestrator

        classification = SimpleNamespace(
            route="needs_more_info",
            confidence=0.8,
            reasoning="Retrieval failed; fail closed.",
            metadata={
                "classifier_parse_ok": True,
                "coverage_signals": {
                    "retrieval_status": "failed",
                    "pinecone_error": "ServiceUnavailable",
                    "failure_kind": "server_error",
                    "retryable": True,
                },
            },
        )
        diagnostics = TicketOrchestrator._classifier_diag(classification)
        outcome = InquiryOutcome(
            inquiry="What are my plan rules?",
            topic="plan_rules",
            route="needs_more_info",
            needs_more_info_message="Please try again later.",
            diagnostics=diagnostics,
        )

        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)

        assert diagnostics["classifier"]["coverage_signals"] == {
            "retrieval_status": "failed",
            "pinecone_error": "ServiceUnavailable",
            "failure_kind": "server_error",
            "retryable": True,
        }
        assert entry["participant_reply_safe"] is False
        assert entry["error"]["code"] == "PINECONE_TRANSIENT_FAILURE"
        assert state.value != "succeeded"
        assert next_action.value == "use_legacy_or_human"

    def test_unsafe_classifier_retrieval_is_non_retryable_and_never_pinecone(self):
        from api.ticket_worker import _entry_from_outcome, aggregate_states

        outcome = InquiryOutcome(
            inquiry="synthetic retirement request",
            topic="general",
            route="needs_more_info",
            needs_more_info_message="Please use assisted support.",
            diagnostics={
                "classifier": {
                    "coverage_signals": {
                        "retrieval_status": "blocked",
                        "failure_kind": "unsafe_query",
                        "retryable": False,
                    }
                }
            },
        )

        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)

        assert entry["participant_reply_safe"] is False
        assert entry["error"] == {
            "code": "UNSAFE_RETRIEVAL_QUERY",
            "retryable": False,
        }
        assert state.value == "partial"
        assert next_action.value == "use_legacy_or_human"

    def test_failed_classifier_retrieval_survives_execution_plan_round_trip(self):
        from api.ticket_worker import (
            _build_execution_plan,
            _entry_from_outcome,
            _rehydrate_plan,
        )
        from data_pipeline.ticket_orchestrator import TicketOrchestrator

        ext = ExtractedInquiry(
            "What are my plan rules?", "LT Trust", "401(k)", "plan_rules"
        )
        classification = SimpleNamespace(
            route="needs_more_info",
            confidence=0.8,
            reasoning="Retrieval failed; fail closed.",
            user_message="Please try again later.",
            metadata={
                "classifier_parse_ok": True,
                "coverage_signals": {
                    "retrieval_status": "failed",
                    "pinecone_error": "ServiceUnavailable",
                    "failure_kind": "server_error",
                    "retryable": True,
                },
            },
        )
        plan = _build_execution_plan(
            [ext], [classification], [("needs_more_info", None)], 1, 0
        )

        _, restored, _ = _rehydrate_plan(plan)
        diagnostics = TicketOrchestrator._classifier_diag(restored[0])
        entry = _entry_from_outcome(0, InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="needs_more_info",
            needs_more_info_message="Please try again later.",
            diagnostics=diagnostics,
        ))

        assert diagnostics["classifier"]["coverage_signals"][
            "retrieval_status"
        ] == "failed"
        assert entry["participant_reply_safe"] is False
        assert entry["error"]["code"] == "PINECONE_TRANSIENT_FAILURE"


class _RagMetadataFailureOrchestrator:

    async def extract_inquiries(self, req):
        return [ExtractedInquiry(
            "What are rollover rules?", "LT Trust", "401(k)", "rollover"
        )]

    async def classify(self, inquiry):
        return SimpleNamespace(
            route="knowledge_question", confidence=0.9,
            reasoning="Knowledge question", user_message=None,
        )

    async def handle_inquiry(
        self, ext, req, *, total_inquiries, classification=None
    ):
        return InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="knowledge_question",
            knowledge_result=SimpleNamespace(
                answer="Temporary fallback answer",
                key_points=[],
                source_articles=[],
                used_chunks=[],
                confidence_note="uncertain",
                metadata={
                    "error": "vector retrieval unavailable",
                    "error_type": "PineconeException",
                    "retrieval_failure_kind": "server_error",
                    "retrieval_retryable": True,
                },
            ),
        )


class TestWorkerOperationalMetrics:

    @staticmethod
    def _capture(monkeypatch):
        from api import ticket_worker

        calls = []
        monkeypatch.setattr(
            ticket_worker.ticket_metrics,
            "emit",
            lambda metric, value, **labels: calls.append(
                (metric, value, labels)
            ),
        )
        return calls

    async def test_success_emits_validate_route_and_finalize_latencies(
        self, monkeypatch,
    ):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        calls = self._capture(monkeypatch)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        final = await run_ticket_job(
            _worker_app(repo, _HeartbeatBlockingOrchestrator(delay_s=0)),
            rec.job_id,
        )

        assert final.state.value == "succeeded"
        steps = [
            (labels["step"], labels["code"])
            for metric, value, labels in calls
            if metric == "ticket_step_latency_seconds" and value >= 0
        ]
        assert ("validate", "success") in steps
        assert ("retrieve", "success") in steps
        assert ("finalize", "success") in steps

    async def test_success_observes_ordered_durable_phases(self, monkeypatch):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        calls = self._capture(monkeypatch)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        final = await run_ticket_job(
            _worker_app(repo, _HeartbeatBlockingOrchestrator(delay_s=0)),
            rec.job_id,
        )

        assert final.state.value == "succeeded"
        phases = [
            labels["phase"]
            for metric, value, labels in calls
            if metric == "ticket_phase_count" and value == 1
        ]
        assert phases == [
            "handle_inquiry",
            "convert_outcome",
            "validate_durable_document",
            "persist_inquiry_result",
            "mark_terminal",
        ]

    async def test_worker_scope_enables_shared_parser_ticket_metric(
        self, monkeypatch,
    ):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.json_parsing import parse_json_object
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        class _ScopedParserOrchestrator(_HeartbeatBlockingOrchestrator):
            async def handle_inquiry(
                self, ext, req, *, total_inquiries, classification=None,
            ):
                assert parse_json_object('{"safe": true}') == {"safe": True}
                return await super().handle_inquiry(
                    ext,
                    req,
                    total_inquiries=total_inquiries,
                    classification=classification,
                )

        calls = self._capture(monkeypatch)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        await run_ticket_job(
            _worker_app(repo, _ScopedParserOrchestrator(delay_s=0)), rec.job_id,
        )

        assert (
            "ticket_llm_parse_count", 1, {"code": "success"}
        ) in calls

    async def test_partial_job_emits_partial_result_count(
        self, monkeypatch,
    ):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        calls = self._capture(monkeypatch)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        final = await run_ticket_job(
            _worker_app(repo, _RagMetadataFailureOrchestrator()), rec.job_id,
        )

        assert final.state.value == "partial"
        assert (
            "ticket_result_count", 1, {"reason": "partial"}
        ) in calls

    def test_unprocessed_result_metrics_distinguish_job_and_item_counts(
        self, monkeypatch,
    ):
        from api import ticket_worker
        from data_pipeline.ticket_job_models import (
            TicketJobState, new_job_record,
        )

        calls = self._capture(monkeypatch)
        record = new_job_record(
            principal_id="principal",
            request_fingerprint="f" * 64,
            state=TicketJobState.PARTIAL,
            total_inquiries=5,
            unprocessed_inquiries=3,
        )

        ticket_worker._emit_result_metrics(record)

        results = [event for event in calls if event[0] == "ticket_result_count"]
        assert results == [
            ("ticket_result_count", 1, {"reason": "partial"}),
            ("ticket_result_count", 1, {"reason": "truncated"}),
            ("ticket_result_count", 3, {"reason": "unprocessed"}),
        ]


class TestTechnicalFailurePublicationIntegration:

    def test_exception_and_dependency_telemetry_use_closed_allowlists(self):
        from api.ticket_worker import (
            _safe_dependency_code,
            _safe_exception_type,
        )

        PrivateException = type(
            "PRIVATE_PARTICIPANT_TOKEN_123", (RuntimeError,), {}
        )

        class PrivateCodeError(RuntimeError):
            code = "SECRET_CASE_987"

        assert _safe_exception_type(PrivateException()) == "Exception"
        assert _safe_dependency_code(PrivateCodeError()) is None

    def test_failure_fingerprint_depends_only_on_structural_metadata(
        self, caplog,
    ):
        from api.ticket_worker import _log_inquiry_failure

        first_secret = "participant-private-first"  # pragma: allowlist secret
        second_secret = "participant-private-second"  # pragma: allowlist secret
        with caplog.at_level("ERROR", logger="api.ticket_worker"):
            _log_inquiry_failure(
                ValueError(first_secret), default_phase="convert_outcome"
            )
            _log_inquiry_failure(
                ValueError(second_secret), default_phase="convert_outcome"
            )

        records = [
            record.message for record in caplog.records
            if record.name == "api.ticket_worker"
            and record.message.startswith("ticket inquiry failed")
        ]
        assert len(records) == 2
        fingerprints = [
            record.split("fingerprint=", 1)[1].split(" ", 1)[0]
            for record in records
        ]
        assert fingerprints[0] == fingerprints[1]
        assert len(fingerprints[0]) == 64
        assert first_secret not in caplog.text
        assert second_secret not in caplog.text

    def test_durable_validation_log_contains_only_safe_aggregates(self, caplog):
        from api.ticket_worker import _InquiryPhaseFailure, _log_inquiry_failure
        from data_pipeline.durable_document import (
            DurableDocumentValidationError,
            validate_durable_document,
        )

        private_value = "participant-private-value-must-not-leak"
        with pytest.raises(DurableDocumentValidationError) as exc_info:
            validate_durable_document({"result": [[private_value]]})

        wrapped = _InquiryPhaseFailure(
            "validate_durable_document", exc_info.value,
        )
        with caplog.at_level("ERROR", logger="api.ticket_worker"):
            _log_inquiry_failure(
                wrapped,
                default_phase="handle_inquiry",
            )

        assert "phase=validate_durable_document" in caplog.text
        assert "error_type=DurableDocumentValidationError" in caplog.text
        assert "fingerprint=" in caplog.text
        assert "estimated_size_bytes=" in caplog.text
        assert "max_depth=" in caplog.text
        assert "invalid_nested_arrays=1" in caplog.text
        assert private_value not in caplog.text

    async def test_conversion_failure_log_has_safe_phase_and_type(
            self, caplog):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        class InvalidResultOrchestrator(_HeartbeatBlockingOrchestrator):
            async def handle_inquiry(
                self, ext, req, *, total_inquiries, classification=None
            ):
                return InquiryOutcome(
                    inquiry=ext.inquiry,
                    topic=ext.topic,
                    route="generate_response",
                    scrape_status="ok",
                    generate_result=SimpleNamespace(
                        decision="can_proceed", confidence=0.9
                    ),
                    diagnostics={},
                )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        with caplog.at_level("ERROR", logger="api.ticket_worker"):
            final = await run_ticket_job(
                _worker_app(repo, InvalidResultOrchestrator(delay_s=0)),
                rec.job_id,
            )

        assert final.state.value == "failed"
        assert "phase=convert_outcome" in caplog.text
        assert "error_type=AttributeError" in caplog.text
        assert "dependency_code=none" in caplog.text
        assert rec.request_fingerprint not in caplog.text
        assert "estimated_size_bytes=none" in caplog.text
        assert "max_depth=none" in caplog.text
        assert "invalid_nested_arrays=none" in caplog.text

    async def test_pre_effect_durable_failure_has_validation_phase_and_stats(
            self, caplog):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.durable_document import validate_durable_document
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        class PreEffectValidationFailure(_HeartbeatBlockingOrchestrator):
            async def classify(self, inquiry):
                return SimpleNamespace(
                    route="generate_response",
                    confidence=0.9,
                    reasoning="synthetic",
                    user_message=None,
                )

            async def handle_inquiry(
                self, ext, req, *, total_inquiries, classification=None
            ):
                validate_durable_document({"diagnostics": [["private"]]})
                raise AssertionError("unreachable")

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        with caplog.at_level("ERROR", logger="api.ticket_worker"):
            final = await run_ticket_job(
                _worker_app(repo, PreEffectValidationFailure(delay_s=0)),
                rec.job_id,
            )

        entry = final.per_inquiry_status[0]
        assert "phase=validate_durable_document" in caplog.text
        assert "error_type=DurableDocumentValidationError" in caplog.text
        assert "invalid_nested_arrays=1" in caplog.text
        assert entry.get("manual_reconciliation_required") is False

    async def test_rag_metadata_failure_cannot_terminalize_as_publishable(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)

        final = await run_ticket_job(
            _worker_app(repo, _RagMetadataFailureOrchestrator()), rec.job_id
        )

        entry = final.per_inquiry_status[0]
        assert entry["participant_reply_safe"] is False
        assert entry["error"]["code"] == "PINECONE_TRANSIENT_FAILURE"
        assert final.state.value != "succeeded"
        assert final.next_action.value != "send_participant_reply"


class TestUnprocessedResumesOnRetry:

    async def test_unprocessed_inquiry_reprocessed_on_resume(self):
        """P2 (review): 'unprocessed' (presupuesto agotado, retryable) NO es
        terminal — un retry con presupuesto fresco debe reprocesarla, no
        saltarla para siempre."""
        from api.ticket_worker import _execute
        # comprobamos la semántica de done_indexes directamente
        entries = [
            {"index": 0, "execution_status": "succeeded"},
            {"index": 1, "execution_status": "unprocessed"},
        ]
        done = {
            e.get("index") for e in entries
            if e.get("execution_status") not in
            (None, "pending", "running", "unprocessed")
        }
        assert 0 in done and 1 not in done, (
            "un checkpoint 'unprocessed' debe reprocesarse en el retry"
        )
        # y el worker real no debe tratar unprocessed como terminal
        import inspect
        src = inspect.getsource(_execute)
        assert "unprocessed" in src and "done_indexes" in src, (
            "el loop de reanudación debe excluir 'unprocessed' de done_indexes"
        )


class TestManualReconciliationMetric:

    def test_emits_one_sanitized_structured_event_for_flagged_entries(
        self, monkeypatch
    ):
        """La alerta declarativa debe apoyarse en una señal emitida de verdad.

        El evento puede llevar el hash irreversible del job y el trace_id,
        pero nunca IDs crudos ni contenido de los checkpoints.
        """
        from api import ticket_worker
        from data_pipeline.ticket_job_models import new_job_record

        record = new_job_record(
            job_id="raw-job-id-must-not-leak",
            principal_id="raw-principal-must-not-leak",
            request_fingerprint="f" * 64,
            trace_id="trace-safe",
        )
        entries = [
            {
                "manual_reconciliation_required": True,
                "participant_id": "raw-participant-must-not-leak",
            },
            {"manual_reconciliation_required": True},
            {"manual_reconciliation_required": False},
        ]
        calls = []
        monkeypatch.setattr(
            ticket_worker.ticket_metrics,
            "emit",
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        ticket_worker._emit_manual_reconciliation_metric(record, entries)

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ("ticket_manual_reconciliation_required", 2)
        assert kwargs["trace_id"] == "trace-safe"
        assert kwargs["code"] == "manual_reconciliation"
        assert len(kwargs["job_hash"]) == 64
        emitted = repr(calls)
        assert "raw-job-id-must-not-leak" not in emitted
        assert "raw-principal-must-not-leak" not in emitted
        assert "raw-participant-must-not-leak" not in emitted

        calls.clear()
        ticket_worker._emit_manual_reconciliation_metric(
            record, [{"manual_reconciliation_required": False}]
        )
        assert calls == []


class TestTerminalMetricPopulation:

    @staticmethod
    def _capture_terminal_events(monkeypatch, ticket_worker):
        calls = []
        monkeypatch.setattr(
            ticket_worker.ticket_metrics,
            "increment",
            lambda *args, **kwargs: pytest.fail(
                "legacy terminal metric must not be emitted"
            ),
        )
        monkeypatch.setattr(
            ticket_worker.ticket_metrics,
            "emit",
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        return calls

    async def test_early_terminal_return_emits_exactly_once(self, monkeypatch):
        """Los fallos del extractor retornan antes de la agregación normal,
        pero siguen perteneciendo al numerador y denominador terminales.
        """
        from api import ticket_worker
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        calls = self._capture_terminal_events(monkeypatch, ticket_worker)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        final = await ticket_worker.run_ticket_job(
            _worker_app(repo, _real_orchestrator(_FailingRouter())), rec.job_id
        )

        terminal = [c for c in calls if c[0][0] == "ticket_job_terminal"]
        assert len(terminal) == 1
        args, kwargs = terminal[0]
        assert args == ("ticket_job_terminal", 1)
        assert kwargs["state"] == final.state.value == "failed"
        assert kwargs["code"] == final.public_error_code
        assert len(kwargs["job_hash"]) == 64
        assert rec.job_id not in repr(terminal)

    async def test_generic_terminal_failure_emits_exactly_once(self, monkeypatch):
        """La terminalización defensiva de run_ticket_job también forma
        parte de la población, aunque `_execute` falle antes de devolver.
        """
        from api import ticket_worker
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        calls = self._capture_terminal_events(monkeypatch, ticket_worker)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        app = SimpleNamespace(state=SimpleNamespace(
            ticket_repo=repo,
            ticket_orchestrator_factory=lambda: (_ for _ in ()).throw(
                RuntimeError("synthetic factory failure")
            ),
            execution_logger=None,
        ))

        final = await ticket_worker.run_ticket_job(app, rec.job_id)

        terminal = [c for c in calls if c[0][0] == "ticket_job_terminal"]
        assert final.state.value == "failed"
        assert len(terminal) == 1

    async def test_normal_terminal_path_is_not_double_counted(self, monkeypatch):
        from api import ticket_worker
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        calls = self._capture_terminal_events(monkeypatch, ticket_worker)
        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed_repo_job(repo)
        orch = _ResumeOrch([
            ExtractedInquiry("inquiry", "LT Trust", "401(k)", "rollover")
        ])

        final = await ticket_worker.run_ticket_job(
            _worker_app(repo, orch), rec.job_id
        )

        terminal = [c for c in calls if c[0][0] == "ticket_job_terminal"]
        assert final.state.value == "succeeded"
        assert len(terminal) == 1

    def test_terminal_metric_emits_internal_error_per_route(self, monkeypatch):
        from api import ticket_worker
        from data_pipeline.ticket_job_models import (
            TicketJobState, new_job_record,
        )

        calls = self._capture_terminal_events(monkeypatch, ticket_worker)
        record = new_job_record(
            principal_id="principal",
            request_fingerprint="a" * 64,
            state=TicketJobState.PARTIAL,
            per_inquiry_status=[
                {
                    "route": "knowledge_question",
                    "execution_status": "failed",
                    "error": {"code": "INTERNAL_ERROR", "retryable": False},
                },
                {
                    "route": "generate_response",
                    "execution_status": "failed",
                    "error": {
                        "code": "PINECONE_TRANSIENT_FAILURE",
                        "retryable": True,
                    },
                },
            ],
        )

        ticket_worker._emit_terminal_metric(record)

        inquiry_events = [
            call for call in calls
            if call[0][0] == "ticket_inquiry_terminal"
        ]
        assert inquiry_events == [
            (
                ("ticket_inquiry_terminal", 1),
                {
                    "route": "knowledge_question",
                    "code": "INTERNAL_ERROR",
                },
            ),
            (
                ("ticket_inquiry_terminal", 1),
                {
                    "route": "generate_response",
                    "code": "PINECONE_TRANSIENT_FAILURE",
                },
            ),
        ]
