"""The Stage 5 subset of ``/api/admin/v1``.

Scope
-----
Session, live DevRev ticket list/detail/timeline, durable review
create/list/detail/patch, audit-event list, and evidence-link list/create/
delete. Remediation batches arrive in Stage 8; file interchange is deliberately
outside the product contract.
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

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Callable, Optional

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse

from api.reviewer_auth import (
    AuthenticatedReviewer,
    AuthorizationFailed,
    ReviewerAuthError,
    role_at_least,
)
from api.ticket_review_models import (
    BATCH_ID_PATTERN,
    DEFAULT_PAGE_SIZE,
    MAX_CURSOR_LENGTH,
    MAX_DISPLAY_ID_LENGTH,
    MAX_ID_LENGTH,
    MAX_PAGE_SIZE,
    MAX_REASON_LENGTH,
    AuditEvent,
    BatchStatus,
    CancelBatchRequest,
    ClaimBatchResponse,
    CompleteBatchRequest,
    CreateEvidenceLinkRequest,
    CreateRemediationBatchRequest,
    CreateRemediationBatchResponse,
    CursorError,
    CursorPage,
    DeleteEvidenceLinkRequest,
    DevRevHydrationStatus,
    ErrorBody,
    ErrorResponse,
    EvaluationRoute,
    EvaluationStatus,
    EvidenceLink,
    ExtendLeaseRequest,
    HeartbeatBatchRequest,
    LEASE_TOKEN_HEADER,
    InvalidBatchTransition as ContractInvalidBatchTransition,
    MalformedPreconditionError,
    MaterializeBatchRequest,
    MaterializeBatchResponse,
    MissingPreconditionError,
    PatchBatchRequest,
    PreconditionError,
    ReadyBatchRequest,
    ReleaseBatchRequest,
    RemediationBatchItem,
    RemediationBatchView,
    ReviewPatch,
    ReviewStatus,
    ReviewerIdentity,
    ReviewerRole,
    SessionResponse,
    StalePreconditionError,
    StartVerificationRequest,
    TicketEvaluationDetailEnvelope,
    TicketEvaluationSummary,
    TicketReview,
    ClassifiedTimelinePage,
    batch_view,
    format_etag,
    http_status_for_precondition_error,
    open_cursor,
    parse_if_match,
    seal_cursor,
    utc_now,
    validated_batch_id,
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
from data_pipeline.ticket_review_remediation_prompt import (
    PROMPT_TEMPLATE_VERSION,
    PromptRenderError,
    render_remediation_prompt,
    template_sha256,
)
from data_pipeline.ticket_review_repository import (
    BatchAlreadyClaimed,
    BatchContractViolation,
    BatchLeaseLost,
    BatchNotFound,
    BatchReleaseRefused,
    BatchVersionConflict,
    EvidenceCandidateRejected,
    IdempotencyConflict,
    InvalidBatchTransition,
    InvalidReviewTransition,
    LeaseExtensionRefused,
    MutationContext,
    NotAuthorized,
    ReviewIdentityConflict,
    ReviewListQuery,
    ReviewNotFound,
    EvaluationRunNotFound,
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
CODE_BATCH_VERSION_CONFLICT = "BATCH_VERSION_CONFLICT"
CODE_BATCH_CONFLICT = "BATCH_CONFLICT"
CODE_BATCH_ALREADY_CLAIMED = "BATCH_ALREADY_CLAIMED"
CODE_BATCH_LEASE_LOST = "BATCH_LEASE_LOST"
CODE_BATCH_REJECTED = "BATCH_REJECTED"
CODE_PROMPT_UNAVAILABLE = "PROMPT_UNAVAILABLE"
CODE_REMEDIATION_DISABLED = "REMEDIATION_DISABLED"

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
TICKET_EVALUATION_QUERY_PARAMS = frozenset(
    {
        "execution_id",
        "devrev_display_id",
        "route",
        "status",
        "hydration_status",
        "review_status",
        "page_size",
    }
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
    if isinstance(exc, BatchVersionConflict):
        # 412 with the current version, exactly as for a review: the CLI resyncs
        # from this rather than guessing, and a lost race must never look like a
        # server fault the agent should retry blindly.
        return ConsoleHTTPError(
            status.HTTP_412_PRECONDITION_FAILED,
            CODE_BATCH_VERSION_CONFLICT,
            "The batch changed. Re-read it before writing.",
            current_version=exc.current_version,
            changed_at=exc.changed_at,
        )

    # -- not found ------------------------------------------------------
    if isinstance(
        exc,
        (
            TicketNotFound,
            EvaluationRunNotFound,
            ReviewNotFound,
            BatchNotFound,
            DevRevNotFoundError,
            DevRevScopeError,
        ),
    ):
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
    if isinstance(exc, BatchAlreadyClaimed):
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT,
            CODE_BATCH_ALREADY_CLAIMED,
            "another agent holds a live lease on this batch",
        )
    if isinstance(exc, BatchLeaseLost):
        # 409, not 403: the caller's identity was fine and its lease was not.
        # The CLI distinguishes these to decide whether to re-claim or to stop.
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT, CODE_BATCH_LEASE_LOST, str(exc) or "the lease is gone"
        )
    if isinstance(exc, (BatchReleaseRefused, LeaseExtensionRefused, BatchContractViolation)):
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT, CODE_BATCH_REJECTED, str(exc) or "that change conflicts"
        )
    if isinstance(exc, (InvalidBatchTransition, ContractInvalidBatchTransition)):
        # The message is this module's own, built from the closed state machine,
        # and it is the only way a caller learns *which* part of a submission
        # was incomplete. It never echoes a remote body or record content.
        return ConsoleHTTPError(
            status.HTTP_409_CONFLICT, CODE_BATCH_CONFLICT, str(exc) or "that change conflicts"
        )
    if isinstance(exc, NotAuthorized):
        return ConsoleHTTPError(
            status.HTTP_403_FORBIDDEN, CODE_FORBIDDEN, "that role may not do this"
        )
    if isinstance(exc, PromptRenderError):
        # A packaging or configuration fault, never the caller's: 503 so an
        # operator looks at the revision rather than at the request.
        return ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_PROMPT_UNAVAILABLE,
            "the remediation prompt cannot be rendered in this build",
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


async def authenticated_agent(
    reviewer: Annotated[AuthenticatedReviewer, Depends(authenticated_reviewer)],
) -> AuthenticatedReviewer:
    """The verified remediation agent, and nothing else.

    Deliberately not ``require_role(ReviewerRole.AGENT)``: ``role_at_least``
    returns ``False`` whenever either side is ``agent``, by design, so a route
    written that way would deny every caller including the real agent. The
    ``is_agent`` flag is set only by :mod:`api.reviewer_auth` after a signed IAP
    assertion matched the configured service account, so a human who somehow
    carried ``role: agent`` still fails here.
    """
    if not (reviewer.is_agent and reviewer.role is ReviewerRole.AGENT):
        logger.info(
            "console agent route denied; held=%s subject_hash=%s",
            reviewer.role.value,
            reviewer.subject_hash[:12],
        )
        raise AuthorizationFailed("this route belongs to the remediation agent")
    return reviewer


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


def validated_execution_id(raw: str) -> str:
    """Accept legacy and invocation-scoped producer identities.

    New IDs use ``job_id-e{lease_epoch}-a{attempt}:inquiry_index``; the
    existing bounded opaque-path rules already admit that URL-safe shape.
    """
    value = (raw or "").strip()
    job_id, separator, index = value.rpartition(":")
    if (
        separator != ":"
        or not job_id
        or len(value) > 160
        or not index.isdecimal()
        or any(
            char
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for char in job_id
        )
    ):
        raise ConsoleHTTPError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            CODE_VALIDATION_FAILED,
            "that execution id is not usable",
        )
    return value


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
            # secrets, no host names. ``remediation_enabled`` is derived rather
            # than hard-coded: the batch routes exist in every build now, but a
            # deployment without an agent identity and a configured repository
            # could only freeze work that nothing is able to claim.
            "remediation_enabled": _remediation_enabled(settings),
            "synthetic_verification": bool(settings.ENABLE_SYNTHETIC_VERIFICATION),
            "classification_configured": not classification_diagnostics(settings),
        },
    )


# ---------------------------------------------------------------------------
# RAG-produced ticket evaluations
# ---------------------------------------------------------------------------


@router.get(
    "/tickets",
    response_model=CursorPage[TicketEvaluationSummary],
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    summary="One page of persisted ticket-associated RAG executions",
)
async def list_tickets(
    request: Request,
    response: Response,
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    execution_id_filter: Annotated[
        Optional[str], Query(alias="execution_id", min_length=3, max_length=160)
    ] = None,
    devrev_display_id: Annotated[
        Optional[str], Query(min_length=3, max_length=MAX_DISPLAY_ID_LENGTH)
    ] = None,
    route: Annotated[Optional[EvaluationRoute], Query()] = None,
    run_status: Annotated[Optional[EvaluationStatus], Query(alias="status")] = None,
    hydration_status: Annotated[Optional[DevRevHydrationStatus], Query()] = None,
    review_status: Annotated[Optional[ReviewStatus], Query()] = None,
) -> CursorPage[TicketEvaluationSummary]:
    """List the internal execution ledger; DevRev discovery is never used."""
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)

    unknown_query = set(request.query_params) - TICKET_EVALUATION_QUERY_PARAMS
    if unknown_query:
        raise UnsupportedQuery(
            "unsupported evaluation query fields: " + ", ".join(sorted(unknown_query))
        )
    filters: dict[str, str] = {}
    if execution_id_filter is not None:
        filters["execution_id"] = validated_execution_id(execution_id_filter)
    if devrev_display_id is not None:
        display_id = validated_ticket_ref(devrev_display_id)
        if display_id.startswith("don:"):
            raise UnsupportedQuery("devrev_display_id requires a display id")
        filters["devrev_display_id"] = display_id
    if route is not None:
        filters["route"] = route.value
    if run_status is not None:
        filters["status"] = run_status.value
    if hydration_status is not None:
        filters["hydration_status"] = hydration_status.value
    if review_status is not None:
        filters["review_status"] = review_status.value

    fingerprint = filter_fingerprint(
        {"collection": "ticket_evaluations", "filters": filters, "version": 1}
    )
    repository_cursor: Optional[str] = None
    if page_cursor is not None:
        repository_cursor = open_console_cursor(
            cursor_key,
            page_cursor,
            context=CURSOR_CONTEXT_TICKETS,
            direction="after",
            subject_hash=reviewer.subject_hash,
            fingerprint=fingerprint,
            now=clock(),
        )

    page = await service.list_evaluation_runs(
        cursor=repository_cursor,
        limit=page_size,
        filters=filters,
    )
    return _rewrapped_ticket_page(
        page,
        cursor_key=cursor_key,
        fingerprint=fingerprint,
        subject_hash=reviewer.subject_hash,
        now=clock(),
    )


def _rewrapped_ticket_page(
    page: CursorPage[TicketEvaluationSummary],
    *,
    cursor_key: bytes,
    fingerprint: str,
    subject_hash: str,
    now: datetime,
) -> CursorPage[TicketEvaluationSummary]:
    """Bind the repository's opaque cursor to this reviewer and collection."""
    repository_cursor = page.next_cursor
    next_cursor = (
            seal_console_cursor(
                cursor_key,
                context=CURSOR_CONTEXT_TICKETS,
                direction="after",
                subject_hash=subject_hash,
                fingerprint=fingerprint,
                remote_cursor=repository_cursor,
                now=now,
            )
            if isinstance(repository_cursor, str) and repository_cursor
            else None
        )
    return page.model_copy(update={"next_cursor": next_cursor, "prev_cursor": None})


