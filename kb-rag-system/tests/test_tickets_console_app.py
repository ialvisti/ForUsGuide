"""Stage 5 Steps 5-6 — the standalone app's lifecycle, stack, and boundaries.

What this file exists to prevent:

*   a future edit that quietly gives the admin revision the RAG data plane, and
    with it Pinecone, OpenAI, ForusBots, and the production ``(default)``
    Firestore database;
*   a reordering of the middleware stack that lets a route run before identity
    is resolved, or that stops security headers from reaching an error response;
*   a Content-Security-Policy relaxation — ``'unsafe-inline'`` is the one change
    that would make a stored-XSS path into a reviewer session exploitable;
*   the RAG service's ``X-API-Key`` becoming an accepted credential here;
*   a revision starting with a configuration that should have refused to boot.
"""

from __future__ import annotations

import ast
import base64
import inspect
import logging
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.reviewer_auth import (
    IAP_ASSERTION_HEADER,
    IAP_ISSUER,
    AuthConfigurationError,
)
from api.ticket_review_routes import API_PREFIX
from api.tickets_console_config import TicketConsoleSettings
from api.tickets_console_main import (
    FORBIDDEN_IMPORTS,
    MIDDLEWARE_ORDER,
    PUBLIC_PATHS,
    SECURITY_HEADERS,
    UI_ASSETS_DIRECTORY,
    UI_INDEX_FILE,
    UI_PLACEHOLDER,
    app as production_app,
    build_console_app,
    lifespan,
    safe_route_template,
    validate_console_startup,
)

CURSOR_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
CONSOLE_ORIGIN = "https://tickets-console-abc-uc.a.run.app"
CSRF_SECRET = "synthetic-csrf-value"  # pragma: allowlist secret
PROD_BROKER_URL = "https://tickets-evidence-broker-abc-us-central1.a.run.app"
IAP_AUDIENCE = "/projects/1234567890/locations/us-central1/services/tickets-console"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/synthetic:product/1"
ADMIN_EMAIL = "admin@example.invalid"
ASSERTION = "synthetic.iap.assertion"
T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "iap",
        "IAP_AUDIENCE": IAP_AUDIENCE,
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "ROLE_BINDINGS_JSON": f'{{"{ADMIN_EMAIL}": "admin"}}',
        "CSRF_SIGNING_SECRET": CSRF_SECRET,
        "CURSOR_AEAD_KEY": CURSOR_KEY_B64,
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "GCP_PROJECT": "emulator-project",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-emulator",
        "DEVREV_ALLOWED_PART_DONS": [SYNTHETIC_PART],
        "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS": [2],
        "DEVREV_ALLOWED_TIMELINE_VISIBILITIES": ["internal", "external"],
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


def _production_settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    values: dict[str, object] = {
        "ENVIRONMENT": "production",
        "AUTH_MODE": "iap",
        "ALLOW_LOCAL_AUTH": False,
        "ALLOW_UNBOUND_VIEWERS": False,
        "ENABLE_SYNTHETIC_VERIFICATION": False,
        "GCP_PROJECT": "rag-kb-system",
        "FIRESTORE_DATABASE": "tickets-console-prod",
        "EVIDENCE_BROKER_URL": PROD_BROKER_URL,
        "EVIDENCE_BROKER_AUDIENCE": PROD_BROKER_URL,
        "DEVREV_TOKEN": "synthetic-devrev-value",
    }
    values.update(overrides)
    return _settings(monkeypatch, **values)


def _client(monkeypatch, *, settings=None, **collaborators) -> TestClient:
    resolved = settings if settings is not None else _settings(monkeypatch)

    def verifier(token: str, audience: str):
        return {
            "iss": IAP_ISSUER,
            "aud": audience,
            "sub": f"accounts.google.com:{ADMIN_EMAIL}",
            "email": ADMIN_EMAIL,
        }

    collaborators.setdefault("claims_verifier", verifier)
    collaborators.setdefault("clock", lambda: T0)
    collaborators.setdefault("firestore_database", "tickets-console-emulator")
    app = build_console_app(resolved, **collaborators)
    return TestClient(app, raise_server_exceptions=False)


def _auth(**extra) -> dict[str, str]:
    headers = {IAP_ASSERTION_HEADER: ASSERTION}
    headers.update(extra)
    return headers


# =====================================================================
# The middleware stack
# =====================================================================


