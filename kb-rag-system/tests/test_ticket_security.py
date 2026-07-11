"""
Tests de seguridad del ticket handler (Task 2/6 del plan de remediación).

Cubren HT-04 (autorización de objetos), HT-06 (límites de recursos) y HT-10
(expansión de modo). Usan el path de autenticación REAL (sin dependency
override de verify_api_key) para poder probar identidad multi-principal.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome

KEY_N8N = "key-n8n-aaaaaaaaaaaaaaaa"
KEY_OPS = "key-ops-bbbbbbbbbbbbbbbb"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY_N8N)
    monkeypatch.setenv("PINECONE_API_KEY", "p")
    monkeypatch.setenv("OPENAI_API_KEY", "o")

    from api.config import settings as app_settings
    monkeypatch.setattr(app_settings, "API_KEY", KEY_N8N)
    monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
    # Dos principals válidos: la identidad viene de la credencial, no del body.
    # (El setting API_CLIENT_KEYS nace en Task 6; hasta entonces el intento de
    # configurarlo es un no-op y los tests multi-principal fallan en rojo.)
    if "API_CLIENT_KEYS" in type(app_settings).model_fields:
        monkeypatch.setattr(
            app_settings, "API_CLIENT_KEYS", {"n8n": KEY_N8N, "ops": KEY_OPS}
        )

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


def _body(**over):
    base = dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": "401k", "email_body": "quiero retirar mi 401k"},
        record_keeper="LT Trust",
    )
    base.update(over)
    return base


def _gr_result():
    return SimpleNamespace(decision="can_proceed", confidence=0.8,
                           response={"outcome": "can_proceed"}, source_articles=[],
                           used_chunks=[], coverage_gaps=[], metadata={})


class FakeOrch:
    def __init__(self):
        self._ext = ExtractedInquiry("cash out 401k", "LT Trust", "401(k)", "rollover")

    async def extract_inquiries(self, req):
        return [self._ext]

    async def classify(self, inquiry):
        return SimpleNamespace(route="generate_response", confidence=0.9,
                               reasoning="r", user_message=None)

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        return InquiryOutcome(inquiry=ext.inquiry, topic=ext.topic,
                              route="generate_response", scrape_status="ok",
                              generate_result=_gr_result())


def _use_orch(client):
    from api.main import app, get_ticket_orchestrator
    orch = FakeOrch()
    app.dependency_overrides[get_ticket_orchestrator] = lambda: orch
    # el worker durable resuelve el orchestrator vía factory en app.state
    client.app.state.ticket_orchestrator_factory = lambda: orch


class TestModeExpansion:

    def test_disabled_mode_cannot_be_expanded_by_request(self, client, monkeypatch):
        """Invariante 9 (lock de Task 0): el body no expande el kill switch."""
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        r = client.post("/api/v1/handle-ticket",
                        json=_body(ticket_handler_mode="full"),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code == 503


class TestObjectAuthorization:

    def test_cross_principal_job_poll_is_403(self, client):
        """Invariante 10: un job sólo es visible para el principal que lo creó."""
        _use_orch(client)
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code == 202, (
            f"POST con credencial de principal n8n devolvió {r.status_code}: "
            "la autenticación multi-principal no existe"
        )
        job_id = r.json()["ticket_job_id"]

        r_other = client.get(f"/api/v1/tickets/{job_id}",
                             headers={"X-API-Key": KEY_OPS})
        assert r_other.status_code == 403
        # el 403 debe ser de OWNERSHIP con credencial válida, no un 403 de
        # "invalid API key" (eso probaría otra cosa)
        detail = r_other.json().get("detail")
        assert isinstance(detail, dict) and detail.get("code") == "TICKET_JOB_FORBIDDEN", (
            f"403 sin código de ownership: {detail!r} — la identidad "
            "multi-principal / autorización de objetos no existe"
        )

        r_owner = client.get(f"/api/v1/tickets/{job_id}",
                             headers={"X-API-Key": KEY_N8N})
        assert r_owner.status_code == 200

    def test_invalid_participant_plan_pair_is_403(self, client):
        """Invariante 10: la asociación participant-plan se verifica contra una
        fuente canónica ANTES de cualquier scrape."""
        _use_orch(client)

        async def _reject(participant_id, plan_id):
            return False

        client.app.state.participant_plan_validator = _reject
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code == 403, (
            f"pareja participant-plan inválida devolvió {r.status_code}: "
            "no existe verificación de asociación"
        )


class TestResourceBounds:

    def test_oversized_body_is_413(self, client):
        _use_orch(client)
        huge = _body()
        huge["ticket"]["email_body"] = "x" * (1024 * 1024 + 1)
        r = client.post("/api/v1/handle-ticket", json=huge,
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code in (413, 422), (
            f"un body de >1MB fue aceptado con {r.status_code}"
        )

    def test_oversized_idempotency_key_is_rejected(self, client):
        _use_orch(client)
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N,
                                 "Idempotency-Key": "k" * (1024 * 1024)})
        assert r.status_code in (400, 422), (
            f"una Idempotency-Key de 1MB fue aceptada con {r.status_code}"
        )

    def test_rate_limit_returns_429_and_retry_after(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "RATE_LIMIT_HANDLE_TICKET", 3)
        _use_orch(client)
        responses = [
            client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})
            for _ in range(6)
        ]
        statuses = [r.status_code for r in responses]
        assert 429 in statuses, f"nunca hubo 429: {statuses}"
        first_429 = next(r for r in responses if r.status_code == 429)
        assert "Retry-After" in first_429.headers