@router.get(
    "/tickets/{execution_id}",
    response_model=TicketEvaluationDetailEnvelope,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="One persisted RAG execution with its review and DevRev context",
)
async def get_ticket_detail(
    request: Request,
    response: Response,
    execution_id: Annotated[str, Path(min_length=3, max_length=160)],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
 ) -> TicketEvaluationDetailEnvelope:
    """Require a persisted execution before making any DevRev request."""
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    value = validated_execution_id(execution_id)
    return await service.get_evaluation_detail(
        value,
        reviewer.identity,
        include_evidence=True,
    )


@router.get(
    "/tickets/{execution_id}/timeline",
    response_model=ClassifiedTimelinePage,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="One bounded, forward-only timeline page",
)
async def get_ticket_timeline(
    request: Request,
    response: Response,
    execution_id: Annotated[str, Path(min_length=3, max_length=160)],
    reviewer: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.VIEWER))],
    service: Annotated[Any, Depends(get_service)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ClassifiedTimelinePage:
    """Page a conversation only after proving the RAG execution exists.

    DevRev's timeline pagination has no backward mode in the allowlisted surface,
    so there is no ``before`` direction to mint and none is accepted.
    """
    _no_store(response)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    execution_id = validated_execution_id(execution_id)
    detail = await service.get_evaluation_detail(execution_id)
    reference = detail.execution.devrev_work_id or detail.execution.event.ticket_id
    fingerprint = filter_fingerprint({"execution_id": execution_id})

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
        execution_id=execution_id,
        subject_hash=reviewer.subject_hash,
        now=clock(),
    )


