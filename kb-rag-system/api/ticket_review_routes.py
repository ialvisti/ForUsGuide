"""The Stage 5 subset of ``/api/admin/v1``.

Scope
-----
Session, live DevRev ticket list/detail/timeline, durable review
create/list/detail/patch, audit-event list, and evidence-link list/create/
delete. Remediation batches arrive in Stage 8 and CSV import/export in Stage 9;
they are deliberately **absent** rather than stubbed, because a route that exists
and returns "not implemented" is indistinguishable in OpenAPI from one that
works, and clients build against OpenAPI.

Rules this module holds
-----------------------
*   Route bodies call the service or the repository. They never touch Firestore
    or DevRev directly, so the query grammar, scope allowlists, audit chain, and
    retention envelopes cannot be bypassed by a new endpoint.
*   Every failure becomes one envelope, ``{"error": {...}}``, built from typed
    exceptions in one place. No public message echoes a remote body, a stack
    trace, or another reviewer's unsaved content.
*   A remote DevRev cursor never leaves the server. The browser gets an
    authenticated-encrypted console cursor bound to the endpoint, direction,
    filter set, and subject, and it travels in the ``X-Tickets-Cursor`` *header*
    — a query parameter would reach access logs, proxies, and browser history.
    A raw cursor query parameter is refused outright.
*   ``PATCH`` and evidence ``DELETE`` require a quoted ``If-Match``; the response
    carries the next ``ETag``. A stale precondition returns the current version
    and the time it changed, never the other reviewer's data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse

from api.reviewer_auth import (
    AuthenticatedReviewer,
    AuthorizationFailed,
    ReviewerAuthError,
    role_at_least,
)
from api.ticket_review_models import (
    DEFAULT_PAGE_SIZE,
    MAX_CURSOR_LENGTH,
    MAX_DISPLAY_ID_LENGTH,
    MAX_ID_LENGTH,
    MAX_PAGE_SIZE,
    MAX_REASON_LENGTH,
    AuditEvent,
    CreateEvidenceLinkRequest,
    CreateReviewRequest,
    CursorError,
    CursorPage,
    DeleteEvidenceLinkRequest,
    DevRevTicketFilters,
    DevRevTicketWithReviewSummary,
    ErrorBody,
    ErrorResponse,
    EvidenceLink,
    MalformedPreconditionError,
    MissingPreconditionError,
    PreconditionError,
    ReviewPatch,
    ReviewStatus,
    ReviewerIdentity,
    ReviewerRole,
    SessionResponse,
    StalePreconditionError,
    TicketDetailEnvelope,
    TicketReview,
    ClassifiedTimelinePage,
    format_etag,
    http_status_for_precondition_error,
    open_cursor,
    parse_if_match,
    seal_cursor,
    utc_now,
)
from api.tickets_console_config import classification_diagnostics
from api.tickets_csrf import CURSOR_HEADER, mint_csrf_token
from data_pipeline.devrev_client import (
    DevRevAuthenticationError,
    DevRevConflictError,
    DevRevError,
    DevRevNotFoundError,
    DevRevPaginationError,
    DevRevPermissionError,
    DevRevProtocolError,
    DevRevRateLimitError,
    DevRevRequestError,
    DevRevResourceLimitError,
    DevRevScopeError,
    DevRevTransientError,
)
from data_pipeline.ticket_evidence_client import (
    EvidenceAuthorizationError,
    EvidenceBrokerUnavailable,
    EvidenceClientError,
)
from data_pipeline.ticket_review_repository import (
    EvidenceCandidateRejected,
    IdempotencyConflict,
    InvalidReviewTransition,
    MutationContext,
    NotAuthorized,
    ReviewIdentityConflict,
    ReviewListQuery,
    ReviewNotFound,
    ReviewRepositoryError,
    ReviewVersionConflict,
    UnsupportedFilterCombination,
)
from data_pipeline.ticket_review_service import (
    EvidenceLinkRejected,
    ServiceError,
    TicketNotFound,
    UnsupportedQuery,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/v1", tags=["Ticket Review Console"])

API_PREFIX = "/api/admin/v1"

# ---------------------------------------------------------------------------
# Error codes. UPPER_SNAKE throughout so one convention covers every failure;
# the master plan's sample envelope uses this form.
# ---------------------------------------------------------------------------
CODE_UNAUTHENTICATED = "UNAUTHENTICATED"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_VALIDATION_FAILED = "VALIDATION_FAILED"
CODE_UNSUPPORTED_FILTER = "UNSUPPORTED_FILTER_COMBINATION"
CODE_PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
CODE_PRECONDITION_MALFORMED = "PRECONDITION_MALFORMED"
CODE_REVIEW_VERSION_CONFLICT = "REVIEW_VERSION_CONFLICT"
CODE_REVIEW_CONFLICT = "REVIEW_CONFLICT"
CODE_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
CODE_EVIDENCE_LINK_REJECTED = "EVIDENCE_LINK_REJECTED"
CODE_CURSOR_REJECTED = "CURSOR_REJECTED"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
CODE_UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
CODE_UPSTREAM_PROTOCOL = "UPSTREAM_PROTOCOL_ERROR"
CODE_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
CODE_NOT_INITIALIZED = "NOT_INITIALIZED"
CODE_INTERNAL = "INTERNAL_ERROR"

# ---------------------------------------------------------------------------
# Console cursor contexts. The context string is AES-GCM associated data, so a
# token minted for one endpoint or direction cannot be opened as another even
# though both use the same key.
# ---------------------------------------------------------------------------
CURSOR_CONTEXT_TICKETS = "tickets:console:tickets-list:v1"
CURSOR_CONTEXT_TIMELINE = "tickets:console:timeline:v1"

#: Query parameter names a caller must never use to page. Refused explicitly so
#: a remote cursor cannot be smuggled into a URL, where it would be logged.
FORBIDDEN_CURSOR_PARAMS = frozenset(
    {"cursor", "next_cursor", "prev_cursor", "page_token", "next", "before", "after"}
)

#: Per-subject fixed-window request bounds. A console user cannot legitimately
#: exceed these, and DevRev's own rate limit is a shared resource: one reviewer
#: holding a key down must not exhaust it for everyone.
READ_RATE_LIMIT_PER_MINUTE = 240
WRITE_RATE_LIMIT_PER_MINUTE = 60


class ConsoleHTTPError(Exception):
    """A failure already resolved to a status, a code, and a safe message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        current_version: Optional[int] = None,
        changed_at: Optional[datetime] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.current_version = current_version
        self.changed_at = changed_at