class TestMiddlewareOrder:

    def test_the_declared_order_is_the_registered_order(self):
        """``user_middleware[0]`` is the outermost layer.

        Registration walks ``MIDDLEWARE_ORDER`` in reverse because Starlette
        inserts each new middleware at the front, so this test is what proves the
        reversal was not dropped or doubled.
        """
        registered = [
            middleware.kwargs["dispatch"].__name__
            for middleware in production_app.user_middleware
        ]
        assert registered == [dispatch.__name__ for dispatch in MIDDLEWARE_ORDER]

    def test_the_order_is_the_one_the_contract_documents(self):
        assert [dispatch.__name__ for dispatch in MIDDLEWARE_ORDER] == [
            "add_request_id",
            "add_security_headers",
            "log_requests",
            "handle_exceptions",
            "limit_body_size",
            "authenticate",
            "guard_unsafe_requests",
        ]

    def test_identity_is_resolved_before_the_unsafe_guard(self):
        names = [dispatch.__name__ for dispatch in MIDDLEWARE_ORDER]
        assert names.index("authenticate") < names.index("guard_unsafe_requests")

    def test_security_headers_wrap_the_authentication_layer(self):
        names = [dispatch.__name__ for dispatch in MIDDLEWARE_ORDER]
        assert names.index("add_security_headers") < names.index("authenticate")

    def test_the_request_id_exists_before_anything_logs_it(self):
        names = [dispatch.__name__ for dispatch in MIDDLEWARE_ORDER]
        assert names.index("add_request_id") < names.index("log_requests")


# =====================================================================
# Security headers and caching
# =====================================================================