def _rewrapped_timeline(
    page: ClassifiedTimelinePage,
    *,
    cursor_key: bytes,
    execution_id: str,
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
            fingerprint=filter_fingerprint({"execution_id": execution_id}),
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


# ---------------------------------------------------------------------------
# Remediation batches
#
# Fourteen endpoints, exactly the set in the master route table. Three rules run
# through all of them and are worth stating once:
#
# *   **Who may act is decided by the phase, not by seniority.** A remediator
#     curates (create/ready/cancel/prompt), the agent authors (claim/heartbeat/
#     materialize/patch/release), and an independent reviewer or admin judges
#     (start-verification/complete). ``admin`` additionally owns the one bounded
#     lease extension. No role reaches across those lines, which is why the
#     dependencies below differ per route rather than all asking for a minimum.
# *   **The version travels in the body, not in ``If-Match``.** These are the
#     only routes with a non-browser caller, and a CLI has no ETag cache to
#     revalidate; ``expected_version`` in the envelope is the same optimistic
#     check expressed where the caller actually holds it. The response still
#     carries ``ETag`` so a browser can behave normally.
# *   **The lease is a credential.** It arrives as a ``SecretStr``, is unwrapped
#     exactly at the repository call, and is returned in plaintext exactly once,
#     from ``claim``. Nothing here logs it, echoes it, or puts it in an error.
# ---------------------------------------------------------------------------

BATCHES_PATH = "/remediation-batches"

#: The routes the agent's CSRF exemption covers, as templates over one batch id.
#: Read by ``api.tickets_console_main`` when it builds the unsafe-request policy.
#: Human-only routes (``:ready``, ``:cancel``, ``:start-verification``,
#: ``:complete``, ``:extend-lease``) are absent: a browser caller must always
#: prove same-origin intent, and listing them would hand that waiver to a page
#: running in a reviewer's browser.
AGENT_ROUTE_TEMPLATES = frozenset(
    {
        f"{API_PREFIX}{BATCHES_PATH}/{{batch_id}}/claim",
        f"{API_PREFIX}{BATCHES_PATH}/{{batch_id}}/heartbeat",
        f"{API_PREFIX}{BATCHES_PATH}/{{batch_id}}:materialize",
        f"{API_PREFIX}{BATCHES_PATH}/{{batch_id}}:release",
        f"{API_PREFIX}{BATCHES_PATH}/{{batch_id}}",
    }
)

BatchIdPath = Annotated[str, Path(max_length=MAX_ID_LENGTH, pattern=BATCH_ID_PATTERN)]

_BATCH_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    412: {"model": ErrorResponse},
}


