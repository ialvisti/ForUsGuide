"""
Integration tests for the /api/v1/handle-ticket + /api/v1/tickets/{id} endpoints.

The lifespan runs with patched engine constructors (mirrors tests/test_api.py),
so app.state has the real TicketJobStore / idempotency cache / bg_tasks. The
orchestrator dependency is overridden with a fake that returns canned outcomes;
``verify_api_key`` is overridden to a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
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

    mock_engine = Mock()
    mock_pinecone = Mock()
    mock_pinecone.get_index_stats.return_value = {"total_vectors": 0}
    mock_inquiry_router = Mock()

    with patch("api.main.validate_settings"), \
         patch("api.main.RAGEngine", return_value=mock_engine), \
         patch("api.main.PineconeUploader", return_value=mock_pinecone), \
         patch("api.main.InquiryRouterEngine", return_value=mock_inquiry_router):
        from api.main import app, verify_api_key
        app.dependency_overrides[verify_api_key] = lambda: None
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


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

    def test_empty_extraction_200_needs_more_info(self, client):
        _use_orch(client, FakeOrch([], _cls("needs_more_info"), None))
        r = client.post("/api/v1/handle-ticket", json=_body())
        assert r.status_code == 200
        data = r.json()
        assert data["route_taken"] == "needs_more_info"
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
        # classifier says generate_response, but knowledge_only coerces to NMI inline
        _use_orch(client, FakeOrch([_ext()], _cls("generate_response"), None))
        r = client.post("/api/v1/handle-ticket", json=_body(ticket_handler_mode="knowledge_only"))
        assert r.status_code == 200
        data = r.json()
        assert data["route_taken"] == "needs_more_info"
        assert "ticket_handler_override" in data["primary"]["diagnostics"]

    def test_shadow_returns_fallback(self, client):
        _use_orch(client, FakeOrch([_ext()], _cls("knowledge_question"), None))
        r = client.post("/api/v1/handle-ticket", json=_body(ticket_handler_mode="shadow"))
        assert r.status_code == 200
        data = r.json()
        assert data["route_taken"] == "needs_more_info"
        assert data["metadata"]["fallback"] is True
        assert data["metadata"]["shadow_routes"] == ["knowledge_question"]


# ---------------------------------------------------------------------------
# Slow (job) path
# ---------------------------------------------------------------------------

class TestSlowPath:

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
        # the job exists in the in-process store
        assert client.app.state.ticket_jobs.get(data["ticket_job_id"]) is not None

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
        store = client.app.state.ticket_jobs
        job = store.create()
        store.set_state(job.ticket_job_id, state="succeeded", outcomes=[outcome],
                        forusbots_job_ids=["job-9"], total_inquiries=1)
        r = client.get(f"/api/v1/tickets/{job.ticket_job_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["state"] == "succeeded"
        assert data["route_taken"] == "generate_response"
        assert data["primary"]["generate_response"]["decision"] == "can_proceed"
        assert data["forusbots_job_ids"] == ["job-9"]


# ---------------------------------------------------------------------------
# Background runner (direct, deterministic)
# ---------------------------------------------------------------------------

class TestRunTicketJob:

    async def test_run_ticket_job_succeeded(self):
        from api.main import _run_ticket_job
        from data_pipeline.ticket_jobs import TicketJobStore
        from api.models import HandleTicketRequest

        store = TicketJobStore()
        job = store.create()
        app = SimpleNamespace(state=SimpleNamespace(ticket_jobs=store))
        outcome = InquiryOutcome(inquiry="q", topic="rollover", route="generate_response",
                                 scrape_status="ok", generate_result=_gr_result(),
                                 diagnostics={"forusbots_job_id": "j1"})
        orch = FakeOrch([_ext()], _cls("generate_response"), outcome)
        req = HandleTicketRequest(**_body())

        await _run_ticket_job(app, job.ticket_job_id, orch, [_ext()], [_cls("generate_response")],
                              [("generate_response", None)], req, 1, None, None, "full", 0.0)

        updated = store.get(job.ticket_job_id)
        assert updated.state == "succeeded"
        assert updated.forusbots_job_ids == ["j1"]
        assert len(updated.outcomes) == 1

    async def test_run_ticket_job_partial_on_scrape_failure(self):
        from api.main import _run_ticket_job
        from data_pipeline.ticket_jobs import TicketJobStore
        from api.models import HandleTicketRequest

        store = TicketJobStore()
        job = store.create()
        app = SimpleNamespace(state=SimpleNamespace(ticket_jobs=store))
        outcome = InquiryOutcome(inquiry="q", topic="rollover", route="generate_response",
                                 scrape_status="failed", generate_result=_gr_result("uncertain"))
        orch = FakeOrch([_ext()], _cls("generate_response"), outcome)
        req = HandleTicketRequest(**_body())

        await _run_ticket_job(app, job.ticket_job_id, orch, [_ext()], [_cls("generate_response")],
                              [("generate_response", None)], req, 1, None, None, "full", 0.0)

        assert store.get(job.ticket_job_id).state == "partial"


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

    def test_50_concurrent_same_key_create_one_execution(self, client):
        """Invariante 2: una key idempotente produce como máximo UNA ejecución
        lógica. La reserva debe ocurrir en transacción ANTES de cualquier LLM."""
        from concurrent.futures import ThreadPoolExecutor

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
    process-local se recrea; sólo el backend durable (si existe) se comparte."""
    st = app.state
    if hasattr(st, "ticket_repo"):
        from data_pipeline.ticket_job_repository import TicketJobRepository
        st.ticket_repo = TicketJobRepository(st.ticket_repo.backend)
    if hasattr(st, "ticket_jobs"):
        from data_pipeline.ticket_jobs import TicketJobStore
        st.ticket_jobs = TicketJobStore()
    if hasattr(st, "ticket_idem"):
        from cachetools import TTLCache
        st.ticket_idem = TTLCache(maxsize=2048, ttl=1800)


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
        st = client.app.state
        for t in list(getattr(st, "bg_tasks", [])):
            t.cancel()

        r2 = client.get(f"/api/v1/tickets/{job_id}")
        assert r2.status_code == 200
        assert r2.json()["state"] in {
            "queued", "running", "succeeded", "partial", "failed", "timeout", "cancelled"
        }

    def test_partial_scrape_aggregates_to_partial(self):
        """HT-07: scrape_status=partial no puede publicarse como succeeded."""
        from api.main import _aggregate_job_state
        outcome = InquiryOutcome(inquiry="q", topic="rollover",
                                 route="generate_response", scrape_status="partial",
                                 generate_result=_gr_result())
        assert _aggregate_job_state([outcome]) == "partial"

    async def test_second_inquiry_timeout_preserves_first_result(self, monkeypatch):
        """HT-08: el timeout de una inquiry no borra los resultados ya
        completados ni se etiqueta como timeout total."""
        import asyncio
        from api.main import _run_ticket_job
        from api.config import settings as app_settings
        from data_pipeline.ticket_jobs import TicketJobStore
        from api.models import HandleTicketRequest

        monkeypatch.setattr(app_settings, "TICKET_INQUIRY_BUDGET_S", 0.05)
        monkeypatch.setattr(app_settings, "TICKET_TOTAL_BUDGET_S", 5.0)

        class TwoSpeedOrch(FakeOrch):
            def __init__(self):
                super().__init__([_ext(), _ext("second", "hardship")], _cls("generate_response"), None)

            async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
                if ext.inquiry == "second":
                    await asyncio.sleep(1.0)      # excede el budget por inquiry
                return _gr_outcome()

        store = TicketJobStore()
        job = store.create()
        app = SimpleNamespace(state=SimpleNamespace(ticket_jobs=store))
        req = HandleTicketRequest(**_body())
        exts = [_ext(), _ext("second", "hardship")]
        cls_ = [_cls("generate_response")] * 2
        gated = [("generate_response", None)] * 2

        await _run_ticket_job(app, job.ticket_job_id, TwoSpeedOrch(), exts, cls_,
                              gated, req, 2, None, None, "full", 0.0)

        updated = store.get(job.ticket_job_id)
        assert len(updated.outcomes) >= 1, "el resultado de la primera inquiry se perdió"
        assert updated.state == "partial", (
            f"estado {updated.state!r}: un timeout por-inquiry con resultados "
            "previos debe agregar a partial, no a timeout total"
        )
        assert updated.error != "ticket_total_budget_exceeded"

    async def test_cancelled_worker_marks_job_cancelled_or_retryable(self):
        """La cancelación del worker no puede dejar el job en running eterno."""
        import asyncio
        from api.main import _run_ticket_job
        from data_pipeline.ticket_jobs import TicketJobStore
        from api.models import HandleTicketRequest

        class HangingOrch(FakeOrch):
            async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
                await asyncio.sleep(30)
                return _gr_outcome()

        store = TicketJobStore()
        job = store.create()
        app = SimpleNamespace(state=SimpleNamespace(ticket_jobs=store))
        req = HandleTicketRequest(**_body())
        orch = HangingOrch([_ext()], _cls("generate_response"), None)

        task = asyncio.create_task(_run_ticket_job(
            app, job.ticket_job_id, orch, [_ext()], [_cls("generate_response")],
            [("generate_response", None)], req, 1, None, None, "full", 0.0))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        state = store.get(job.ticket_job_id).state
        assert state != "running", (
            "worker cancelado dejó el job en running para siempre; debe quedar "
            "cancelled o retryable (queued/failed)"
        )
