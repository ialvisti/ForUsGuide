"""Private Cloud Run app for idempotent RAG ticket-evaluation ingestion.

This process owns a DevRev read credential and the evaluation Firestore
database.  It deliberately does not import the RAG application, Pinecone, or
the browser console's IAP/CSRF stack.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from api.google_oidc import (
    FORBIDDEN_SERVERLESS_HEADER,
    WORKLOAD_AUTH_HEADER,
    decode_unverified_header,
    verify_google_id_token,
)
from api.ticket_evaluation_models import MAX_EVENT_SIZE_BYTES, TicketEvaluationEvent
from api.ticket_review_models import DevRevHydrationStatus
from api.tickets_console_config import (
    DEVREV_OFFICIAL_API_BASE,
    DEVREV_PINNED_VERSION,
    decode_cursor_aead_key,
    resolve_tickets_firestore_database,
)
from data_pipeline.ticket_review_repository import EvaluationReplayConflict

logger = logging.getLogger(__name__)

INGEST_PREFIX = "/internal/v1"
INGEST_ROUTE = f"{INGEST_PREFIX}/ticket-evaluations/{{execution_id}}"
RETRY_ROUTE = f"{INGEST_PREFIX}/ticket-evaluations:retry-hydration"
ALLOWED_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})


class TicketEvaluationIngestSettings(BaseSettings):
    """Settings owned only by the private evaluation-ingest revision."""

    model_config = SettingsConfigDict(
        env_prefix="TICKET_EVALUATION_INGEST_",
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    ENVIRONMENT: str = "production"
    GCP_PROJECT: str = ""
    FIRESTORE_DATABASE: str = ""
    CURSOR_AEAD_KEY: SecretStr = Field(default=SecretStr(""))
    OIDC_AUDIENCE: str = ""
    ALLOWED_SERVICE_ACCOUNTS: list[str] = Field(default_factory=list)

    DEVREV_TOKEN: SecretStr = Field(default=SecretStr(""))
    DEVREV_API_BASE: str = DEVREV_OFFICIAL_API_BASE
    DEVREV_VERSION: str = DEVREV_PINNED_VERSION
    DEVREV_ALLOWED_PART_DONS: list[str] = Field(default_factory=list)
    DEVREV_ALLOWED_TICKET_VISIBILITY_IDS: list[int] = Field(default_factory=list)
    DEVREV_ALLOWED_TIMELINE_VISIBILITIES: list[str] = Field(default_factory=list)


def validate_ingest_settings(settings: TicketEvaluationIngestSettings) -> bool:
    errors: list[str] = []
    try:
        resolve_tickets_firestore_database(
            settings.FIRESTORE_DATABASE, environment=settings.ENVIRONMENT
        )
    except ValueError as exc:
        errors.append(str(exc))
    try:
        decode_cursor_aead_key(settings.CURSOR_AEAD_KEY)
    except ValueError as exc:
        errors.append(str(exc))
    if not settings.GCP_PROJECT.strip():
        errors.append("GCP_PROJECT is required")
    if not settings.OIDC_AUDIENCE.startswith("https://"):
        errors.append("OIDC_AUDIENCE must be an https URL")
    if not settings.ALLOWED_SERVICE_ACCOUNTS:
        errors.append("ALLOWED_SERVICE_ACCOUNTS must be non-empty")
    if any(
        not value.endswith(".iam.gserviceaccount.com")
        for value in settings.ALLOWED_SERVICE_ACCOUNTS
    ):
        errors.append("ALLOWED_SERVICE_ACCOUNTS must contain exact service-account emails")
    if not settings.DEVREV_TOKEN.get_secret_value().strip():
        errors.append("DEVREV_TOKEN is required")
    if settings.DEVREV_API_BASE.rstrip("/") != DEVREV_OFFICIAL_API_BASE:
        errors.append(f"DEVREV_API_BASE must be {DEVREV_OFFICIAL_API_BASE}")
    if settings.DEVREV_VERSION != DEVREV_PINNED_VERSION:
        errors.append(f"DEVREV_VERSION must be {DEVREV_PINNED_VERSION}")
    if not settings.DEVREV_ALLOWED_PART_DONS:
        errors.append("DEVREV_ALLOWED_PART_DONS must be non-empty")
    if not settings.DEVREV_ALLOWED_TICKET_VISIBILITY_IDS:
        errors.append("DEVREV_ALLOWED_TICKET_VISIBILITY_IDS must be non-empty")
    if not settings.DEVREV_ALLOWED_TIMELINE_VISIBILITIES:
        errors.append("DEVREV_ALLOWED_TIMELINE_VISIBILITIES must be non-empty")
    if errors:
        raise ValueError("Invalid ticket evaluation ingest configuration: " + "; ".join(errors))
    return True


class IngestAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool = True
    execution_id: str
    replayed: bool
    hydration_status: DevRevHydrationStatus


class HydrationRetryAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempted: int = Field(ge=0)
    succeeded: int = Field(ge=0)


TokenVerifier = Callable[[str, str], dict[str, Any]]
HeaderDecoder = Callable[[str], dict[str, Any]]


def _authorize(request: Request) -> str:
    if request.headers.get(FORBIDDEN_SERVERLESS_HEADER) is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid auth header")
    raw = request.headers.get(WORKLOAD_AUTH_HEADER, "")
    if not raw.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload identity required")
    token = raw.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload identity required")

    decoder: HeaderDecoder = request.app.state.token_header_decoder
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        header = decoder(token)
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise ValueError("unsigned token")
        claims = verifier(token, request.app.state.oidc_audience)
    except HTTPException:
        raise
    except (ValueError, KeyError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid workload identity") from None
    except Exception as exc:
        logger.error("ticket evaluation OIDC verifier unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="workload identity verifier unavailable",
        ) from exc

    audience = request.app.state.oidc_audience
    email = claims.get("email")
    if (
        claims.get("iss") not in ALLOWED_ISSUERS
        or claims.get("aud") != audience
        or claims.get("email_verified") is not True
        or not isinstance(email, str)
        or not any(
            hmac.compare_digest(email, allowed)
            for allowed in request.app.state.allowed_service_accounts
        )
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid workload identity")
    return email


def _configure_routes(app: FastAPI) -> FastAPI:
    @app.middleware("http")
    async def bound_request_body(request: Request, call_next: Callable[..., Any]):
        if request.method in {"PUT", "POST"} and request.url.path.startswith(INGEST_PREFIX):
            declared = request.headers.get("content-length")
            if declared is not None:
                if not declared.isdecimal():
                    return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CONTENT_LENGTH"}})
                if int(declared) > MAX_EVENT_SIZE_BYTES:
                    return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE"}})
            received = 0
            original_receive = request.receive

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message.get("type") == "http.request":
                    received += len(message.get("body", b""))
                    if received > MAX_EVENT_SIZE_BYTES:
                        raise _RequestTooLarge
                return message

            request._receive = limited_receive  # noqa: SLF001 - bounded ASGI seam
        try:
            response = await call_next(request)
        except _RequestTooLarge:
            response = JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": {"code": "REQUEST_TOO_LARGE"}},
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request, response: Response) -> dict[str, bool]:
        ready = request.app.state.service is not None
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": ready}

    @app.put(
        INGEST_ROUTE,
        response_model=IngestAcknowledgement,
        responses={409: {"description": "Conflicting replay"}},
    )
    async def ingest_evaluation(
        payload: TicketEvaluationEvent,
        response: Response,
        execution_id: str = Path(min_length=3, max_length=160),
        _caller: str = Depends(_authorize),
    ) -> IngestAcknowledgement:
        if execution_id != payload.execution_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="path execution id must match body execution id",
            )
        try:
            result = await app.state.service.ingest(payload)
        except EvaluationReplayConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="execution id conflicts with persisted evidence",
            ) from exc
        response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return IngestAcknowledgement(
            execution_id=result.run.execution_id,
            replayed=not result.created,
            hydration_status=result.run.hydration_status,
        )

    @app.post(RETRY_ROUTE, response_model=HydrationRetryAcknowledgement)
    async def retry_hydration(
        limit: int = Query(default=20, ge=1, le=100),
        _caller: str = Depends(_authorize),
    ) -> HydrationRetryAcknowledgement:
        attempted, succeeded = await app.state.service.retry_due_hydrations(limit=limit)
        return HydrationRetryAcknowledgement(attempted=attempted, succeeded=succeeded)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "REQUEST_REJECTED", "message": str(exc.detail)}},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default response includes Pydantic's rejected ``input``.
        # This boundary receives untrusted diagnostic maps, so returning those
        # values would echo the exact credential-shaped data we rejected.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request validation failed",
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    return app


class _RequestTooLarge(Exception):
    """Internal streaming body-limit signal; carries no request content."""


def build_ingest_app(
    *,
    service: Any,
    oidc_audience: str,
    allowed_service_accounts: Sequence[str],
    token_verifier: TokenVerifier,
    token_header_decoder: Optional[HeaderDecoder] = None,
) -> FastAPI:
    """Build a fully injected app for contract tests and local smoke checks."""
    app = FastAPI(title="ForUs Ticket Evaluation Ingest", docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.oidc_audience = oidc_audience
    app.state.allowed_service_accounts = tuple(allowed_service_accounts)
    app.state.token_verifier = token_verifier
    # A custom verifier is a test seam; production always supplies the real
    # decoder below and rejects unsigned or unknown JOSE algorithms.
    app.state.token_header_decoder = token_header_decoder or (lambda _token: {"alg": "RS256"})
    return _configure_routes(app)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = TicketEvaluationIngestSettings()
    validate_ingest_settings(settings)

    from data_pipeline.devrev_client import DevRevClient
    from data_pipeline.ticket_review_repository import (
        FirestoreTicketReviewBackend,
        TicketReviewRepository,
    )
    from data_pipeline.ticket_review_service import TicketEvaluationService

    backend = FirestoreTicketReviewBackend(
        project=settings.GCP_PROJECT,
        database=settings.FIRESTORE_DATABASE,
        environment=settings.ENVIRONMENT,
    )
    repository = TicketReviewRepository(
        backend,
        cursor_key=decode_cursor_aead_key(settings.CURSOR_AEAD_KEY),
    )
    devrev = DevRevClient(
        token=settings.DEVREV_TOKEN,
        allowed_part_dons=settings.DEVREV_ALLOWED_PART_DONS,
        allowed_ticket_visibility_ids=settings.DEVREV_ALLOWED_TICKET_VISIBILITY_IDS,
        allowed_timeline_visibilities=settings.DEVREV_ALLOWED_TIMELINE_VISIBILITIES,
        base_url=settings.DEVREV_API_BASE,
        api_version=settings.DEVREV_VERSION,
        environment=settings.ENVIRONMENT,
    )
    app.state.service = TicketEvaluationService(devrev=devrev, repository=repository)
    app.state.oidc_audience = settings.OIDC_AUDIENCE
    app.state.allowed_service_accounts = tuple(settings.ALLOWED_SERVICE_ACCOUNTS)
    app.state.token_verifier = verify_google_id_token
    app.state.token_header_decoder = decode_unverified_header
    try:
        yield
    finally:
        await devrev.aclose()


def _production_app() -> FastAPI:
    app = FastAPI(
        title="ForUs Ticket Evaluation Ingest",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    # Default state keeps readiness fail-closed before lifespan completes.
    app.state.service = None
    app.state.oidc_audience = ""
    app.state.allowed_service_accounts = ()
    app.state.token_verifier = verify_google_id_token
    app.state.token_header_decoder = decode_unverified_header
    return _configure_routes(app)


app = _production_app()


__all__ = [
    "INGEST_ROUTE",
    "RETRY_ROUTE",
    "TicketEvaluationIngestSettings",
    "app",
    "build_ingest_app",
    "validate_ingest_settings",
]