#: Domain separator for the lease-token derivation below. Distinct from every
#: other use of the console's AEAD key.
_LEASE_TOKEN_INFO = b"tickets-console/lease-token/v1"


def _lease_token_for(key: bytes, *, batch_id: str, idempotency_key: str) -> str:
    """Derive this claim's lease token from the console key.

    Derived rather than random, and derived rather than caller-supplied, because
    both alternatives break something:

    *   a **caller-chosen** token is a bearer credential produced by whatever
        the client happened to use, and the console would be trusting its
        entropy;
    *   a **freshly random** token cannot survive an idempotent retry. The
        repository binds the token's hash into the claim's request digest, so a
        retry carrying the same ``Idempotency-Key`` and a new random token is a
        conflict, and a replay would otherwise hand back a token that no longer
        matches the stored hash.

    An HMAC over the key, the batch, and the validated idempotency key is
    unguessable without the console's secret and reproducible for exactly the
    retry it should be reproducible for.
    """
    mac = hmac.new(
        key,
        b"|".join(
            (
                _LEASE_TOKEN_INFO,
                batch_id.encode("utf-8"),
                idempotency_key.encode("utf-8"),
            )
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def _remediation_enabled(settings: Any) -> bool:
    """Whether this deployment may run the agent handoff at all.

    A console with batch routes published but nothing able to claim them would
    let a reviewer freeze work that no agent can ever pick up — a broken feature
    rather than a disabled one. So the repository contract the prompt renders
    from is always required.

    The agent *service account* is required only where IAP is the identity
    source. Under ``AUTH_MODE=local`` there is no IAP assertion to match an
    account against: the agent identity comes from the local-auth path, which
    ``validate_console_auth_startup`` already confines to a loopback peer in a
    non-strict environment. Demanding an account there would be unsatisfiable —
    ``AGENT_IAP_TARGET_AUDIENCE`` must be the console origin plus ``/*`` over
    https, and a loopback fixture has no https origin.
    """
    if not (getattr(settings, "REPO_ID", "") or "").strip():
        return False
    if not (getattr(settings, "EXPECTED_BASE_REF", "") or "").strip():
        return False
    if (getattr(settings, "AUTH_MODE", "") or "").strip() != "local":
        return bool((getattr(settings, "AGENT_SERVICE_ACCOUNT", "") or "").strip())
    return True


def _require_remediation(settings: Any) -> None:
    if not _remediation_enabled(settings):
        raise ConsoleHTTPError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            CODE_REMEDIATION_DISABLED,
            "remediation batches are not configured in this deployment",
        )


def _batch_response(response: Response, batch: Any) -> RemediationBatchView:
    """Render a batch for a human caller and stamp its validator."""
    _no_store(response)
    response.headers["ETag"] = format_etag(batch.version)
    return batch_view(batch)


async def _readable_batch(repository: Any, batch_id: str) -> Any:
    """Load a batch, 404ing uniformly when it does not exist."""
    return await repository.get_batch(batch_id)


def _assert_agent_may_read(batch: Any, reviewer: AuthenticatedReviewer) -> None:
    """Refuse an agent read of a batch somebody else is holding.

    The console has exactly one agent identity today, so this is not a
    multi-tenant check — it is the object-scoping rule that stops a *second*
    concurrent run of the same agent from reading a batch a different run
    claimed. It fails as 404 rather than 403 so an unauthorized read cannot be
    used to discover which batch ids exist.

    An **unleased** batch is readable. There is no holder to protect, and the
    agent has to be able to look at what it is about to claim: refusing here
    would mean the documented ``batch show`` → ``batch claim`` order could never
    work, and the first thing an agent did in every run would fail.
    """
    lease = getattr(batch, "lease", None)
    if lease is not None and lease.holder != reviewer.identity.email:
        raise BatchNotFound("no remediation batch exists for that id")


@router.post(
    BATCHES_PATH,
    response_model=CreateRemediationBatchResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_BATCH_ERRORS,
    summary="Freeze selected review/version pairs into a remediation batch",
)
async def create_remediation_batch(
    request: Request,
    response: Response,
    payload: CreateRemediationBatchRequest,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REMEDIATOR))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> CreateRemediationBatchResponse:
    """Freeze each named review at the version the caller last saw.

    The version in every ref is a precondition, not a label: a review that moved
    since the reviewer looked at it fails the whole creation rather than being
    frozen at a state nobody chose. ``transition_to_planned`` then moves the
    constituent *reviews* — never the batch, which starts as a ``draft``.
    """
    _no_store(response)
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=True)

    batch = await repository.create_batch(
        review_refs=[ref.model_dump() for ref in payload.review_refs],
        context=_mutation_context(request, reviewer, reason_code="batch_created"),
        prompt_template_sha256=template_sha256(),
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
    )

    planned: list[str] = []
    unchanged: list[str] = []
    if payload.transition_to_planned:
        for ref in payload.review_refs:
            try:
                await repository.patch_review(
                    ref.review_id,
                    ReviewPatch(status=ReviewStatus.PLANNED),
                    expected_version=ref.review_version,
                    context=_mutation_context(
                        request,
                        reviewer,
                        reason_code="batch_planned",
                        suffix=f"plan:{ref.review_id}",
                    ),
                )
            except (InvalidReviewTransition, ReviewVersionConflict, ReviewNotFound):
                # Best-effort by contract: a review already past `planned`, or
                # not yet triaged, is reported as unchanged. Failing the whole
                # request here would throw away a batch that is already correct.
                unchanged.append(ref.review_id)
            else:
                planned.append(ref.review_id)
    else:
        unchanged = [ref.review_id for ref in payload.review_refs]

    response.headers["ETag"] = format_etag(batch.version)
    return CreateRemediationBatchResponse(
        batch=batch_view(batch),
        planned_review_ids=planned,
        unchanged_review_ids=unchanged,
    )


