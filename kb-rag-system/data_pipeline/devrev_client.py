"""Resilient, read-only DevRev adapter for the /tickets review console.

Scope (MVP, frozen by the master plan):

* ``POST /works.list`` with ``type=["ticket"]``, allowlisted structured
  filters, and forward/backward opaque cursors;
* ``GET /works.get`` for one scoped ticket;
* ``GET /timeline-entries.list`` as bounded, forward-only (``mode=after``)
  cursor pages.

Nothing here creates, updates, or deletes a DevRev object, and no owner/tag
display-name lookup endpoint exists: those remain feature-gated until
``/dev-users.list`` and ``/tags.list`` scopes are separately approved.

Design rules this module is required to hold, and where each one lives:

``One shared client``
    :class:`DevRevClient` owns exactly one :class:`httpx.AsyncClient`. It
    carries the base URL, the bearer header, ``Accept``, the pinned
    ``X-Devrev-Version``, all four explicit timeouts, and
    ``follow_redirects=False``.

``Credentials never travel``
    Redirects are refused outright rather than followed, so a 3xx can never
    replay the bearer token against another origin. The token is held in a
    :class:`~pydantic.SecretStr`, excluded from ``__repr__``, and never
    interpolated into an exception or a log record.

``Bounded bodies``
    Success bodies are streamed. An oversized declared ``Content-Length`` is
    refused before a single byte is read; decoded streamed bytes (so
    decompression expansion counts) are tallied against the same cap and abort
    on overflow. JSON is parsed only from that bounded buffer -- the
    convenience decoder on the response object is never used.

``Bounded errors``
    An error body is read to at most 4 KiB purely so its size can be recorded.
    Its content is never retained, logged, or placed in an exception message.

``Fail-closed scope``
    Every list request injects the configured ``applies_to_part`` DONs and
    ticket-visibility IDs and can only be narrowed, never cleared or widened.
    Timeline requests carry the separate timeline-visibility enum allowlist.
    A directly fetched ticket is re-checked against the same scope, so a known
    ID cannot bypass the list filters.

``Honest pagination``
    Cursors are opaque strings: never parsed, never synthesized. A short or
    even empty page is not terminal while ``next_cursor`` exists. Iteration
    stops only when ``next_cursor`` is absent, a repeated cursor is detected,
    or a configured guard trips -- and a guarded result is always reported as
    ``partial``/``truncated``, never as complete.

Two page-level flags have distinct, deliberate meanings:

``truncated``
    A bound cut data short: a page/entry guard tripped, a string was clipped
    to its canonical maximum, or a list was capped.

``partial``
    A record could not be represented at all and was excluded: it fell outside
    the configured scope, belonged to another object, or carried no usable
    timestamp.

Warnings are fixed snake_case tokens, never interpolated remote text, so a
warning can be surfaced in an API response or a log without leaking ticket
content. Counts live in :class:`DevRevCallDiagnostics` instead.

Classification of a timeline author as participant, human agent, or AI is
deliberately *not* done here. The raw DevRev actor id/type is preserved for the
Stage 4 service layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Literal, Optional, Union
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from api.ticket_review_models import (
    DEFAULT_PAGE_SIZE,
    DEVREV_CONNECT_TIMEOUT_S,
    DEVREV_MAX_ENTRIES,
    DEVREV_MAX_PAGES,
    DEVREV_MAX_RESPONSE_BYTES,
    DEVREV_MAX_RETRIES,
    DEVREV_READ_TIMEOUT_S,
    DEVREV_RETRY_AFTER_CAP_S,
    MAX_ATTACHMENTS,
    MAX_CURSOR_LENGTH,
    MAX_DISPLAY_ID_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_ID_LENGTH,
    MAX_LIST_ITEMS,
    MAX_MESSAGE_BODY_LENGTH,
    MAX_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    MAX_TOPIC_LENGTH,
    MAX_UPSTREAM_ERROR_BODY_BYTES,
    MAX_WARNINGS,
    CursorPage,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketFilters,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
)
from api.tickets_console_config import (
    DEVREV_OFFICIAL_API_BASE,
    DEVREV_PINNED_VERSION,
    TicketConsoleSettings,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ENDPOINT_TIMELINE_LIST",
    "ENDPOINT_WORKS_GET",
    "ENDPOINT_WORKS_LIST",
    "IDEMPOTENT_ENDPOINTS",
    "LIST_MODES",
    "NON_RETRYABLE_STATUS_CODES",
    "RETRYABLE_STATUS_CODES",
    "TIMELINE_LIST_MODE",
    "WORKS_LIST_ALLOWED_TICKET_KEYS",
    "WORKS_LIST_ALLOWED_TOP_LEVEL_KEYS",
    "DevRevAuthenticationError",
    "DevRevCallDiagnostics",
    "DevRevClient",
    "DevRevConfigurationError",
    "DevRevConflictError",
    "DevRevError",
    "DevRevNotFoundError",
    "DevRevPaginationError",
    "DevRevPermissionError",
    "DevRevProtocolError",
    "DevRevRateLimitError",
    "DevRevRequestError",
    "DevRevResourceLimitError",
    "DevRevScopeError",
    "DevRevTimelineHydration",
    "DevRevTransientError",
    "cursor_digest",
    "sort_timeline_entries",
]

# =====================================================================
# Wire constants
# =====================================================================

ENDPOINT_WORKS_LIST = "works.list"
ENDPOINT_WORKS_GET = "works.get"
ENDPOINT_TIMELINE_LIST = "timeline-entries.list"

_PATH_WORKS_LIST = "/works.list"
_PATH_WORKS_GET = "/works.get"
_PATH_TIMELINE_LIST = "/timeline-entries.list"

#: Every MVP operation is a read, so a transport failure may be retried on all
#: of them. The set is explicit rather than implied so a future non-read cannot
#: silently inherit read retry semantics.
IDEMPOTENT_ENDPOINTS = frozenset(
    {ENDPOINT_WORKS_LIST, ENDPOINT_WORKS_GET, ENDPOINT_TIMELINE_LIST}
)

#: DevRev documents 429 (rate limit), 500, and 503 as retry-after-a-delay.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})
#: Terminal client errors. Retrying any of these only burns rate-limit budget.
NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 409})

#: ``works.list`` iterates in either direction; the timeline is forward-only.
LIST_MODES = frozenset({"after", "before"})
TIMELINE_LIST_MODE = "after"

WORKS_LIST_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "type",
        "stage",
        "state",
        "applies_to_part",
        "owned_by",
        "created_by",
        "reported_by",
        "tags",
        "created_date",
        "modified_date",
        "ticket",
        "cursor",
        "mode",
        "limit",
    }
)
WORKS_LIST_ALLOWED_TICKET_KEYS = frozenset({"source_channel", "subtype", "visibility"})

_WORK_TYPE_TICKET = "ticket"

# DevRev's documented rate-limit headers (https://developer.devrev.ai/about/rate-limits).
_RATE_LIMIT_HEADER = "x-ratelimit-limit"
_RATE_REMAINING_HEADER = "x-ratelimit-remaining"
_RATE_RESET_HEADER = "x-ratelimit-reset"
_RETRY_AFTER_HEADER = "retry-after"
#: Checked in order; the first present, non-empty value wins.
_REQUEST_ID_HEADERS = ("x-devrev-request-id", "x-request-id", "request-id")
_MAX_REQUEST_ID_LENGTH = 200

_CURSOR_DIGEST_LENGTH = 16

# A DON is DevRev's opaque namespaced identifier. It is validated for shape and
# length only -- never parsed for meaning.
_DON_PATTERN = re.compile(r"^don:[A-Za-z0-9_.:/+-]{1,}\Z")
_DISPLAY_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,20}\Z")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

# Remote timeline entry types this adapter models. Anything else is preserved
# as a bounded UNSUPPORTED entry rather than crashing or being mistaken for a
# participant reply.
_COMMENT_TYPES = frozenset({"timeline_comment"})
_CHANGE_EVENT_TYPES = frozenset({"timeline_change_event"})

# =====================================================================
# Warning tokens
# =====================================================================
#
# Fixed vocabulary on purpose: a warning may cross into an API response, a log
# line, or a UI badge, so it must never carry remote text. Numeric detail
# belongs in DevRevCallDiagnostics.

WARNING_TITLE_TRUNCATED = "title_truncated"
WARNING_BODY_TRUNCATED = "body_truncated"
WARNING_OWNERS_TRUNCATED = "owner_ids_truncated"
WARNING_TAGS_TRUNCATED = "tag_ids_truncated"
WARNING_ATTACHMENTS_TRUNCATED = "attachments_truncated"
WARNING_CHANGE_SUMMARY_TRUNCATED = "change_summary_truncated"
WARNING_OUT_OF_SCOPE_ROWS = "out_of_scope_rows_dropped"
WARNING_OUT_OF_SCOPE_ENTRIES = "out_of_scope_entries_dropped"
WARNING_UNIDENTIFIED_ROWS = "unidentified_rows_dropped"
WARNING_UNDATED_ENTRIES = "undated_entries_dropped"
WARNING_CREATED_DATE_FALLBACK = "created_date_fallback_used"
WARNING_DUPLICATE_ENTRIES = "duplicate_entries_skipped"
WARNING_MAX_PAGES_REACHED = "max_pages_guard_reached"
WARNING_MAX_ENTRIES_REACHED = "max_entries_guard_reached"
WARNING_REMOTE_OVER_LIMIT = "remote_returned_more_items_than_requested"
WARNING_MALFORMED_SORT_TIMESTAMP = "malformed_sort_timestamp_ordered_last"


# =====================================================================
# Typed errors
# =====================================================================


class DevRevError(Exception):
    """Base class for every DevRev adapter failure.

    The message is always safe to surface: it names the endpoint and status and
    nothing else. The upstream body is never retained -- only how many bytes of
    it were read, and whether that read hit the 4 KiB cap.
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: Optional[str] = None,
        status: Optional[int] = None,
        attempts: Optional[int] = None,
        request_id: Optional[str] = None,
        retry_after_s: Optional[float] = None,
        upstream_error_bytes: int = 0,
        upstream_error_truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.attempts = attempts
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.upstream_error_bytes = upstream_error_bytes
        self.upstream_error_truncated = upstream_error_truncated
        #: True when an upstream body existed and was deliberately discarded.
        self.upstream_error_redacted = upstream_error_bytes > 0


