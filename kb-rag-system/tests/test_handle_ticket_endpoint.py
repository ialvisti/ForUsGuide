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