@router.get(
    f"{BATCHES_PATH}/{{batch_id}}",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="One remediation batch, without its lease credential",
)
async def get_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    reviewer: Annotated[AuthenticatedReviewer, Depends(authenticated_reviewer)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Read a batch as a human verifier, or as the agent that holds it."""
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=False)
    batch = await _readable_batch(repository, validated_batch_id(batch_id))
    if reviewer.role is ReviewerRole.AGENT:
        _assert_agent_may_read(batch, reviewer)
    elif not role_at_least(reviewer.role, ReviewerRole.REVIEWER):
        raise AuthorizationFailed("that role may not read a remediation batch")
    return _batch_response(response, batch)


@router.get(
    f"{BATCHES_PATH}/{{batch_id}}/items",
    response_model=CursorPage[RemediationBatchItem],
    responses=_BATCH_ERRORS,
    summary="One bounded page of frozen batch items",
)
async def list_remediation_batch_items(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    reviewer: Annotated[AuthenticatedReviewer, Depends(authenticated_reviewer)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorPage[RemediationBatchItem]:
    """Page the frozen items so an independent verifier can check the work.

    Never carries conversation: the frozen item is identifiers and the reviewer's
    structured judgment. Text an agent might need is behind ``:materialize``,
    which requires a live lease and writes a read-audit event.
    """
    _no_store(response)
    _require_remediation(settings)
    _assert_no_raw_cursor(request)
    _rate_limited(request, reviewer, write=False)
    resolved = validated_batch_id(batch_id)
    batch = await _readable_batch(repository, resolved)
    if reviewer.role is ReviewerRole.AGENT:
        _assert_agent_may_read(batch, reviewer)
    elif not role_at_least(reviewer.role, ReviewerRole.REVIEWER):
        raise AuthorizationFailed("that role may not read a remediation batch")
    page = await repository.list_batch_items(
        resolved, page_size=page_size, cursor=page_cursor
    )
    items = [item for item in page.items if isinstance(item, RemediationBatchItem)]
    return CursorPage[RemediationBatchItem](
        items=items,
        next_cursor=page.next_cursor,
        page_size=max(page.page_size, len(items)),
    )


@router.get(
    f"{BATCHES_PATH}/{{batch_id}}/prompt",
    response_class=Response,
    responses={
        200: {"content": {"text/plain": {}}},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="The reusable Codex prompt for this batch, as plain text",
)
async def get_remediation_batch_prompt(
    request: Request,
    batch_id: BatchIdPath,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REMEDIATOR))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> Response:
    """Render the shipped template with five validated identifiers.

    Everything substituted comes from validated settings plus the batch id. No
    ticket title, body, participant, reviewer comment, token, or key is in scope
    here, because none of it is passed to the renderer. The repository id and the
    base ref come from configuration and never from a filesystem path — a Cloud
    Run container's own paths say nothing about the operator's checkout.
    """
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=False)
    resolved = validated_batch_id(batch_id)
    batch = await _readable_batch(repository, resolved)
    text = render_remediation_prompt(
        console_url=settings.CONSOLE_ORIGIN,
        environment=settings.ENVIRONMENT,
        batch_id=batch.batch_id,
        repo_id=settings.REPO_ID,
        expected_base_ref=settings.EXPECTED_BASE_REF,
        recorded_template_sha256=batch.prompt_template_sha256,
    )
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:ready",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Publish a batch for the agent to claim",
)
async def ready_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: ReadyBatchRequest,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REMEDIATOR))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Move ``draft``/``blocked`` to ``ready``, proving nothing has drifted."""
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=True)
    batch = await repository.ready_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        reason=payload.reason,
        context=_mutation_context(request, reviewer, reason_code="batch_readied"),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:cancel",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Retire a batch with a recorded reason",
)
async def cancel_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: CancelBatchRequest,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REMEDIATOR))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Cancel a batch. Terminal, reasoned, and it invalidates any live lease."""
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=True)
    batch = await repository.cancel_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        reason=payload.reason,
        context=_mutation_context(request, reviewer, reason_code="batch_cancelled"),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}/claim",
    response_model=ClaimBatchResponse,
    responses=_BATCH_ERRORS,
    summary="Claim a ready batch under a bounded lease",
)
async def claim_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    agent: Annotated[AuthenticatedReviewer, Depends(authenticated_agent)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
    cursor_key: Annotated[bytes, Depends(get_cursor_key)],
) -> ClaimBatchResponse:
    """Mint the lease token here, server-side, and return it exactly once.

    Only the SHA-256 is persisted, so this response is the sole moment the
    plaintext exists outside the agent's own memory.
    """
    _no_store(response)
    _require_remediation(settings)
    _rate_limited(request, agent, write=True)
    resolved = validated_batch_id(batch_id)
    context = _mutation_context(request, agent, reason_code="batch_claimed")
    token = _lease_token_for(
        cursor_key, batch_id=resolved, idempotency_key=context.idempotency_key or ""
    )
    claim = await repository.claim_batch(resolved, lease_token=token, context=context)
    response.headers["ETag"] = format_etag(claim.batch.version)
    # The plaintext travels in the header, never in the body: the response model
    # masks it, and that masking is a tested invariant of every envelope that
    # carries a lease token. See LEASE_TOKEN_HEADER.
    response.headers[LEASE_TOKEN_HEADER] = token
    return ClaimBatchResponse(
        batch=claim.batch,
        lease_token=token,
        lease_expires_at=claim.lease_expires_at,
        heartbeat_interval_s=int(settings.REMEDIATION_HEARTBEAT_S),
        max_continuous_lease_s=int(settings.REMEDIATION_MAX_CONTINUOUS_LEASE_S),
    )


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}/heartbeat",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Renew a live lease inside the continuous cap",
)
async def heartbeat_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: HeartbeatBatchRequest,
    agent: Annotated[AuthenticatedReviewer, Depends(authenticated_agent)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Renew the lease. Refused once the two-hour continuous cap is reached."""
    _require_remediation(settings)
    _rate_limited(request, agent, write=True)
    batch = await repository.heartbeat_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        lease_token=payload.lease_token.get_secret_value(),
        context=_mutation_context(request, agent, reason_code="batch_heartbeat"),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:materialize",
    response_model=MaterializeBatchResponse,
    responses=_BATCH_ERRORS,
    summary="Read one bounded, audited page of the frozen records",
)
async def materialize_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: MaterializeBatchRequest,
    agent: Annotated[AuthenticatedReviewer, Depends(authenticated_agent)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
    page_cursor: CursorHeader = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> MaterializeBatchResponse:
    """A POST because it writes a read-audit event, not because it changes state.

    This is the moment ticket-derived data leaves the console for an automated
    caller, so it is the event an operator will look for later. The batch version
    does not move; the audit chain does.
    """
    _no_store(response)
    _require_remediation(settings)
    _assert_no_raw_cursor(request)
    _rate_limited(request, agent, write=True)
    result = await repository.materialize_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        lease_token=payload.lease_token.get_secret_value(),
        include_conversation=payload.include_conversation,
        page_size=page_size,
        cursor=page_cursor,
        context=_mutation_context(
            request,
            agent,
            reason_code=(
                "batch_materialized_with_comments"
                if payload.include_conversation
                else "batch_materialized"
            ),
        ),
    )
    response.headers["ETag"] = format_etag(result.batch.version)
    return MaterializeBatchResponse(
        batch_id=result.batch.batch_id,
        batch_version=result.batch.version,
        items=result.items,
        drifted_review_ids=result.drifted_review_ids,
        conversation_included=result.conversation_included,
        next_cursor=result.next_cursor,
        partial=result.next_cursor is not None,
        truncated=bool(result.drifted_review_ids),
        warnings=result.warnings,
    )