class DevRevAuthenticationError(DevRevError):
    """401: the configured bearer value was missing, expired, or rejected."""


class DevRevPermissionError(DevRevError):
    """403: the identity behind the token is not authorized for this object."""


class DevRevNotFoundError(DevRevError):
    """404: the endpoint or object does not exist."""


class DevRevConflictError(DevRevError):
    """409: the request conflicts with the current remote state."""


class DevRevRateLimitError(DevRevError):
    """429: the rate-limit window is exhausted and retries were used up."""


class DevRevTransientError(DevRevError):
    """A retryable failure (500/503 or a transport error) that never settled."""


class DevRevProtocolError(DevRevError):
    """The response violated the expected contract.

    Covers a refused redirect, an unexpected status, a non-JSON or non-object
    body, a missing required key, and a remote cursor outside its bound.
    """


class DevRevPaginationError(DevRevError):
    """Pagination could not make progress; a repeated cursor was detected."""


class DevRevResourceLimitError(DevRevError):
    """A canonical size or count bound was exceeded.

    Raised for an oversized response body and, under ``strict=True``, when an
    iterator guard trips.
    """


class DevRevScopeError(DevRevPermissionError):
    """The object falls outside the configured DevRev scope.

    A subclass of :class:`DevRevPermissionError` so existing permission
    handling keeps working, but distinguishable because the denial is *ours*,
    decided against configuration, not DevRev's.
    """


class DevRevRequestError(DevRevError):
    """The caller's request is invalid; nothing was sent.

    Raised for an unknown/widened filter, a malformed identifier, an
    out-of-bound cursor or limit, an unknown ``mode``, and use of a closed
    client.
    """


class DevRevConfigurationError(DevRevError):
    """The adapter cannot be constructed safely from this configuration."""


# =====================================================================
# Diagnostics
# =====================================================================


@dataclass(frozen=True, slots=True)
class DevRevCallDiagnostics:
    """A bounded, content-free snapshot of one logical call.

    Safe to emit as structured-log fields. It holds identifiers, counts, and
    rate-limit state -- never a cursor value, a request body, a header value
    other than the documented rate-limit/request-id ones, or ticket text.

    ``attempts`` is the worst single-request attempt count within the call, so a
    multi-page walk that never retried still reports ``1``.
    """

    endpoint: str
    status: Optional[int] = None
    attempts: int = 0
    pages: int = 0
    items: int = 0
    rate_limit_limit: Optional[int] = None
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None
    retry_after_s: Optional[float] = None
    request_id: Optional[str] = None
    duplicate_entries: int = 0
    dropped_out_of_scope: int = 0
    dropped_unidentified: int = 0
    dropped_undated: int = 0
    truncated: bool = False
    partial: bool = False
    cursor_digest: Optional[str] = None

    def as_log_fields(self) -> dict[str, Any]:
        """Return the structured-log projection documented by the plan."""
        fields: dict[str, Any] = {
            "endpoint": self.endpoint,
            "status": self.status,
            "attempts": self.attempts,
            "pages": self.pages,
            "items": self.items,
            "rate_limit_remaining": self.rate_limit_remaining,
            "request_id": self.request_id,
        }
        # Optional detail is emitted only when it says something, so a healthy
        # INFO line stays small.
        for name, value in (
            ("rate_limit_limit", self.rate_limit_limit),
            ("rate_limit_reset", self.rate_limit_reset),
            ("retry_after_s", self.retry_after_s),
            ("cursor_digest", self.cursor_digest),
        ):
            if value is not None:
                fields[name] = value
        for name, count in (
            ("duplicate_entries", self.duplicate_entries),
            ("dropped_out_of_scope", self.dropped_out_of_scope),
            ("dropped_unidentified", self.dropped_unidentified),
            ("dropped_undated", self.dropped_undated),
        ):
            if count:
                fields[name] = count
        if self.truncated:
            fields["truncated"] = True
        if self.partial:
            fields["partial"] = True
        return fields


@dataclass(frozen=True, slots=True)
class DevRevTimelineHydration:
    """An explicitly loaded, bounded set of timeline entries.

    Deliberately not named or flagged "complete": ``truncated``/``partial`` are
    the only honest statements available once a guard exists.
    """

    entries: tuple[DevRevTimelineEntry, ...]
    pages: int
    truncated: bool
    partial: bool
    warnings: tuple[str, ...]
    diagnostics: DevRevCallDiagnostics


class _Accumulator:
    """Mutable per-call scratch space, frozen into diagnostics on demand."""

    __slots__ = (
        "endpoint",
        "status",
        "attempts",
        "pages",
        "items",
        "rate_limit_limit",
        "rate_limit_remaining",
        "rate_limit_reset",
        "retry_after_s",
        "request_id",
        "duplicate_entries",
        "dropped_out_of_scope",
        "dropped_unidentified",
        "dropped_undated",
        "truncated",
        "partial",
        "cursor_digest",
        "_warnings",
    )

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.status: Optional[int] = None
        self.attempts = 0
        self.pages = 0
        self.items = 0
        self.rate_limit_limit: Optional[int] = None
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        self.retry_after_s: Optional[float] = None
        self.request_id: Optional[str] = None
        self.duplicate_entries = 0
        self.dropped_out_of_scope = 0
        self.dropped_unidentified = 0
        self.dropped_undated = 0
        self.truncated = False
        self.partial = False
        self.cursor_digest: Optional[str] = None
        self._warnings: list[str] = []

    def warn(self, token: str) -> None:
        """Record a fixed warning token once, within the bounded list."""
        if token in self._warnings or len(self._warnings) >= MAX_WARNINGS:
            return
        self._warnings.append(token)

    def extend_warnings(self, tokens: Iterable[str]) -> None:
        for token in tokens:
            self.warn(token)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def absorb(self, meta: "_ResponseMeta") -> None:
        """Fold one response's metadata in, keeping the latest known values."""
        self.status = meta.status
        self.attempts = max(self.attempts, meta.attempts)
        for name in ("rate_limit_limit", "rate_limit_remaining", "rate_limit_reset", "request_id"):
            value = getattr(meta, name)
            if value is not None:
                setattr(self, name, value)
        if meta.retry_after_s is not None:
            self.retry_after_s = meta.retry_after_s

    def freeze(self) -> DevRevCallDiagnostics:
        return DevRevCallDiagnostics(
            endpoint=self.endpoint,
            status=self.status,
            attempts=self.attempts,
            pages=self.pages,
            items=self.items,
            rate_limit_limit=self.rate_limit_limit,
            rate_limit_remaining=self.rate_limit_remaining,
            rate_limit_reset=self.rate_limit_reset,
            retry_after_s=self.retry_after_s,
            request_id=self.request_id,
            duplicate_entries=self.duplicate_entries,
            dropped_out_of_scope=self.dropped_out_of_scope,
            dropped_unidentified=self.dropped_unidentified,
            dropped_undated=self.dropped_undated,
            truncated=self.truncated,
            partial=self.partial,
            cursor_digest=self.cursor_digest,
        )


@dataclass(frozen=True, slots=True)
class _ResponseMeta:
    """Safe metadata extracted from one HTTP response."""

    endpoint: str
    status: int
    attempts: int
    rate_limit_limit: Optional[int] = None
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None
    retry_after_s: Optional[float] = None
    request_id: Optional[str] = None


# =====================================================================
# Small pure helpers
# =====================================================================


