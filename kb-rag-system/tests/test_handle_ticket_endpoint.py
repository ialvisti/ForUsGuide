"""
Integration tests for the /api/v1/handle-ticket + /api/v1/tickets/{id} endpoints.

The lifespan runs with patched engine constructors (mirrors tests/test_api.py),
so app.state has the real durable ticket repo / inline queue / worker. The
orchestrator dependency is overridden with a fake that returns canned outcomes;
``verify_api_key`` is overridden to a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("PINECONE_API_KEY", "p")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    # El override del body sólo puede RESTRINGIR el modo del servidor, así que
    # los tests fijan el server mode en full y cada test usa el body para
    # narrowear (disabled/knowledge_only/shadow).
    from api.config import settings as app_settings
    monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
    monkeypatch.setattr(
        app_settings,
        "FORUSBOTS_BASE_URL",
        "https://forusbots.example.test",
    )

    mock_engine = Mock()
    mock_pinecone = Mock()
    mock_pinecone.get_index_stats.return_value = {"total_vectors": 0}
    mock_inquiry_router = Mock()

    with patch("api.main.validate_settings"), \
         patch("api.main.RAGEngine", return_value=mock_engine), \
         patch("api.main.PineconeUploader", return_value=mock_pinecone), \
         patch("api.main.InquiryRouterEngine", return_value=mock_inquiry_router):
        from api.main import app, verify_api_key, verify_v2_api_key

        async def _authenticated_test_principal(request: Request) -> None:
            request.state.principal_id = "test-principal"
            request.state.tenant_id = "test-tenant"

        app.dependency_overrides[verify_api_key] = _authenticated_test_principal
        app.dependency_overrides[verify_v2_api_key] = _authenticated_test_principal
        with TestClient(app) as c:
            # La autorización participant-plan es FAIL-CLOSED (Tarea 4): los
            # tests de contrato del endpoint cablean una fuente sintética que
            # autoriza el par de _body(); los tests de mismatch inyectan la
            # suya propia.
            c.app.state.participant_plan_validator = _AllowAllValidator()
            yield c
        app.dependency_overrides.clear()


class _AllowAllValidator:
    """Fuente canónica sintética: autoriza cualquier par y devuelve los
    campos server-owned (tenant/record keeper) del contrato de Tarea 4."""

    async def authorize(self, *, tenant_id, participant_id, plan_id):
        from api.participant_plan import AuthorizedParticipantPlan
        return AuthorizedParticipantPlan(
            tenant_id=tenant_id, participant_id=participant_id,
            plan_id=plan_id, record_keeper=None,
        )


def _body(**over):
    base = dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": "401k", "email_body": "quiero retirar mi 401k"},
        record_keeper="LT Trust", ticket_handler_mode="full",
    )
    base.update(over)
    return base


# ---- fake orchestrator + outcome builders -----------------------------------

from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome  # noqa: E402


def _ext(inquiry="cash out 401k", topic="rollover"):
    return ExtractedInquiry(inquiry, "LT Trust", "401(k)", topic)


def _kq_result(answer="A"):
    return SimpleNamespace(answer=answer, key_points=["k"], source_articles=[],
                           used_chunks=[], confidence_note="well_covered", metadata={})


def _gr_result(decision="can_proceed"):
    return SimpleNamespace(decision=decision, confidence=0.8, response={"outcome": "can_proceed"},
                           source_articles=[], used_chunks=[], coverage_gaps=[], metadata={})


class FakeOrch:
    def __init__(self, extracted, classification, outcome):
        self._extracted = extracted
        self._classification = classification
        self._outcome = outcome

    async def extract_inquiries(self, req):
        return list(self._extracted)

    async def classify(self, inquiry):
        return self._classification

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        return self._outcome


def _use_orch(client, orch):
    from api.main import app, get_ticket_orchestrator
    app.dependency_overrides[get_ticket_orchestrator] = lambda: orch
    # el worker durable resuelve el orchestrator vía factory en app.state
    client.app.state.ticket_orchestrator_factory = lambda: orch


def _cls(route, **kw):
    return SimpleNamespace(route=route, confidence=kw.get("confidence", 0.9),
                           reasoning="r", user_message=kw.get("user_message"))


# ---------------------------------------------------------------------------
# Fast (inline) routes
# ---------------------------------------------------------------------------

class TestInlineRoutes:

    def test_knowledge_question_inline_200(self, client):
        outcome = InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                                 route="knowledge_question", knowledge_result=_kq_result("Cash-out steps..."))
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"), outcome))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 200
        data = r.json()
        assert data["route_taken"] == "knowledge_question"
        assert data["primary"]["knowledge_answer"]["answer"] == "Cash-out steps..."
        assert data["total_inquiries_in_ticket"] == 1

    def test_needs_more_info_inline_200(self, client):
        outcome = InquiryOutcome(inquiry="hola", topic="general", route="needs_more_info",
                                 needs_more_info_message="¿Más detalle?")
        _use_orch(client, FakeOrch([_ext("hola", "general")], _cls("needs_more_info", user_message="¿Más detalle?"), outcome))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 200
        data = r.json()
        assert data["route_taken"] == "needs_more_info"
        assert data["primary"]["needs_more_info_message"] == "¿Más detalle?"

    def test_empty_extraction_routes_to_legacy_or_human(self, client):
        """Una extracción VÁLIDA y vacía ya no sintetiza un saludo publicable
        (Tarea 6 Paso 1): el ticket va a legacy/humano vía 202 + poll."""
        _use_orch(client, FakeOrch([], _cls("needs_more_info"), None))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 202
        poll = client.get(f"/api/v1/tickets/{r.json()['ticket_job_id']}")
        data = poll.json()
        assert data["state"] == "succeeded"
        assert data["next_action"] == "use_legacy_or_human"
        assert data["total_inquiries_in_ticket"] == 0
        assert data["metadata"]["reason"] == "no_actionable_inquiry"


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

class TestGating:

    def test_disabled_returns_503(self, client):
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"), None))
        r = client.post("/api/v1/handle-ticket", json=_body(ticket_handler_mode="disabled"))
        assert r.status_code == 503

    def test_knowledge_only_coerces_generate_response(self, client):
        """La coerción de modo NO es publicable (Tarea 4 Paso 6): ya no se
        entrega como 200 inline; el ticket va por 202 + poll con acción
        legacy explícita para que n8n resuelva por el flujo anterior."""
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), None))
        r = client.post("/api/v1/handle-ticket", json=_body(ticket_handler_mode="knowledge_only"))
        assert r.status_code == 202
        poll = client.get(f"/api/v1/tickets/{r.json()['ticket_job_id']}")
        assert poll.status_code == 200
        data = poll.json()
        assert data["next_action"] in ("use_legacy", "use_legacy_or_human")
        assert data["primary"]["route"] == "needs_more_info"
        assert "ticket_handler_override" in data["primary"]["diagnostics"]

    def test_shadow_returns_fallback(self, client):
        """Shadow nunca es un 200 inline: el resultado viaja por poll con
        fallback=true y next_action=use_legacy (jamás publicable)."""
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"), None))
        r = client.post("/api/v1/handle-ticket", json=_body(ticket_handler_mode="shadow"))
        assert r.status_code == 202
        poll = client.get(f"/api/v1/tickets/{r.json()['ticket_job_id']}")
        assert poll.status_code == 200
        data = poll.json()
        assert data["metadata"]["fallback"] is True
        assert data["metadata"]["shadow_routes"] == ["knowledge_question"]
        assert data["next_action"] == "use_legacy"


# ---------------------------------------------------------------------------
# Slow (job) path
# ---------------------------------------------------------------------------

class TestSlowPath:

    def test_successful_poll_emits_observed_state_metric(
        self, client, monkeypatch,
    ):
        outcome = InquiryOutcome(
            inquiry="cash out 401k",
            topic="rollover",
            route="generate_response",
            scrape_status="ok",
            generate_result=_gr_result(),
        )
        _use_orch(
            client, FakeOrch([_ext()], _cls("generate_response"), outcome)
        )
        emitted = []
        monkeypatch.setattr(
            "api.main.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        accepted = client.post("/api/v1/handle-ticket", json=_body())
        emitted.clear()

        polled = client.get(
            f"/api/v1/tickets/{accepted.json()['ticket_job_id']}"
        )

        assert polled.status_code == 200
        assert emitted == [
            ("ticket_n8n_poll_count", 1, {"state": "succeeded"})
        ]

    def test_generate_response_returns_202_and_creates_job(self, client):
        outcome = InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                                 route="generate_response", scrape_status="ok",
                                 generate_result=_gr_result())
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), outcome))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 202
        data = r.json()
        assert data["ticket_job_id"]
        assert data["poll_url"].endswith(data["ticket_job_id"])
        # el job es durable y recuperable vía GET (no memoria de proceso)
        poll = client.get(f"/api/v1/tickets/{data['ticket_job_id']}")
        assert poll.status_code == 200

    def test_get_unknown_job_404(self, client):
        r = client.get("/api/v1/tickets/does-not-exist")
        assert r.status_code == 404

    def test_idempotency_key_reuses_same_job(self, client):
        outcome = InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                                 route="generate_response", scrape_status="ok",
                                 generate_result=_gr_result())
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), outcome))
        headers = {"Idempotency-Key": "ticket-abc-123"}
        r1 = client.post("/api/v1/handle-ticket", json=_body(), headers=headers)
        r2 = client.post("/api/v1/handle-ticket", json=_body(), headers=headers)
        assert r1.status_code == 202 and r2.status_code == 202
        # the retry returns the SAME job — no duplicate orchestration / scrape
        assert r1.json()["ticket_job_id"] == r2.json()["ticket_job_id"]

    def test_get_returns_stored_results(self, client):
        outcome = InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                                 route="generate_response", scrape_status="ok",
                                 generate_result=_gr_result(), diagnostics={"forusbots_job_id": "job-9"})
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), outcome))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 202
        job_id = r.json()["ticket_job_id"]
        r2 = client.get(f"/api/v1/tickets/{job_id}")
        assert r2.status_code == 200
        data = r2.json()
        assert data["state"] == "succeeded"
        assert data["route_taken"] == "generate_response"
        assert data["primary"]["generate_response"]["decision"] == "can_proceed"
        assert data["forusbots_job_ids"] == ["job-9"]


# ---------------------------------------------------------------------------
# Background runner (direct, deterministic)
# ---------------------------------------------------------------------------

class TestRunTicketJob:
    """Worker durable directo (sin HTTP): la ÚNICA implementación de
    ejecución (api.ticket_worker.run_ticket_job)."""

    @staticmethod
    def _worker_app(repo, orch):
        return SimpleNamespace(state=SimpleNamespace(
            ticket_repo=repo,
            ticket_orchestrator_factory=lambda: orch,
            execution_logger=None,
        ))

    @staticmethod
    async def _seed_job(repo, mode="full"):
        from data_pipeline.ticket_job_models import (
            fingerprint_request, new_job_record,
        )
        payload = _body()
        fp = fingerprint_request(payload)
        rec, _ = await repo.create_or_get(
            principal_id="default", idempotency_key=None,
            request_fingerprint=fp,
            candidate=new_job_record(principal_id="default",
                                     request_fingerprint=fp, mode=mode,
                                     request_payload=payload),
        )
        return rec

    async def test_run_ticket_job_succeeded(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        outcome = InquiryOutcome(inquiry="q", topic="rollover", route="generate_response",
                                 scrape_status="ok", generate_result=_gr_result(),
                                 diagnostics={"forusbots_job_id": "j1"})
        orch = FakeOrch([_ext()], _cls("generate_response"), outcome)
        rec = await self._seed_job(repo)

        final = await run_ticket_job(self._worker_app(repo, orch), rec.job_id)

        assert final.state.value == "succeeded"
        assert final.forusbots_job_ids == ["j1"]
        assert len(final.per_inquiry_status) == 1
        assert final.per_inquiry_status[0]["participant_reply_safe"] is True
        assert final.next_action.value == "send_participant_reply"
        # duración congelada en terminal (HT-26)
        assert final.elapsed_s is not None

    async def test_run_ticket_job_partial_on_scrape_failure(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        outcome = InquiryOutcome(inquiry="q", topic="rollover", route="generate_response",
                                 scrape_status="failed", generate_result=_gr_result("uncertain"))
        orch = FakeOrch([_ext()], _cls("generate_response"), outcome)
        rec = await self._seed_job(repo)

        final = await run_ticket_job(self._worker_app(repo, orch), rec.job_id)

        assert final.state.value == "partial"
        assert final.per_inquiry_status[0]["participant_reply_safe"] is False
        assert final.next_action.value == "use_legacy_or_human"

    async def test_duplicate_delivery_executes_once(self):
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        orch = CountingOrch([_ext()], _cls("knowledge_question"),
                            InquiryOutcome(inquiry="q", topic="t",
                                           route="knowledge_question",
                                           knowledge_result=_kq_result()))
        rec = await self._seed_job(repo)
        app = self._worker_app(repo, orch)

        first = await run_ticket_job(app, rec.job_id, worker_id="t#0")
        second = await run_ticket_job(app, rec.job_id, worker_id="t#1")

        assert first is not None and first.state.value == "succeeded"
        assert second is None            # delivery duplicado: claim rechazado
        assert orch.extract_calls == 1


# ---------------------------------------------------------------------------
# Task 2 regressions — idempotencia (HT-05)
# ---------------------------------------------------------------------------

class CountingOrch(FakeOrch):
    """FakeOrch que cuenta ejecuciones lógicas y cede el event loop para abrir
    la ventana de carrera check-then-set."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.extract_calls = 0
        self.handle_calls = 0

    async def extract_inquiries(self, req):
        self.extract_calls += 1
        import asyncio
        await asyncio.sleep(0.01)
        return list(self._extracted)

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        self.handle_calls += 1
        import asyncio
        await asyncio.sleep(0.01)
        return self._outcome


