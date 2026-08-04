"""Standalone FastAPI app for the read-only ticket evidence broker.

This is a **separate Cloud Run service** with its own dedicated service
account, and it is the only component permitted to read production
``(default)``. Its whole reason to exist is that ``roles/datastore.user`` and
``roles/datastore.viewer`` are database-scoped: IAM cannot say "may read
``execution_logs`` only". This app is that narrowing, expressed in code and
enforced by tests.

Deliberate non-imports
----------------------
``api.main``, ``data_pipeline.rag_engine``, Pinecone, OpenAI, the DevRev
client, the ticket worker, and ``data_pipeline.ticket_review_repository`` are
all absent, and ``tests/test_ticket_evidence_broker.py`` asserts their absence.
That is not tidiness: importing ``api.main`` would build the RAG settings
singleton and construct LLM/vector clients inside a revision whose service
account exists purely to run one indexed Firestore query, and importing the
console repository would give the broker a second database's worth of
capability it has no reason to hold.

What it exposes
---------------
One service-to-service endpoint::

    POST /internal/v1/ticket-evidence:lookup

It accepts one bounded, transient DevRev DON from the authenticated console
service account, hashes it in memory once per active lookup-key version, and
returns a bounded sanitized :class:`RagEvidenceEnvelope`. It is not a browser
API: there is no session, no cookie, no CSRF, and no HTML.

Authorization lives here rather than in
``data_pipeline.ticket_evidence_broker`` because this is the only layer that
sees a verified caller identity. The query layer stays a pure, testable
function.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from api.ticket_review_models import (
    EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    RagEvidenceEnvelope,
    TicketEvidenceLookupRequest,
)
from api.tickets_console_config import (
    STRICT_ENVIRONMENTS,
    EvidenceBrokerSettings,
    resolve_tickets_firestore_database,
    validate_evidence_broker_settings,
)
from data_pipeline.ticket_evidence_broker import (
    EvidenceBrokerError,
    FirestoreEvidenceBackend,
    TicketEvidenceBroker,
)

logger = logging.getLogger(__name__)

LOOKUP_PATH = "/internal/v1/ticket-evidence:lookup"

# Modules this revision must never pull in. Asserted by the broker tests, and
# listed here so the reason travels with the constraint.
FORBIDDEN_IMPORTS = (
    "api.main",
    "api.ticket_worker",
    "data_pipeline.rag_engine",
    "data_pipeline.ticket_review_repository",
    "data_pipeline.devrev_client",
    "data_pipeline.pinecone_uploader",
    "pinecone",
    "openai",
)


class BrokerAuthorizationError(Exception):
    """The caller is not the configured console service account."""


def authorize_console_caller(
    claims: Mapping[str, Any],
    *,
    expected_audience: str,
    expected_service_account: str,
) -> str:
    """Return the verified caller email, or raise.

    Checks are exact-match, never prefix or suffix. The audience must be the
    broker's own URL so an ID token minted for a *different* Cloud Run service
    cannot be replayed here, and the caller must be the one configured console
    service account — group membership or a shared domain is not enough for a
    service that can read production logs.
    """
    if not expected_audience or not expected_service_account:
        raise BrokerAuthorizationError("the broker caller allowlist is not configured")
    audience = claims.get("aud")
    if not isinstance(audience, str) or audience != expected_audience:
        raise BrokerAuthorizationError("token audience does not match this service")
    email = claims.get("email")
    if not isinstance(email, str) or email != expected_service_account:
        raise BrokerAuthorizationError("caller is not the console service account")
    # Google mints ``email_verified=true`` for service-account ID tokens. An
    # absent or false claim means this is not one.
    if claims.get("email_verified") is not True:
        raise BrokerAuthorizationError("caller identity is not verified")
    return email


def _default_claims_verifier(token: str, audience: str) -> Mapping[str, Any]:
    """Verify a Google-signed ID token. Imported lazily so tests stay offline."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(
        token, google_requests.Request(), audience=audience
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize settings, one read-only Firestore client, and the broker.

    Nothing else is constructed. Startup fails closed: a revision with no
    keyring, no audience, or no caller allowlist must refuse to serve rather
    than answer every lookup with "no evidence", which is indistinguishable
    from a genuinely uncorrelated ticket.
    """
    settings = EvidenceBrokerSettings()
    validate_evidence_broker_settings(settings)
    app.state.settings = settings

    database = resolve_tickets_firestore_database(
        settings.FIRESTORE_DATABASE or "(default)",
        environment=(
            "local" if settings.ENVIRONMENT not in STRICT_ENVIRONMENTS else settings.ENVIRONMENT
        ),
    ) if settings.FIRESTORE_DATABASE else "(default)"

    # Imported here, not at module scope, so the app object can be imported
    # (and its route table inspected) without the Firestore SDK or ADC.
    from google.cloud import firestore

    client = firestore.AsyncClient(project=settings.GCP_PROJECT or None, database=database)
    app.state.firestore = client
    app.state.broker = TicketEvidenceBroker.from_settings(
        settings, FirestoreEvidenceBackend(client)
    )
    logger.info(
        "evidence broker ready; database=%s active_key_versions=%d",
        database,
        len(app.state.broker.active_versions),
    )
    try:
        yield
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


app = FastAPI(
    title="Tickets Evidence Broker",
    description=(
        "One bounded, read-only, service-to-service lookup that maps a DevRev "
        "DON to a sanitized RAG provenance envelope. Not a browser API."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # No interactive docs: this service has exactly one caller, and an open
    # schema explorer on a revision with production log access is not useful.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def get_settings(request: Request) -> EvidenceBrokerSettings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="broker is not initialized",
        )
    return settings


def get_broker(request: Request) -> TicketEvidenceBroker:
    broker = getattr(request.app.state, "broker", None)
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="broker is not initialized",
        )
    return broker


def get_claims_verifier(request: Request) -> Callable[[str, str], Mapping[str, Any]]:
    """The ID-token verifier, injectable so tests never need a real token."""
    return getattr(request.app.state, "claims_verifier", _default_claims_verifier)


async def require_console_caller(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    settings: EvidenceBrokerSettings = Depends(get_settings),
    verifier: Callable[[str, str], Mapping[str, Any]] = Depends(get_claims_verifier),
) -> str:
    """Authorize exactly one caller: the configured console service account.

    Every failure returns the same ``403`` with the same body. Distinguishing
    "no token" from "wrong audience" from "wrong service account" would tell an
    unauthorized caller which half of the boundary it had already cleared.
    """
    denied = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="caller is not authorized"
    )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise denied
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise denied
    try:
        claims = verifier(token, settings.AUDIENCE)
        caller = authorize_console_caller(
            claims,
            expected_audience=settings.AUDIENCE,
            expected_service_account=settings.CONSOLE_SERVICE_ACCOUNT,
        )
    except BrokerAuthorizationError as exc:
        # The reason is logged for an operator and never returned to the caller.
        logger.warning("evidence broker denied a caller; reason=%s", exc)
        raise denied from exc
    except Exception as exc:  # noqa: BLE001 - a bad token is a 403, not a 500
        logger.warning(
            "evidence broker token verification failed; error_type=%s", type(exc).__name__
        )
        raise denied from exc
    request.state.caller = caller
    return caller


@app.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    """Liveness only. Never touches Firestore."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> dict[str, Any]:
    """Readiness: the broker was constructed and has at least one active key."""
    broker = getattr(request.app.state, "broker", None)
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready"
        )
    return {"status": "ok", "active_key_versions": len(broker.active_versions)}


@app.post(
    LOOKUP_PATH,
    response_model=RagEvidenceEnvelope,
    response_model_exclude_none=False,
    dependencies=[Depends(require_console_caller)],
)
async def lookup_ticket_evidence(
    payload: TicketEvidenceLookupRequest,
    broker: TicketEvidenceBroker = Depends(get_broker),
) -> RagEvidenceEnvelope:
    """Map one transient DON to a bounded sanitized provenance envelope.

    The DON is never logged, echoed, or persisted; a broker failure returns a
    generic ``503`` so an internal message cannot leak through the boundary.
    """
    try:
        envelope = await broker.lookup(payload)
    except EvidenceBrokerError as exc:
        logger.error("evidence broker lookup failed; error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="evidence lookup is unavailable",
        ) from exc
    return envelope


__all__ = [
    "EVIDENCE_BROKER_MAX_RESPONSE_BYTES",
    "FORBIDDEN_IMPORTS",
    "LOOKUP_PATH",
    "BrokerAuthorizationError",
    "app",
    "authorize_console_caller",
    "lifespan",
    "require_console_caller",
]
