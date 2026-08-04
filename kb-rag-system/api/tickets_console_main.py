"""The standalone `/tickets` administrative application.

This is a **separate FastAPI app and a separate Cloud Run service** from the RAG
API. It shares a repository and a container image, and nothing else: it does not
import :mod:`api.main`, and it never constructs a Pinecone, OpenAI, or ForusBots
client. ``tests/test_tickets_console_app.py`` asserts those absences, because the
cost of the coupling is not tidiness — it is that an admin revision holding the
DevRev token, the CSRF secret, and the cursor key would also hold the RAG data
plane's credentials and blast radius.

Deliberate non-features
-----------------------
*   **No** ``X-API-Key``. The RAG service's shared key authenticates a machine
    client; this app authenticates *people*, and accepting a bearer secret would
    reintroduce an identity with no subject, no role, and no audit actor.
*   **No CORS.** Not a permissive policy — none at all. The browser UI is served
    from this same origin, so any cross-origin request is by definition not the
    console, and ``Access-Control-Allow-Origin`` would only ever help an
    attacker's page read a reviewer's data.
*   **No interactive docs.** A schema explorer on a revision with production
    review data is not useful enough to justify the inline scripts that
    ``Content-Security-Policy`` would have to allow.

The middleware order is part of the contract
--------------------------------------------
``MIDDLEWARE_ORDER`` lists the stack outermost-first and a test pins it. The
order is not arbitrary:

1.  the request id exists before anything can log or report it;
2.  security headers wrap *every* response, including the 401 the auth layer
    returns and the 500 the exception boundary produces;
3.  the access log records the status that was actually sent;
4.  the exception boundary turns anything unhandled into the stable envelope;
5.  the body limit rejects a huge payload before it is materialized;
6.  authentication resolves identity, so the guard below can bind a CSRF token
    to a subject;
7.  the unsafe-request guard runs last, closest to the route, because it needs
    both the resolved identity and the request's own headers.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from api.reviewer_auth import (
    AuthConfigurationError,
    ReviewerAuthenticator,
    ReviewerAuthError,
    validate_console_auth_startup,
)
from api.rate_limit import FixedWindowRateLimiter
from api.ticket_review_models import (
    MAX_JSON_REQUEST_BYTES,
    utc_now,
)
from api.ticket_review_routes import (
    API_PREFIX,
    CODE_NOT_FOUND,
    CODE_VALIDATION_FAILED,
    ConsoleHTTPError,
    error_response,
    map_exception,
    request_id_of,
    router as admin_router,
)
from api.tickets_console_config import (
    DEVREV_OFFICIAL_API_BASE,
    STRICT_ENVIRONMENTS,
    TicketConsoleSettings,
    decode_cursor_aead_key,
    resolve_tickets_firestore_database,
    validate_ticket_console_settings,
)
from api.tickets_csrf import (
    SAFE_METHODS,
    UnsafeRequestRejected,
    assert_unsafe_request_allowed,
    policy_from_settings,
    validate_verification_settings,
)

logger = logging.getLogger(__name__)

APP_TITLE = "ForUs Ticket Review Console"
APP_VERSION = "1.0.0"

#: Reachable without a verified identity. Deliberately only the probes: an asset
#: or an HTML page is behind IAP like everything else, and the assertion arrives
#: on those requests too.
PUBLIC_PATHS = frozenset({"/livez", "/readyz"})

UI_DIRECTORY = Path(__file__).resolve().parent / "tickets_ui"
UI_ASSETS_DIRECTORY = UI_DIRECTORY / "assets"
UI_INDEX_FILE = UI_DIRECTORY / "index.html"
UI_PLACEHOLDER = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Ticket Review Console</title></head><body>"
    "<h1>Ticket Review Console</h1>"
    "<p>The user interface stage is not installed in this build.</p>"
    "</body></html>"
)

#: Modules this revision must never pull in, and why: see the module docstring.
FORBIDDEN_IMPORTS = (
    "api.main",
    "api.ticket_worker",
    "data_pipeline.rag_engine",
    "data_pipeline.pinecone_uploader",
    "data_pipeline.forusbots_client",
    "pinecone",
    "openai",
)

SECURITY_HEADERS: dict[str, str] = {
    # No inline script or style is permitted, so Stage 6 must ship external
    # files. 'unsafe-inline' here would defeat the only real mitigation the
    # console has against a stored-XSS path into a reviewer's session.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

_REQUEST_ID_HEADER = "X-Request-ID"
_UNCLASSIFIED_ROUTE = "/{unclassified}"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


async def add_request_id(request: Request, call_next: Callable[..., Awaitable[Response]]):
    """Stamp a server-generated request id. A client's value is never trusted."""
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[_REQUEST_ID_HEADER] = request_id
    return response