@router.patch(
    f"{BATCHES_PATH}/{{batch_id}}",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Record plan, progress, or the final submission",
)
async def patch_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: PatchBatchRequest,
    agent: Annotated[AuthenticatedReviewer, Depends(authenticated_agent)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """The agent's only write path, and it stops at ``changes_proposed``.

    ``verifying`` and ``completed`` are refused here whatever the request says.
    An agent that could reach either would be able to declare its own work
    verified, which is the one thing this whole handoff exists to prevent.
    """
    _require_remediation(settings)
    _rate_limited(request, agent, write=True)
    if payload.transition in {BatchStatus.VERIFYING, BatchStatus.COMPLETED}:
        raise ConsoleHTTPError(
            status.HTTP_409_CONFLICT,
            CODE_BATCH_CONFLICT,
            "an agent may not verify or complete a batch",
        )
    batch = await repository.patch_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        lease_token=payload.lease_token.get_secret_value(),
        transition=payload.transition,
        plan_artifact=payload.plan_artifact,
        branch=payload.branch,
        commit_sha=payload.commit_sha,
        uncommitted_reason=payload.uncommitted_reason,
        pr_url=payload.pr_url,
        changed_files=payload.changed_files or None,
        test_evidence=payload.test_evidence or None,
        verification_summary=payload.summary,
        per_review_outcomes=payload.per_review_outcomes or None,
        context=_mutation_context(request, agent, reason_code="batch_updated"),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:release",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Give a claim back, to 'ready' or to 'blocked'",
)
async def release_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: ReleaseBatchRequest,
    agent: Annotated[AuthenticatedReviewer, Depends(authenticated_agent)],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Release atomically, and never leave an unleased active batch behind.

    ``ready`` is available only while no durable work exists and every frozen
    review still matches; otherwise the repository insists on ``blocked`` with a
    reason, so the next agent inherits a state it can reason about.
    """
    _require_remediation(settings)
    _rate_limited(request, agent, write=True)
    batch = await repository.release_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        lease_token=payload.lease_token.get_secret_value(),
        disposition=BatchStatus(payload.disposition),
        reason=payload.reason,
        context=_mutation_context(request, agent, reason_code="batch_released"),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:start-verification",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Take a submitted batch on for independent review",
)
async def start_remediation_batch_verification(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: StartVerificationRequest,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """``changes_proposed`` to ``verifying``, by somebody who did not author it."""
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=True)
    batch = await repository.start_batch_verification(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        attestation=payload.independent_verifier_attestation,
        reason=payload.reason,
        context=_mutation_context(
            request, reviewer, reason_code="verification_started"
        ),
    )
    return _batch_response(response, batch)


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:complete",
    response_model=CreateRemediationBatchResponse,
    responses=_BATCH_ERRORS,
    summary="Close a verified batch and resolve the reviews it fixed",
)
async def complete_remediation_batch(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: CompleteBatchRequest,
    reviewer: Annotated[
        AuthenticatedReviewer, Depends(require_role(ReviewerRole.REVIEWER))
    ],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> CreateRemediationBatchResponse:
    """Complete the batch and its review resolutions in one transaction.

    Reuses the creation response shape because the interesting part of the answer
    is the same question in reverse: which reviews did this actually move, and
    which did it leave alone. A review whose own lifecycle cannot reach the
    decided status is reported unchanged rather than forced.
    """
    _require_remediation(settings)
    _rate_limited(request, reviewer, write=True)
    completion = await repository.complete_batch(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        decision=payload.decision,
        verification_evidence=payload.verification_evidence or None,
        per_review_decisions=payload.per_review_decisions or None,
        reason=payload.reason,
        context=_mutation_context(request, reviewer, reason_code="batch_completed"),
    )
    _no_store(response)
    response.headers["ETag"] = format_etag(completion.batch.version)
    return CreateRemediationBatchResponse(
        batch=batch_view(completion.batch),
        planned_review_ids=completion.resolved_review_ids,
        unchanged_review_ids=completion.unchanged_review_ids,
    )


@router.post(
    f"{BATCHES_PATH}/{{batch_id}}:extend-lease",
    response_model=RemediationBatchView,
    responses=_BATCH_ERRORS,
    summary="Grant the one bounded lease extension past the continuous cap",
)
async def extend_remediation_batch_lease(
    request: Request,
    response: Response,
    batch_id: BatchIdPath,
    payload: ExtendLeaseRequest,
    admin: Annotated[AuthenticatedReviewer, Depends(require_role(ReviewerRole.ADMIN))],
    repository: Annotated[Any, Depends(get_repository)],
    settings: Annotated[Any, Depends(get_settings)],
) -> RemediationBatchView:
    """Admin-only, once per batch, and it never resets the continuous window."""
    _require_remediation(settings)
    _rate_limited(request, admin, write=True)
    batch = await repository.extend_lease(
        validated_batch_id(batch_id),
        expected_version=payload.expected_version,
        additional_minutes=payload.additional_minutes,
        reason=payload.reason,
        context=_mutation_context(request, admin, reason_code="batch_lease_extended"),
    )
    return _batch_response(response, batch)


__all__ = [
    "AGENT_ROUTE_TEMPLATES",
    "API_PREFIX",
    "BATCHES_PATH",
    "CODE_BATCH_ALREADY_CLAIMED",
    "CODE_BATCH_CONFLICT",
    "CODE_BATCH_LEASE_LOST",
    "CODE_BATCH_REJECTED",
    "CODE_BATCH_VERSION_CONFLICT",
    "CODE_PROMPT_UNAVAILABLE",
    "CODE_REMEDIATION_DISABLED",
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