def error_body(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    current_version: Optional[int] = None,
    changed_at: Optional[datetime] = None,
) -> ErrorResponse:
    """Build the one public error envelope."""
    return ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message[:MAX_REASON_LENGTH],
            request_id=request_id,
            current_version=current_version,
            changed_at=changed_at,
        )
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    current_version: Optional[int] = None,
    changed_at: Optional[datetime] = None,
) -> JSONResponse:
    """Render the error envelope, always uncacheable."""
    payload = error_body(
        code,
        message,
        request_id=request_id,
        current_version=current_version,
        changed_at=changed_at,
    )
    merged = {"Cache-Control": "no-store"}
    merged.update(dict(headers or {}))
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json", exclude_none=True),
        headers=merged,
    )


def request_id_of(request: Request) -> Optional[str]:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------


def map_exception(exc: BaseException) -> ConsoleHTTPError:
    """Resolve any typed failure to a status, a code, and a safe message.

    Grouped by *what the caller should do*, not by which module raised:

    * 404 for absent **and** out-of-scope objects, so a denial never confirms
      that an object the caller may not see nevertheless exists;
    * 412/428/422 for the precondition family, 409 only for a valid-version
      business conflict;
    * 429 for our own bound and for DevRev's, with ``Retry-After`` when known;
    * 502 when an upstream misbehaved, 503 when it was simply unavailable — the
      distinction tells an operator whether to look at a contract or at capacity.
    """
    if isinstance(exc, ConsoleHTTPError):
        return exc

    # -- identity -------------------------------------------------------
    if isinstance(exc, ReviewerAuthError):
        return ConsoleHTTPError(exc.status, exc.code, str(exc) or "not authorized")

    # -- preconditions --------------------------------------------------
    if isinstance(exc, PreconditionError):
        code = {
            MissingPreconditionError: CODE_PRECONDITION_REQUIRED,
            MalformedPreconditionError: CODE_PRECONDITION_MALFORMED,
            StalePreconditionError: CODE_REVIEW_VERSION_CONFLICT,
        }.get(type(exc), CODE_PRECONDITION_MALFORMED)
        return ConsoleHTTPError(
            http_status_for_precondition_error(exc),
            code,
            str(exc) or "the precondition is not acceptable",
            current_version=getattr(exc, "current_version", None),
        )

    if isinstance(exc, ReviewVersionConflict):
        return ConsoleHTTPError(
            status.HTTP_412_PRECONDITION_FAILED,
            CODE_REVIEW_VERSION_CONFLICT,
            "The review changed. Reload before saving.",
            current_version=exc.current_version,
            changed_at=exc.changed_at,
        )

    # -- not found ------------------------------------------------------
    if isinstance(exc, (TicketNotFound, ReviewNotFound, DevRevNotFoundError, DevRevScopeError)):
        return ConsoleHTTPError(
            status.HTTP_404_NOT_FOUND, CODE_NOT_FOUND, "that record is not available"
        )

    # -- caller error ---------------------------------------------------
    if isinstance(exc, (UnsupportedQuery, UnsupportedFilterCombination)):
        return ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_UNSUPPORTED_FILTER,
            str(exc) or "that filter combination is not supported",
        )
    if isinstance(exc, CursorError):
        return ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_CURSOR_REJECTED,
            "that page cursor is not usable; reload the first page",
        )
    if isinstance(exc, DevRevRequestError):
        return ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that request is not valid",
        )

    # -- conflicts ------------------------------------------------------
    if isinstance(exc, IdempotencyConflict):
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT,
            CODE_IDEMPOTENCY_CONFLICT,
            "that Idempotency-Key was already used for a different request",
        )
    if isinstance(exc, (EvidenceLinkRejected, EvidenceCandidateRejected)):
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT,
            CODE_EVIDENCE_LINK_REJECTED,
            "that evidence candidate is not linkable; reload the ticket",
        )
    if isinstance(exc, (InvalidReviewTransition, ReviewIdentityConflict, DevRevConflictError)):
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT, CODE_REVIEW_CONFLICT, str(exc) or "that change conflicts"
        )
    if isinstance(exc, NotAuthorized):
        return ConsoleHTTPError(
            status.HTTP_403_FORBIDDEN, CODE_FORBIDDEN, "that role may not do this"
        )

    # -- upstream -------------------------------------------------------
    if isinstance(exc, DevRevRateLimitError):
        headers = {}
        retry_after = getattr(exc, "retry_after_s", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            headers["Retry-After"] = str(int(retry_after))
        return ConsoleHTTPError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            CODE_UPSTREAM_RATE_LIMITED,
            "DevRev is rate limiting this console; try again shortly",
            headers=headers,
        )
    if isinstance(exc, DevRevTransientError):
        return ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_UPSTREAM_UNAVAILABLE,
            "DevRev is unavailable; the durable review data is unaffected",
        )
    if isinstance(
        exc,
        (
            DevRevAuthenticationError,
            DevRevPermissionError,
            DevRevProtocolError,
            DevRevPaginationError,
            DevRevResourceLimitError,
        ),
    ):
        # Our credential or contract problem, not the caller's: 502.
        return ConsoleHTTPError(
            status.HTTP_502_BAD_GATEWAY,
            CODE_UPSTREAM_PROTOCOL,
            "DevRev returned a response this console cannot use",
        )
    if isinstance(exc, EvidenceBrokerUnavailable):
        return ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_EVIDENCE_UNAVAILABLE,
            "evidence unavailable: the broker could not be reached",
        )
    if isinstance(exc, (EvidenceAuthorizationError, EvidenceClientError)):
        return ConsoleHTTPError(
            status.HTTP_502_BAD_GATEWAY,
            CODE_EVIDENCE_UNAVAILABLE,
            "evidence unavailable: the broker rejected this console",
        )
    if isinstance(exc, DevRevError):
        return ConsoleHTTPError(
            status.HTTP_502_BAD_GATEWAY,
            CODE_UPSTREAM_PROTOCOL,
            "DevRev returned a response this console cannot use",
        )

    # -- everything else ------------------------------------------------
    if isinstance(exc, (ServiceError, ReviewRepositoryError)):
        return ConsoleHTTPError(
            status.HTTP_502_BAD_GATEWAY, CODE_INTERNAL, "that operation could not complete"
        )
    return ConsoleHTTPError(
        status.HTTP_500_INTERNAL_SERVER_ERROR, CODE_INTERNAL, "an unexpected error occurred"
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_NOT_INITIALIZED,
            "the console is not initialized",
        )
    return value