def _gr_outcome():
    return InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                          route="generate_response", scrape_status="ok",
                          generate_result=_gr_result())


class TestIdempotencyRegressions:

    def test_50_concurrent_same_key_create_one_execution(self, client, monkeypatch):
        """Invariante 2: una key idempotente produce como máximo UNA ejecución
        lógica. La reserva debe ocurrir en transacción ANTES de cualquier LLM."""
        from concurrent.futures import ThreadPoolExecutor

        # el sujeto de este test es la idempotencia, no el rate limit
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "RATE_LIMIT_HANDLE_TICKET", 1000)

        orch = CountingOrch([_ext()], _cls("generate_response"), _gr_outcome())
        _use_orch(client, orch)
        headers = {"Idempotency-Key": "concurrent-key-1"}

        def _post(_):
            return client.post("/api/v1/handle-ticket", json=_body(), headers=headers)

        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = list(pool.map(_post, range(50)))

        assert all(r.status_code == 202 for r in responses)
        job_ids = {r.json()["ticket_job_id"] for r in responses}
        assert len(job_ids) == 1, f"{len(job_ids)} jobs distintos para la misma key"
        assert orch.extract_calls == 1, (
            f"{orch.extract_calls} extracciones LLM para la misma key: la reserva "
            "idempotente debe preceder a la extracción"
        )

    def test_same_key_different_payload_returns_409(self, client):
        """Invariante 3: misma key + payload distinto = 409, nunca cruza
        resultados entre tickets."""
        orch = CountingOrch([_ext()], _cls("generate_response"), _gr_outcome())
        _use_orch(client, orch)
        headers = {"Idempotency-Key": "mismatch-key-1"}
        r1 = client.post("/api/v1/handle-ticket", json=_body(), headers=headers)
        assert r1.status_code == 202
        other = _body(participant_id="999999",
                      ticket={"username": "Other", "user_email": "o@f.com",
                              "email_subject": "distinto", "email_body": "otro ticket"})
        r2 = client.post("/api/v1/handle-ticket", json=other, headers=headers)
        assert r2.status_code == 409, (
            f"payload distinto con la misma key devolvió {r2.status_code}; "
            "debe ser 409 IDEMPOTENCY_PAYLOAD_MISMATCH"
        )

    def test_inline_result_is_idempotent(self, client):
        """La ruta rápida (200 inline) también debe replayar por key: hoy
        ejecuta dos veces el pipeline completo."""
        outcome = InquiryOutcome(inquiry="cash out 401k", topic="rollover",
                                 route="knowledge_question",
                                 knowledge_result=_kq_result("answer-1"))
        orch = CountingOrch([_ext()], _cls("knowledge_question"), outcome)
        _use_orch(client, orch)
        headers = {"Idempotency-Key": "inline-key-1"}
        r1 = client.post("/api/v1/handle-ticket", json=_body(), headers=headers)
        r2 = client.post("/api/v1/handle-ticket", json=_body(), headers=headers)
        assert r1.status_code in (200, 202)
        assert orch.extract_calls == 1, (
            "el replay inline re-ejecutó el pipeline (extract llamado "
            f"{orch.extract_calls} veces)"
        )
        # y el segundo request devuelve el mismo resultado lógico
        if r1.status_code == 200 and r2.status_code == 200:
            assert r1.json()["primary"] == r2.json()["primary"]