def cursor_digest(cursor: Optional[str]) -> Optional[str]:
    """Return a short one-way digest of an opaque cursor, or ``None``.

    Cursors may encode server-side query state, so they never reach a log. A
    digest is enough to correlate "the same page was fetched twice".
    """
    if not cursor:
        return None
    return hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:_CURSOR_DIGEST_LENGTH]


def _clip(value: Optional[str], limit: int) -> tuple[Optional[str], bool]:
    """Bound a string to ``limit`` characters, reporting whether it was cut."""
    if value is None:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _text(raw: Any) -> Optional[str]:
    """Return a non-empty string, or ``None`` for anything else."""
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped or None
    return None


def _scalar_name(raw: Any) -> Optional[str]:
    """Read a DevRev field that may be a bare string or a named object.

    ``stage``/``state`` arrive either as ``"open"`` or as
    ``{"name": ..., "display_name": ...}`` depending on endpoint and version.
    """
    if isinstance(raw, str):
        return _text(raw)
    if isinstance(raw, Mapping):
        for key in ("name", "display_name"):
            value = _text(raw.get(key))
            if value is not None:
                return value
    return None


def _object_id(raw: Any) -> Optional[str]:
    """Read an identifier that may be a bare DON or an object carrying one."""
    if isinstance(raw, str):
        return _text(raw)
    if isinstance(raw, Mapping):
        return _text(raw.get("id"))
    return None


def _strict_int(raw: Any) -> Optional[int]:
    """Accept only a real integer. ``bool`` and numeric strings are rejected."""
    # `bool` is a subclass of `int`, so a JSON `true` would otherwise become a
    # visibility of 1. Checked first and separately so the type narrows.
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, int):
        return None
    return raw


def _parse_datetime(raw: Any) -> Optional[datetime]:
    """Parse a DevRev ISO-8601 timestamp into an aware UTC datetime.

    A naive value is a protocol deviation; DevRev always sends ``Z``. It is
    interpreted as UTC rather than discarded, because the alternative is
    dropping a real record over a missing suffix.
    """
    text = _text(raw)
    if text is None:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_id_list(raw: Any, limit: int) -> tuple[list[str], bool]:
    """Collect at most ``limit`` identifiers from a DevRev reference list."""
    if isinstance(raw, Mapping) or isinstance(raw, str):
        candidates: list[Any] = [raw]
    elif isinstance(raw, Sequence):
        candidates = list(raw)
    else:
        return [], False
    collected: list[str] = []
    for candidate in candidates:
        identifier = _object_id(candidate)
        if identifier is None or len(identifier) > MAX_ID_LENGTH:
            continue
        if identifier in collected:
            continue
        if len(collected) >= limit:
            return collected, True
        collected.append(identifier)
    return collected, False


def _actor(raw: Any) -> Optional[DevRevActor]:
    """Normalize a DevRev user reference, preserving its raw actor type.

    No human/AI/participant judgement happens here: that is Stage 4's job and
    depends on configured author-id allowlists this module never sees.
    """
    actor_id = _object_id(raw)
    if actor_id is None or len(actor_id) > MAX_ID_LENGTH:
        return None
    actor_type = DevRevActorType.UNKNOWN
    display_name: Optional[str] = None
    if isinstance(raw, Mapping):
        candidate = _text(raw.get("type"))
        if candidate is not None:
            try:
                actor_type = DevRevActorType(candidate)
            except ValueError:
                actor_type = DevRevActorType.UNKNOWN
        display_name, _ = _clip(_text(raw.get("display_name")), MAX_DISPLAY_NAME_LENGTH)
    return DevRevActor(actor_id=actor_id, actor_type=actor_type, display_name=display_name)


def sort_timeline_entries(
    entries: Sequence[DevRevTimelineEntry],
) -> tuple[tuple[DevRevTimelineEntry, ...], tuple[str, ...]]:
    """Order an explicitly loaded set chronologically, stably.

    The key is ``(usable_timestamp, created_at, original_position)``, so equal
    timestamps keep DevRev's order. An entry whose ``created_at`` is missing or
    naive -- only reachable when validation was bypassed -- sorts last and
    raises a warning instead of being silently reordered or dropped.

    This is intentionally separate from page adaptation: a single page is always
    returned in the order DevRev produced it.
    """
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    decorated: list[tuple[int, datetime, int, DevRevTimelineEntry]] = []
    malformed = False
    for position, entry in enumerate(entries):
        candidate = getattr(entry, "created_at", None)
        usable: Optional[datetime] = None
        if (
            isinstance(candidate, datetime)
            and candidate.tzinfo is not None
            and candidate.utcoffset() is not None
        ):
            usable = candidate
        else:
            malformed = True
        decorated.append(
            (0 if usable is not None else 1, usable or epoch, position, entry)
        )
    # The leading rank forces every unusable timestamp after every usable one,
    # and `position` keeps DevRev's order for ties in both groups.
    decorated.sort(key=lambda row: (row[0], row[1], row[2]))
    ordered = tuple(row[3] for row in decorated)
    return ordered, ((WARNING_MALFORMED_SORT_TIMESTAMP,) if malformed else ())


# =====================================================================
# Client
# =====================================================================