def get_settings(request: Request) -> Any:
    return _state(request, "settings")


def get_service(request: Request) -> Any:
    return _state(request, "service")


def get_repository(request: Request) -> Any:
    return _state(request, "repository")


def get_clock(request: Request) -> Callable[[], datetime]:
    clock = getattr(request.app.state, "clock", None)
    return clock if callable(clock) else utc_now


def get_cursor_key(request: Request) -> bytes:
    key = getattr(request.app.state, "cursor_key", None)
    if not isinstance(key, bytes):
        raise ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_NOT_INITIALIZED,
            "the console is not initialized",
        )
    return key


async def authenticated_reviewer(request: Request) -> AuthenticatedReviewer:
    """The verified caller, resolved by the console's auth middleware.

    The middleware runs before any route, so a missing value here means the
    request bypassed it — which is a refusal, never a default identity.
    """
    reviewer = getattr(request.state, "reviewer", None)
    if not isinstance(reviewer, AuthenticatedReviewer):
        raise ConsoleHTTPError(
            status.HTTP_401_UNAUTHORIZED, CODE_UNAUTHENTICATED, "authentication is required"
        )
    return reviewer


async def authenticated_identity(request: Request) -> ReviewerIdentity:
    """The verified identity alone, for callers that do not need the role."""
    return (await authenticated_reviewer(request)).identity


def require_role(minimum: ReviewerRole) -> Callable[..., Any]:
    """Build a dependency that enforces a minimum ladder role.

    ``agent`` satisfies nothing here by construction: Stage 8's routes will ask
    for that identity explicitly rather than borrowing a human minimum.
    """

    async def _dependency(
        reviewer: Annotated[AuthenticatedReviewer, Depends(authenticated_reviewer)],
    ) -> AuthenticatedReviewer:
        if not role_at_least(reviewer.role, minimum):
            logger.info(
                "console authorization denied; held=%s required=%s subject_hash=%s",
                reviewer.role.value,
                minimum.value,
                reviewer.subject_hash[:12],
            )
            raise AuthorizationFailed("that role may not do this")
        return reviewer

    return _dependency