# ---------------------------------------------------------------------------
# Task 2 regressions — durabilidad y estados (HT-01, HT-07, HT-08)
# ---------------------------------------------------------------------------

def _simulate_new_instance(app):
    """Simula que el poll llega a OTRA instancia de Cloud Run: todo objeto
    process-local se recrea; sólo el backend durable se comparte."""
    st = app.state
    from data_pipeline.ticket_job_repository import TicketJobRepository
    st.ticket_repo = TicketJobRepository(st.ticket_repo.backend)


class TestDurabilityRegressions:

    def test_poll_from_second_app_instance_finds_job(self, client):
        """Invariante 1: todo 202 corresponde a un job recuperable desde
        cualquier instancia (hoy: TTLCache local → 404)."""
        orch = CountingOrch([_ext()], _cls("generate_response"), _gr_outcome())
        _use_orch(client, orch)
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 202
        job_id = r.json()["ticket_job_id"]

        _simulate_new_instance(client.app)

        r2 = client.get(f"/api/v1/tickets/{job_id}")
        assert r2.status_code == 200, (
            f"poll desde otra instancia devolvió {r2.status_code}: el job vive "
            "en memoria process-local"
        )

    def test_worker_restart_preserves_job(self, client):
        """Un restart/deploy no puede hacer desaparecer un job aceptado."""
        orch = CountingOrch([_ext()], _cls("generate_response"), _gr_outcome())
        _use_orch(client, orch)
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 202
        job_id = r.json()["ticket_job_id"]

        # restart: se pierde el proceso entero (stores y tasks locales)
        _simulate_new_instance(client.app)
        queue = client.app.state.ticket_queue
        for t in list(getattr(queue, "_tasks", [])):
            t.cancel()

        r2 = client.get(f"/api/v1/tickets/{job_id}")
        assert r2.status_code == 200
        assert r2.json()["state"] in {
            "queued", "running", "succeeded", "partial", "failed", "timeout", "cancelled"
        }

    def test_partial_scrape_aggregates_to_partial(self):
        """HT-07: scrape_status=partial no puede publicarse como succeeded."""
        from api.ticket_worker import _entry_from_outcome, aggregate_states
        outcome = InquiryOutcome(inquiry="q", topic="rollover",
                                 route="generate_response", scrape_status="partial",
                                 generate_result=_gr_result())
        entry = _entry_from_outcome(0, outcome)
        state, next_action = aggregate_states([entry], unprocessed=0)
        assert state.value == "partial"
        assert next_action.value == "use_legacy_or_human"
        assert entry["participant_reply_safe"] is False

    async def test_second_inquiry_timeout_preserves_first_result(self, monkeypatch):
        """HT-08: el timeout de una inquiry no borra los resultados ya
        completados ni se etiqueta como timeout total."""
        import asyncio
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        monkeypatch.setattr(app_settings, "TICKET_INQUIRY_BUDGET_S", 0.05)
        monkeypatch.setattr(app_settings, "TICKET_TOTAL_BUDGET_S", 5.0)

        class TwoSpeedOrch(FakeOrch):
            def __init__(self):
                super().__init__([_ext(), _ext("second", "hardship")],
                                 _cls("generate_response"), None)

            async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
                if ext.inquiry == "second":
                    await asyncio.sleep(1.0)      # excede el budget por inquiry
                return _gr_outcome()

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await TestRunTicketJob._seed_job(repo)
        app = TestRunTicketJob._worker_app(repo, TwoSpeedOrch())

        final = await run_ticket_job(app, rec.job_id)

        completed = [e for e in final.per_inquiry_status if e.get("result")]
        assert len(completed) >= 1, "el resultado de la primera inquiry se perdió"
        assert final.state.value == "partial", (
            f"estado {final.state!r}: un timeout por-inquiry con resultados "
            "previos debe agregar a partial, no a timeout total"
        )
        timeouts = [e for e in final.per_inquiry_status
                    if e.get("execution_status") == "timeout"]
        assert timeouts and timeouts[0]["error"]["code"] == "INQUIRY_TIMEOUT", (
            "el código debe distinguir INQUIRY_TIMEOUT de TOTAL_JOB_TIMEOUT"
        )

    async def test_cancelled_worker_marks_job_cancelled_or_retryable(self):
        """La cancelación del worker no puede dejar el job en running eterno."""
        import asyncio
        from api.ticket_worker import run_ticket_job
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend, TicketJobRepository,
        )

        class HangingOrch(FakeOrch):
            async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
                await asyncio.sleep(30)
                return _gr_outcome()

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await TestRunTicketJob._seed_job(repo)
        orch = HangingOrch([_ext()], _cls("generate_response"), None)
        app = TestRunTicketJob._worker_app(repo, orch)

        task = asyncio.create_task(run_ticket_job(app, rec.job_id))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        final = await repo.get(rec.job_id)
        assert final.state.value != "running", (
            "worker cancelado dejó el job en running para siempre; debe quedar "
            "cancelled o retryable (queued/failed)"
        )
        assert final.state.value == "queued" and final.retryable is True


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 1) — contrato v2 e
# idempotencia obligatoria. RED hasta cerrar la Tarea 4.
# ---------------------------------------------------------------------------

