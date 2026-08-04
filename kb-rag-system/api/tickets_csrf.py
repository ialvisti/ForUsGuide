"""Unsafe-method protection for the /tickets console.

This module is the console's answer to "a verified identity is not consent". IAP
proves *who* is calling; nothing about a signed assertion proves the reviewer
*meant* to make this request. A page on another origin can make the browser
replay an authenticated request, so every unsafe method must additionally prove
same-origin intent, a live session-bound token, an exact content type, and a
replay-safe key.

The five checks, and why each one is not redundant
--------------------------------------------------
``Origin``
    The only header a cross-site attacker cannot forge from a page. Compared
    against *configuration*, never against ``Host``/``X-Forwarded-Host``, which
    the caller controls.
``Sec-Fetch-Site``
    Fetch Metadata catches the cases ``Origin`` omits, and is set by the browser
    rather than by script. It is required, not optional: a modern browser always
    sends it, so its absence means the caller is not one.
``X-CSRF-Token``
    Defends against the residual case where a same-origin injection can issue
    requests but cannot read the in-memory token. Bound to the session subject,
    so one reviewer's token cannot act for another.
``Content-Type``
    ``application/x-www-form-urlencoded``, ``multipart/form-data`` and
    ``text/plain`` are the three types an HTML form can send without a preflight.
    Requiring strict JSON means a form POST is not even a candidate request.
``Idempotency-Key``
    A retried mutation must not become two mutations. The repository's
    idempotency ledger keys off this value.

The documented non-browser exception
------------------------------------
The remediation CLI (Stage 8) is not a browser: it has no origin and no session.
It may skip Origin/Fetch-Metadata/CSRF, but *only* after signed IAP verification
proved it is the configured service account, only on an allowlisted route, and
only when the request carries no cookies. Content type, idempotency, ETag
preconditions, batch leases, and audit all stay mandatory. Stage 5 ships an
empty route allowlist, so the exception is inert until Stage 8 grants it.

The staging verification handoff
--------------------------------
A cross-role verification run needs to prove "phase N really happened, and phase
N+1 is next" without a human clicking through it. The handoff token does that
and nothing else: it is an *ordering* artifact that grants no authority, and
every normal IAP/RBAC/ETag/idempotency/state check still runs when it is
present. Its key is an HKDF subkey of ``TICKETS_CURSOR_AEAD_KEY``, domain
separated so a page cursor can never be opened as a handoff or vice versa. It is
unavailable in production by construction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from api.reviewer_auth import (
    LOOPBACK_HOSTS,
    ROLE_LADDER,
    AuthConfigurationError,
    AuthenticatedReviewer,
    validate_console_origin,
)
from api.ticket_review_models import (
    CURSOR_AEAD_KEY_BYTES,
    MAX_ID_LENGTH,
    CursorError,
    ReviewerRole,
    open_cursor,
    seal_cursor,
)
from api.tickets_console_config import (
    STRICT_ENVIRONMENTS,
    TicketConsoleSettings,
    decode_cursor_aead_key,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header and media-type vocabulary
# ---------------------------------------------------------------------------
ORIGIN_HEADER = "Origin"
FETCH_SITE_HEADER = "Sec-Fetch-Site"
FETCH_MODE_HEADER = "Sec-Fetch-Mode"
COOKIE_HEADER = "Cookie"
CONTENT_TYPE_HEADER = "Content-Type"
CSRF_HEADER = "X-CSRF-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Console cursors travel in a request header, never in a URL: a query string
#: reaches access logs, proxies, and browser history.
CURSOR_HEADER = "X-Tickets-Cursor"

VERIFICATION_RUN_HEADER = "X-Tickets-Verification-Run"
VERIFICATION_PHASE_HEADER = "X-Tickets-Verification-Phase"
VERIFICATION_HANDOFF_HEADER = "X-Tickets-Verification-Handoff"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
JSON_CONTENT_TYPE = "application/json"
CSV_CONTENT_TYPE = "text/csv"
SAME_ORIGIN = "same-origin"

CSRF_TOKEN_VERSION = "v1"  # noqa: S105 - a format version, not a credential
_CSRF_MAX_TOKEN_LENGTH = MAX_ID_LENGTH
_CSRF_MAX_TTL_S = 24 * 60 * 60

# 8..128 characters, opening on an alphanumeric. Deliberately narrow: this value
# becomes part of a Firestore document id in the idempotency ledger.
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")

# ---------------------------------------------------------------------------
# Verification handoff
# ---------------------------------------------------------------------------

#: AEAD associated data. Distinct from every cursor context in the repository
#: and from the service's evidence-candidate context.
HANDOFF_CONTEXT = "tickets:verification-handoff:v1"

#: HKDF ``info``. Changing this string invalidates every outstanding handoff,
#: which is the intended way to revoke them.
HANDOFF_HKDF_INFO = b"tickets-console/verification-handoff/v1"

#: A handoff exists to bridge two requests in one scripted run, so minutes is
#: the right order of magnitude. The ceiling is enforced at mint time.
HANDOFF_MAX_TTL_S = 15 * 60
HANDOFF_MAX_RESOURCES = 25
HANDOFF_MAX_RESOURCE_ID_LENGTH = 128

#: The ordered, closed phase vocabulary a verification run walks. Stage 5 owns
#: none of the routes that emit these; the sequence is frozen here so Stage 8 and
#: Stage 9 cannot each invent their own and let a reordering slip through.
VERIFICATION_PHASES: tuple[str, ...] = (
    "batch_created",
    "batch_claimed",
    "batch_materialized",
    "changes_proposed",
    "verification_started",
    "batch_completed",
)


# ---------------------------------------------------------------------------
# Typed refusals
# ---------------------------------------------------------------------------


class UnsafeRequestRejected(Exception):
    """Base class for every unsafe-method refusal."""

    code = "REQUEST_REJECTED"
    status = 403


class OriginRejected(UnsafeRequestRejected):
    """The ``Origin`` header is absent or is not the configured console."""

    code = "CSRF_ORIGIN_REJECTED"
    status = 403


class FetchMetadataRejected(UnsafeRequestRejected):
    """``Sec-Fetch-Site`` is absent or is not ``same-origin``."""

    code = "CSRF_FETCH_SITE_REJECTED"
    status = 403


class ContentTypeRejected(UnsafeRequestRejected):
    """The request body is not the exact media type this route accepts."""

    code = "UNSUPPORTED_MEDIA_TYPE"
    status = 415


class CsrfTokenRejected(UnsafeRequestRejected):
    """The CSRF token is absent, expired, malformed, or bound elsewhere."""

    code = "CSRF_TOKEN_REJECTED"
    status = 403


class IdempotencyKeyRejected(UnsafeRequestRejected):
    """``Idempotency-Key`` is absent or outside the accepted shape."""

    code = "IDEMPOTENCY_KEY_REQUIRED"
    status = 400


class VerificationHandoffError(Exception):
    """A verification handoff was absent, malformed, out of order, or expired."""

    code = "VERIFICATION_HANDOFF_REJECTED"
    status = 403


# ---------------------------------------------------------------------------
# The session CSRF token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MintedCsrfToken:
    """A CSRF token and the instant it stops being accepted."""

    token: str
    expires_at: datetime


def _csrf_signature(secret: str, *, subject: str, expires_at: int) -> str:
    payload = f"{CSRF_TOKEN_VERSION}:{subject}:{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_csrf_token(
    secret: str, *, subject: str, now: datetime, ttl_s: int
) -> MintedCsrfToken:
    """Derive a short-lived token bound to one session subject.

    The subject is covered by the MAC but never written into the token: binding
    does not require disclosure, and the browser keeps this value in memory
    where a copied token is already the attacker's smaller problem.
    """
    if not secret:
        raise AuthConfigurationError("CSRF_SIGNING_SECRET is required")
    if not subject:
        raise AuthConfigurationError("a session subject is required to mint a CSRF token")
    if ttl_s <= 0 or ttl_s > _CSRF_MAX_TTL_S:
        raise AuthConfigurationError("CSRF_TOKEN_TTL_S is outside the supported range")
    expires_at = now + timedelta(seconds=ttl_s)
    stamp = int(expires_at.timestamp())
    signature = _csrf_signature(secret, subject=subject, expires_at=stamp)
    return MintedCsrfToken(
        token=f"{CSRF_TOKEN_VERSION}.{stamp}.{signature}", expires_at=expires_at
    )


def verify_csrf_token(secret: str, token: object, *, subject: str, now: datetime) -> None:
    """Verify a token for this session, or raise :class:`CsrfTokenRejected`.

    Every failure — absent, wrong version, unparseable expiry, elapsed, forged
    MAC, or minted for another subject — raises the same exception with the same
    message. A caller learns only that the token was not acceptable.
    """
    if not secret:
        # A revision with no secret must refuse rather than accept everything.
        raise CsrfTokenRejected("a CSRF token is required")
    if not isinstance(token, str):
        raise CsrfTokenRejected("a CSRF token is required")
    candidate = token.strip()
    if not candidate or len(candidate) > _CSRF_MAX_TOKEN_LENGTH:
        raise CsrfTokenRejected("a CSRF token is required")
    parts = candidate.split(".")
    if len(parts) != 3:
        raise CsrfTokenRejected("the CSRF token is not acceptable")
    version, raw_expiry, signature = parts
    if version != CSRF_TOKEN_VERSION or not signature:
        raise CsrfTokenRejected("the CSRF token is not acceptable")
    if not raw_expiry.isascii() or not raw_expiry.isdecimal() or len(raw_expiry) > 20:
        raise CsrfTokenRejected("the CSRF token is not acceptable")
    expires_at = int(raw_expiry)
    expected = _csrf_signature(secret, subject=subject, expires_at=expires_at)
    if not hmac.compare_digest(signature, expected):
        raise CsrfTokenRejected("the CSRF token is not acceptable")
    # Expiry is compared only after the MAC, so a forged token cannot be
    # distinguished from an expired one by the error it produces.
    if now.timestamp() >= expires_at:
        raise CsrfTokenRejected("the CSRF token is not acceptable")


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def validate_idempotency_key(value: object) -> str:
    """Return a bounded, ledger-safe idempotency key, or raise."""
    if not isinstance(value, str):
        raise IdempotencyKeyRejected("an Idempotency-Key header is required")
    candidate = value.strip()
    if not candidate:
        raise IdempotencyKeyRejected("an Idempotency-Key header is required")
    if _IDEMPOTENCY_PATTERN.match(candidate) is None:
        raise IdempotencyKeyRejected(
            "Idempotency-Key must be 8-128 characters of letters, digits, '.', "
            "'_', ':' or '-'"
        )
    return candidate


# ---------------------------------------------------------------------------
# The unsafe-request policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnsafeRequestPolicy:
    """Everything the guard needs, resolved once at startup.

    ``csrf_secret`` is excluded from ``repr`` so a policy object in a log record
    or a traceback frame cannot leak it.
    """

    console_origin: str
    csrf_secret: str = field(repr=False)
    csrf_ttl_s: int
    agent_routes: frozenset[str] = frozenset()


def policy_from_settings(settings: TicketConsoleSettings) -> UnsafeRequestPolicy:
    """Build the policy, refusing an origin that could never match a header."""
    origin = validate_console_origin(
        settings.CONSOLE_ORIGIN, environment=(settings.ENVIRONMENT or "").strip()
    )
    secret = settings.CSRF_SIGNING_SECRET
    raw_secret = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    return UnsafeRequestPolicy(
        console_origin=origin,
        csrf_secret=(raw_secret or "").strip(),
        csrf_ttl_s=int(settings.CSRF_TOKEN_TTL_S),
        # Stage 5 owns no agent route. Stage 8 passes its own allowlist rather
        # than widening this default.
        agent_routes=frozenset(),
    )


def _normalized_origin(value: str) -> str:
    """Normalize an ``Origin`` for comparison — case only, never structure.

    A serialized origin is ``scheme://host[:port]`` with no trailing slash, so a
    header that carries one did not come from a browser's origin serializer.
    Stripping it here would widen the comparison for no legitimate caller, which
    is why only case (insensitive for scheme and host by definition) is folded.
    The configured side is already canonical: ``validate_console_origin``
    rebuilds it from its parsed scheme and netloc.
    """
    return value.strip().lower()


def _media_type(raw: object) -> tuple[str, dict[str, str]]:
    if not isinstance(raw, str) or not raw.strip():
        return "", {}
    head, _, tail = raw.partition(";")
    parameters: dict[str, str] = {}
    for chunk in tail.split(";"):
        name, _, value = chunk.partition("=")
        if name.strip():
            parameters[name.strip().lower()] = value.strip().strip('"').lower()
    return head.strip().lower(), parameters


def agent_exception_applies(
    policy: UnsafeRequestPolicy,
    *,
    path: str,
    reviewer: AuthenticatedReviewer,
    headers: Mapping[str, str],
) -> bool:
    """Whether this request may skip Origin/Fetch-Metadata/CSRF.

    Four independent conditions, all required:

    1. the caller authenticated as the configured service account — ``is_agent``
       is set only by :mod:`api.reviewer_auth` after signed IAP verification, so
       a self-declared ``role: agent`` cannot reach it;
    2. the route is explicitly allowlisted;
    3. no cookies are present, because cookies mean a browser and a browser must
       prove same-origin intent;
    4. any ``Origin`` that *is* present still has to be the console's own, so a
       cross-site page cannot borrow the exemption by omitting Fetch Metadata.
    """
    if not (reviewer.is_agent and reviewer.role is ReviewerRole.AGENT):
        return False
    if path not in policy.agent_routes:
        return False
    lookup = _headers_view(headers)
    if (lookup.get(COOKIE_HEADER) or "").strip():
        return False
    return True


def assert_unsafe_request_allowed(
    policy: UnsafeRequestPolicy,
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    reviewer: AuthenticatedReviewer,
    now: datetime,
    expected_content_type: str = JSON_CONTENT_TYPE,
) -> str:
    """Run the whole unsafe-method matrix and return the idempotency key.

    Check order is deliberate and pinned by tests: the cross-site verdict comes
    first because it is decisive and cheap, and because reporting a CSRF failure
    to a foreign origin would confirm that its ``Origin`` was acceptable.
    """
    if method.upper() in SAFE_METHODS:  # pragma: no cover - callers pre-filter
        return ""

    lookup = _headers_view(headers)
    exempt = agent_exception_applies(
        policy, path=path, reviewer=reviewer, headers=headers
    )

    origin = (lookup.get(ORIGIN_HEADER) or "").strip()
    if exempt:
        # An agent normally sends no Origin at all. If it sends one anyway, it
        # must still be ours.
        if origin and _normalized_origin(origin) != _normalized_origin(
            policy.console_origin
        ):
            raise OriginRejected("this request did not come from the console")
    else:
        if not policy.console_origin:
            raise OriginRejected("the console origin is not configured")
        if not origin or _normalized_origin(origin) != _normalized_origin(
            policy.console_origin
        ):
            raise OriginRejected("this request did not come from the console")

        site = (lookup.get(FETCH_SITE_HEADER) or "").strip().lower()
        if site != SAME_ORIGIN:
            raise FetchMetadataRejected("this request did not come from the console")

    media_type, parameters = _media_type(lookup.get(CONTENT_TYPE_HEADER))
    if media_type != expected_content_type:
        raise ContentTypeRejected(f"this route requires {expected_content_type}")
    charset = parameters.get("charset")
    if charset is not None and charset not in {"utf-8", "utf8"}:
        raise ContentTypeRejected(f"this route requires {expected_content_type}; UTF-8 only")

    if not exempt:
        verify_csrf_token(
            policy.csrf_secret,
            lookup.get(CSRF_HEADER),
            subject=reviewer.identity.subject,
            now=now,
        )

    return validate_idempotency_key(lookup.get(IDEMPOTENCY_HEADER))


def _headers_view(headers: Mapping[str, str]) -> "_CaseInsensitiveHeaders":
    if isinstance(headers, _CaseInsensitiveHeaders):
        return headers
    return _CaseInsensitiveHeaders(headers)


class _CaseInsensitiveHeaders:
    """Case-insensitive read-only view, so a plain dict behaves like Headers."""

    __slots__ = ("_items",)

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._items = {
            str(key).lower(): value
            for key, value in (
                headers.items() if hasattr(headers, "items") else headers or {}
            )
        }

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self._items.get(name.lower(), default)


# ---------------------------------------------------------------------------
# The staging verification handoff
# ---------------------------------------------------------------------------


def handoff_enabled(
    settings: TicketConsoleSettings, *, client_host: Optional[str] = None
) -> bool:
    """Whether the synthetic-verification handoff may be issued at all.

    Production is excluded here as well as at startup. Two independent refusals
    for the same rule is intentional: a misconfigured revision must not be one
    forgotten validation call away from minting these in production.
    """
    if not settings.ENABLE_SYNTHETIC_VERIFICATION:
        return False
    environment = (settings.ENVIRONMENT or "").strip()
    if environment == "production":
        return False
    if environment == "staging":
        return True
    if environment == "local":
        return (client_host or "").strip().lower() in LOOPBACK_HOSTS
    return False


def validate_verification_settings(settings: TicketConsoleSettings) -> bool:
    """Refuse a revision that enables the handoff where it must not exist."""
    environment = (settings.ENVIRONMENT or "").strip()
    if settings.ENABLE_SYNTHETIC_VERIFICATION and environment == "production":
        raise AuthConfigurationError(
            "ENABLE_SYNTHETIC_VERIFICATION must be false in production"
        )
    if settings.ENABLE_SYNTHETIC_VERIFICATION and environment not in (
        STRICT_ENVIRONMENTS | {"local"}
    ):
        raise AuthConfigurationError(
            "ENABLE_SYNTHETIC_VERIFICATION requires a known environment"
        )
    return True


def derive_handoff_key(cursor_key: bytes) -> bytes:
    """Derive the handoff subkey from the cursor key by HKDF-SHA256.

    Domain separation is the whole security argument for reusing one configured
    secret: a sealed page cursor and a sealed handoff are encrypted under
    different keys, so neither can be opened as the other even though both use
    the same AEAD and the same code path.
    """
    if not isinstance(cursor_key, bytes) or len(cursor_key) != CURSOR_AEAD_KEY_BYTES:
        raise ValueError(f"cursor key must be exactly {CURSOR_AEAD_KEY_BYTES} bytes")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=CURSOR_AEAD_KEY_BYTES,
        salt=None,
        info=HANDOFF_HKDF_INFO,
    ).derive(cursor_key)


def handoff_key_from_settings(settings: TicketConsoleSettings) -> bytes:
    """Derive the handoff subkey straight from validated settings."""
    return derive_handoff_key(decode_cursor_aead_key(settings.CURSOR_AEAD_KEY))


def next_verification_phase(phase: str) -> Optional[str]:
    """The phase that legitimately follows ``phase``, or ``None`` at the end."""
    try:
        index = VERIFICATION_PHASES.index(phase)
    except ValueError as exc:
        raise VerificationHandoffError("that verification phase does not exist") from exc
    if index + 1 >= len(VERIFICATION_PHASES):
        return None
    return VERIFICATION_PHASES[index + 1]


def _validated_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise VerificationHandoffError("a verification run id is required")
    candidate = run_id.strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError, TypeError) as exc:
        raise VerificationHandoffError("the verification run id must be a UUID") from exc
    if str(parsed) != candidate.lower():
        raise VerificationHandoffError("the verification run id must be a canonical UUID")
    return candidate.lower()


def _validated_phase(phase: object) -> str:
    if not isinstance(phase, str) or phase not in VERIFICATION_PHASES:
        raise VerificationHandoffError("that verification phase does not exist")
    return phase


def _validated_resources(resources: object) -> dict[str, int]:
    """Bound the server-created resource ids/versions a handoff may carry.

    Ids and integer versions only. A caller-supplied blob here would be a way to
    smuggle ticket text or an email address into a token that crosses phases.
    """
    if resources is None:
        return {}
    if not isinstance(resources, Mapping):
        raise VerificationHandoffError("handoff resources must be an id-to-version map")
    if len(resources) > HANDOFF_MAX_RESOURCES:
        raise VerificationHandoffError("too many handoff resources")
    bounded: dict[str, int] = {}
    for key, value in resources.items():
        if not isinstance(key, str) or not key.strip():
            raise VerificationHandoffError("a handoff resource id must be a string")
        if len(key) > HANDOFF_MAX_RESOURCE_ID_LENGTH:
            raise VerificationHandoffError("a handoff resource id is too long")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VerificationHandoffError("a handoff resource version must be an integer")
        bounded[key] = int(value)
    return bounded


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_verification_handoff(
    key: bytes,
    *,
    environment: str,
    run_id: str,
    phase: str,
    next_role: ReviewerRole,
    prior_token: Optional[str] = None,
    resources: Optional[Mapping[str, int]] = None,
    now: datetime,
    ttl_s: int = HANDOFF_MAX_TTL_S,
) -> str:
    """Seal an ordering token for the next phase of a verification run.

    Binds the environment, the run, the digest of the token that authorized the
    previous phase, the exact next phase and role, and the server-created
    resource versions. It deliberately carries no email, no ticket, comment,
    conversation or CSV content, no secret, and no bearer material.
    """
    validated_phase = _validated_phase(phase)
    if not isinstance(next_role, ReviewerRole) or next_role not in ROLE_LADDER:
        raise VerificationHandoffError(
            "a handoff must name a human role on the privilege ladder"
        )
    if ttl_s <= 0 or ttl_s > HANDOFF_MAX_TTL_S:
        raise VerificationHandoffError("the handoff ttl is outside the supported range")
    if not isinstance(environment, str) or not environment.strip():
        raise VerificationHandoffError("a handoff must name its environment")

    prior_digest: Optional[str] = None
    if prior_token is not None:
        if not isinstance(prior_token, str) or not prior_token.strip():
            raise VerificationHandoffError("the prior handoff token is not usable")
        prior_digest = _token_digest(prior_token)
        # A phase cannot be its own predecessor: that is exactly the replay a
        # prior-token binding exists to stop.
        prior_payload = _peek_handoff(key, prior_token, now=now)
        if prior_payload is not None and prior_payload.get("p") == validated_phase:
            raise VerificationHandoffError(
                "a handoff cannot chain a phase to itself"
            )

    payload: dict[str, Any] = {
        "e": environment.strip(),
        "r": _validated_run_id(run_id),
        "p": validated_phase,
        "n": next_role.value,
        "d": prior_digest,
        "s": _validated_resources(resources),
    }
    return seal_cursor(key, payload, context=HANDOFF_CONTEXT, ttl_s=ttl_s, now=now)


def _peek_handoff(
    key: bytes, token: str, *, now: datetime
) -> Optional[Mapping[str, Any]]:
    """Open a token without binding checks, for chain validation only."""
    try:
        return open_cursor(key, token, context=HANDOFF_CONTEXT, now=now)
    except CursorError:
        return None


def open_verification_handoff(
    key: bytes,
    token: object,
    *,
    environment: str,
    run_id: str,
    expected_phase: str,
    prior_token: Optional[str] = None,
    now: datetime,
) -> dict[str, Any]:
    """Authenticate a handoff and prove it is the one this phase expects.

    Raises :class:`VerificationHandoffError` for a tampered, expired,
    wrong-environment, wrong-run, reordered, or wrongly-chained token. The
    returned payload is metadata for the caller's own state checks; it is never
    a substitute for them.
    """
    if not isinstance(token, str) or not token.strip():
        raise VerificationHandoffError("a verification handoff is required")
    try:
        payload = open_cursor(key, token.strip(), context=HANDOFF_CONTEXT, now=now)
    except CursorError as exc:
        raise VerificationHandoffError("the verification handoff is not acceptable") from exc

    if payload.get("e") != (environment or "").strip():
        raise VerificationHandoffError("the verification handoff is not acceptable")
    if payload.get("r") != _validated_run_id(run_id):
        raise VerificationHandoffError("the verification handoff is not acceptable")
    if payload.get("p") != _validated_phase(expected_phase):
        raise VerificationHandoffError("the verification handoff is out of order")

    expected_digest = payload.get("d")
    supplied_digest = (
        _token_digest(prior_token)
        if isinstance(prior_token, str) and prior_token.strip()
        else None
    )
    if expected_digest is None:
        if supplied_digest is not None:
            raise VerificationHandoffError("this phase accepts no prior handoff")
    else:
        if supplied_digest is None:
            raise VerificationHandoffError("this phase requires the prior handoff")
        if not hmac.compare_digest(str(expected_digest), supplied_digest):
            raise VerificationHandoffError("the prior handoff does not match")

    return {
        "environment": payload.get("e"),
        "run_id": payload.get("r"),
        "phase": payload.get("p"),
        "next_role": payload.get("n"),
        "resources": payload.get("s") or {},
    }


__all__ = [
    "CONTENT_TYPE_HEADER",
    "COOKIE_HEADER",
    "CSRF_HEADER",
    "CSRF_TOKEN_VERSION",
    "CSV_CONTENT_TYPE",
    "CURSOR_HEADER",
    "FETCH_MODE_HEADER",
    "FETCH_SITE_HEADER",
    "HANDOFF_CONTEXT",
    "HANDOFF_HKDF_INFO",
    "HANDOFF_MAX_RESOURCES",
    "HANDOFF_MAX_TTL_S",
    "IDEMPOTENCY_HEADER",
    "JSON_CONTENT_TYPE",
    "ORIGIN_HEADER",
    "SAFE_METHODS",
    "SAME_ORIGIN",
    "VERIFICATION_HANDOFF_HEADER",
    "VERIFICATION_PHASES",
    "VERIFICATION_PHASE_HEADER",
    "VERIFICATION_RUN_HEADER",
    "ContentTypeRejected",
    "CsrfTokenRejected",
    "FetchMetadataRejected",
    "IdempotencyKeyRejected",
    "MintedCsrfToken",
    "OriginRejected",
    "UnsafeRequestPolicy",
    "UnsafeRequestRejected",
    "VerificationHandoffError",
    "agent_exception_applies",
    "assert_unsafe_request_allowed",
    "derive_handoff_key",
    "handoff_enabled",
    "handoff_key_from_settings",
    "mint_csrf_token",
    "mint_verification_handoff",
    "next_verification_phase",
    "open_verification_handoff",
    "policy_from_settings",
    "validate_idempotency_key",
    "validate_verification_settings",
    "verify_csrf_token",
]