async def add_security_headers(
    request: Request, call_next: Callable[..., Awaitable[Response]]
):
    """Apply the security headers to every response, success or failure."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Every HTML page and every authenticated API response is uncacheable: a
    # shared cache holding one reviewer's queue would serve it to the next.
    if request.url.path.startswith(API_PREFIX) or request.url.path.startswith("/tickets"):
        response.headers["Cache-Control"] = "no-store"
    return response


def safe_route_template(path: str) -> str:
    """A bounded route template for logs — never an identifier-bearing path.

    A ticket display id and a review id are both customer-linked, so the raw path
    is reduced to its shape before anything writes it down.
    """
    if path in PUBLIC_PATHS or path == "/tickets":
        return path
    for pattern, template in (
        (rf"^{re.escape(API_PREFIX)}/session$", f"{API_PREFIX}/session"),
        (rf"^{re.escape(API_PREFIX)}/tickets$", f"{API_PREFIX}/tickets"),
        (
            rf"^{re.escape(API_PREFIX)}/tickets/[^/]+/timeline$",
            f"{API_PREFIX}/tickets/{{ticket_ref}}/timeline",
        ),
        (
            rf"^{re.escape(API_PREFIX)}/tickets/[^/]+/review$",
            f"{API_PREFIX}/tickets/{{ticket_ref}}/review",
        ),
        (rf"^{re.escape(API_PREFIX)}/tickets/[^/]+$", f"{API_PREFIX}/tickets/{{ticket_ref}}"),
        (rf"^{re.escape(API_PREFIX)}/reviews$", f"{API_PREFIX}/reviews"),
        (
            rf"^{re.escape(API_PREFIX)}/reviews/[^/]+/audit-events$",
            f"{API_PREFIX}/reviews/{{review_id}}/audit-events",
        ),
        (
            rf"^{re.escape(API_PREFIX)}/reviews/[^/]+/evidence-links/[^/]+$",
            f"{API_PREFIX}/reviews/{{review_id}}/evidence-links/{{link_id}}",
        ),
        (
            rf"^{re.escape(API_PREFIX)}/reviews/[^/]+/evidence-links$",
            f"{API_PREFIX}/reviews/{{review_id}}/evidence-links",
        ),
        (rf"^{re.escape(API_PREFIX)}/reviews/[^/]+$", f"{API_PREFIX}/reviews/{{review_id}}"),
        (r"^/tickets/assets/.+$", "/tickets/assets/{asset}"),
        (r"^/tickets/[^/]+$", "/tickets/{display_id}"),
    ):
        if re.match(pattern, path):
            return template
    return _UNCLASSIFIED_ROUTE


async def log_requests(request: Request, call_next: Callable[..., Awaitable[Response]]):
    """One structured line per request: shape, status, duration, subject hash.

    Never the path's identifiers, never a header value, never a cursor, and never
    the assertion. The subject hash is the only identity that appears.
    """
    started = time.monotonic()
    request_id = request_id_of(request) or "unknown"
    template = safe_route_template(request.url.path)
    response = await call_next(request)
    reviewer = getattr(request.state, "reviewer", None)
    logger.info(
        "console request; request_id=%s method=%s route=%s status=%d duration_ms=%d "
        "subject_hash=%s",
        request_id,
        request.method,
        template,
        response.status_code,
        int((time.monotonic() - started) * 1000),
        (reviewer.subject_hash[:12] if reviewer is not None else "-"),
    )
    return response


async def handle_exceptions(request: Request, call_next: Callable[..., Awaitable[Response]]):
    """Turn anything unhandled into the stable envelope.

    Exceptions raised *inside* middleware do not reach FastAPI's handlers, so the
    boundary is duplicated here. The type name is logged; the message never is,
    because it can quote a ticket body or an upstream payload.
    """
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 - this is the boundary
        mapped = map_exception(exc)
        if mapped.status_code >= 500:
            logger.error(
                "console request failed; request_id=%s error_type=%s",
                request_id_of(request),
                type(exc).__name__,
            )
        return error_response(
            mapped.status_code,
            mapped.code,
            mapped.message,
            request_id=request_id_of(request),
            headers=mapped.headers,
            current_version=mapped.current_version,
            changed_at=mapped.changed_at,
        )


async def limit_body_size(request: Request, call_next: Callable[..., Awaitable[Response]]):
    """Refuse an oversized body before it is materialized.

    ``Content-Length`` is only a prevalidation: a client may omit it or declare
    less than it sends, so the stream is counted as well.
    """
    max_bytes = int(getattr(request.app.state, "max_request_bytes", MAX_JSON_REQUEST_BYTES))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and max_bytes > 0:
        declared = request.headers.get("content-length")
        if declared is not None and re.fullmatch(r"[0-9]+", declared) is None:
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                CODE_VALIDATION_FAILED,
                "the content length is not valid",
                request_id=request_id_of(request),
            )
        if declared is not None and int(declared) > max_bytes:
            return error_response(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "REQUEST_BODY_TOO_LARGE",
                "the request body is too large",
                request_id=request_id_of(request),
            )
        received = 0
        original_receive = request.receive

        async def counting_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise ConsoleHTTPError(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "REQUEST_BODY_TOO_LARGE",
                        "the request body is too large",
                    )
            return message

        request._receive = counting_receive  # noqa: SLF001 - the documented seam
    return await call_next(request)


async def authenticate(request: Request, call_next: Callable[..., Awaitable[Response]]):
    """Resolve the verified caller once, before any route or guard runs."""
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    authenticator = getattr(request.app.state, "authenticator", None)
    if authenticator is None:
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "NOT_INITIALIZED",
            "the console is not initialized",
            request_id=request_id_of(request),
        )
    try:
        request.state.reviewer = authenticator.authenticate(
            request.headers,
            client_host=(request.client.host if request.client else None),
            request_id=request_id_of(request),
        )
    except ReviewerAuthError as exc:
        return error_response(
            exc.status,
            exc.code,
            str(exc) or "authentication failed",
            request_id=request_id_of(request),
        )
    return await call_next(request)


async def guard_unsafe_requests(
    request: Request, call_next: Callable[..., Awaitable[Response]]
):
    """Enforce the unsafe-method matrix and publish the idempotency key."""
    if request.method.upper() in SAFE_METHODS or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    policy = getattr(request.app.state, "unsafe_policy", None)
    reviewer = getattr(request.state, "reviewer", None)
    if policy is None or reviewer is None:
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "NOT_INITIALIZED",
            "the console is not initialized",
            request_id=request_id_of(request),
        )
    clock = getattr(request.app.state, "clock", None)
    now = clock() if callable(clock) else utc_now()
    try:
        request.state.idempotency_key = assert_unsafe_request_allowed(
            policy,
            method=request.method,
            path=request.url.path,
            headers=request.headers,
            reviewer=reviewer,
            now=now,
        )
    except UnsafeRequestRejected as exc:
        return error_response(
            exc.status,
            exc.code,
            str(exc) or "the request was rejected",
            request_id=request_id_of(request),
        )
    except AuthConfigurationError as exc:
        logger.error("console unsafe-request policy is unusable; reason=%s", exc)
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "NOT_INITIALIZED",
            "the console is not initialized",
            request_id=request_id_of(request),
        )
    return await call_next(request)


#: Outermost first. Pinned by ``tests/test_tickets_console_app.py``.
MIDDLEWARE_ORDER: tuple[Callable[..., Any], ...] = (
    add_request_id,
    add_security_headers,
    log_requests,
    handle_exceptions,
    limit_body_size,
    authenticate,
    guard_unsafe_requests,
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def install_exception_handlers(app: FastAPI) -> None:
    """Map every failure a route can raise onto the one envelope."""

    @app.exception_handler(ConsoleHTTPError)
    async def _console_error(request: Request, exc: ConsoleHTTPError) -> JSONResponse:
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            request_id=request_id_of(request),
            headers=exc.headers,
            current_version=exc.current_version,
            changed_at=exc.changed_at,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # The public message is generic on purpose: pydantic's error list quotes
        # the offending input, which for this API is ticket text or an email.
        logger.info(
            "console request failed validation; request_id=%s error_count=%d",
            request_id_of(request),
            len(exc.errors()),
        )
        return error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "the request is not valid",
            request_id=request_id_of(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = CODE_NOT_FOUND if exc.status_code == 404 else "HTTP_ERROR"
        return error_response(
            exc.status_code,
            code,
            "that record is not available" if exc.status_code == 404 else "request rejected",
            request_id=request_id_of(request),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        mapped = map_exception(exc)
        if mapped.status_code >= 500:
            logger.error(
                "console route failed; request_id=%s error_type=%s",
                request_id_of(request),
                type(exc).__name__,
            )
        return error_response(
            mapped.status_code,
            mapped.code,
            mapped.message,
            request_id=request_id_of(request),
            headers=mapped.headers,
            current_version=mapped.current_version,
            changed_at=mapped.changed_at,
        )


# ---------------------------------------------------------------------------
# Health and UI
# ---------------------------------------------------------------------------


def install_health_and_ui(app: FastAPI) -> None:
    """Probes and the browser entry points."""

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        """Liveness only. Touches nothing external, by design: a liveness probe
        that depends on Firestore turns a dependency blip into a restart loop."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> dict[str, Any]:
        """Readiness from *configuration*, never from a participant's ticket.

        Calling a real ticket here would put customer data in the probe path and
        spend DevRev rate limit on every health check.
        """
        state = request.app.state
        settings: Optional[TicketConsoleSettings] = getattr(state, "settings", None)
        if settings is None:
            raise ConsoleHTTPError(
                status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_READY", "not ready"
            )
        missing = [
            name
            for name in ("repository", "service", "devrev", "authenticator", "unsafe_policy")
            if getattr(state, name, None) is None
        ]
        database = getattr(state, "firestore_database", "")
        official_devrev = (
            settings.DEVREV_API_BASE or ""
        ).rstrip("/") == DEVREV_OFFICIAL_API_BASE
        strict = (settings.ENVIRONMENT or "").strip() in STRICT_ENVIRONMENTS
        if missing or not database or (strict and not official_devrev):
            logger.warning("console not ready; missing=%s", ",".join(missing) or "-")
            raise ConsoleHTTPError(
                status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_READY", "not ready"
            )
        return {
            "status": "ok",
            "environment": settings.ENVIRONMENT,
            "firestore_database": database,
            "evidence_broker": bool(getattr(state, "evidence_client", None)),
            "ui_installed": UI_INDEX_FILE.is_file(),
        }

    @app.get("/tickets", include_in_schema=False, response_class=HTMLResponse)
    async def tickets_index() -> HTMLResponse:
        return _ui_response()

    @app.get("/tickets/{display_id}", include_in_schema=False, response_class=HTMLResponse)
    async def tickets_detail(display_id: str) -> HTMLResponse:
        """The same document for every deep link: routing happens client-side.

        ``display_id`` is deliberately unused — echoing it into the HTML would be
        a reflected-content sink, and the page fetches what it needs from the API
        under the caller's own authorization.
        """
        del display_id
        return _ui_response()

    if UI_ASSETS_DIRECTORY.is_dir():
        # Only the assets directory, and only when it exists. Mounting the UI
        # root would expose templates and any stray file beside them.
        app.mount(
            "/tickets/assets",
            StaticFiles(directory=str(UI_ASSETS_DIRECTORY)),
            name="tickets-assets",
        )