class DevRevClient:
    """A read-only DevRev adapter over one shared ``httpx.AsyncClient``.

    Construct it from :class:`~api.tickets_console_config.TicketConsoleSettings`
    via :meth:`from_settings` in application code. The bearer value must come
    from server-side configuration; MVP deployment metadata identifies it as a
    PAT belonging to a dedicated read-only DevRev integration user. A
    browser-supplied token is never accepted, and no AAT/SUT/session exchange
    is implemented.
    """

    def __init__(
        self,
        *,
        token: Union[str, SecretStr],
        allowed_part_dons: Sequence[str],
        allowed_ticket_visibility_ids: Sequence[int],
        allowed_timeline_visibilities: Sequence[str],
        base_url: str = DEVREV_OFFICIAL_API_BASE,
        api_version: str = DEVREV_PINNED_VERSION,
        environment: str = "production",
        allow_non_official_base: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEVREV_MAX_PAGES,
        max_entries: int = DEVREV_MAX_ENTRIES,
        max_retries: int = DEVREV_MAX_RETRIES,
        max_response_bytes: int = DEVREV_MAX_RESPONSE_BYTES,
        connect_timeout_s: float = DEVREV_CONNECT_TIMEOUT_S,
        timeout_s: float = DEVREV_READ_TIMEOUT_S,
        retry_after_cap_s: float = DEVREV_RETRY_AFTER_CAP_S,
        backoff_base_s: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        jitter: Optional[Callable[[], float]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._token = self._validated_token(token)
        self._api_version = self._validated_version(api_version)
        self._environment = environment
        self._base_url = self._validated_base_url(
            base_url, environment=environment, allow_non_official_base=allow_non_official_base
        )
        self._allowed_parts = self._validated_parts(allowed_part_dons)
        self._allowed_ticket_visibility = self._validated_visibility_ids(
            allowed_ticket_visibility_ids
        )
        self._allowed_timeline_visibility = self._validated_timeline_visibilities(
            allowed_timeline_visibilities
        )

        self._page_size = self._bounded("page_size", page_size, 1, MAX_PAGE_SIZE)
        self._max_pages = self._bounded("max_pages", max_pages, 1, DEVREV_MAX_PAGES)
        self._max_entries = self._bounded("max_entries", max_entries, 1, DEVREV_MAX_ENTRIES)
        self._max_retries = self._bounded("max_retries", max_retries, 1, DEVREV_MAX_RETRIES)
        self._max_response_bytes = self._bounded(
            "max_response_bytes", max_response_bytes, 1, DEVREV_MAX_RESPONSE_BYTES
        )
        self._retry_after_cap_s = float(
            self._bounded_float("retry_after_cap_s", retry_after_cap_s, DEVREV_RETRY_AFTER_CAP_S)
        )
        self._backoff_base_s = float(
            self._bounded_float("backoff_base_s", backoff_base_s, float("inf"))
        )
        connect = self._bounded_float(
            "connect_timeout_s", connect_timeout_s, DEVREV_CONNECT_TIMEOUT_S
        )
        read = self._bounded_float("timeout_s", timeout_s, DEVREV_READ_TIMEOUT_S)
        # All four are explicit: an unset pool or write timeout is how a
        # well-behaved client still ends up hanging forever.
        self._timeout = httpx.Timeout(connect=connect, read=read, write=read, pool=read)

        # Injectable so retry timing is deterministic under test and the module
        # needs no real clock, real sleep, or real randomness to be verified.
        self._sleep: Callable[[float], Any] = sleep or asyncio.sleep
        self._jitter: Callable[[], float] = (
            jitter or random.random  # noqa: S311 - backoff jitter, not crypto
        )
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._last_diagnostics: Optional[DevRevCallDiagnostics] = None

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=self._timeout,
            # A 3xx must never replay the bearer header against another origin.
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {self._token.get_secret_value()}",
                "Accept": "application/json",
                "X-Devrev-Version": self._api_version,
            },
        )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: TicketConsoleSettings,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        jitter: Optional[Callable[[], float]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> "DevRevClient":
        """Build the adapter from the isolated console configuration."""
        return cls(
            token=settings.DEVREV_TOKEN,
            allowed_part_dons=settings.DEVREV_ALLOWED_PART_DONS,
            allowed_ticket_visibility_ids=settings.DEVREV_ALLOWED_TICKET_VISIBILITY_IDS,
            allowed_timeline_visibilities=settings.DEVREV_ALLOWED_TIMELINE_VISIBILITIES,
            base_url=settings.DEVREV_API_BASE,
            api_version=settings.DEVREV_VERSION,
            environment=settings.ENVIRONMENT,
            allow_non_official_base=settings.ALLOW_NON_OFFICIAL_DEVREV_BASE,
            page_size=settings.DEVREV_PAGE_SIZE,
            max_pages=settings.DEVREV_MAX_PAGES,
            max_entries=settings.MAX_TIMELINE_ENTRIES,
            max_retries=settings.DEVREV_MAX_RETRIES,
            max_response_bytes=settings.DEVREV_MAX_RESPONSE_BYTES,
            connect_timeout_s=settings.DEVREV_CONNECT_TIMEOUT_S,
            timeout_s=settings.DEVREV_TIMEOUT_S,
            transport=transport,
            sleep=sleep,
            jitter=jitter,
            clock=clock,
        )

    @staticmethod
    def _validated_token(token: Union[str, SecretStr]) -> SecretStr:
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(raw, str) or not raw.strip():
            raise DevRevConfigurationError("a server-side DevRev bearer value is required")
        # A CR/LF or space in a header value is request smuggling, not a typo.
        if any(char.isspace() for char in raw) or not raw.isprintable():
            raise DevRevConfigurationError(
                "the DevRev bearer value must be a single printable token"
            )
        return SecretStr(raw)

    @staticmethod
    def _validated_version(api_version: str) -> str:
        if api_version != DEVREV_PINNED_VERSION:
            raise DevRevConfigurationError(
                f"the DevRev API version must stay pinned to {DEVREV_PINNED_VERSION}"
            )
        return api_version

    @staticmethod
    def _validated_base_url(
        base_url: str, *, environment: str, allow_non_official_base: bool
    ) -> str:
        candidate = (base_url or "").strip().rstrip("/")
        if not candidate:
            raise DevRevConfigurationError("a DevRev API base URL is required")
        parts = urlsplit(candidate)
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            raise DevRevConfigurationError("the DevRev API base URL must be an absolute http(s) URL")
        if parts.username or parts.password or "@" in parts.netloc:
            raise DevRevConfigurationError("the DevRev API base URL must not embed credentials")
        if parts.query or parts.fragment:
            raise DevRevConfigurationError(
                "the DevRev API base URL must not carry a query or fragment"
            )
        official = candidate == DEVREV_OFFICIAL_API_BASE
        if not official:
            if environment == "production":
                raise DevRevConfigurationError(
                    "production must use the official DevRev API origin"
                )
            if not allow_non_official_base:
                raise DevRevConfigurationError(
                    "a non-official DevRev API origin requires an explicit override"
                )
        if parts.scheme == "http":
            host = (parts.hostname or "").lower()
            if environment == "production" or host not in _LOOPBACK_HOSTS:
                raise DevRevConfigurationError(
                    "plain http is only acceptable for a loopback fixture server"
                )
        return candidate

    @staticmethod
    def _validated_parts(parts: Sequence[str]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for part in parts or ():
            value = _text(part)
            if value is None or len(value) > MAX_ID_LENGTH:
                continue
            if value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            raise DevRevConfigurationError(
                "DEVREV_ALLOWED_PART_DONS must be a non-empty allowlist; DevRev access fails closed"
            )
        return tuple(cleaned)

    @staticmethod
    def _validated_visibility_ids(ids: Sequence[int]) -> tuple[int, ...]:
        cleaned: list[int] = []
        for candidate in ids or ():
            value = _strict_int(candidate)
            if value is None or value in cleaned:
                continue
            cleaned.append(value)
        if not cleaned:
            raise DevRevConfigurationError(
                "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS must be a non-empty integer allowlist"
            )
        return tuple(cleaned)

    @staticmethod
    def _validated_timeline_visibilities(values: Sequence[str]) -> tuple[TimelineVisibility, ...]:
        cleaned: list[TimelineVisibility] = []
        for candidate in values or ():
            text = _text(candidate)
            if text is None:
                continue
            try:
                visibility = TimelineVisibility(text)
            except ValueError as exc:
                raise DevRevConfigurationError(
                    "DEVREV_ALLOWED_TIMELINE_VISIBILITIES must use the closed visibility enum"
                ) from exc
            if visibility not in cleaned:
                cleaned.append(visibility)
        if not cleaned:
            raise DevRevConfigurationError(
                "DEVREV_ALLOWED_TIMELINE_VISIBILITIES must be a non-empty allowlist"
            )
        return tuple(cleaned)

    @staticmethod
    def _bounded(label: str, value: Any, minimum: int, maximum: int) -> int:
        candidate = _strict_int(value)
        if candidate is None or candidate < minimum or candidate > maximum:
            raise DevRevConfigurationError(
                f"{label} must be an integer within [{minimum}, {maximum}]"
            )
        return candidate

    @staticmethod
    def _bounded_float(label: str, value: Any, maximum: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DevRevConfigurationError(f"{label} must be a positive number")
        candidate = float(value)
        if candidate <= 0 or candidate > maximum:
            raise DevRevConfigurationError(
                f"{label} must be a positive number no greater than {maximum}"
            )
        return candidate

    # ------------------------------------------------------------------
    # Lifecycle and introspection
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        # Deliberately excludes the bearer value and every allowlist entry.
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"api_version={self._api_version!r}, environment={self._environment!r}, "
            f"closed={self.is_closed!r})"
        )

    async def __aenter__(self) -> "DevRevClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the shared transport. Safe to call more than once."""
        await self._client.aclose()

    @property
    def is_closed(self) -> bool:
        return bool(self._client.is_closed)

    @property
    def follow_redirects(self) -> bool:
        return bool(self._client.follow_redirects)

    @property
    def timeout(self) -> httpx.Timeout:
        return self._timeout

    @property
    def last_diagnostics(self) -> Optional[DevRevCallDiagnostics]:
        """Diagnostics for the most recently completed logical call."""
        return self._last_diagnostics

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    async def list_tickets(
        self,
        filters: Union[DevRevTicketFilters, Mapping[str, Any]],
        *,
        cursor: Optional[str] = None,
        mode: Literal["after", "before"] = "after",
        limit: Optional[int] = None,
    ) -> CursorPage[DevRevTicketSummary]:
        """List tickets through ``POST /works.list``.

        ``type`` is always forced to ``["ticket"]`` and the configured part and
        ticket-visibility scope is always applied. Both cursors are preserved so
        the UI can page in either direction.
        """
        self._require_open()
        validated = self._validated_filters(filters)
        effective_limit = self._effective_limit(limit)
        body = self._works_list_body(
            validated, cursor=self._validated_cursor(cursor), mode=mode, limit=effective_limit
        )

        accumulator = _Accumulator(ENDPOINT_WORKS_LIST)
        accumulator.cursor_digest = cursor_digest(cursor)
        payload, meta = await self._request_json(
            "POST", _PATH_WORKS_LIST, endpoint=ENDPOINT_WORKS_LIST, json_body=body, attempts_into=accumulator
        )
        accumulator.pages = 1

        works = payload.get("works")
        if not isinstance(works, list):
            raise DevRevProtocolError(
                "works.list response did not carry a works array",
                endpoint=ENDPOINT_WORKS_LIST,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        if len(works) > MAX_PAGE_SIZE:
            raise DevRevResourceLimitError(
                f"works.list returned more than the canonical maximum of {MAX_PAGE_SIZE} items",
                endpoint=ENDPOINT_WORKS_LIST,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        if len(works) > effective_limit:
            accumulator.truncated = True
            accumulator.warn(WARNING_REMOTE_OVER_LIMIT)

        items: list[DevRevTicketSummary] = []
        for raw in works:
            summary = self._normalize_summary(raw, accumulator, detail=False)
            if summary is not None:
                items.append(summary)
        accumulator.items = len(items)

        page: CursorPage[DevRevTicketSummary] = CursorPage[DevRevTicketSummary](
            items=items,
            next_cursor=self._remote_cursor(payload.get("next_cursor"), meta),
            prev_cursor=self._remote_cursor(payload.get("prev_cursor"), meta),
            page_size=min(max(effective_limit, len(items)), MAX_PAGE_SIZE),
            truncated=accumulator.truncated,
            partial=accumulator.partial,
            warnings=accumulator.warnings,
        )
        self._finish(accumulator)
        return page

    async def get_ticket(self, work_id: str) -> DevRevTicketDetail:
        """Fetch one ticket through ``GET /works.get``, then enforce scope.

        A known identifier must not become a way around the list filters, so the
        returned object is re-checked against the configured part and
        ticket-visibility allowlists and fails closed when either is absent.
        """
        self._require_open()
        identifier = self._validated_work_identifier(work_id)
        accumulator = _Accumulator(ENDPOINT_WORKS_GET)
        payload, meta = await self._request_json(
            "GET",
            _PATH_WORKS_GET,
            endpoint=ENDPOINT_WORKS_GET,
            params={"id": identifier},
            attempts_into=accumulator,
        )
        accumulator.pages = 1

        work = payload.get("work")
        if not isinstance(work, Mapping):
            raise DevRevProtocolError(
                "works.get response did not carry a work object",
                endpoint=ENDPOINT_WORKS_GET,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )

        self._assert_in_scope(work, endpoint=ENDPOINT_WORKS_GET, meta=meta)
        detail = self._normalize_summary(work, accumulator, detail=True)
        if detail is None or not isinstance(detail, DevRevTicketDetail):
            raise DevRevProtocolError(
                "works.get returned a work object without a usable identifier pair",
                endpoint=ENDPOINT_WORKS_GET,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        accumulator.items = 1
        self._finish(accumulator)
        return detail

    async def list_timeline_page(
        self,
        object_id: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> TimelinePage:
        """Return exactly one bounded, forward-paginated timeline page.

        ``mode`` is intentionally not a parameter: timeline navigation is always
        ``after``. The configured timeline-visibility allowlist is sent as a
        request filter and re-checked on the response, and a caller cannot widen
        it.

        Ticket-level scope is enforced by :meth:`get_ticket`; a service layer
        that renders a conversation must resolve the ticket first.
        """
        self._require_open()
        accumulator = _Accumulator(ENDPOINT_TIMELINE_LIST)
        accumulator.cursor_digest = cursor_digest(cursor)
        page = await self._fetch_timeline_page(
            object_id, cursor=cursor, limit=limit, accumulator=accumulator
        )
        self._finish(accumulator)
        return page

    async def iter_timeline_entries(
        self,
        object_id: str,
        *,
        max_pages: Optional[int] = None,
        max_entries: Optional[int] = None,
        strict: bool = False,
    ) -> AsyncIterator[DevRevTimelineEntry]:
        """Walk forward through the timeline, deduplicating by entry id.

        An empty or short page is not terminal while ``next_cursor`` exists.
        Iteration ends only when the cursor is absent, a repeated cursor is
        detected (:class:`DevRevPaginationError`), or a configured guard trips.
        A guard trip marks :attr:`last_diagnostics` ``truncated``/``partial``, or
        raises :class:`DevRevResourceLimitError` when ``strict`` is set.
        """
        accumulator = _Accumulator(ENDPOINT_TIMELINE_LIST)
        async for entry in self._walk_timeline(
            object_id,
            max_pages=max_pages,
            max_entries=max_entries,
            strict=strict,
            accumulator=accumulator,
        ):
            yield entry
        # Reached only when the walk ran to one of its own stop conditions. An
        # abandoned iterator therefore logs nothing, and a raised guard/pagination
        # error is reported by the exception rather than a success line.
        self._finish(accumulator)

    async def load_timeline(
        self,
        object_id: str,
        *,
        max_pages: Optional[int] = None,
        max_entries: Optional[int] = None,
        strict: bool = False,
        sort: bool = False,
    ) -> DevRevTimelineHydration:
        """Load a bounded timeline set explicitly, optionally sorted.

        The result reports ``truncated``/``partial`` rather than claiming to be
        the whole conversation. ``sort=True`` applies
        :func:`sort_timeline_entries`; the default preserves DevRev's order.
        """
        accumulator = _Accumulator(ENDPOINT_TIMELINE_LIST)
        entries = [
            entry
            async for entry in self._walk_timeline(
                object_id,
                max_pages=max_pages,
                max_entries=max_entries,
                strict=strict,
                accumulator=accumulator,
            )
        ]
        ordered = tuple(entries)
        if sort:
            ordered, sort_warnings = sort_timeline_entries(ordered)
            accumulator.extend_warnings(sort_warnings)
        diagnostics = self._finish(accumulator)
        return DevRevTimelineHydration(
            entries=ordered,
            pages=accumulator.pages,
            truncated=accumulator.truncated,
            partial=accumulator.partial,
            warnings=tuple(accumulator.warnings),
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _walk_timeline(
        self,
        object_id: str,
        *,
        max_pages: Optional[int],
        max_entries: Optional[int],
        strict: bool,
        accumulator: _Accumulator,
    ) -> AsyncIterator[DevRevTimelineEntry]:
        self._require_open()
        # `is None` rather than a truthiness test: an explicit 0 is a caller
        # error and must be refused, not silently replaced by the default.
        page_guard = self._caller_guard("max_pages", max_pages, self._max_pages)
        entry_guard = self._caller_guard("max_entries", max_entries, self._max_entries)

        cursor: Optional[str] = None
        requested_cursors: set[str] = set()
        seen_entries: set[str] = set()
        yielded = 0

        while True:
            if accumulator.pages >= page_guard:
                accumulator.truncated = True
                accumulator.partial = True
                accumulator.warn(WARNING_MAX_PAGES_REACHED)
                self._publish(accumulator)
                if strict:
                    raise DevRevResourceLimitError(
                        f"timeline iteration stopped at the configured {page_guard}-page guard",
                        endpoint=ENDPOINT_TIMELINE_LIST,
                    )
                return

            accumulator.cursor_digest = cursor_digest(cursor)
            page = await self._fetch_timeline_page(
                object_id, cursor=cursor, limit=None, accumulator=accumulator
            )

            for entry in page.items:
                if entry.entry_id in seen_entries:
                    accumulator.duplicate_entries += 1
                    accumulator.warn(WARNING_DUPLICATE_ENTRIES)
                    continue
                if yielded >= entry_guard:
                    accumulator.truncated = True
                    accumulator.partial = True
                    accumulator.warn(WARNING_MAX_ENTRIES_REACHED)
                    self._publish(accumulator)
                    if strict:
                        raise DevRevResourceLimitError(
                            f"timeline iteration stopped at the configured "
                            f"{entry_guard}-entry guard",
                            endpoint=ENDPOINT_TIMELINE_LIST,
                        )
                    return
                seen_entries.add(entry.entry_id)
                yielded += 1
                accumulator.items = yielded
                self._publish(accumulator)
                yield entry

            next_cursor = page.next_cursor
            if not next_cursor:
                # The only terminal condition DevRev documents.
                self._publish(accumulator)
                return
            if next_cursor in requested_cursors:
                raise DevRevPaginationError(
                    "DevRev repeated a pagination cursor; refusing to loop",
                    endpoint=ENDPOINT_TIMELINE_LIST,
                    status=accumulator.status,
                    request_id=accumulator.request_id,
                )
            requested_cursors.add(next_cursor)
            cursor = next_cursor

    async def _fetch_timeline_page(
        self,
        object_id: str,
        *,
        cursor: Optional[str],
        limit: Optional[int],
        accumulator: _Accumulator,
    ) -> TimelinePage:
        identifier = self._validated_work_identifier(object_id)
        effective_limit = self._effective_limit(limit)
        params: dict[str, Any] = {
            "object": identifier,
            "mode": TIMELINE_LIST_MODE,
            "limit": effective_limit,
            # The separate timeline-visibility allowlist, always applied.
            "visibility": [v.value for v in self._allowed_timeline_visibility],
        }
        validated_cursor = self._validated_cursor(cursor)
        if validated_cursor is not None:
            params["cursor"] = validated_cursor

        payload, meta = await self._request_json(
            "GET",
            _PATH_TIMELINE_LIST,
            endpoint=ENDPOINT_TIMELINE_LIST,
            params=params,
            attempts_into=accumulator,
        )
        accumulator.pages += 1

        raw_entries = payload.get("timeline_entries")
        if not isinstance(raw_entries, list):
            raise DevRevProtocolError(
                "timeline-entries.list response did not carry a timeline_entries array",
                endpoint=ENDPOINT_TIMELINE_LIST,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        if len(raw_entries) > MAX_PAGE_SIZE:
            raise DevRevResourceLimitError(
                f"timeline-entries.list returned more than the canonical maximum of "
                f"{MAX_PAGE_SIZE} entries",
                endpoint=ENDPOINT_TIMELINE_LIST,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        if len(raw_entries) > effective_limit:
            accumulator.truncated = True
            accumulator.warn(WARNING_REMOTE_OVER_LIMIT)

        entries: list[DevRevTimelineEntry] = []
        for raw in raw_entries:
            entry = self._normalize_timeline_entry(raw, identifier, accumulator)
            if entry is not None:
                entries.append(entry)

        return TimelinePage(
            items=entries,
            next_cursor=self._remote_cursor(payload.get("next_cursor"), meta),
            prev_cursor=self._remote_cursor(payload.get("prev_cursor"), meta),
            page_size=min(max(effective_limit, len(entries)), MAX_PAGE_SIZE),
            truncated=accumulator.truncated,
            partial=accumulator.partial,
            warnings=accumulator.warnings,
        )

    # ------------------------------------------------------------------
    # Request validation
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._client.is_closed:
            raise DevRevRequestError("the DevRev client is closed")

    @staticmethod
    def _validated_filters(
        filters: Union[DevRevTicketFilters, Mapping[str, Any]],
    ) -> DevRevTicketFilters:
        if isinstance(filters, DevRevTicketFilters):
            return filters
        if not isinstance(filters, Mapping):
            raise DevRevRequestError("filters must be a DevRevTicketFilters or a mapping")
        try:
            # `extra="forbid"` rejects an unknown key, and the model has no
            # `type` field at all, so a caller cannot widen the work type.
            return DevRevTicketFilters.model_validate(dict(filters))
        except ValidationError as exc:
            raise DevRevRequestError(
                "filters are limited to the allowlisted works.list surface; "
                "the ticket work type cannot be overridden"
            ) from exc

    def _effective_limit(self, limit: Optional[int]) -> int:
        if limit is None:
            return self._page_size
        candidate = _strict_int(limit)
        if candidate is None or candidate < 1 or candidate > MAX_PAGE_SIZE:
            raise DevRevRequestError(
                f"limit must be an integer within [1, {MAX_PAGE_SIZE}]"
            )
        return candidate

    @staticmethod
    def _caller_guard(label: str, requested: Optional[int], configured: int) -> int:
        """Resolve a per-call iterator guard against the configured ceiling."""
        if requested is None:
            return configured
        candidate = _strict_int(requested)
        if candidate is None or candidate < 1 or candidate > configured:
            raise DevRevRequestError(f"{label} must be an integer within [1, {configured}]")
        return candidate

    @staticmethod
    def _validated_cursor(cursor: Optional[str]) -> Optional[str]:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor.strip():
            raise DevRevRequestError("a cursor must be a non-empty opaque string")
        if len(cursor) > MAX_CURSOR_LENGTH:
            raise DevRevRequestError(
                f"a cursor must not exceed the canonical {MAX_CURSOR_LENGTH} characters"
            )
        # Returned verbatim: DevRev cursors are opaque and must never be
        # parsed, trimmed, or re-encoded.
        return cursor

    @staticmethod
    def _remote_cursor(raw: Any, meta: _ResponseMeta) -> Optional[str]:
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise DevRevProtocolError(
                "DevRev returned a non-string pagination cursor",
                endpoint=meta.endpoint,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        if not raw:
            return None
        if len(raw) > MAX_CURSOR_LENGTH:
            raise DevRevProtocolError(
                f"DevRev returned a cursor longer than the canonical "
                f"{MAX_CURSOR_LENGTH} characters",
                endpoint=meta.endpoint,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )
        return raw

    @staticmethod
    def _validated_work_identifier(work_id: str) -> str:
        if not isinstance(work_id, str):
            raise DevRevRequestError("a DevRev identifier must be a string")
        candidate = work_id.strip()
        if not candidate:
            raise DevRevRequestError("a DevRev identifier is required")
        if _DON_PATTERN.match(candidate) and len(candidate) <= MAX_ID_LENGTH:
            return candidate
        if _DISPLAY_ID_PATTERN.match(candidate) and len(candidate) <= MAX_DISPLAY_ID_LENGTH:
            return candidate
        raise DevRevRequestError(
            "a DevRev identifier must be a bounded DON or display id"
        )

    def _works_list_body(
        self,
        filters: DevRevTicketFilters,
        *,
        cursor: Optional[str],
        mode: str,
        limit: int,
    ) -> dict[str, Any]:
        if mode not in LIST_MODES:
            raise DevRevRequestError(f"mode must be one of {sorted(LIST_MODES)}")

        body: dict[str, Any] = {"type": [_WORK_TYPE_TICKET]}
        if filters.stage:
            # DevRev's `stage` is a stage-filter object, not a bare array.
            body["stage"] = {"name": list(filters.stage)}
        for key, values in (
            ("state", filters.state),
            ("owned_by", filters.owned_by),
            ("created_by", filters.created_by),
            ("reported_by", filters.reported_by),
            ("tags", filters.tags),
        ):
            if values:
                body[key] = list(values)
        if filters.created_date:
            body["created_date"] = filters.created_date
        if filters.modified_date:
            body["modified_date"] = filters.modified_date

        body["applies_to_part"] = self._scoped_parts(filters.applies_to_part)

        ticket: dict[str, Any] = {}
        if filters.ticket_source_channel:
            ticket["source_channel"] = list(filters.ticket_source_channel)
        if filters.ticket_subtype:
            ticket["subtype"] = list(filters.ticket_subtype)
        ticket["visibility"] = self._scoped_visibility(filters.ticket_visibility)
        body["ticket"] = ticket

        if cursor is not None:
            body["cursor"] = cursor
        body["mode"] = mode
        body["limit"] = limit

        unexpected = set(body) - WORKS_LIST_ALLOWED_TOP_LEVEL_KEYS
        if unexpected or set(ticket) - WORKS_LIST_ALLOWED_TICKET_KEYS:
            raise DevRevRequestError("refusing to send a works.list key outside the allowlist")
        return body

    def _scoped_parts(self, requested: Sequence[str]) -> list[str]:
        """Intersect the caller's parts with configuration, failing closed.

        An absent request means "the whole configured scope", never "no scope".
        Any value outside the allowlist is a widening attempt and is refused
        rather than quietly dropped, so a caller cannot discover which parts
        exist by watching results shrink. A repeated allowed value is a
        harmless duplicate, not a violation.
        """
        if not requested:
            return list(self._allowed_parts)
        deduplicated = list(dict.fromkeys(requested))
        if not set(deduplicated) <= set(self._allowed_parts):
            raise DevRevScopeError(
                "applies_to_part may only narrow the configured DevRev part scope"
            )
        return deduplicated

    def _scoped_visibility(self, requested: Sequence[int]) -> list[int]:
        """Intersect the caller's ticket visibility with configuration."""
        if not requested:
            return list(self._allowed_ticket_visibility)
        deduplicated = list(dict.fromkeys(requested))
        if not set(deduplicated) <= set(self._allowed_ticket_visibility):
            raise DevRevScopeError(
                "ticket visibility may only narrow the configured DevRev visibility scope"
            )
        return deduplicated

    # ------------------------------------------------------------------
    # Scope enforcement on responses
    # ------------------------------------------------------------------

    def _in_scope(self, work: Mapping[str, Any]) -> bool:
        work_type = _text(work.get("type"))
        if work_type is not None and work_type != _WORK_TYPE_TICKET:
            return False
        part = _object_id(work.get("applies_to_part"))
        if part is None or part not in self._allowed_parts:
            return False
        ticket = work.get("ticket")
        visibility = _strict_int(ticket.get("visibility")) if isinstance(ticket, Mapping) else None
        return visibility is not None and visibility in self._allowed_ticket_visibility

    def _assert_in_scope(
        self, work: Mapping[str, Any], *, endpoint: str, meta: _ResponseMeta
    ) -> None:
        if not self._in_scope(work):
            # One generic message for every reason, so a denial never discloses
            # whether the object exists or which check failed.
            raise DevRevScopeError(
                "the requested ticket is outside the configured DevRev scope",
                endpoint=endpoint,
                status=meta.status,
                request_id=meta.request_id,
                attempts=meta.attempts,
            )

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize_summary(
        self, raw: Any, accumulator: _Accumulator, *, detail: bool
    ) -> Optional[DevRevTicketSummary]:
        """Normalize one work object, or drop it with an accounted warning."""
        if not isinstance(raw, Mapping):
            accumulator.dropped_unidentified += 1
            accumulator.partial = True
            accumulator.warn(WARNING_UNIDENTIFIED_ROWS)
            return None

        if not detail and not self._in_scope(raw):
            accumulator.dropped_out_of_scope += 1
            accumulator.partial = True
            accumulator.warn(WARNING_OUT_OF_SCOPE_ROWS)
            return None

        work_id = _text(raw.get("id"))
        display_id = _text(raw.get("display_id"))
        if (
            work_id is None
            or display_id is None
            or len(work_id) > MAX_ID_LENGTH
            or len(display_id) > MAX_DISPLAY_ID_LENGTH
        ):
            accumulator.dropped_unidentified += 1
            accumulator.partial = True
            accumulator.warn(WARNING_UNIDENTIFIED_ROWS)
            return None

        title, title_clipped = _clip(_text(raw.get("title")), MAX_TITLE_LENGTH)
        if title_clipped:
            accumulator.truncated = True
            accumulator.warn(WARNING_TITLE_TRUNCATED)

        owner_ids, owners_clipped = _bounded_id_list(raw.get("owned_by"), MAX_LIST_ITEMS)
        if owners_clipped:
            accumulator.truncated = True
            accumulator.warn(WARNING_OWNERS_TRUNCATED)
        tag_ids, tags_clipped = _bounded_id_list(raw.get("tags"), MAX_LIST_ITEMS)
        if tags_clipped:
            accumulator.truncated = True
            accumulator.warn(WARNING_TAGS_TRUNCATED)

        raw_ticket = raw.get("ticket")
        ticket: Mapping[str, Any] = raw_ticket if isinstance(raw_ticket, Mapping) else {}
        source_channel, _ = _clip(_scalar_name(ticket.get("source_channel")), MAX_TOPIC_LENGTH)
        subtype, _ = _clip(
            _scalar_name(ticket.get("subtype")) or _scalar_name(raw.get("subtype")),
            MAX_TOPIC_LENGTH,
        )
        stage, _ = _clip(_scalar_name(raw.get("stage")), MAX_TOPIC_LENGTH)
        state, _ = _clip(
            _scalar_name(raw.get("state")) or _scalar_name(raw.get("state_display_name")),
            MAX_TOPIC_LENGTH,
        )
        severity, _ = _clip(_scalar_name(raw.get("severity")), MAX_TOPIC_LENGTH)

        applies_to_part = _object_id(raw.get("applies_to_part"))
        if applies_to_part is not None and len(applies_to_part) > MAX_ID_LENGTH:
            applies_to_part = None

        object_version = _strict_int(raw.get("object_version"))
        if object_version is not None and object_version < 0:
            object_version = None

        fields: dict[str, Any] = {
            "devrev_work_id": work_id,
            "devrev_display_id": display_id,
            "title": title,
            "stage": stage,
            "state": state,
            "severity": severity,
            "applies_to_part": applies_to_part,
            "ticket_visibility": _strict_int(ticket.get("visibility")),
            "source_channel": source_channel,
            "subtype": subtype,
            "object_version": object_version,
            "owner_ids": owner_ids,
            "reporter": _actor(raw.get("reported_by")),
            "tag_ids": tag_ids,
            "created_at": _parse_datetime(raw.get("created_date")),
            "modified_at": _parse_datetime(raw.get("modified_date")),
        }

        if not detail:
            return DevRevTicketSummary(**fields)

        body, body_clipped = _clip(_text(raw.get("body")), MAX_MESSAGE_BODY_LENGTH)
        if body_clipped:
            accumulator.truncated = True
            accumulator.warn(WARNING_BODY_TRUNCATED)
        # Attachments have their own canonical bound, deliberately distinct from
        # MAX_LIST_ITEMS so tightening one never reshapes the other.
        attachments, attachments_clipped = _bounded_id_list(raw.get("artifacts"), MAX_ATTACHMENTS)
        if attachments_clipped:
            accumulator.truncated = True
            accumulator.warn(WARNING_ATTACHMENTS_TRUNCATED)
        timeline_count = _strict_int(raw.get("timeline_entry_count"))
        if timeline_count is not None and timeline_count < 0:
            timeline_count = None

        return DevRevTicketDetail(
            **fields,
            body=body,
            attachments=attachments,
            timeline_entry_count=timeline_count,
        )

    def _normalize_timeline_entry(
        self, raw: Any, object_id: str, accumulator: _Accumulator
    ) -> Optional[DevRevTimelineEntry]:
        """Normalize one timeline entry, or drop it with an accounted warning."""
        if not isinstance(raw, Mapping):
            accumulator.dropped_unidentified += 1
            accumulator.partial = True
            accumulator.warn(WARNING_UNIDENTIFIED_ROWS)
            return None

        entry_id = _text(raw.get("id"))
        entry_object = _object_id(raw.get("object"))
        if entry_id is None or len(entry_id) > MAX_ID_LENGTH:
            accumulator.dropped_unidentified += 1
            accumulator.partial = True
            accumulator.warn(WARNING_UNIDENTIFIED_ROWS)
            return None
        if entry_object is None or entry_object != object_id:
            # An entry belonging to another object cannot be rendered under this
            # ticket, whatever the reason.
            accumulator.dropped_out_of_scope += 1
            accumulator.partial = True
            accumulator.warn(WARNING_OUT_OF_SCOPE_ENTRIES)
            return None

        remote_visibility = _text(raw.get("visibility"))
        visibility = TimelineVisibility.PRIVATE
        if remote_visibility is not None:
            try:
                visibility = TimelineVisibility(remote_visibility)
            except ValueError:
                # An unmodelled visibility is treated as the most restrictive
                # value rather than guessed at.
                visibility = TimelineVisibility.PRIVATE
            if visibility not in self._allowed_timeline_visibility:
                accumulator.dropped_out_of_scope += 1
                accumulator.partial = True
                accumulator.warn(WARNING_OUT_OF_SCOPE_ENTRIES)
                return None
        # An entry with no declared visibility keeps the fail-closed default and
        # is retained: those are change events and unmodelled types, which carry
        # no body, so nothing readable is exposed.

        created = _parse_datetime(raw.get("created_date"))
        modified = _parse_datetime(raw.get("modified_date"))
        if created is None:
            if modified is None:
                # No timestamp at all: the entry cannot be ordered or dated, and
                # inventing one would be fabrication.
                accumulator.dropped_undated += 1
                accumulator.partial = True
                accumulator.warn(WARNING_UNDATED_ENTRIES)
                return None
            created = modified
            accumulator.warn(WARNING_CREATED_DATE_FALLBACK)

        remote_type = _text(raw.get("type"))
        fields: dict[str, Any] = {
            "entry_id": entry_id,
            "object_id": object_id,
            "visibility": visibility,
            "created_at": created,
            "modified_at": modified,
        }

        if remote_type in _COMMENT_TYPES:
            body, body_clipped = _clip(_text(raw.get("body")), MAX_MESSAGE_BODY_LENGTH)
            if body_clipped:
                accumulator.truncated = True
                accumulator.warn(WARNING_BODY_TRUNCATED)
            body_type, _ = _clip(_text(raw.get("body_type")), MAX_TOPIC_LENGTH)
            thread_id = _object_id(raw.get("thread"))
            in_reply_to = _object_id(raw.get("reply_to")) or _object_id(raw.get("in_reply_to"))
            return DevRevTimelineEntry(
                **fields,
                kind=TimelineEntryKind.COMMENT,
                body=body,
                body_type=body_type,
                author=_actor(raw.get("created_by")),
                thread_id=thread_id if thread_id and len(thread_id) <= MAX_ID_LENGTH else None,
                in_reply_to=(
                    in_reply_to if in_reply_to and len(in_reply_to) <= MAX_ID_LENGTH else None
                ),
            )

        if remote_type in _CHANGE_EVENT_TYPES:
            summary, summary_clipped = _clip(
                self._change_summary(raw), MAX_TOPIC_LENGTH
            )
            if summary_clipped:
                accumulator.truncated = True
                accumulator.warn(WARNING_CHANGE_SUMMARY_TRUNCATED)
            # No body and no author, by contract: a change event must never be
            # renderable as an authored participant reply.
            return DevRevTimelineEntry(
                **fields,
                kind=TimelineEntryKind.CHANGE_EVENT,
                change_summary=summary,
            )

        unsupported_type, _ = _clip(remote_type, MAX_TOPIC_LENGTH)
        return DevRevTimelineEntry(
            **fields,
            kind=TimelineEntryKind.UNSUPPORTED,
            unsupported_type=unsupported_type,
        )

    @staticmethod
    def _change_summary(raw: Mapping[str, Any]) -> Optional[str]:
        """Describe a change event as ``field: old -> new`` when possible."""
        change = raw.get("change")
        if not isinstance(change, Mapping):
            event = raw.get("event")
            change = event if isinstance(event, Mapping) else None
        if not isinstance(change, Mapping):
            return None
        field = _scalar_name(change.get("field")) or _scalar_name(change.get("type"))
        if field is None:
            return None
        old = _scalar_name(change.get("old"))
        new = _scalar_name(change.get("new"))
        if old is None and new is None:
            return field
        return f"{field}: {old or ''} -> {new or ''}".strip()

    # ------------------------------------------------------------------
    # HTTP with bounded bodies and deterministic retry
    # ------------------------------------------------------------------

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        json_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        idempotent: bool = True,
        attempts_into: Optional[_Accumulator] = None,
    ) -> tuple[dict[str, Any], _ResponseMeta]:
        """Issue one logical request with bounded retry and a bounded body.

        Only 429, 500, 503, and transport failures are retried, and only when
        the operation is idempotent. Every MVP endpoint is a read; the flag
        exists so a future non-read cannot inherit read semantics by accident.
        """
        last: Optional[DevRevError] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                payload, meta = await self._send_once(
                    method,
                    path,
                    endpoint=endpoint,
                    json_body=json_body,
                    params=params,
                    attempt=attempt,
                )
            except asyncio.CancelledError:
                # Cancellation is a control-flow signal, never a DevRev error.
                raise
            except (DevRevRateLimitError, DevRevTransientError) as exc:
                last = exc
                if not idempotent or attempt >= self._max_retries:
                    raise
                delay = self._retry_delay(attempt, exc.retry_after_s)
                logger.warning(
                    "devrev retry",
                    extra={
                        "devrev": {
                            "endpoint": endpoint,
                            "status": exc.status,
                            "attempt": attempt,
                            "delay_s": delay,
                            "request_id": exc.request_id,
                        }
                    },
                )
                await self._sleep(delay)
                continue

            if attempts_into is not None:
                attempts_into.absorb(meta)
            return payload, meta

        raise last or DevRevTransientError(
            "DevRev request exhausted its retries", endpoint=endpoint
        )

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        json_body: Optional[Mapping[str, Any]],
        params: Optional[Mapping[str, Any]],
        attempt: int,
    ) -> tuple[dict[str, Any], _ResponseMeta]:
        try:
            async with self._client.stream(
                method, path, json=json_body, params=params
            ) as response:
                meta = self._meta_from(response, endpoint=endpoint, attempt=attempt)

                if 300 <= response.status_code < 400:
                    # follow_redirects is off; naming the Location target would
                    # only help an attacker confirm the redirect landed.
                    raise DevRevProtocolError(
                        f"{endpoint}: refusing to follow an HTTP "
                        f"{response.status_code} redirect",
                        endpoint=endpoint,
                        status=response.status_code,
                        request_id=meta.request_id,
                        attempts=attempt,
                    )

                if response.status_code >= 400:
                    read, truncated = await self._read_bounded(
                        response, MAX_UPSTREAM_ERROR_BODY_BYTES
                    )
                    raise self._error_for_status(
                        meta, upstream_error_bytes=read, upstream_error_truncated=truncated
                    )

                if response.status_code != 200:
                    raise DevRevProtocolError(
                        f"{endpoint}: unexpected HTTP {response.status_code}",
                        endpoint=endpoint,
                        status=response.status_code,
                        request_id=meta.request_id,
                        attempts=attempt,
                    )

                buffer = await self._read_capped(response, meta)
        except asyncio.CancelledError:
            raise
        except httpx.TransportError:
            # The underlying failure is deliberately not echoed or chained: its
            # message can embed a resolved host, proxy URL, or TLS detail.
            raise DevRevTransientError(
                f"{endpoint}: transport unavailable",
                endpoint=endpoint,
                attempts=attempt,
            ) from None
        except httpx.HTTPError:
            raise DevRevProtocolError(
                f"{endpoint}: malformed HTTP exchange",
                endpoint=endpoint,
                attempts=attempt,
            ) from None

        try:
            payload = json.loads(buffer)
        except ValueError as exc:
            raise DevRevProtocolError(
                f"{endpoint}: response body was not valid JSON",
                endpoint=endpoint,
                status=meta.status,
                request_id=meta.request_id,
                attempts=attempt,
            ) from exc
        if not isinstance(payload, dict):
            raise DevRevProtocolError(
                f"{endpoint}: response body was not a JSON object",
                endpoint=endpoint,
                status=meta.status,
                request_id=meta.request_id,
                attempts=attempt,
            )
        return payload, meta

    async def _read_capped(self, response: httpx.Response, meta: _ResponseMeta) -> bytes:
        """Stream a success body, refusing anything past the canonical cap.

        A declared ``Content-Length`` over the cap is rejected before the first
        byte is read. Otherwise decoded bytes are tallied as they arrive, so a
        chunked body or a compressed body that expands past the cap aborts
        immediately. JSON is parsed only from what this returns.
        """
        cap = self._max_response_bytes
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    raise DevRevResourceLimitError(
                        f"{meta.endpoint}: declared response size exceeds the "
                        f"{cap}-byte cap",
                        endpoint=meta.endpoint,
                        status=meta.status,
                        request_id=meta.request_id,
                        attempts=meta.attempts,
                    )
            except ValueError:
                # An unparseable declaration proves nothing; the byte counter
                # below is the real guard.
                pass

        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > cap:
                # Drop the buffer before raising so an oversized body is never
                # retained, let alone logged.
                buffer.clear()
                raise DevRevResourceLimitError(
                    f"{meta.endpoint}: response body exceeds the {cap}-byte cap",
                    endpoint=meta.endpoint,
                    status=meta.status,
                    request_id=meta.request_id,
                    attempts=meta.attempts,
                )
        return bytes(buffer)

    @staticmethod
    async def _read_bounded(response: httpx.Response, cap: int) -> tuple[int, bool]:
        """Consume at most ``cap`` bytes of an error body, keeping only counts.

        The content is never returned: an upstream error message can contain
        scraped participant text, and DevRev's documented body carries nothing a
        client should branch on beyond the status.
        """
        read = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            remaining = cap - read
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                read += remaining
                truncated = True
                break
            read += len(chunk)
        return read, truncated

    def _meta_from(
        self, response: httpx.Response, *, endpoint: str, attempt: int
    ) -> _ResponseMeta:
        headers = response.headers
        return _ResponseMeta(
            endpoint=endpoint,
            status=response.status_code,
            attempts=attempt,
            rate_limit_limit=self._header_int(headers.get(_RATE_LIMIT_HEADER)),
            rate_limit_remaining=self._header_int(headers.get(_RATE_REMAINING_HEADER)),
            rate_limit_reset=self._header_int(headers.get(_RATE_RESET_HEADER)),
            retry_after_s=self._parse_retry_after(headers.get(_RETRY_AFTER_HEADER)),
            request_id=self._request_id(headers),
        )

    @staticmethod
    def _header_int(raw: Optional[str]) -> Optional[int]:
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _request_id(headers: httpx.Headers) -> Optional[str]:
        for name in _REQUEST_ID_HEADERS:
            raw = headers.get(name)
            if raw is None:
                continue
            cleaned = "".join(char for char in raw.strip() if char.isprintable())
            if cleaned:
                return cleaned[:_MAX_REQUEST_ID_LENGTH]
        return None

    def _parse_retry_after(self, raw: Optional[str]) -> Optional[float]:
        """Read ``Retry-After`` as delta-seconds or an HTTP-date.

        The result is clamped into ``[0, retry_after_cap_s]``: DevRev's window
        is five minutes, and a client that obediently sleeps that long has
        simply stopped responding.
        """
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            return None
        try:
            seconds = float(int(text))
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return None
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - self._clock()).total_seconds()
        return min(max(seconds, 0.0), self._retry_after_cap_s)

    def _error_for_status(
        self, meta: _ResponseMeta, *, upstream_error_bytes: int, upstream_error_truncated: bool
    ) -> DevRevError:
        """Map a status to a typed error whose message is safe to surface."""
        status = meta.status
        common: dict[str, Any] = {
            "endpoint": meta.endpoint,
            "status": status,
            "attempts": meta.attempts,
            "request_id": meta.request_id,
            "upstream_error_bytes": upstream_error_bytes,
            "upstream_error_truncated": upstream_error_truncated,
        }
        message = f"{meta.endpoint}: HTTP {status}"
        if status == 401:
            return DevRevAuthenticationError(message, **common)
        if status == 403:
            return DevRevPermissionError(message, **common)
        if status == 404:
            return DevRevNotFoundError(message, **common)
        if status == 409:
            return DevRevConflictError(message, **common)
        if status == 429:
            return DevRevRateLimitError(message, retry_after_s=meta.retry_after_s, **common)
        if status in {500, 503}:
            return DevRevTransientError(message, **common)
        # 400 and every other 4xx/5xx: the request or the contract is wrong, and
        # retrying it would only burn rate-limit budget.
        return DevRevProtocolError(message, **common)

    def _retry_delay(self, attempt: int, retry_after_s: Optional[float]) -> float:
        """Prefer ``Retry-After``; otherwise exponential backoff plus jitter."""
        if retry_after_s is not None:
            return min(max(retry_after_s, 0.0), self._retry_after_cap_s)
        base = self._backoff_base_s * 2.0 ** (attempt - 1)
        # Injectable jitter keeps the schedule deterministic under test while
        # still de-synchronizing real concurrent callers.
        jittered = base + max(0.0, min(1.0, self._jitter())) * base
        return min(max(jittered, 0.0), self._retry_after_cap_s)

    def _publish(self, accumulator: _Accumulator) -> DevRevCallDiagnostics:
        """Expose in-progress diagnostics without emitting a log line.

        A long iteration publishes after every page and entry so a consumer that
        stops early still sees accurate counts; logging each of those would bury
        the one line that matters.
        """
        diagnostics = accumulator.freeze()
        self._last_diagnostics = diagnostics
        return diagnostics

    def _finish(self, accumulator: _Accumulator) -> DevRevCallDiagnostics:
        """Publish and log exactly one safe summary line for a completed call.

        The record carries only the endpoint name, status, attempt/page/item
        counts, the remote request id, rate-limit state, and a cursor *digest* --
        never a cursor value, a request body, an auth header, or ticket text.
        """
        diagnostics = self._publish(accumulator)
        logger.info("devrev call", extra={"devrev": diagnostics.as_log_fields()})
        return diagnostics
