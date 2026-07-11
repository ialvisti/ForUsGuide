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