def _v2_body(**over):
    """Body v2: sin ticket_handler_mode (v2 lo rechaza) ni idempotency_key."""
    base = _body()
    base.pop("ticket_handler_mode", None)
    base.pop("idempotency_key", None)
    base.update(over)
    return base


class SlowOrch(FakeOrch):
    """Mantiene el job en RUNNING (extract nunca termina) para probar cuotas
    de jobs pendientes sin carreras."""

    async def extract_inquiries(self, req):
        import asyncio
        await asyncio.sleep(30)
        return list(self._extracted)


class TestV2ContractRegressions:

    def test_v2_requires_idempotency_key_header(self, client):
        """Bloqueo 5 del plan: v2 debe EXIGIR Idempotency-Key; hoy un POST sin
        header se acepta y el replay es imposible."""
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"),
                                   InquiryOutcome(inquiry="q", topic="t",
                                                  route="knowledge_question",
                                                  knowledge_result=_kq_result())))
        r = client.post("/api/v2/handle-ticket", json=_v2_body())
        assert r.status_code in (400, 422), (
            f"v2 sin Idempotency-Key devolvió {r.status_code}; el header debe "
            "ser obligatorio (plan Tarea 4 Paso 4)"
        )

    def test_v2_always_returns_202_and_replays_same_job(self, client):
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), _gr_outcome()))
        headers = {"Idempotency-Key": "v2-key-1"}
        r1 = client.post("/api/v2/handle-ticket", json=_v2_body(), headers=headers)
        r2 = client.post("/api/v2/handle-ticket", json=_v2_body(), headers=headers)
        assert r1.status_code == 202 and r2.status_code == 202
        assert r1.json()["ticket_job_id"] == r2.json()["ticket_job_id"]
        assert r2.json()["idempotency_replayed"] is True

    def test_v2_poll_exposes_forusbots_job_ids_for_reconciliation(self, client):
        """El poll durable debe conservar los IDs de efectos externos también
        en v2; sin ellos n8n/operaciones no pueden reconciliar un POST ambiguo."""
        outcome = InquiryOutcome(
            inquiry="cash out 401k",
            topic="rollover",
            route="generate_response",
            scrape_status="ok",
            generate_result=_gr_result(),
            diagnostics={"forusbots_job_id": "forusbots-v2-9"},
        )
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), outcome))

        accepted = client.post(
            "/api/v2/handle-ticket",
            json=_v2_body(),
            headers={"Idempotency-Key": "v2-forusbots-reconciliation"},
        )
        assert accepted.status_code == 202

        polled = client.get(
            f"/api/v2/ticket-jobs/{accepted.json()['ticket_job_id']}"
        )
        assert polled.status_code == 200
        assert polled.json()["forusbots_job_ids"] == ["forusbots-v2-9"]

    def test_v2_same_key_different_payload_is_409(self, client):
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), _gr_outcome()))
        headers = {"Idempotency-Key": "v2-mismatch-1"}
        r1 = client.post("/api/v2/handle-ticket", json=_v2_body(), headers=headers)
        assert r1.status_code == 202
        r2 = client.post("/api/v2/handle-ticket",
                         json=_v2_body(participant_id="999999"), headers=headers)
        assert r2.status_code == 409

    def test_replay_pending_job_bypasses_quota_and_reensures_enqueue(self, client, monkeypatch):
        """Bloqueo 5 del plan: el replay de un job pendiente debe resolverse
        ANTES de las cuotas de jobs nuevos; hoy el cap de outstanding
        (count_active) corre primero y bloquea el replay con 429."""
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_MAX_OUTSTANDING_JOBS", 1)

        _use_orch(client, SlowOrch([_ext()], _cls("generate_response"), _gr_outcome()))
        headers = {"Idempotency-Key": "replay-pending-1"}
        r1 = client.post("/api/v2/handle-ticket", json=_v2_body(), headers=headers)
        assert r1.status_code == 202
        # el job sigue no-terminal (SlowOrch): outstanding == cap == 1
        r2 = client.post("/api/v2/handle-ticket", json=_v2_body(), headers=headers)
        assert r2.status_code == 202, (
            f"el replay del MISMO job pendiente devolvió {r2.status_code}; la "
            "resolución idempotente debe preceder al cap de jobs nuevos"
        )
        assert r2.json()["ticket_job_id"] == r1.json()["ticket_job_id"]

    def test_v1_inline_requires_send_participant_reply(self, client):
        """Bloqueo del plan (Tarea 4 Paso 6): un 200 inline de v1 sólo es
        válido con next_action=send_participant_reply y sin fallback; hoy un
        job shadow (use_legacy + fallback=true) se entrega como 200 plano."""
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"), None))
        r = client.post("/api/v1/handle-ticket",
                        json=_body(ticket_handler_mode="shadow"))
        if r.status_code == 200:
            data = r.json()
            assert data.get("next_action") in ("use_legacy", "use_legacy_or_human"), (
                "un job shadow succeeded+use_legacy se publicó como 200 inline "
                "sin acción legacy explícita (oculta use_legacy en un 200)"
            )
        else:
            assert r.status_code == 202