class TestSecurityHeaders:

    @pytest.mark.parametrize("name", sorted(SECURITY_HEADERS))
    def test_every_header_is_present_on_a_success(self, monkeypatch, name):
        response = _client(monkeypatch).get("/tickets", headers=_auth())
        assert response.headers[name] == SECURITY_HEADERS[name]

    def test_headers_are_present_on_an_unauthenticated_failure(self, monkeypatch):
        response = _client(monkeypatch).get(f"{API_PREFIX}/session")
        assert response.status_code == 401
        assert response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_headers_are_present_on_a_not_found(self, monkeypatch):
        response = _client(monkeypatch).get("/definitely-not-a-route", headers=_auth())
        assert response.status_code == 404
        assert response.headers["Referrer-Policy"] == "no-referrer"

    def test_the_csp_allows_no_inline_script_or_style(self):
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy
        assert "script-src 'self'" in policy
        assert "style-src 'self'" in policy

    def test_the_csp_pins_the_documented_directives(self):
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        for directive in (
            "default-src 'self'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            assert directive in policy

    def test_the_csp_names_no_external_host(self):
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        assert "http://" not in policy and "https://" not in policy
        assert "*" not in policy

    def test_html_is_never_cached(self, monkeypatch):
        response = _client(monkeypatch).get("/tickets", headers=_auth())
        assert response.headers["Cache-Control"] == "no-store"

    def test_authenticated_api_reads_are_never_cached(self, monkeypatch):
        response = _client(monkeypatch).get(f"{API_PREFIX}/session", headers=_auth())
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"

    def test_permissions_policy_disables_device_access(self):
        assert SECURITY_HEADERS["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )


class TestCors:

    def test_no_cors_middleware_is_installed(self):
        names = [middleware.cls.__name__ for middleware in production_app.user_middleware]
        assert "CORSMiddleware" not in names

    def test_no_response_advertises_a_permissive_origin(self, monkeypatch):
        client = _client(monkeypatch)
        for path in ("/livez", "/tickets", f"{API_PREFIX}/session"):
            response = client.get(path, headers=_auth(Origin="https://attacker.example"))
            assert "access-control-allow-origin" not in {
                key.lower() for key in response.headers
            }

    def test_a_preflight_is_not_answered_permissively(self, monkeypatch):
        response = _client(monkeypatch).options(
            f"{API_PREFIX}/reviews",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers.get("access-control-allow-origin") is None


# =====================================================================
# Health probes
# =====================================================================


class TestHealth:

    def test_livez_needs_no_identity(self, monkeypatch):
        response = _client(monkeypatch).get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_livez_is_public_by_declaration(self):
        assert "/livez" in PUBLIC_PATHS and "/readyz" in PUBLIC_PATHS

    def test_readyz_needs_no_identity_and_reports_configuration(self, monkeypatch):
        response = _client(
            monkeypatch, devrev=object(), repository=object(), service=object()
        ).get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["firestore_database"] == "tickets-console-emulator"
        assert body["ui_installed"] is UI_INDEX_FILE.is_file()

    def test_readyz_is_503_when_a_collaborator_is_missing(self, monkeypatch):
        response = _client(monkeypatch).get("/readyz")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "NOT_READY"

    def test_readyz_is_503_without_a_resolved_database(self, monkeypatch):
        response = _client(
            monkeypatch,
            devrev=object(),
            repository=object(),
            service=object(),
            firestore_database="",
        ).get("/readyz")
        assert response.status_code == 503

    def test_readyz_never_names_a_participant_ticket(self, monkeypatch):
        body = _client(
            monkeypatch, devrev=object(), repository=object(), service=object()
        ).get("/readyz").json()
        assert set(body) == {
            "status",
            "environment",
            "firestore_database",
            "evidence_broker",
            "ui_installed",
        }

    def test_the_probes_are_absent_from_the_documented_schema(self):
        assert "/livez" not in production_app.openapi()["paths"]
        assert "/readyz" not in production_app.openapi()["paths"]


# =====================================================================
# The browser entry points
# =====================================================================


class TestUserInterface:

    def test_the_index_serves_html(self, monkeypatch):
        response = _client(monkeypatch).get("/tickets", headers=_auth())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_a_deep_link_serves_the_same_document(self, monkeypatch):
        client = _client(monkeypatch)
        index = client.get("/tickets", headers=_auth())
        deep = client.get("/tickets/TKT-424242", headers=_auth())
        assert deep.status_code == 200
        assert deep.text == index.text

    def test_a_deep_link_never_reflects_its_path_into_the_page(self, monkeypatch):
        """The display id is not echoed, so the page is not a reflection sink.

        The payload carries no ``/``: a slashed one would not match the
        single-segment route at all and the test would pass for the wrong reason.
        """
        payload = "TKT-<img src=x onerror=alert(1)>"
        response = _client(monkeypatch).get(f"/tickets/{payload}", headers=_auth())
        assert response.status_code == 200
        assert "onerror" not in response.text
        assert "TKT-" not in response.text

    def test_the_placeholder_says_the_ui_stage_is_not_installed(self, monkeypatch):
        if UI_INDEX_FILE.is_file():
            pytest.skip("Stage 6 has installed a real UI in this build")
        response = _client(monkeypatch).get("/tickets", headers=_auth())
        assert "not installed" in response.text
        assert response.text == UI_PLACEHOLDER

    def test_the_placeholder_carries_no_inline_script(self):
        assert "<script" not in UI_PLACEHOLDER.lower()
        assert "onerror" not in UI_PLACEHOLDER.lower()

    def test_only_the_assets_directory_is_ever_mounted(self):
        mounted = [
            route.path
            for route in production_app.routes
            if route.__class__.__name__ == "Mount"
        ]
        assert mounted in ([], ["/tickets/assets"])

    def test_the_assets_mount_appears_only_when_the_directory_exists(self):
        mounted = [
            route.path
            for route in production_app.routes
            if route.__class__.__name__ == "Mount"
        ]
        assert bool(mounted) is UI_ASSETS_DIRECTORY.is_dir()

    def test_the_html_pages_still_require_a_verified_identity(self, monkeypatch):
        response = _client(monkeypatch).get("/tickets")
        assert response.status_code == 401


# =====================================================================
# The import boundary
# =====================================================================


class TestImportBoundary:

    def _imports(self, module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return names

    @pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
    def test_the_app_module_never_imports_the_rag_data_plane(self, forbidden):
        import api.tickets_console_main as module

        offenders = [name for name in self._imports(module) if forbidden in name]
        assert offenders == [], offenders

    @pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
    def test_the_router_never_imports_the_rag_data_plane(self, forbidden):
        import api.ticket_review_routes as module

        offenders = [name for name in self._imports(module) if forbidden in name]
        assert offenders == [], offenders

    def test_importing_the_app_does_not_load_the_rag_settings_singleton(self):
        """``api.config`` builds ``Settings()`` at import time.

        Pulling it in would construct the OpenAI/Gemini/httpx client stack inside
        a revision whose only job is to serve reviewers.
        """
        import api.ticket_review_routes
        import api.tickets_console_main

        for module in (api.tickets_console_main, api.ticket_review_routes):
            assert not any(
                name.startswith("api.config") for name in self._imports(module)
            )

    def test_no_route_accepts_the_rag_api_key(self, monkeypatch):
        client = _client(monkeypatch)
        for path in ("/tickets", f"{API_PREFIX}/session", f"{API_PREFIX}/reviews"):
            response = client.get(path, headers={"X-API-Key": "any-value"})
            assert response.status_code == 401, path

    def test_the_api_key_header_is_absent_from_the_schema(self):
        assert "X-API-Key" not in str(production_app.openapi())


# =====================================================================
# Startup validation
# =====================================================================


class TestStartupValidation:

    def test_a_valid_local_configuration_starts(self, monkeypatch):
        assert validate_console_startup(_settings(monkeypatch)) is True

    def test_a_valid_production_configuration_starts(self, monkeypatch):
        assert validate_console_startup(_production_settings(monkeypatch)) is True

    def test_a_missing_console_origin_refuses_to_start(self, monkeypatch):
        with pytest.raises(AuthConfigurationError):
            validate_console_startup(_settings(monkeypatch, CONSOLE_ORIGIN=""))

    @pytest.mark.parametrize(
        "origin",
        [
            f"{CONSOLE_ORIGIN}/tickets",
            "tickets-console-abc-uc.a.run.app",
            f"{CONSOLE_ORIGIN}?x=1",
        ],
    )
    def test_an_unusable_console_origin_refuses_to_start(self, monkeypatch, origin):
        with pytest.raises(AuthConfigurationError):
            validate_console_startup(_settings(monkeypatch, CONSOLE_ORIGIN=origin))

    def test_a_plain_http_origin_refuses_to_start_in_production(self, monkeypatch):
        with pytest.raises(AuthConfigurationError):
            validate_console_startup(
                _production_settings(monkeypatch, CONSOLE_ORIGIN="http://console.invalid")
            )

    def test_local_auth_in_production_refuses_to_start(self, monkeypatch):
        with pytest.raises((AuthConfigurationError, ValueError)):
            validate_console_startup(
                _production_settings(
                    monkeypatch, AUTH_MODE="local", ALLOW_LOCAL_AUTH=True
                )
            )

    def test_synthetic_verification_in_production_refuses_to_start(self, monkeypatch):
        with pytest.raises((AuthConfigurationError, ValueError)):
            validate_console_startup(
                _production_settings(monkeypatch, ENABLE_SYNTHETIC_VERIFICATION=True)
            )

    def test_synthetic_verification_in_staging_is_allowed(self, monkeypatch):
        settings = _production_settings(
            monkeypatch,
            ENVIRONMENT="staging",
            FIRESTORE_DATABASE="tickets-console-staging",
            ENABLE_SYNTHETIC_VERIFICATION=True,
        )
        assert validate_console_startup(settings) is True

    def test_a_default_firestore_database_refuses_to_start(self, monkeypatch):
        with pytest.raises(ValueError):
            validate_console_startup(
                _production_settings(monkeypatch, FIRESTORE_DATABASE="(default)")
            )

    def test_a_missing_role_binding_refuses_to_start_in_production(self, monkeypatch):
        with pytest.raises((AuthConfigurationError, ValueError)):
            validate_console_startup(
                _production_settings(monkeypatch, ROLE_BINDINGS_JSON="")
            )

    async def test_the_lifespan_refuses_an_invalid_configuration(self, monkeypatch):
        """Startup fails before any client is constructed.

        ``TicketConsoleSettings`` is replaced so the assertion cannot depend on
        the repository's gitignored ``.env``.
        """
        import api.tickets_console_main as module

        bad = _settings(monkeypatch, CONSOLE_ORIGIN="")
        monkeypatch.setattr(module, "TicketConsoleSettings", lambda: bad)
        with pytest.raises((AuthConfigurationError, ValueError)):
            async with lifespan(module._new_app()):  # noqa: SLF001 - startup seam
                pass

    def test_the_production_app_declares_a_lifespan(self):
        assert production_app.router.lifespan_context is not None


# =====================================================================
# Request bounds, logging, and the error boundary
# =====================================================================


class TestRequestBounds:

    def test_an_oversized_declared_body_is_413(self, monkeypatch):
        client = _client(monkeypatch, settings=_settings(monkeypatch, MAX_JSON_BYTES=64))
        response = client.post(
            f"{API_PREFIX}/tickets/TKT-1/review",
            headers=_auth(**{"Content-Type": "application/json"}),
            content=b"x" * 512,
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"

    def test_a_malformed_content_length_is_400(self, monkeypatch):
        client = _client(monkeypatch)
        response = client.post(
            f"{API_PREFIX}/tickets/TKT-1/review",
            headers=_auth(
                **{"Content-Type": "application/json", "Content-Length": "not-a-number"}
            ),
            content=b"{}",
        )
        assert response.status_code in (400, 422)

    def test_the_request_id_is_server_generated_and_echoed(self, monkeypatch):
        response = _client(monkeypatch).get("/livez", headers={"X-Request-ID": "chosen"})
        assert response.headers["X-Request-ID"] != "chosen"
        assert len(response.headers["X-Request-ID"]) == 32


class TestStructuredLogging:

    def test_the_access_log_records_a_route_shape_not_an_identifier(
        self, monkeypatch, caplog
    ):
        client = _client(monkeypatch)
        review_id = "c" * 64
        with caplog.at_level(logging.INFO, logger="api.tickets_console_main"):
            client.get(f"{API_PREFIX}/reviews/{review_id}", headers=_auth())
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert review_id not in text
        assert f"{API_PREFIX}/reviews/{{review_id}}" in text

    def test_the_access_log_never_contains_the_assertion(self, monkeypatch, caplog):
        client = _client(monkeypatch)
        with caplog.at_level(logging.DEBUG):
            client.get(f"{API_PREFIX}/session", headers=_auth())
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert ASSERTION not in text

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/livez", "/livez"),
            ("/tickets", "/tickets"),
            ("/tickets/TKT-42", "/tickets/{display_id}"),
            ("/tickets/assets/app.js", "/tickets/assets/{asset}"),
            (f"{API_PREFIX}/session", f"{API_PREFIX}/session"),
            (f"{API_PREFIX}/tickets", f"{API_PREFIX}/tickets"),
            (f"{API_PREFIX}/tickets/TKT-42", f"{API_PREFIX}/tickets/{{ticket_ref}}"),
            (
                f"{API_PREFIX}/tickets/TKT-42/timeline",
                f"{API_PREFIX}/tickets/{{ticket_ref}}/timeline",
            ),
            (
                f"{API_PREFIX}/tickets/TKT-42/review",
                f"{API_PREFIX}/tickets/{{ticket_ref}}/review",
            ),
            (f"{API_PREFIX}/reviews", f"{API_PREFIX}/reviews"),
            (f"{API_PREFIX}/reviews/{'a' * 64}", f"{API_PREFIX}/reviews/{{review_id}}"),
            (
                f"{API_PREFIX}/reviews/{'a' * 64}/audit-events",
                f"{API_PREFIX}/reviews/{{review_id}}/audit-events",
            ),
            (
                f"{API_PREFIX}/reviews/{'a' * 64}/evidence-links",
                f"{API_PREFIX}/reviews/{{review_id}}/evidence-links",
            ),
            (
                f"{API_PREFIX}/reviews/{'a' * 64}/evidence-links/{'b' * 64}",
                f"{API_PREFIX}/reviews/{{review_id}}/evidence-links/{{link_id}}",
            ),
            ("/something/else", "/{unclassified}"),
        ],
    )
    def test_route_templates_are_bounded(self, path, expected):
        assert safe_route_template(path) == expected


class TestErrorBoundary:

    def test_an_unexpected_route_failure_is_a_generic_500(self, monkeypatch):
        class _Exploding:
            async def list_reviews(self, query):
                raise RuntimeError("participant secret leaked into the message")

        client = _client(monkeypatch, service=_Exploding(), repository=object())
        response = client.get(f"{API_PREFIX}/reviews", headers=_auth())
        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "participant secret" not in response.text
        assert "Traceback" not in response.text

    def test_an_uninitialized_app_answers_503_not_500(self, monkeypatch):
        response = _client(monkeypatch).get(f"{API_PREFIX}/reviews", headers=_auth())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "NOT_INITIALIZED"

    def test_an_uninitialized_authenticator_is_503(self, monkeypatch):
        client = _client(monkeypatch)
        client.app.state.authenticator = None
        response = client.get(f"{API_PREFIX}/session", headers=_auth())
        assert response.status_code == 503

    def test_every_failure_shape_is_the_one_envelope(self, monkeypatch):
        client = _client(monkeypatch)
        for path, expected in (
            ("/nope", 404),
            (f"{API_PREFIX}/reviews", 503),
        ):
            response = client.get(path, headers=_auth())
            assert response.status_code == expected, path
            body = response.json()
            assert set(body) == {"error"}
            assert body["error"]["request_id"]


class TestStateWiring:

    def test_the_app_state_carries_exactly_the_console_collaborators(self, monkeypatch):
        client = _client(monkeypatch)
        state = client.app.state
        assert isinstance(state.cursor_key, bytes) and len(state.cursor_key) == 32
        assert state.unsafe_policy.console_origin == CONSOLE_ORIGIN
        assert state.authenticator is not None
        assert state.rate_limiter is not None
        assert state.max_request_bytes > 0

    def test_the_cursor_key_never_appears_in_a_response(self, monkeypatch):
        client = _client(monkeypatch)
        body = client.get(f"{API_PREFIX}/session", headers=_auth()).text
        assert CURSOR_KEY_B64 not in body

    def test_the_unsafe_policy_hides_its_secret(self, monkeypatch):
        client = _client(monkeypatch)
        assert CSRF_SECRET not in repr(client.app.state.unsafe_policy)