def _ui_response() -> HTMLResponse:
    """Serve the built UI when Stage 6 has installed it, else the placeholder."""
    if UI_INDEX_FILE.is_file():
        return HTMLResponse(
            UI_INDEX_FILE.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(
        UI_PLACEHOLDER, status_code=status.HTTP_200_OK, headers={"Cache-Control": "no-store"}
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _configure(app: FastAPI) -> FastAPI:
    """Register the stack every instance of this app shares."""
    for dispatch in reversed(MIDDLEWARE_ORDER):
        # ``add_middleware`` inserts at the front, so adding innermost-first
        # leaves ``user_middleware[0]`` as the outermost layer.
        app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)
    install_exception_handlers(app)
    install_health_and_ui(app)
    app.include_router(admin_router)
    return app


def _new_app(lifespan: Any = None) -> FastAPI:
    return FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=(
            "Authenticated administrative API for the ForUs ticket review "
            "console. Identity comes from a verified IAP assertion; there is no "
            "API key."
        ),
        lifespan=lifespan,
        # See the module docstring: no schema explorer on an admin plane.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )


def wire_console_state(
    app: FastAPI,
    settings: TicketConsoleSettings,
    *,
    devrev: Any = None,
    repository: Any = None,
    service: Any = None,
    evidence_client: Any = None,
    authenticator: Any = None,
    claims_verifier: Any = None,
    clock: Optional[Callable[[], Any]] = None,
    rate_limiter: Any = None,
    firestore_database: Optional[str] = None,
) -> FastAPI:
    """Populate ``app.state``. Every collaborator is injectable for tests.

    The production path passes none of them and gets the real DevRev client,
    Firestore-backed repository, service, and evidence broker client. A test
    passes fakes and never opens a socket or touches Firestore.
    """
    app.state.settings = settings
    app.state.clock = clock or utc_now
    app.state.cursor_key = decode_cursor_aead_key(settings.CURSOR_AEAD_KEY)
    app.state.unsafe_policy = policy_from_settings(settings)
    app.state.authenticator = authenticator or ReviewerAuthenticator.from_settings(
        settings, claims_verifier=claims_verifier, clock=app.state.clock
    )
    app.state.devrev = devrev
    app.state.repository = repository
    app.state.service = service
    app.state.evidence_client = evidence_client
    app.state.rate_limiter = (
        rate_limiter if rate_limiter is not None else FixedWindowRateLimiter()
    )
    app.state.max_request_bytes = int(settings.MAX_JSON_BYTES)
    app.state.firestore_database = firestore_database or ""
    return app


def build_console_app(settings: TicketConsoleSettings, **collaborators: Any) -> FastAPI:
    """Build a fully wired app without the production lifespan.

    This is the seam the test suite uses: it exercises the real middleware stack,
    the real router, and the real error envelope against injected fakes.
    """
    validate_console_startup(settings)
    app = _configure(_new_app())
    return wire_console_state(app, settings, **collaborators)


def validate_console_startup(settings: TicketConsoleSettings) -> bool:
    """Every startup refusal, in one place.

    Fails closed and reports all three families of problem: the shared console
    configuration, the identity configuration, and the synthetic-verification
    flag that must not exist in production.
    """
    validate_ticket_console_settings(settings)
    validate_console_auth_startup(settings)
    validate_verification_settings(settings)
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration, then build exactly the collaborators needed.

    Nothing here constructs a ``(default)`` Firestore client: the database name
    is resolved through ``resolve_tickets_firestore_database``, which refuses
    ``(default)`` in staging and production and pins the provisioned name.
    """
    settings = TicketConsoleSettings()
    validate_console_startup(settings)

    database = resolve_tickets_firestore_database(
        settings.FIRESTORE_DATABASE, environment=(settings.ENVIRONMENT or "").strip()
    )

    # Imported here rather than at module scope so this module can be imported —
    # and its OpenAPI inspected — with no Firestore SDK, no ADC, and no network.
    from data_pipeline.devrev_client import DevRevClient
    from data_pipeline.ticket_evidence_client import TicketEvidenceClient
    from data_pipeline.ticket_review_repository import TicketReviewRepository
    from data_pipeline.ticket_review_service import MessageClassifier, TicketReviewService

    devrev = DevRevClient.from_settings(settings)
    repository = TicketReviewRepository.from_settings(settings)
    evidence_client = TicketEvidenceClient.from_settings(settings)
    service = TicketReviewService(
        devrev=devrev,
        repository=repository,
        classifier=MessageClassifier.from_settings(settings),
        candidate_key=decode_cursor_aead_key(settings.CURSOR_AEAD_KEY),
        broker=evidence_client,
        page_size=settings.DEVREV_PAGE_SIZE,
    )
    wire_console_state(
        app,
        settings,
        devrev=devrev,
        repository=repository,
        service=service,
        evidence_client=evidence_client,
        firestore_database=database,
    )
    logger.info(
        "tickets console ready; environment=%s database=%s ui_installed=%s",
        settings.ENVIRONMENT,
        database,
        UI_INDEX_FILE.is_file(),
    )
    try:
        yield
    finally:
        # One shared connection pool per collaborator, closed once.
        for closable in (evidence_client, devrev):
            close = getattr(closable, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                    logger.warning(
                        "console shutdown close failed; error_type=%s", type(exc).__name__
                    )


app = _configure(_new_app(lifespan=lifespan))


__all__ = [
    "APP_TITLE",
    "APP_VERSION",
    "FORBIDDEN_IMPORTS",
    "MIDDLEWARE_ORDER",
    "PUBLIC_PATHS",
    "SECURITY_HEADERS",
    "UI_ASSETS_DIRECTORY",
    "UI_DIRECTORY",
    "UI_INDEX_FILE",
    "UI_PLACEHOLDER",
    "add_request_id",
    "add_security_headers",
    "app",
    "authenticate",
    "build_console_app",
    "guard_unsafe_requests",
    "handle_exceptions",
    "install_exception_handlers",
    "install_health_and_ui",
    "lifespan",
    "limit_body_size",
    "log_requests",
    "safe_route_template",
    "validate_console_startup",
    "wire_console_state",
]