class TestParticipantPlanFailClosed:

    def test_active_mode_without_participant_plan_validator_fails_closed(self, client):
        """Bloqueo 1 del plan: participant_plan_validator=None en modo activo
        autoriza TODO hoy (fail-open). Debe fallar cerrado: 503 o rechazo."""
        client.app.state.participant_plan_validator = None
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"),
                                   InquiryOutcome(inquiry="q", topic="t",
                                                  route="knowledge_question",
                                                  knowledge_result=_kq_result())))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 503, (
            f"modo activo sin validador aceptó el ticket ({r.status_code}); "
            "una configuración None en modo activo debe ser fail-closed"
        )

    def test_wrong_participant_plan_or_tenant_is_403(self, client):
        """El contrato objetivo (Tarea 4 Paso 1) es un validador tenant-aware
        con .authorize(*, tenant_id, participant_id, plan_id) -> modelo | None.
        Hoy el call site invoca validator(participant_id, plan_id) posicional,
        así que un validador conforme al Protocol nuevo rompe con 503."""

        class RejectingValidator:
            async def authorize(self, *, tenant_id, participant_id, plan_id):
                return None  # mismatch

        client.app.state.participant_plan_validator = RejectingValidator()
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"),
                                   InquiryOutcome(inquiry="q", topic="t",
                                                  route="knowledge_question",
                                                  knowledge_result=_kq_result())))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 403, (
            f"mismatch participant-plan devolvió {r.status_code}; el contrato "
            "tenant-aware debe producir 403 PARTICIPANT_PLAN_MISMATCH"
        )
