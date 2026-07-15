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
        monkeypatch.setattr(
            app_settings,
            "API_CLIENT_TENANTS",
            {"n8n": "tenant-n8n", "ops": "tenant-ops"},
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
            # Fail-closed (Tarea 4): sin fuente participant-plan no hay
            # autorización; estos tests usan una fuente sintética allow-all
            # salvo los que inyectan mismatch/indisponibilidad.
            c.app.state.participant_plan_validator = _AllowAllPP()
            yield c
        app.dependency_overrides.clear()


class _AllowAllPP:
    async def authorize(self, *, tenant_id, participant_id, plan_id):
        from api.participant_plan import AuthorizedParticipantPlan
        return AuthorizedParticipantPlan(
            tenant_id=tenant_id, participant_id=participant_id, plan_id=plan_id,
        )


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
        fuente canónica ANTES de cualquier scrape (contrato tenant-aware de
        Tarea 4: authorize(*, tenant_id, participant_id, plan_id) → None)."""
        _use_orch(client)

        class _Rejecting:
            async def authorize(self, *, tenant_id, participant_id, plan_id):
                return None

        client.app.state.participant_plan_validator = _Rejecting()
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code == 403, (
            f"pareja participant-plan inválida devolvió {r.status_code}: "
            "no existe verificación de asociación"
        )
        assert r.json()["detail"]["code"] == "PARTICIPANT_PLAN_MISMATCH"

    def test_canonical_validator_cannot_authorize_different_identity(self, client):
        """Un adaptador defectuoso no puede autorizar A y devolver el registro B."""
        _use_orch(client)

        class _WrongCanonicalRecord:
            async def authorize(self, *, tenant_id, participant_id, plan_id):
                from api.participant_plan import AuthorizedParticipantPlan
                return AuthorizedParticipantPlan(
                    tenant_id=tenant_id,
                    participant_id="different-participant",
                    plan_id=plan_id,
                    record_keeper="Canonical RK",
                )

        client.app.state.participant_plan_validator = _WrongCanonicalRecord()
        response = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers={"X-API-Key": KEY_N8N},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "PARTICIPANT_PLAN_MISMATCH"

    def test_poll_rejects_same_principal_after_tenant_changes(
            self, client, monkeypatch):
        """Ownership incluye tenant; principal por sí solo no cruza tenants."""
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "API_CLIENT_TENANTS", {"n8n": "tenant-a"})
        _use_orch(client)
        created = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers={"X-API-Key": KEY_N8N},
        )
        assert created.status_code == 202

        monkeypatch.setattr(app_settings, "API_CLIENT_TENANTS", {"n8n": "tenant-b"})
        polled = client.get(
            f"/api/v1/tickets/{created.json()['ticket_job_id']}",
            headers={"X-API-Key": KEY_N8N},
        )
        assert polled.status_code == 403

    def test_idempotent_post_cannot_replay_job_after_tenant_changes(
            self, client, monkeypatch):
        """El replay se autoriza antes de reasegurar Cloud Tasks.

        Si el mismo principal fue remapeado, la key del tenant anterior debe
        dar conflicto sin devolver ni reencolar aquel job.
        """
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "API_CLIENT_TENANTS", {"n8n": "tenant-a"})
        _use_orch(client)
        headers = {
            "X-API-Key": KEY_N8N,
            "Idempotency-Key": "tenant-bound-route-key",
        }
        created = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers=headers,
        )
        assert created.status_code == 202

        monkeypatch.setattr(app_settings, "API_CLIENT_TENANTS", {"n8n": "tenant-b"})
        replay = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers=headers,
        )

        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "IDEMPOTENCY_TENANT_MISMATCH"


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


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 1) — identidad workload WIF
# en v2. RED hasta cerrar la Tarea 4 Paso 2a.
# ---------------------------------------------------------------------------

def _v2_body(**over):
    base = _body()
    base.pop("ticket_handler_mode", None)
    base.update(over)
    return base


def _unsigned_jwt(payload: dict) -> str:
    """JWT alg=none (sin firma): nunca debe aceptarse."""
    import base64
    import json as _json

    def _b64(obj):
        return base64.urlsafe_b64encode(
            _json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."


class TestWorkloadIdentityV2:
    """v2 activo exige DOS credenciales independientes: X-API-Key (cliente/
    tenant) y X-ForUs-Workload-Authorization con un ID token Google-signed de
    la SA n8n-ticket-invoker-{env}. Cloud Run elimina la firma de
    X-Serverless-Authorization antes de entregarlo, así que ese header queda
    prohibido. Parametrizado para el caso privado (Authorization duplicado)
    y el caso público (header propio como control obligatorio)."""

    @staticmethod
    def _enable_wif(monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WIF_AUDIENCE",
                            "https://kb-rag-system.example.run.app")
        monkeypatch.setattr(
            app_settings, "TICKET_WIF_EXPECTED_EMAIL",
            "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com")

    @pytest.mark.parametrize("extra_headers", [
        {},                                                # sin token workload
        {"X-ForUs-Workload-Authorization": "Bearer garbage-token"},  # basura
    ])
    def test_v2_rejects_missing_or_wrong_workload_identity_token_when_public(
            self, client, monkeypatch, extra_headers):
        self._enable_wif(monkeypatch)
        _use_orch(client)
        r = client.post("/api/v2/handle-ticket", json=_v2_body(),
                        headers={"X-API-Key": KEY_N8N,
                                 "Idempotency-Key": "wif-red-1",
                                 **extra_headers})
        assert r.status_code in (401, 403), (
            f"v2 sin identidad workload verificable devolvió {r.status_code}; "
            "X-API-Key solo no autoriza (plan Tarea 4 Paso 2a)"
        )

    def test_v2_accepts_expected_wif_service_account_and_audience(self, client, monkeypatch):
        from api import auth as api_auth
        if not hasattr(api_auth, "verify_workload_identity_token"):
            pytest.fail(
                "RED: api.auth.verify_workload_identity_token no existe — la "
                "verificación WIF de v2 (firma/issuer/audience/SA/exp) no está "
                "implementada (plan Tarea 4 Paso 2a)"
            )
        self._enable_wif(monkeypatch)
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "https://kb-rag-system.example.run.app",
            "email": "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com",
            "email_verified": True,
            "exp": 4102444800,
        }
        monkeypatch.setattr(
            api_auth, "_verify_google_id_token", lambda token, audience: claims,
            raising=True,
        )
        _use_orch(client)
        # token estructuralmente válido (RS256): el pre-parseo local pasa y la
        # verificación de firma queda en el seam patcheado
        import base64
        import json as _json

        def _b64(obj):
            return base64.urlsafe_b64encode(
                _json.dumps(obj).encode()).rstrip(b"=").decode()

        token = (f"{_b64({'alg': 'RS256', 'typ': 'JWT', 'kid': 'k1'})}."
                 f"{_b64(claims)}.c2ln")
        r = client.post("/api/v2/handle-ticket", json=_v2_body(),
                        headers={"X-API-Key": KEY_N8N,
                                 "Idempotency-Key": "wif-ok-1",
                                 "X-ForUs-Workload-Authorization": f"Bearer {token}"})
        assert r.status_code == 202

    def test_v2_rejects_legacy_api_key_without_explicit_tenant_mapping(
            self, client, monkeypatch):
        """v2 nunca hereda el principal/tenant ``default`` de API_KEY legacy."""
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "API_CLIENT_KEYS", {})
        monkeypatch.setattr(app_settings, "API_CLIENT_TENANTS", {})
        _use_orch(client)
        response = client.post(
            "/api/v2/handle-ticket",
            json=_v2_body(),
            headers={
                "X-API-Key": KEY_N8N,
                "Idempotency-Key": "strict-v2-client",
            },
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("headers", [
        # Cloud Run despoja la firma de X-Serverless-Authorization: prohibido.
        {"X-Serverless-Authorization": "Bearer whatever"},
        # JWT sin firma / alg none: jamás aceptable.
        {"X-ForUs-Workload-Authorization":
            "Bearer PLACEHOLDER_UNSIGNED"},
    ])
    def test_v2_rejects_x_serverless_authorization_or_unsigned_token(
            self, client, monkeypatch, headers):
        self._enable_wif(monkeypatch)
        if "X-ForUs-Workload-Authorization" in headers:
            headers = {"X-ForUs-Workload-Authorization":
                       "Bearer " + _unsigned_jwt({
                           "iss": "https://accounts.google.com",
                           "aud": "https://kb-rag-system.example.run.app",
                           "email": "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com",
                           "email_verified": True,
                       })}
        _use_orch(client)
        r = client.post("/api/v2/handle-ticket", json=_v2_body(),
                        headers={"X-API-Key": KEY_N8N,
                                 "Idempotency-Key": "wif-red-2",
                                 **headers})
        assert r.status_code in (401, 403), (
            f"token no verificable aceptado con {r.status_code}: "
            "X-Serverless-Authorization y JWT sin firma deben rechazarse"
        )


# ---------------------------------------------------------------------------
# Revisión adversarial (Tarea 15 Paso 5) — P1: v1 no puede evadir WIF; P2:
# record_keeper server-owned incluso cuando la fuente devuelve None.
# ---------------------------------------------------------------------------


async def test_workload_token_verification_does_not_block_event_loop(
        monkeypatch):
    """google-auth puede hacer I/O síncrono al refrescar certificados. La
    dependencia async no debe ejecutar ese trabajo en el event loop."""
    import threading
    from api import main as api_main

    event_loop_thread = threading.get_ident()
    verifier_threads = []

    def fake_verify(_request):
        verifier_threads.append(threading.get_ident())

    monkeypatch.setattr(
        api_main,
        "verify_workload_identity_token",
        fake_verify,
    )

    await api_main.verify_workload_identity(object())

    assert verifier_threads
    assert verifier_threads[0] != event_loop_thread


async def test_global_error_logging_never_emits_exception_payload(caplog):
    from starlette.requests import Request
    from api.middleware import handle_errors, log_requests

    sentinel = "participant-secret-sentinel@example.com"
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v2/handle-ticket",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    })
    request.state.request_id = "safe-request-id"

    async def explode(_request):
        raise RuntimeError(sentinel)

    with caplog.at_level("ERROR"):
        response = await handle_errors(
            request,
            lambda req: log_requests(req, explode),
        )

    assert response.status_code == 500
    assert sentinel not in caplog.text

    caplog.clear()
    from api.main import general_exception_handler

    with caplog.at_level("ERROR"):
        response = await general_exception_handler(
            request, RuntimeError(sentinel)
        )
    assert response.status_code == 500
    assert sentinel not in caplog.text

class TestV1AlsoRequiresWorkloadIdentity:
    """P1 review: v1 alcanza el MISMO pipeline durable que v2, así que la
    identidad workload también lo protege cuando está configurada. Un
    X-API-Key filtrado no basta una vez activado WIF."""

    def test_v1_rejects_missing_workload_token_when_wif_enforced(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "TICKET_WIF_AUDIENCE",
                            "https://kb-rag-system.example.run.app")
        monkeypatch.setattr(
            app_settings, "TICKET_WIF_EXPECTED_EMAIL",
            "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com")
        _use_orch(client)
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})  # sin token workload
        assert r.status_code in (401, 403), (
            f"v1 con WIF activo aceptó sólo X-API-Key ({r.status_code}); el "
            "segundo factor es evadible vía v1 (P1 review)"
        )

    def test_v1_still_works_without_wif_during_migration(self, client):
        """Ventana de migración: WIF no configurado ⇒ v1 sigue con X-API-Key
        (no rompe el caller legacy)."""
        _use_orch(client)
        r = client.post("/api/v1/handle-ticket", json=_body(),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code in (200, 202)

    def test_v1_poll_rejects_missing_workload_token_when_wif_enforced(
            self, client, monkeypatch):
        """El segundo factor protege también el GET que revela el resultado.

        El job se crea durante la ventana legacy para aislar la política del
        poll; al activar WIF, una API key filtrada no debe poder leerlo.
        """
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WIF_AUDIENCE", "")
        monkeypatch.setattr(app_settings, "TICKET_WIF_EXPECTED_EMAIL", "")
        _use_orch(client)
        created = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers={"X-API-Key": KEY_N8N},
        )
        assert created.status_code in (200, 202)
        job_id = created.json().get("ticket_job_id")
        assert job_id, "el test necesita el contrato durable 202 + poll"

        monkeypatch.setattr(
            app_settings,
            "TICKET_WIF_AUDIENCE",
            "https://kb-rag-system.example.run.app",
        )
        monkeypatch.setattr(
            app_settings,
            "TICKET_WIF_EXPECTED_EMAIL",
            "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com",
        )
        polled = client.get(
            f"/api/v1/tickets/{job_id}",
            headers={"X-API-Key": KEY_N8N},
        )

        assert polled.status_code in (401, 403), (
            "GET v1 aceptó sólo X-API-Key con WIF activo; el resultado "
            "durable evade el segundo factor"
        )

    def test_v1_poll_remains_available_when_handler_disabled_without_wif(
            self, client, monkeypatch):
        """El kill switch no invalida el poll de jobs ya aceptados."""
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "TICKET_WIF_AUDIENCE", "")
        monkeypatch.setattr(app_settings, "TICKET_WIF_EXPECTED_EMAIL", "")
        _use_orch(client)
        created = client.post(
            "/api/v1/handle-ticket",
            json=_body(),
            headers={"X-API-Key": KEY_N8N},
        )
        assert created.status_code in (200, 202)
        job_id = created.json().get("ticket_job_id")
        assert job_id, "el test necesita el contrato durable 202 + poll"

        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        polled = client.get(
            f"/api/v1/tickets/{job_id}",
            headers={"X-API-Key": KEY_N8N},
        )

        assert polled.status_code == 200


class TestRecordKeeperServerOwned:

    def test_caller_record_keeper_dropped_when_canonical_is_none(self, client):
        """P2 review: la fuente canónica devuelve record_keeper=None; el valor
        del body NO debe sobrevivir en el job persistido."""
        class _NoneRK:
            async def authorize(self, *, tenant_id, participant_id, plan_id):
                from api.participant_plan import AuthorizedParticipantPlan
                return AuthorizedParticipantPlan(
                    tenant_id=tenant_id, participant_id=participant_id,
                    plan_id=plan_id, record_keeper=None)

        client.app.state.participant_plan_validator = _NoneRK()
        _use_orch(client)
        r = client.post("/api/v1/handle-ticket",
                        json=_body(record_keeper="Caller Provided RK"),
                        headers={"X-API-Key": KEY_N8N})
        assert r.status_code in (200, 202)
        job_id = r.json().get("ticket_job_id") or r.json().get("primary", {}).get("job_id")

        async def _get(repo, jid):
            return await repo.get(jid)

        if job_id:
            rec = client.portal.call(_get, client.app.state.ticket_repo, job_id)
            assert rec.request_payload.get("record_keeper") is None, (
                "el record_keeper del caller sobrevivió pese a que la fuente "
                "canónica no lo afirmó (P2 review)"
            )