def _rate_limited(
    request: Request, reviewer: AuthenticatedReviewer, *, write: bool
) -> None:
    """Apply the per-subject fixed window, if the app installed a limiter."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    limit = WRITE_RATE_LIMIT_PER_MINUTE if write else READ_RATE_LIMIT_PER_MINUTE
    kind = "write" if write else "read"
    allowed, retry_after = limiter.check((kind, reviewer.subject_hash), limit)
    if not allowed:
        raise ConsoleHTTPError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            CODE_RATE_LIMITED,
            "too many requests; slow down",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )


def _assert_no_raw_cursor(request: Request) -> None:
    """Refuse a cursor supplied as a query parameter.

    The remote DevRev cursor and the wrapper token are both server-only, and a
    URL is the one place a token is guaranteed to be written to a log.
    """
    offending = sorted(
        name for name in request.query_params.keys() if name.lower() in FORBIDDEN_CURSOR_PARAMS
    )
    if offending:
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            f"page cursors travel in the {CURSOR_HEADER} header, not in the URL",
        )


def _mutation_context(
    request: Request,
    reviewer: AuthenticatedReviewer,
    *,
    reason_code: Optional[str] = None,
    suffix: Optional[str] = None,
) -> MutationContext:
    """Build the audit context from the *verified* actor, never from the body."""
    key = getattr(request.state, "idempotency_key", None)
    if not isinstance(key, str) or not key:
        # The unsafe-request guard runs in middleware and stores the validated
        # key. Its absence means this route was reached without the guard.
        raise ConsoleHTTPError(
            status.HTTP_400_BAD_REQUEST,
            "IDEMPOTENCY_KEY_REQUIRED",
            "an Idempotency-Key header is required",
        )
    return MutationContext(
        actor=reviewer.identity,
        actor_role=reviewer.role,
        request_id=request_id_of(request),
        idempotency_key=f"{key}:{suffix}" if suffix else key,
        reason_code=reason_code,
    )


# ---------------------------------------------------------------------------
# Ticket references and cursors
# ---------------------------------------------------------------------------

_MAX_TICKET_REF_LENGTH = MAX_ID_LENGTH


def validated_ticket_ref(raw: str) -> str:
    """Accept a bounded display id or an opaque DevRev reference, nothing else.

    Never a URL, never a path fragment: this value is passed to the adapter and
    would otherwise be a way to point the console at an arbitrary host, or to
    build a Firestore path out of caller input.
    """
    value = (raw or "").strip()
    if not value or len(value) > _MAX_TICKET_REF_LENGTH:
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that ticket reference is not usable",
        )
    lowered = value.lower()
    if (
        "://" in lowered
        or lowered.startswith(("http", "//", "/"))
        or ".." in value
        or any(char.isspace() for char in value)
    ):
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that ticket reference is not usable",
        )
    if value.startswith("don:"):
        body = value[4:]
        if not body or not all(
            char.isalnum() or char in "_.:/+-" for char in body
        ):
            raise ConsoleHTTPError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                CODE_VALIDATION_FAILED,
                "that ticket reference is not usable",
            )
        return value
    # A display id such as ``TKT-123``.
    if len(value) > MAX_DISPLAY_ID_LENGTH:
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that ticket reference is not usable",
        )
    prefix, _, digits = value.rpartition("-")
    if not prefix or not digits.isdecimal() or not prefix.replace("-", "").isalnum():
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that ticket reference is not usable",
        )
    return value.upper()


def filter_fingerprint(payload: Mapping[str, Any]) -> str:
    """A stable digest of the filter set a cursor was minted for."""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def seal_console_cursor(
    key: bytes,
    *,
    context: str,
    direction: str,
    subject_hash: str,
    fingerprint: str,
    remote_cursor: str,
    now: datetime,
) -> str:
    """Wrap a remote cursor so only this endpoint, user, and filter can use it."""
    return seal_cursor(
        key,
        {"c": remote_cursor, "f": fingerprint, "s": subject_hash},
        context=f"{context}:{direction}",
        now=now,
    )


def open_console_cursor(
    key: bytes,
    token: str,
    *,
    context: str,
    direction: str,
    subject_hash: str,
    fingerprint: str,
    now: datetime,
) -> str:
    """Unwrap a console cursor, refusing every kind of reuse.

    The endpoint and direction are bound as associated data, so a mismatch fails
    authentication outright. The subject and filter digest are compared after
    decryption, which is what stops one reviewer's cursor from paging another's
    filtered result set.
    """
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise CursorError("cursor token is not acceptable")
    payload = open_cursor(key, token, context=f"{context}:{direction}", now=now)
    if payload.get("s") != subject_hash:
        raise CursorError("cursor does not belong to this session")
    if payload.get("f") != fingerprint:
        raise CursorError("cursor does not belong to this filter")
    remote = payload.get("c")
    if not isinstance(remote, str) or not remote:
        raise CursorError("cursor payload is not readable")
    return remote


CursorHeader = Annotated[
    Optional[str],
    Header(
        alias=CURSOR_HEADER,
        max_length=MAX_CURSOR_LENGTH,
        description="Opaque console page cursor returned by a previous response.",
    ),
]
IfMatchHeader = Annotated[
    Optional[str],
    Header(alias="If-Match", description='Quoted strong validator such as "v3".'),
]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@router.get(
    "/session",
    response_model=SessionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="The verified caller, their role, and a fresh CSRF token",
)
async def get_session(
    request: Request,
    response: Response,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    settings: Annotated[Any, Depends(get_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> SessionResponse:
    """Issue a short-lived, session-bound CSRF token.

    The browser holds it in memory only: a cookie would be sent automatically and
    would defeat the purpose, and local storage would survive an XSS long enough
    to be exfiltrated.
    """
    _no_store(response)
    secret = settings.CSRF_SIGNING_SECRET
    raw_secret = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    minted = mint_csrf_token(
        (raw_secret or "").strip(),
        subject=reviewer.identity.subject,
        now=clock(),
        ttl_s=int(settings.CSRF_TOKEN_TTL_S),
    )
    return SessionResponse(
        identity=reviewer.identity,
        role=reviewer.role,
        csrf_token=minted.token,
        csrf_expires_at=minted.expires_at,
        feature_flags={
            # Only booleans a UI branches on. No configuration values, no
            # secrets, no host names.
            "remediation_enabled": False,
            "import_export_enabled": False,
            "synthetic_verification": bool(settings.ENABLE_SYNTHETIC_VERIFICATION),
            "classification_configured": not classification_diagnostics(settings),
        },
    )


# ---------------------------------------------------------------------------
# Live DevRev tickets
# ---------------------------------------------------------------------------


@router.get(
    "/tickets",
    response_model=CursorPage[DevRevTicketWithReviewSummary],
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    summary="One live works.list page overlaid with review summaries",
)
async def list_tickets(
    request: Request,
    response: Response,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    page_cursor: CursorHeader = None,
    ticket_id: Annotated[Optional[str], Query(max_length=MAX_DISPLAY_ID_LENGTH)] = None,
    mode: Annotated[Literal["after", "before"], Query()] = "after",
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    stage: Annotated[Optional[list[str]], Query()] = None,
    state: Annotated[Optional[list[str]], Query()] = None,
    applies_to_part: Annotated[Optional[list[str]], Query()] = None,
    owned_by: Annotated[Optional[list[str]], Query()] = None,
    created_by: Annotated[Optional[list[str]], Query()] = None,
    reported_by: Annotated[Optional[list[str]], Query()] = None,
    tags: Annotated[Optional[list[str]], Query()] = None,
    ticket_source_channel: Annotated[Optional[list[str]], Query()] = None,
    ticket_subtype: Annotated[Optional[list[str]], Query()] = None,
    ticket_visibility: Annotated[Optional[list[int]], Query()] = None,
    created_date: Annotated[Optional[str], Query(max_length=80)] = None,
    modified_date: Annotated[Optional[str], Query(max_length=80)] = None,
) -> CursorPage[DevRevTicketWithReviewSummary]:
    """List live tickets, or resolve exactly one by display id.

    An exact ``ticket_id`` is mutually exclusive with every list filter: it takes
    the scoped ``works.get`` path because ``works.list`` has no display-id filter
    in the allowlisted surface, so combining them would silently ignore one half
    of the request.
    """
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)

    filters = DevRevTicketFilters(
        stage=stage or [],
        state=state or [],
        applies_to_part=applies_to_part or [],
        owned_by=owned_by or [],
        created_by=created_by or [],
        reported_by=reported_by or [],
        tags=tags or [],
        ticket_source_channel=ticket_source_channel or [],
        ticket_subtype=ticket_subtype or [],
        ticket_visibility=ticket_visibility or [],
        created_date=created_date,
        modified_date=modified_date,
    )
    has_filters = any(filters.model_dump(exclude_none=True).values())

    if ticket_id is not None:
        if has_filters or page_cursor is not None:
            raise ConsoleHTTPError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                CODE_UNSUPPORTED_FILTER,
                "an exact ticket id cannot be combined with list filters or a cursor",
            )
        page = await service.get_live_ticket_by_display_id(
            validated_ticket_ref(ticket_id), reviewer.identity
        )
        return _rewrapped_ticket_page(
            page,
            cursor_key=cursor_key,
            fingerprint="exact",
            subject_hash=reviewer.subject_hash,
            now=clock(),
            singleton=True,
        )

    fingerprint = filter_fingerprint(filters.model_dump(mode="json", exclude_none=True))
    remote_cursor: Optional[str] = None
    if page_cursor is not None:
        remote_cursor = open_console_cursor(
            cursor_key,
            page_cursor,
            context=CURSOR_CONTEXT_TICKETS,
            direction=mode,
            subject_hash=reviewer.subject_hash,
            fingerprint=fingerprint,
            now=clock(),
        )

    page = await service.list_live_tickets(
        filters, cursor=remote_cursor, mode=mode, limit=page_size
    )
    return _rewrapped_ticket_page(
        page,
        cursor_key=cursor_key,
        fingerprint=fingerprint,
        subject_hash=reviewer.subject_hash,
        now=clock(),
    )


def _rewrapped_ticket_page(
    page: CursorPage[DevRevTicketWithReviewSummary],
    *,
    cursor_key: bytes,
    fingerprint: str,
    subject_hash: str,
    now: datetime,
    singleton: bool = False,
) -> CursorPage[DevRevTicketWithReviewSummary]:
    """Replace remote cursors with bound console tokens.

    A singleton exact-id result gets no cursors at all: there is nothing to page
    through, and offering one would invite a pointless second call.
    """
    if singleton:
        return page.model_copy(update={"next_cursor": None, "prev_cursor": None})
    updates: dict[str, Any] = {}
    for field, direction in (("next_cursor", "after"), ("prev_cursor", "before")):
        remote = getattr(page, field, None)
        updates[field] = (
            seal_console_cursor(
                cursor_key,
                context=CURSOR_CONTEXT_TICKETS,
                direction=direction,
                subject_hash=subject_hash,
                fingerprint=fingerprint,
                remote_cursor=remote,
                now=now,
            )
            if isinstance(remote, str) and remote
            else None
        )
    return page.model_copy(update=updates)


@router.get(
    "/tickets/{ticket_ref}",
    response_model=TicketDetailEnvelope,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="One ticket, its review, one timeline page, and its evidence",
)
async def get_ticket_detail(
    request: Request,
    response: Response,
    ticket_ref: Annotated[str, Path(max_length=_MAX_TICKET_REF_LENGTH)],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    page_cursor: CursorHeader = None,
) -> TicketDetailEnvelope:
    """Hydrate one ticket. A DevRev outage still returns the durable review."""
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    reference = validated_ticket_ref(ticket_ref)

    timeline_cursor: Optional[str] = None
    if page_cursor is not None:
        timeline_cursor = open_console_cursor(
            cursor_key,
            page_cursor,
            context=CURSOR_CONTEXT_TIMELINE,
            direction="after",
            subject_hash=reviewer.subject_hash,
            fingerprint=filter_fingerprint({"ref": reference}),
            now=clock(),
        )

    envelope = await service.get_ticket_detail(
        reference, reviewer.identity, timeline_cursor=timeline_cursor
    )
    if envelope.timeline is None:
        return envelope
    return envelope.model_copy(
        update={
            "timeline": _rewrapped_timeline(
                envelope.timeline,
                cursor_key=cursor_key,
                reference=reference,
                subject_hash=reviewer.subject_hash,
                now=clock(),
            )
        }
    )


@router.get(
    "/tickets/{ticket_ref}/timeline",
    response_model=ClassifiedTimelinePage,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="One bounded, forward-only timeline page",
)
async def get_ticket_timeline(
    request: Request,
    response: Response,
    ticket_ref: Annotated[str, Path(max_length=_MAX_TICKET_REF_LENGTH)],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ClassifiedTimelinePage:
    """Page a conversation forward only.

    DevRev's timeline pagination has no backward mode in the allowlisted surface,
    so there is no ``before`` direction to mint and none is accepted.
    """
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    reference = validated_ticket_ref(ticket_ref)
    fingerprint = filter_fingerprint({"ref": reference})

    remote_cursor: Optional[str] = None
    if page_cursor is not None:
        remote_cursor = open_console_cursor(
            cursor_key,
            page_cursor,
            context=CURSOR_CONTEXT_TIMELINE,
            direction="after",
            subject_hash=reviewer.subject_hash,
            fingerprint=fingerprint,
            now=clock(),
        )

    page = await service.get_timeline_page(reference, remote_cursor, limit=page_size)
    return _rewrapped_timeline(
        page,
        cursor_key=cursor_key,
        reference=reference,
        subject_hash=reviewer.subject_hash,
        now=clock(),
    )


def _rewrapped_timeline(
    page: ClassifiedTimelinePage,
    *,
    cursor_key: bytes,
    reference: str,
    subject_hash: str,
    now: datetime,
) -> ClassifiedTimelinePage:
    remote = getattr(page, "next_cursor", None)
    wrapped = (
        seal_console_cursor(
            cursor_key,
            context=CURSOR_CONTEXT_TIMELINE,
            direction="after",
            subject_hash=subject_hash,
            fingerprint=filter_fingerprint({"ref": reference}),
            remote_cursor=remote,
            now=now,
        )
        if isinstance(remote, str) and remote
        else None
    )
    # A timeline page never offers a backward cursor.
    return page.model_copy(update={"next_cursor": wrapped, "prev_cursor": None})


# ---------------------------------------------------------------------------
# Durable reviews
# ---------------------------------------------------------------------------


@router.post(
    "/tickets/{ticket_ref}/review",
    response_model=TicketReview,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Create the durable review for a live ticket, idempotently",
)
async def create_review(
    request: Request,
    response: Response,
    ticket_ref: Annotated[str, Path(max_length=_MAX_TICKET_REF_LENGTH)],
    payload: CreateReviewRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))],
    service: Annotated[Any, Depends(get_service)],
) -> TicketReview:
    """Seed a review from identifiers, then apply any supplied initial fields.

    The service seeds identifiers only — no DevRev title or body is copied into
    durable storage. Optional initial fields therefore travel as a second,
    separately-keyed audited patch rather than being silently dropped, mirroring
    how the service chains its own correlation update.
    """
    _no_store(response)
    _rate_limited(request, reviewer, write=True)
    reference = validated_ticket_ref(ticket_ref)

    review = await service.import_review(
        reference, reviewer.identity, _mutation_context(request, reviewer)
    )

    seeded = payload.model_dump(exclude_none=True)
    if seeded:
        review = await service.patch_review(
            review.review_id,
            ReviewPatch(**seeded),
            review.version,
            reviewer.identity,
            _mutation_context(request, reviewer, suffix="seed"),
        )
    response.headers["ETag"] = format_etag(review.version)
    return review


@router.get(
    "/reviews",
    response_model=CursorPage[TicketReview],
    responses={422: {"model": ErrorResponse}},
    summary="The durable review queue",
)
async def list_reviews(
    request: Request,
    response: Response,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    page_cursor: CursorHeader = None,
    statuses: Annotated[Optional[list[ReviewStatus]], Query()] = None,
    facet: Annotated[Optional[str], Query(max_length=80)] = None,
    facet_value: Annotated[Optional[str], Query(max_length=200)] = None,
    title_contains: Annotated[Optional[str], Query(max_length=512)] = None,
    devrev_display_id: Annotated[Optional[str], Query(max_length=MAX_DISPLAY_ID_LENGTH)] = None,
    updated_after: Annotated[Optional[datetime], Query()] = None,
    updated_before: Annotated[Optional[datetime], Query()] = None,
    include_reversed: Annotated[bool, Query()] = False,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorPage[TicketReview]:
    """Page the Firestore queue.

    The repository's own cursor is already signed and filter-bound, so it is
    transported through the same header rather than wrapped a second time.
    """
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)

    if (facet is None) != (facet_value is None):
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_UNSUPPORTED_FILTER,
            "a facet filter needs both a name and a value",
        )
    query = ReviewListQuery(
        statuses=statuses or [],
        facets={facet: facet_value} if facet is not None and facet_value is not None else {},
        title_contains=title_contains,
        devrev_display_id=devrev_display_id,
        updated_after=updated_after,
        updated_before=updated_before,
        include_reversed=include_reversed,
        page_size=page_size,
        cursor=page_cursor,
    )
    return await service.list_reviews(query)


@router.get(
    "/reviews/{review_id}",
    response_model=TicketReview,
    responses={404: {"model": ErrorResponse}},
    summary="One durable review",
)
async def get_review(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    repository: Annotated[Any, Depends(get_repository)],
) -> TicketReview:
    _no_store(response)
    _rate_limited(request, reviewer, write=False)
    review = await repository.get_review(review_id)
    response.headers["ETag"] = format_etag(review.version)
    return review


@router.patch(
    "/reviews/{review_id}",
    response_model=TicketReview,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        412: {"model": ErrorResponse},
        428: {"model": ErrorResponse},
    },
    summary="Update a review under optimistic concurrency",
)
async def patch_review(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    patch: ReviewPatch,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))],
    service: Annotated[Any, Depends(get_service)],
    if_match: IfMatchHeader = None,
    admin_reopen: Annotated[bool, Query()] = False,
) -> TicketReview:
    """Apply a patch, refusing a blind overwrite.

    ``admin_reopen`` is admin-only and explicit: the transition table refuses a
    move out of ``resolved``/``won't fix`` by design, so reopening has to be a
    stated intent rather than a side effect of a normal patch.
    """
    _no_store(response)
    _rate_limited(request, reviewer, write=True)
    expected_version = parse_if_match(if_match)
    if admin_reopen and reviewer.role is not ReviewerRole.ADMIN:
        raise AuthorizationFailed("only an admin may reopen a closed review")

    review = await service.patch_review(
        review_id,
        patch,
        expected_version,
        reviewer.identity,
        _mutation_context(request, reviewer),
        admin_reopen=admin_reopen,
    )
    response.headers["ETag"] = format_etag(review.version)
    return review


@router.get(
    "/reviews/{review_id}/audit-events",
    response_model=CursorPage[AuditEvent],
    responses={404: {"model": ErrorResponse}},
    summary="The review's append-only audit ledger",
)
async def list_audit_events(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    repository: Annotated[Any, Depends(get_repository)],
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorPage[AuditEvent]:
    """Return one bounded page of audit events.

    The repository's audit read is ordered and bounded but does not mint a
    cursor, so ``next_cursor`` is honestly ``null`` here rather than a token that
    would page nowhere.
    """
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    # Proves the review exists (and 404s uniformly if it does not) before
    # reading its subcollection.
    await repository.get_review(review_id)
    page = await repository.list_audit_events(review_id, page_size=page_size)
    events = [item for item in page.items if isinstance(item, AuditEvent)]
    return CursorPage[AuditEvent](
        items=events, next_cursor=None, page_size=max(page.page_size, len(events))
    )


# ---------------------------------------------------------------------------
# Evidence links
# ---------------------------------------------------------------------------


@router.get(
    "/reviews/{review_id}/evidence-links",
    response_model=CursorPage[EvidenceLink],
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Live evidence links for a review",
)
async def list_evidence_links(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    repository: Annotated[Any, Depends(get_repository)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorPage[EvidenceLink]:
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    await repository.get_review(review_id)
    page = await repository.list_evidence_links(
        review_id, page_size=page_size, cursor=page_cursor
    )
    links = [item for item in page.items if isinstance(item, EvidenceLink)]
    return CursorPage[EvidenceLink](
        items=links,
        next_cursor=page.next_cursor,
        page_size=max(page.page_size, len(links)),
    )


@router.post(
    "/reviews/{review_id}/evidence-links",
    response_model=TicketReview,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        412: {"model": ErrorResponse},
        428: {"model": ErrorResponse},
    },
    summary="Link a server-minted broker candidate to a review",
)
async def create_evidence_link(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    payload: CreateEvidenceLinkRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))],
    service: Annotated[Any, Depends(get_service)],
    if_match: IfMatchHeader = None,
) -> TicketReview:
    """Turn a candidate token into a durable, reasoned, audited link.

    The caller never supplies an execution id — only a short-lived token the
    server minted after its own bounded broker lookup, which the service
    revalidates against the current broker result before writing.
    """
    _no_store(response)
    _rate_limited(request, reviewer, write=True)
    expected_version = parse_if_match(if_match)
    review = await service.create_evidence_link(
        review_id,
        candidate_token=payload.broker_candidate_token,
        reason=payload.reason,
        expected_version=expected_version,
        actor=reviewer.identity,
        request_context=_mutation_context(request, reviewer, reason_code="evidence_linked"),
    )
    response.headers["ETag"] = format_etag(review.version)
    return review


@router.delete(
    "/reviews/{review_id}/evidence-links/{link_id}",
    response_model=TicketReview,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        412: {"model": ErrorResponse},
        428: {"model": ErrorResponse},
    },
    summary="Unlink evidence with an explicit reason",
)
async def delete_evidence_link(
    request: Request,
    response: Response,
    review_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    link_id: Annotated[str, Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
    payload: DeleteEvidenceLinkRequest,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))],
    repository: Annotated[Any, Depends(get_repository)],
    if_match: IfMatchHeader = None,
) -> TicketReview:
    """Retire a link in place, keeping the reviewer's reason in the audit trail.

    The reason is mandatory: an unexplained unlink is indistinguishable from
    tampering when the record is read back two years later.
    """
    _no_store(response)
    _rate_limited(request, reviewer, write=True)
    expected_version = parse_if_match(if_match)
    review = await repository.unlink_evidence_link(
        review_id,
        link_id,
        reason=payload.reason,
        expected_version=expected_version,
        context=_mutation_context(request, reviewer, reason_code="evidence_unlinked"),
    )
    response.headers["ETag"] = format_etag(review.version)
    return review


__all__ = [
    "API_PREFIX",
    "CODE_CURSOR_REJECTED",
    "CODE_EVIDENCE_LINK_REJECTED",
    "CODE_EVIDENCE_UNAVAILABLE",
    "CODE_FORBIDDEN",
    "CODE_IDEMPOTENCY_CONFLICT",
    "CODE_INTERNAL",
    "CODE_NOT_FOUND",
    "CODE_NOT_INITIALIZED",
    "CODE_PRECONDITION_MALFORMED",
    "CODE_PRECONDITION_REQUIRED",
    "CODE_RATE_LIMITED",
    "CODE_REVIEW_CONFLICT",
    "CODE_REVIEW_VERSION_CONFLICT",
    "CODE_UNAUTHENTICATED",
    "CODE_UNSUPPORTED_FILTER",
    "CODE_UPSTREAM_PROTOCOL",
    "CODE_UPSTREAM_RATE_LIMITED",
    "CODE_UPSTREAM_UNAVAILABLE",
    "CODE_VALIDATION_FAILED",
    "CURSOR_CONTEXT_TICKETS",
    "CURSOR_CONTEXT_TIMELINE",
    "FORBIDDEN_CURSOR_PARAMS",
    "READ_RATE_LIMIT_PER_MINUTE",
    "WRITE_RATE_LIMIT_PER_MINUTE",
    "ConsoleHTTPError",
    "authenticated_identity",
    "authenticated_reviewer",
    "error_body",
    "error_response",
    "filter_fingerprint",
    "map_exception",
    "open_console_cursor",
    "request_id_of",
    "require_role",
    "router",
    "seal_console_cursor",
    "validated_ticket_ref",
]
