"""Closed domain contracts for the /tickets review console.

This module is the single source of truth for the console's enums, bounds,
state machines, ETag/precondition semantics, audit hash chain, and cursor
authenticated encryption. It is deliberately free of I/O: no Firestore, no
DevRev, no GCP, and no import of the main RAG ``api.config`` singleton.

Design rules frozen by the Stage 1 ADR:

* every model uses ``extra="forbid"`` — an unknown remote or client field is an
  error, never silently retained;
* every datetime is timezone-aware and normalized to UTC;
* raw DevRev payloads are never stored on a public model (there is no ``raw``
  field anywhere);
* the durable :class:`TicketReview` carries no DevRev title — titles are
  live/cache data only;
* state transitions live in tested pure functions, never scattered across
  routes.

The numeric and string bounds below mirror the master plan's canonical limits
table verbatim. Changing one requires an ADR and cross-layer contract tests.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Generic, Optional, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from api.ticket_evaluation_models import (
    ChunkEvidence,
    EvaluationRoute,
    EvaluationStatus,
    SourceReference,
    TicketEvaluationEvent,
)

# =====================================================================
# Canonical limits and defaults (master plan, "Canonical limits" table)
# =====================================================================

SCHEMA_VERSION = "1.0"
HASH_SCHEMA_VERSION = 1
GENESIS_EVENT_HASH = "0" * 64

# Pagination and iterator guards.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
DEVREV_MAX_PAGES = 100
DEVREV_MAX_ENTRIES = 5_000

# DevRev HTTP behavior.
DEVREV_CONNECT_TIMEOUT_S = 5.0
DEVREV_READ_TIMEOUT_S = 20.0
DEVREV_MAX_RETRIES = 3
DEVREV_RETRY_AFTER_CAP_S = 60

# Cache, idempotency, and retention windows (seconds unless named *_DAYS).
CACHE_TTL_S = 15 * 60
MESSAGE_CACHE_TTL_S = 24 * 60 * 60
IDEMPOTENCY_TTL_S = 7 * 24 * 60 * 60
CSRF_TOKEN_TTL_S = 60 * 60
REVIEW_RETENTION_DAYS = 730
AUDIT_RETENTION_DAYS = 2_555

# String bounds.
MAX_ID_LENGTH = 256
MAX_DISPLAY_ID_LENGTH = 64
MAX_CURSOR_LENGTH = 2_048
MAX_TITLE_LENGTH = 512
MAX_LEGACY_TYPE_LENGTH = 80
MAX_TOPIC_LENGTH = 80
MAX_COMMENTS_LENGTH = 10_000
MAX_EXPECTED_BEHAVIOR_LENGTH = 10_000
MAX_SUMMARY_LENGTH = 5_000
MAX_MESSAGE_BODY_LENGTH = 50_000
MAX_URL_LENGTH = 2_048

# Collection and request bounds.
MAX_ATTACHMENTS = 20
MAX_EVIDENCE_REFS_PER_REVIEW = 200
MAX_BATCH_REVIEWS = 100
MAX_JSON_REQUEST_BYTES = 1 * 1024 * 1024
MAX_UPSTREAM_ERROR_BODY_BYTES = 4 * 1024
DEVREV_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
EVIDENCE_BROKER_MAX_RESPONSE_BYTES = 512 * 1024

# Remediation lease windows.
REMEDIATION_LEASE_S = 15 * 60
REMEDIATION_HEARTBEAT_S = 5 * 60
REMEDIATION_MAX_CONTINUOUS_LEASE_S = 2 * 60 * 60
MAX_LEASE_EXTENSION_MINUTES = 120

# Firestore platform limit; used to prove the batch parent/item split is needed.
FIRESTORE_MAX_DOCUMENT_BYTES = 1_048_576

# Responsive contract (canonical table: "Responsive breakpoint / touch target").
# Frozen here so the Stage 6/7 UI cannot drift from the agreed values.
RESPONSIVE_BREAKPOINT_PX = 768
MIN_TOUCH_TARGET_PX = 44

# Bounds that the canonical table does not name explicitly. They exist so that
# no field is unbounded; they are not "canonical" and may be tightened freely.
# MAX_LIST_ITEMS is deliberately distinct from the canonical MAX_ATTACHMENTS so
# that tightening the attachment rule never silently reshapes unrelated lists.
MAX_LIST_ITEMS = 20
MAX_EMAIL_LENGTH = 320
MAX_DISPLAY_NAME_LENGTH = 200
MAX_REASON_LENGTH = 1_000
MAX_WARNINGS = 20
MAX_CHANGED_FIELDS = 64
MAX_METADATA_KEYS = 8
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 200
MAX_IF_MATCH_HEADER_LENGTH = 64
#: One repository path per changed file, and a branch/ref is not a free-text
#: field. Both are agent-supplied, so both are bounded here rather than at the
#: route: the repository is the layer that has to survive a lying client.
MAX_CHANGED_FILE_PATH_LENGTH = 512
MAX_GIT_REF_LENGTH = 255
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"

# Cursor authenticated encryption.
CURSOR_AEAD_KEY_BYTES = 32
CURSOR_NONCE_BYTES = 12
CURSOR_TAG_BYTES = 16
# Console cursors are short-lived by contract; the window matches the live
# list/detail cache so a token cannot outlive the data it points at.
CURSOR_DEFAULT_TTL_S = CACHE_TTL_S
CURSOR_EXPIRY_KEY = "_exp"

# Stage 4 — hydration, correlation, and the evidence broker.
#
# A manual-evidence candidate token is short lived on purpose: it authorizes
# one reviewer to link one already-observed broker result, so it must expire
# well inside a review session rather than living as long as a page cursor.
EVIDENCE_CANDIDATE_TTL_S = 10 * 60
MAX_EVIDENCE_CANDIDATES = 10
# The broker never fans out over an unbounded keyring, and never returns an
# unbounded result set, whatever a caller asks for.
MAX_BROKER_KEY_VERSIONS = 5
MAX_BROKER_RESULTS = 25
# The versioned, additive execution-log schema. v0 is every legacy document
# written before Stage 4, which carries none of the provenance fields.
EXECUTION_LOG_SCHEMA_VERSION = 1
LEGACY_EXECUTION_LOG_SCHEMA_VERSION = 0
# The lookup HMAC is a hex SHA-256 digest, so it obeys the same shape rule as
# every other hash in this module.
CORRELATION_HMAC_VERSION = 1
# Replay window for the n8n ingress signature, per the master plan.
INGRESS_SIGNATURE_MAX_SKEW_S = 5 * 60

# The only body types the console renders as text. Everything else — HTML,
# Markdown, an unknown remote value — becomes a bounded placeholder, so no
# remote markup can reach the browser.
PLAIN_TEXT_BODY_TYPES = frozenset({"text", "text/plain", "plain", "plaintext"})


# =====================================================================
# Errors
# =====================================================================


class TicketReviewContractError(Exception):
    """Base class for every Stage 1 contract violation."""


class InvalidReviewTransition(TicketReviewContractError):
    """A review status change is not permitted by the closed table."""


class InvalidBatchTransition(TicketReviewContractError):
    """A remediation-batch status change is not permitted."""


class PreconditionError(TicketReviewContractError):
    """Base class for ``If-Match`` precondition failures."""


class MissingPreconditionError(PreconditionError):
    """No ``If-Match`` header was supplied. Maps to HTTP 428."""


class MalformedPreconditionError(PreconditionError):
    """The ``If-Match`` header is not a quoted ``"vN"``. Maps to HTTP 422."""


class StalePreconditionError(PreconditionError):
    """The supplied version is not current. Maps to HTTP 412.

    The version metadata is optional so the error can be constructed and
    mapped generically; ``ensure_if_match`` always populates it, and only
    these two safe integers are ever exposed to the caller — never another
    reviewer's unsaved content.
    """

    def __init__(
        self,
        message: str,
        *,
        supplied_version: Optional[int] = None,
        current_version: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.supplied_version = supplied_version
        self.current_version = current_version


class CursorError(TicketReviewContractError):
    """A console cursor token is unreadable, tampered, or out of context.

    The message never contains the plaintext payload or the key.
    """


# =====================================================================
# Small helpers and reusable annotated types
# =====================================================================


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def review_id_for_devrev_work(devrev_work_id: str) -> str:
    """Derive the deterministic Firestore document ID for a DevRev work item.

    A DevRev DON contains ``/`` and therefore cannot be a Firestore document
    ID. The SHA-256 hex digest is stable, lowercase, and path-safe.
    """
    normalized = devrev_work_id.strip()
    if not normalized:
        raise ValueError("devrev_work_id is required")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _reject_non_integer(value: Any) -> Any:
    """Reject booleans, floats, and numeric strings for integer fields.

    ``bool`` is a subclass of ``int`` in Python, so a checkbox would otherwise
    become a rating of 1. Pydantic's lax mode would likewise accept ``"3"`` and
    ``3.0``; both are contract violations here.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("value must be an integer")
    return value


StrictInt = Annotated[int, BeforeValidator(_reject_non_integer)]
Rating = Annotated[int, BeforeValidator(_reject_non_integer), Field(ge=1, le=5)]
Sha256Hex = Annotated[str, Field(pattern=SHA256_HEX_PATTERN)]

#: A batch id is server-minted. The repository mints ``uuid4().hex``; the
#: canonical dashed form is accepted too so an operator can paste either the
#: value this console printed or the one a UUID tool produced. Nothing else is
#: allowed: this string reaches a Firestore document path and a URL path
#: segment, so a slash, a dot-segment, or a colon here would be a traversal.
#: ``$`` rather than ``\Z`` because a ``Field(pattern=...)`` is compiled by
#: pydantic-core's Rust engine, which has no ``\Z``. Routes do not rely on this
#: pattern alone: they pass the path segment through
#: :func:`validated_batch_id`, which strips and re-checks it.
BATCH_ID_PATTERN = (
    r"^(?:[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
BatchId = Annotated[str, Field(pattern=BATCH_ID_PATTERN)]
_BATCH_ID_PATTERN = re.compile(
    r"^(?:[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)

#: A commit is a full 40-hex object name. An abbreviated SHA is refused because
#: it stops identifying one commit as soon as the repository grows.
GIT_COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: `git check-ref-format` in the subset this console needs: no space, no control
#: character, no `..`, no leading/trailing slash or dot, none of `~^:?*[\`.
_GIT_REF_PATTERN = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)[A-Za-z0-9._/-]+(?<!/)(?<!\.)\Z")
#: A repository-relative path. Never absolute, never a dot-segment, never a
#: backslash: an agent reporting `../../etc/passwd` as "changed" is reporting
#: something this console must not record as if it were part of the repository.
_REPO_PATH_PATTERN = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)[A-Za-z0-9._/-]+(?<!/)\Z")
# \Z rather than $: in Python `$` also matches just before a trailing newline,
# which would accept '"v3"\n' as a valid strong validator.
_IF_MATCH_PATTERN = re.compile(r'^"v([1-9][0-9]*)"\Z')
# The console cursor alphabet is exactly unpadded base64url. Anything else --
# embedded whitespace, the standard '+'/'/' alphabet, stray padding -- is
# rejected rather than silently normalized, so one cursor has one encoding.
_CURSOR_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\Z")
_B64URL_TO_STD = str.maketrans("-_", "+/")


def validated_batch_id(value: str) -> str:
    """Accept one server-minted batch id, or raise ``ValueError``.

    Applied to the path segment before it ever reaches a Firestore document
    path. Surrounding whitespace is stripped (an id pasted from a file arrives
    with a newline) and the *stripped* value is what the caller then uses, so
    the laxness ``$`` would allow cannot survive past this function.
    """
    candidate = (value or "").strip()
    if not _BATCH_ID_PATTERN.match(candidate):
        raise ValueError("that batch id is not usable")
    return candidate


def validated_git_ref(value: str) -> str:
    """Accept one branch or ref name, or raise ``ValueError``.

    This value is rendered into a copied prompt and compared against what the
    CLI reports about the working repository, so a newline or a shell
    metacharacter here would travel into an operator's terminal.
    """
    candidate = (value or "").strip()
    if not candidate or len(candidate) > MAX_GIT_REF_LENGTH:
        raise ValueError("a git ref must be 1..255 characters")
    if not _GIT_REF_PATTERN.match(candidate):
        raise ValueError("that git ref is not a well-formed ref name")
    return candidate


def validated_repo_path(value: str) -> str:
    """Accept one repository-relative file path, or raise ``ValueError``."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > MAX_CHANGED_FILE_PATH_LENGTH:
        raise ValueError("a changed-file path must be 1..512 characters")
    if not _REPO_PATH_PATTERN.match(candidate):
        raise ValueError("a changed-file path must be repository-relative")
    return candidate


class _Base(BaseModel):
    """Shared strict configuration for every console model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    @field_validator("*", mode="after")
    @classmethod
    def _normalize_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return _require_utc(value)
        return value


# =====================================================================
# Closed enums
# =====================================================================


class ReviewStatus(str, Enum):
    """Durable review lifecycle. Closed set; unknown strings fail."""

    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    TRIAGED = "triaged"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    CHANGES_PROPOSED = "changes_proposed"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    BLOCKED = "blocked"
    WONT_FIX = "wont_fix"


class ObservationType(str, Enum):
    """Root-cause taxonomy. Distinct from the legacy sheet ``Type`` column."""

    CORRECT = "correct"
    KNOWLEDGE_GAP = "knowledge_gap"
    KNOWLEDGE_CONFLICT = "knowledge_conflict"
    RETRIEVAL_MISS = "retrieval_miss"
    CHUNKING_OR_METADATA = "chunking_or_metadata"
    PROMPT_INSTRUCTION = "prompt_instruction"
    ORCHESTRATION_LOGIC = "orchestration_logic"
    SOURCE_DATA = "source_data"
    PRIVACY_OR_COMPLIANCE = "privacy_or_compliance"
    OTHER = "other"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RemediationTarget(str, Enum):
    KB = "kb"
    PROMPT = "prompt"
    CODE = "code"
    WORKFLOW = "workflow"
    SOURCE_DATA = "source_data"
    NONE = "none"
    UNKNOWN = "unknown"


class CorrelationStatus(str, Enum):
    LINKED = "linked"
    MANUAL = "manual"
    UNAVAILABLE = "unavailable"


class CorrelationTrust(str, Enum):
    NONE = "none"
    CANDIDATE = "candidate"
    VERIFIED_WORKLOAD = "verified_workload"
    MANUAL_REVIEWER = "manual_reviewer"


class ImportState(str, Enum):
    ACTIVE = "active"
    REVERSED = "reversed"


class ReviewerRole(str, Enum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    REMEDIATOR = "remediator"
    ADMIN = "admin"
    AGENT = "agent"


class BatchStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    CLAIMED = "claimed"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    CHANGES_PROPOSED = "changes_proposed"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ResolutionOutcome(str, Enum):
    FIXED = "fixed"
    NO_CHANGE = "no_change"
    DUPLICATE = "duplicate"
    ACCEPTED_RISK = "accepted_risk"


class TimelineEntryKind(str, Enum):
    """Normalized timeline entry classes.

    ``UNSUPPORTED`` is how an unknown remote type is preserved without either
    crashing or being mistaken for an authored reply.
    """

    COMMENT = "comment"
    CHANGE_EVENT = "change_event"
    UNSUPPORTED = "unsupported"


class TimelineVisibility(str, Enum):
    PRIVATE = "private"
    INTERNAL = "internal"
    EXTERNAL = "external"
    PUBLIC = "public"


class DevRevActorType(str, Enum):
    DEV_USER = "dev_user"
    REV_USER = "rev_user"
    SYS_USER = "sys_user"
    AUTOMATION = "automation"
    UNKNOWN = "unknown"


class MessageActorClass(str, Enum):
    """Who authored a normalized conversation entry.

    Derived in the service layer from configured identity sets and DevRev
    actor *types* only. A display name is never sufficient evidence: a Rev user
    may be called "Support Bot" and an automation may be called "Ana", so an
    ambiguous entry becomes :attr:`UNKNOWN` rather than a guess.
    """

    PARTICIPANT = "participant"
    HUMAN_AGENT = "human_agent"
    AI_OR_SYSTEM = "ai_or_system"
    EVENT = "event"
    UNKNOWN = "unknown"


class MessageClassificationBasis(str, Enum):
    """The evidence that produced a :class:`MessageActorClass`.

    Recorded so the UI and the tests can prove a classification came from an
    identity/type and never from a display-name string match.
    """

    CONFIGURED_AI_AUTHOR_ID = "configured_ai_author_id"
    CONFIGURED_SYSTEM_AUTHOR_ID = "configured_system_author_id"
    CONFIGURED_HUMAN_AUTHOR_ID = "configured_human_author_id"
    EXTERNAL_ACTOR_TYPE = "external_actor_type"
    SYSTEM_ACTOR_TYPE = "system_actor_type"
    INTERNAL_ACTOR_TYPE = "internal_actor_type"
    CHANGE_EVENT = "change_event"
    AMBIGUOUS = "ambiguous"
    NO_AUTHOR = "no_author"


class MessageRendering(str, Enum):
    """How the UI may render a normalized body.

    ``PLACEHOLDER`` is how an unmodelled or non-plain-text body survives
    without ever handing markup to the browser.
    """

    TEXT = "text"
    PLACEHOLDER = "placeholder"


class CacheState(str, Enum):
    """Whether the bounded message cache behaved on this request.

    ``DEGRADED`` exists so a failed cache write can be reported without
    failing an otherwise valid read.
    """

    FRESH = "fresh"
    DEGRADED = "degraded"
    DISABLED = "disabled"


# =====================================================================
# Identity
# =====================================================================


class ReviewerIdentity(_Base):
    """An application identity resolved from a verified IAP assertion.

    This is *not* the audit actor. Audit actors are recorded independently on
    every :class:`AuditEvent`, and the historical reviewer name lives in
    ``TicketReview.legacy_reviewer_display_name``.
    """

    subject: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    email: str = Field(..., min_length=3, max_length=MAX_EMAIL_LENGTH)
    display_name: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)

    @field_validator("subject", mode="before")
    @classmethod
    def _require_subject(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("IAP subject is required")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if not _EMAIL_PATTERN.match(normalized):
            raise ValueError("email is not a valid address")
        return normalized


class DevRevActor(_Base):
    """A DevRev-side author. Classification happens in the service layer."""

    actor_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    actor_type: DevRevActorType = Field(default=DevRevActorType.UNKNOWN)
    display_name: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)


# =====================================================================
# Live DevRev models (title and body may exist here; never durably)
# =====================================================================


class DevRevTicketSummary(_Base):
    """One row of a live ``works.list`` page. Cache/live only."""

    devrev_work_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    devrev_display_id: str = Field(..., min_length=1, max_length=MAX_DISPLAY_ID_LENGTH)
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)
    stage: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    state: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    severity: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    applies_to_part: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    ticket_visibility: Optional[StrictInt] = Field(default=None)
    source_channel: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    subtype: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    object_version: Optional[StrictInt] = Field(default=None, ge=0)
    owner_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    reporter: Optional[DevRevActor] = Field(default=None)
    tag_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    created_at: Optional[AwareDatetime] = Field(default=None)
    modified_at: Optional[AwareDatetime] = Field(default=None)


class DevRevTicketDetail(DevRevTicketSummary):
    """A live ``works.get`` object. Never persisted to a durable review."""

    body: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_BODY_LENGTH)
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    timeline_entry_count: Optional[StrictInt] = Field(default=None, ge=0)


class DevRevTimelineEntry(_Base):
    """One normalized timeline entry.

    A change event carries no ``body``/``author`` so it can never be rendered
    as a participant reply.
    """

    entry_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    object_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    kind: TimelineEntryKind = Field(...)
    # Fail closed: DevRev omits `visibility` on change events and on entry
    # types we do not model, so an absent value becomes the most restrictive
    # one rather than making the entry unrepresentable or defaulting to public.
    visibility: TimelineVisibility = Field(default=TimelineVisibility.PRIVATE)
    body: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_BODY_LENGTH)
    body_type: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    author: Optional[DevRevActor] = Field(default=None)
    thread_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    in_reply_to: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    change_summary: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    unsupported_type: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    created_at: AwareDatetime = Field(...)
    modified_at: Optional[AwareDatetime] = Field(default=None)


class NormalizedMessage(_Base):
    """One classified, render-safe conversation entry.

    A separate wrapper rather than extra fields on
    :class:`DevRevTimelineEntry`, which is ``extra="forbid"`` and owned by the
    Stage 2 adapter. Three invariants matter:

    * ``actor_class``/``basis`` come from identities and DevRev actor types,
      never from a display-name string match;
    * ``participant_facing`` is false for every internal/private entry, so a
      caller cannot accidentally splice an agent-only note into the reply;
    * ``body`` is populated only for a plain-text body. Anything else becomes
      a :attr:`MessageRendering.PLACEHOLDER` with metadata, so no remote markup
      reaches the UI.
    """

    entry_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    object_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    kind: TimelineEntryKind = Field(...)
    visibility: TimelineVisibility = Field(default=TimelineVisibility.PRIVATE)
    actor_class: MessageActorClass = Field(default=MessageActorClass.UNKNOWN)
    basis: MessageClassificationBasis = Field(default=MessageClassificationBasis.AMBIGUOUS)
    actor: Optional[DevRevActor] = Field(default=None)
    internal: bool = Field(default=True)
    participant_facing: bool = Field(default=False)
    rendering: MessageRendering = Field(default=MessageRendering.PLACEHOLDER)
    body: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_BODY_LENGTH)
    body_type: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    placeholder_reason: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    body_length: StrictInt = Field(default=0, ge=0)
    change_summary: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    unsupported_type: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    thread_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    in_reply_to: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    created_at: AwareDatetime = Field(...)
    modified_at: Optional[AwareDatetime] = Field(default=None)

    @model_validator(mode="after")
    def _placeholder_carries_no_body(self) -> NormalizedMessage:
        if self.rendering is MessageRendering.PLACEHOLDER and self.body is not None:
            raise ValueError("a placeholder message must not carry a rendered body")
        if self.rendering is MessageRendering.TEXT and self.placeholder_reason is not None:
            raise ValueError("a rendered message must not carry a placeholder reason")
        if self.participant_facing and self.internal:
            raise ValueError("an internal message can never be participant facing")
        return self


class DevRevTicketFilters(_Base):
    """The allowlisted ``works.list`` filter surface.

    ``type`` is deliberately absent: the client always forces
    ``type=["ticket"]`` and a caller may never widen it.
    """

    stage: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    state: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    applies_to_part: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    owned_by: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    created_by: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    reported_by: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    tags: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    created_date: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    modified_date: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    ticket_source_channel: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    ticket_subtype: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    ticket_visibility: list[StrictInt] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


# =====================================================================
# RAG provenance
# =====================================================================


class ObservedChunkRef(_Base):
    """A reference to an ALREADY EXISTING Pinecone vector.

    This model never mints or reformats a vector ID; it records what was
    observed at query time. Chunk IDs are not claimed to be stable across
    reindexing.
    """

    observed_vector_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    article_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    content_sha256: Sha256Hex = Field(...)
    chunk_ordinal: Annotated[int, BeforeValidator(_reject_non_integer)] = Field(..., ge=0)
    namespace: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    score: Optional[float] = Field(default=None)


class RagProvenance(_Base):
    """Available RAG evidence. Defaults assert nothing rather than guess."""

    correlation_status: CorrelationStatus = Field(default=CorrelationStatus.UNAVAILABLE)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.NONE)
    correlation_source: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    missing_provenance: bool = Field(default=True)
    index_name: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    index_version: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    namespace: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    deployed_revision: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    prompt_template_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    prompt_template_sha256: Optional[Sha256Hex] = Field(default=None)
    response_sha256: Optional[Sha256Hex] = Field(default=None)
    observed_chunks: list[ObservedChunkRef] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )


class EvidenceLink(_Base):
    """A reviewer-confirmed link between a review and sanitized RAG evidence."""

    link_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    review_id: Sha256Hex = Field(...)
    evidence_reference: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    evidence_digest: Sha256Hex = Field(...)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)
    linked_by: ReviewerIdentity = Field(...)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.MANUAL_REVIEWER)
    source_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    linked_at: AwareDatetime = Field(default_factory=utc_now)
    version: StrictInt = Field(default=1, ge=1)

    @field_validator("source_url")
    @classmethod
    def _https_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("source_url must use https")
        return value


# =====================================================================
# Verification and resolution
# =====================================================================


class VerificationEvidence(_Base):
    """A bounded record of one verification command. Never full output."""

    command_label: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH * 4)
    exit_code: StrictInt = Field(...)
    passed: StrictInt = Field(default=0, ge=0)
    failed: StrictInt = Field(default=0, ge=0)
    skipped: StrictInt = Field(default=0, ge=0)
    output_sha256: Sha256Hex = Field(...)
    runtime_s: float = Field(..., ge=0.0)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)


class ReviewResolution(_Base):
    """The closed object a terminal review must carry."""

    outcome: ResolutionOutcome = Field(...)
    batch_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    plan_artifact: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    branch: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    commit_sha: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    pr_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    test_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    verification_summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    verified_by: Optional[ReviewerIdentity] = Field(default=None)
    verified_at: Optional[AwareDatetime] = Field(default=None)
    no_change_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)

    @field_validator("pr_url")
    @classmethod
    def _https_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("pr_url must use https")
        return value

    @model_validator(mode="after")
    def _non_fixed_needs_a_reason(self) -> ReviewResolution:
        if self.outcome is not ResolutionOutcome.FIXED and not self.no_change_reason:
            raise ValueError("no_change_reason is required when the outcome is not 'fixed'")
        return self

    def has_defensible_verification(self) -> bool:
        """True when the resolution carries evidence or an explicit reason."""
        return bool(self.test_evidence) or bool(self.no_change_reason)


# =====================================================================
# Durable review
# =====================================================================


class TicketReview(_Base):
    """The durable, structured review record.

    Deliberately absent: any DevRev title, message body, participant name, or
    raw remote payload. Those remain live/cache-only data.
    """

    schema_version: str = Field(default=SCHEMA_VERSION, max_length=MAX_TOPIC_LENGTH)
    review_id: Sha256Hex = Field(...)
    devrev_work_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    devrev_display_id: str = Field(..., min_length=1, max_length=MAX_DISPLAY_ID_LENGTH)
    devrev_object_version: Optional[StrictInt] = Field(default=None, ge=0)

    # Legacy sheet fields, preserved as first-class data.
    topic: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    legacy_type: Optional[str] = Field(default=None, max_length=MAX_LEGACY_TYPE_LENGTH)
    rating: Optional[Rating] = Field(default=None)
    legacy_reviewer_display_name: Optional[str] = Field(
        default=None, max_length=MAX_DISPLAY_NAME_LENGTH
    )
    comments: Optional[str] = Field(default=None, max_length=MAX_COMMENTS_LENGTH)

    # New structured judgment.
    observation_type: Optional[ObservationType] = Field(default=None)
    expected_behavior: Optional[str] = Field(default=None, max_length=MAX_EXPECTED_BEHAVIOR_LENGTH)
    severity: Optional[Severity] = Field(default=None)
    status: ReviewStatus = Field(default=ReviewStatus.UNREVIEWED)
    remediation_target: RemediationTarget = Field(default=RemediationTarget.UNKNOWN)
    assigned_reviewer: Optional[ReviewerIdentity] = Field(default=None)

    # Evidence correlation.
    correlation_status: CorrelationStatus = Field(default=CorrelationStatus.UNAVAILABLE)
    correlation_source: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.NONE)
    ticket_job_ids: list[str] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )
    request_id_hashes: list[Sha256Hex] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )
    source_article_ids: list[str] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )
    chunk_refs: list[ObservedChunkRef] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )
    pipeline_provenance: RagProvenance = Field(default_factory=RagProvenance)

    # Lifecycle, retention, and concurrency.
    resolution: Optional[ReviewResolution] = Field(default=None)
    import_state: ImportState = Field(default=ImportState.ACTIVE)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    last_devrev_sync_at: Optional[AwareDatetime] = Field(default=None)
    resolved_at: Optional[AwareDatetime] = Field(default=None)


class ReviewPatch(_Base):
    """The only mutable surface of a review.

    Immutable identifiers, the version, and server timestamps are absent, so
    ``extra="forbid"`` rejects them in the request body. The version travels in
    the quoted ``If-Match`` header instead.
    """

    topic: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    legacy_type: Optional[str] = Field(default=None, max_length=MAX_LEGACY_TYPE_LENGTH)
    observation_type: Optional[ObservationType] = Field(default=None)
    rating: Optional[Rating] = Field(default=None)
    comments: Optional[str] = Field(default=None, max_length=MAX_COMMENTS_LENGTH)
    expected_behavior: Optional[str] = Field(default=None, max_length=MAX_EXPECTED_BEHAVIOR_LENGTH)
    severity: Optional[Severity] = Field(default=None)
    status: Optional[ReviewStatus] = Field(default=None)
    remediation_target: Optional[RemediationTarget] = Field(default=None)
    assigned_reviewer: Optional[ReviewerIdentity] = Field(default=None)
    resolution: Optional[ReviewResolution] = Field(default=None)


# =====================================================================
# Append-only, hash-chained audit
# =====================================================================


class AuditEvent(_Base):
    """One application-append-only, tamper-evident audit record.

    Frozen at the model level: the repository appends, it never updates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    parent_kind: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    parent_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    event_type: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    actor_subject: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    actor_email: Optional[str] = Field(default=None, max_length=MAX_EMAIL_LENGTH)
    actor_subject_hash: Sha256Hex = Field(...)
    request_id_hash: Optional[Sha256Hex] = Field(default=None)
    idempotency_key_hash: Optional[Sha256Hex] = Field(default=None)
    hash_schema_version: StrictInt = Field(default=HASH_SCHEMA_VERSION, ge=1)
    occurred_at_unix_us: StrictInt = Field(..., ge=0)
    previous_version: Optional[StrictInt] = Field(default=None, ge=0)
    new_version: Optional[StrictInt] = Field(default=None, ge=0)
    changed_fields: list[str] = Field(default_factory=list, max_length=MAX_CHANGED_FIELDS)
    metadata: dict[str, str] = Field(default_factory=dict)
    # Genesis is 64 ASCII zeroes, which is itself valid lowercase hex, so the
    # chain link gets the same pattern as event_hash rather than a bare length
    # check that would accept uppercase or non-hex text.
    previous_event_hash: Sha256Hex = Field(default=GENESIS_EVENT_HASH)
    event_hash: Optional[Sha256Hex] = Field(default=None)

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep ``metadata`` operational, never a smuggling route for content.

        Only ``reason_code`` enters the frozen audit hash payload, so an
        unbounded free-text map here would be both a PII path into the durable
        ledger and a field the tamper-evident chain does not cover.
        """
        if len(value) > MAX_METADATA_KEYS:
            raise ValueError(f"metadata accepts at most {MAX_METADATA_KEYS} keys")
        for key, item in value.items():
            if len(key) > MAX_METADATA_KEY_LENGTH:
                raise ValueError(f"metadata key exceeds {MAX_METADATA_KEY_LENGTH} characters")
            if len(item) > MAX_METADATA_VALUE_LENGTH:
                raise ValueError(
                    f"metadata value for {key!r} exceeds "
                    f"{MAX_METADATA_VALUE_LENGTH} characters"
                )
        return value


def audit_event_hash_payload(event: AuditEvent) -> dict[str, Any]:
    """Build the frozen canonical hash input for an audit event.

    Exactly these literal keys, always present (JSON ``null`` when absent).
    Raw actor email/subject, display text, ``created_at``, and every
    later-enriched field are deliberately excluded.
    """

    def _nfc(value: Optional[str]) -> Optional[str]:
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value

    changed = sorted({unicodedata.normalize("NFC", f) for f in event.changed_fields})
    return {
        "hash_schema_version": event.hash_schema_version,
        "event_id": _nfc(event.event_id),
        "parent_kind": _nfc(event.parent_kind),
        "parent_id": _nfc(event.parent_id),
        "event_type": _nfc(event.event_type),
        "actor_subject_hash": event.actor_subject_hash,
        "request_id_hash": event.request_id_hash,
        "idempotency_key_hash": event.idempotency_key_hash,
        "previous_version": event.previous_version,
        "new_version": event.new_version,
        "changed_fields": changed,
        "reason_code": _nfc(event.metadata.get("reason_code")),
        "previous_event_hash": event.previous_event_hash,
        "occurred_at_unix_us": event.occurred_at_unix_us,
    }


def compute_audit_event_hash(event: AuditEvent) -> str:
    """Return the lowercase hex SHA-256 of the canonical hash payload."""
    payload = audit_event_hash_payload(event)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# =====================================================================
# Remediation batches
# =====================================================================


class BatchLease(_Base):
    """An agent's bounded claim on a batch.

    Only the token *hash* is ever persisted; the raw lease token is returned
    once at claim time and never stored or logged.
    """

    lease_token_hash: Sha256Hex = Field(...)
    holder: str = Field(..., min_length=1, max_length=MAX_EMAIL_LENGTH)
    acquired_at: AwareDatetime = Field(...)
    expires_at: AwareDatetime = Field(...)
    last_heartbeat_at: AwareDatetime = Field(...)
    continuous_since: Optional[AwareDatetime] = Field(default=None)

    @model_validator(mode="after")
    def _bounded_window(self) -> BatchLease:
        window = (self.expires_at - self.acquired_at).total_seconds()
        if window <= 0:
            raise ValueError("lease expires_at must be after acquired_at")
        if window > REMEDIATION_LEASE_S:
            raise ValueError(f"lease window must not exceed {REMEDIATION_LEASE_S} seconds")
        return self

    def continuous_cap_reached(self, *, at: datetime) -> bool:
        """True once continuous renewal has reached the two-hour cap."""
        started = self.continuous_since or self.acquired_at
        elapsed = (_require_utc(at) - started).total_seconds()
        return elapsed >= REMEDIATION_MAX_CONTINUOUS_LEASE_S


class BatchOutcome(_Base):
    """Bounded human decision recorded when a batch leaves the agent path."""

    decision: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    extension_minutes: Optional[StrictInt] = Field(
        default=None, ge=1, le=MAX_LEASE_EXTENSION_MINUTES
    )


class RemediationBatchItem(_Base):
    """One frozen ``(review_id, review_version)`` pair.

    Stored as ``remediation_batches/{batch_id}/items/{review_id}`` so the
    parent never grows with the item count.
    """

    review_id: Sha256Hex = Field(..., frozen=True)
    review_version: StrictInt = Field(..., ge=1, frozen=True)
    devrev_display_id: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_ID_LENGTH)
    observation_type: Optional[ObservationType] = Field(default=None)
    severity: Optional[Severity] = Field(default=None)
    remediation_target: Optional[RemediationTarget] = Field(default=None)
    comments_excerpt: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    outcome: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    #: The agent's own bounded note for this one review. It lives on the item
    #: document rather than the parent so a 100-review batch cannot grow the
    #: parent past the Firestore document limit.
    outcome_summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    #: The independent human verdict, recorded only by ``:complete``. Kept
    #: separate from ``outcome`` so an agent claim and a human decision are
    #: never conflated when this record is read back years later.
    verified_outcome: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)


class RemediationBatch(_Base):
    """The batch parent: counts, status, digests, and lease summary only.

    There is intentionally no ``items``/``review_refs`` array — see the Stage 1
    ADR and ``TestBatchDocumentBudget``.
    """

    batch_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    schema_version: str = Field(default=SCHEMA_VERSION, max_length=MAX_TOPIC_LENGTH)
    status: BatchStatus = Field(default=BatchStatus.DRAFT)
    created_by: ReviewerIdentity = Field(...)
    item_count: StrictInt = Field(..., ge=0, le=MAX_BATCH_REVIEWS)
    item_set_digest: Sha256Hex = Field(...)
    lease: Optional[BatchLease] = Field(default=None)
    #: Who last claimed this batch. Retained after the lease is invalidated
    #: because the independent-verifier rule has to be enforced at
    #: ``:start-verification`` and ``:complete`` — long after submission cleared
    #: the lease. Without this the rule would silently pass for everyone.
    claimed_by: Optional[ReviewerIdentity] = Field(default=None)
    plan_artifact: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    branch: Optional[str] = Field(default=None, max_length=MAX_GIT_REF_LENGTH)
    commit_sha: Optional[str] = Field(default=None, pattern=GIT_COMMIT_SHA_PATTERN)
    #: Why there is no commit. Exactly one of ``commit_sha``/this field is
    #: required to submit: "the work is not committed" is an acceptable state to
    #: report and an unacceptable one to leave unexplained.
    uncommitted_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    changed_files: list[str] = Field(default_factory=list, max_length=MAX_CHANGED_FIELDS)
    pr_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    # The master plan's Firestore contract requires the batch parent to store
    # "bounded test evidence" alongside the plan/branch/commit references.
    test_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    verification_summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    #: The independent human's evidence, kept apart from the agent's
    #: ``test_evidence`` so "the agent says its tests passed" can never be read
    #: as "a second person checked".
    verification_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    #: What the independent verifier asserted when they took the batch on. Kept
    #: durably rather than only in the audit metadata because ``metadata`` values
    #: are capped at 200 characters and this is the record that says a second
    #: person looked, and on what basis.
    verification_attestation: Optional[str] = Field(
        default=None, max_length=MAX_REASON_LENGTH
    )
    verified_by: Optional[ReviewerIdentity] = Field(default=None)
    verified_at: Optional[AwareDatetime] = Field(default=None)
    outcome: Optional[BatchOutcome] = Field(default=None)
    #: Which prompt text was handed to the agent. The digest is recorded rather
    #: than the prompt so the record proves *which* instructions were issued
    #: without storing a second copy of them.
    prompt_template_sha256: Optional[Sha256Hex] = Field(default=None)
    prompt_template_version: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    prompt_issued_at: Optional[AwareDatetime] = Field(default=None)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("branch")
    @classmethod
    def _well_formed_branch(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validated_git_ref(value)

    @field_validator("changed_files")
    @classmethod
    def _repo_relative_changed_files(cls, value: list[str]) -> list[str]:
        return [validated_repo_path(item) for item in value]


class BatchLeaseSummary(_Base):
    """A lease as a human may see it: the window, never the credential.

    ``BatchLease.lease_token_hash`` is deliberately absent. It is only a digest,
    but a reviewer verifying a batch has no use for it, and the narrower the
    human-facing shape is the fewer ways there are to widen it by accident.
    """

    holder: str = Field(..., min_length=1, max_length=MAX_EMAIL_LENGTH)
    acquired_at: AwareDatetime = Field(...)
    expires_at: AwareDatetime = Field(...)
    last_heartbeat_at: AwareDatetime = Field(...)
    continuous_since: Optional[AwareDatetime] = Field(default=None)


class RemediationBatchView(_Base):
    """The batch as a human role reads it.

    Identical to :class:`RemediationBatch` except that the lease is reduced to
    :class:`BatchLeaseSummary`. Built only by :func:`batch_view`.
    """

    batch_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    schema_version: str = Field(default=SCHEMA_VERSION, max_length=MAX_TOPIC_LENGTH)
    status: BatchStatus = Field(...)
    created_by: ReviewerIdentity = Field(...)
    item_count: StrictInt = Field(..., ge=0, le=MAX_BATCH_REVIEWS)
    item_set_digest: Sha256Hex = Field(...)
    lease: Optional[BatchLeaseSummary] = Field(default=None)
    claimed_by: Optional[ReviewerIdentity] = Field(default=None)
    plan_artifact: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    branch: Optional[str] = Field(default=None, max_length=MAX_GIT_REF_LENGTH)
    commit_sha: Optional[str] = Field(default=None, pattern=GIT_COMMIT_SHA_PATTERN)
    uncommitted_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    changed_files: list[str] = Field(default_factory=list, max_length=MAX_CHANGED_FIELDS)
    pr_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    test_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    verification_summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    verification_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    #: What the independent verifier asserted when they took the batch on. Kept
    #: durably rather than only in the audit metadata because ``metadata`` values
    #: are capped at 200 characters and this is the record that says a second
    #: person looked, and on what basis.
    verification_attestation: Optional[str] = Field(
        default=None, max_length=MAX_REASON_LENGTH
    )
    verified_by: Optional[ReviewerIdentity] = Field(default=None)
    verified_at: Optional[AwareDatetime] = Field(default=None)
    outcome: Optional[BatchOutcome] = Field(default=None)
    prompt_template_sha256: Optional[Sha256Hex] = Field(default=None)
    prompt_template_version: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    prompt_issued_at: Optional[AwareDatetime] = Field(default=None)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(..., ge=1)
    created_at: AwareDatetime = Field(...)
    updated_at: AwareDatetime = Field(...)


def batch_view(batch: RemediationBatch) -> RemediationBatchView:
    """Project a batch onto its human-readable view, dropping the token hash."""
    payload = batch.model_dump()
    lease = payload.pop("lease", None)
    summary = (
        None
        if not lease
        else BatchLeaseSummary(
            holder=lease["holder"],
            acquired_at=lease["acquired_at"],
            expires_at=lease["expires_at"],
            last_heartbeat_at=lease["last_heartbeat_at"],
            continuous_since=lease.get("continuous_since"),
        )
    )
    return RemediationBatchView(lease=summary, **payload)


# =====================================================================
# Cursor pagination envelopes
# =====================================================================

T = TypeVar("T")


class CursorPage(_Base, Generic[T]):
    """A bounded page with opaque forward/backward cursors.

    ``partial``/``truncated`` exist so a guarded iterator result is never
    labelled complete.
    """

    items: list[T] = Field(default_factory=list, max_length=MAX_PAGE_SIZE)
    next_cursor: Optional[str] = Field(default=None, max_length=MAX_CURSOR_LENGTH)
    prev_cursor: Optional[str] = Field(default=None, max_length=MAX_CURSOR_LENGTH)
    page_size: StrictInt = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    partial: bool = Field(default=False)
    truncated: bool = Field(default=False)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)

    @model_validator(mode="after")
    def _items_fit_the_declared_page(self) -> CursorPage[T]:
        if len(self.items) > self.page_size:
            raise ValueError("a page cannot carry more items than its declared page_size")
        return self


class TimelinePage(CursorPage[DevRevTimelineEntry]):
    """One bounded, forward-only timeline page (``mode=after`` always)."""


class ClassifiedTimelinePage(TimelinePage):
    """A timeline page whose entries also carry a service classification.

    Subclasses :class:`TimelinePage` so a hydration service can satisfy the
    Stage 4 interface without widening the Stage 2 adapter's return type.
    ``items`` stays the adapter's normalized source order; ``messages`` is the
    parallel classified projection the UI renders.
    """

    messages: list[NormalizedMessage] = Field(
        default_factory=list, max_length=MAX_PAGE_SIZE
    )
    cache_state: CacheState = Field(default=CacheState.FRESH)
    diagnostics: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)

    @model_validator(mode="after")
    def _one_message_per_entry(self) -> ClassifiedTimelinePage:
        if len(self.messages) != len(self.items):
            raise ValueError("every timeline entry needs exactly one classified message")
        return self


class TicketReviewSummary(_Base):
    """The bounded review projection a live list row may carry.

    A list page overlays this and never the full review: it must not become a
    second copy of reviewer comments, resolution evidence, or chunk refs.
    """

    review_id: Sha256Hex = Field(...)
    status: ReviewStatus = Field(...)
    topic: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    legacy_type: Optional[str] = Field(default=None, max_length=MAX_LEGACY_TYPE_LENGTH)
    observation_type: Optional[ObservationType] = Field(default=None)
    rating: Optional[Rating] = Field(default=None)
    severity: Optional[Severity] = Field(default=None)
    remediation_target: RemediationTarget = Field(default=RemediationTarget.UNKNOWN)
    assigned_reviewer_email: Optional[str] = Field(default=None, max_length=MAX_EMAIL_LENGTH)
    correlation_status: CorrelationStatus = Field(default=CorrelationStatus.UNAVAILABLE)
    import_state: ImportState = Field(default=ImportState.ACTIVE)
    updated_at: AwareDatetime = Field(...)
    version: StrictInt = Field(..., ge=1)

    @classmethod
    def of(cls, review: TicketReview) -> TicketReviewSummary:
        """Project a durable review, dropping every unbounded judgment field."""
        return cls(
            review_id=review.review_id,
            status=review.status,
            topic=review.topic,
            legacy_type=review.legacy_type,
            observation_type=review.observation_type,
            rating=review.rating,
            severity=review.severity,
            remediation_target=review.remediation_target,
            assigned_reviewer_email=(
                review.assigned_reviewer.email if review.assigned_reviewer else None
            ),
            correlation_status=review.correlation_status,
            import_state=review.import_state,
            updated_at=review.updated_at,
            version=review.version,
        )


class DevRevTicketWithReviewSummary(_Base):
    """One live list row joined to its durable review, if one exists.

    Composition rather than inheritance: ``DevRevTicketSummary`` and
    ``TicketReview`` both declare ``severity``, ``created_at`` and
    ``devrev_display_id`` with different meanings and types, so merging them
    into one flat model would silently conflate live DevRev state with human
    judgment. ``review=None`` means "no durable review exists" and is never a
    fabricated unreviewed document.
    """

    ticket: DevRevTicketSummary = Field(...)
    review: Optional[TicketReviewSummary] = Field(default=None)


class EvidenceCandidateLink(_Base):
    """A suggested — never stored — correlation between a review and evidence.

    ``candidate_token`` is an authenticated-encrypted server token bound to
    ticket, review, actor, the sanitized broker reference/digest, the broker
    result digest, and an expiry. The caller echoes it back verbatim; it can
    neither be edited into another ticket's evidence nor extended.
    """

    candidate_token: str = Field(..., min_length=1, max_length=MAX_CURSOR_LENGTH)
    evidence_reference: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    evidence_digest: Sha256Hex = Field(...)
    broker_result_digest: Sha256Hex = Field(...)
    rationale: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.CANDIDATE)
    expires_at: AwareDatetime = Field(...)

    @field_validator("correlation_trust")
    @classmethod
    def _a_candidate_is_never_verified(cls, value: CorrelationTrust) -> CorrelationTrust:
        # A suggestion must not be able to describe itself as a verified
        # workload correlation; only the producer's signed path may do that.
        if value is not CorrelationTrust.CANDIDATE:
            raise ValueError("a candidate link is always CorrelationTrust.CANDIDATE")
        return value


class TicketEvidenceSummary(_Base):
    """What can be defended about a ticket's RAG evidence, and nothing more.

    Defaults assert nothing: an absent correlation is ``unavailable`` with an
    explicit reason, never an inferred link.

    ``provenance`` and ``executions`` are two projections of the same broker
    answer, and both are kept because they answer different questions.
    ``provenance`` is the retrieval/index side — what was searched and which
    vectors came back. ``executions`` is the whole sanitized record, which also
    carries *which model on which route answered, when, how long it took, and
    whether it failed*. A reviewer judging an answer needs the second; Stage 4's
    producer path and its tests are written against the first. Projecting only
    ``provenance`` — as this model did before Stage 7 — discarded roughly two
    thirds of an already-allowlisted record before it reached the reviewer.
    """

    correlation_status: CorrelationStatus = Field(default=CorrelationStatus.UNAVAILABLE)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.NONE)
    correlation_source: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    unavailable_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    provenance: list[RagProvenance] = Field(
        default_factory=list, max_length=MAX_BROKER_RESULTS
    )
    executions: list[RagEvidenceRecord] = Field(
        default_factory=list, max_length=MAX_BROKER_RESULTS
    )
    linked_count: StrictInt = Field(default=0, ge=0)
    candidate_links: list[EvidenceCandidateLink] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_CANDIDATES
    )
    broker_available: bool = Field(default=False)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)

    @model_validator(mode="after")
    def _a_candidate_never_implies_a_link(self) -> TicketEvidenceSummary:
        if self.candidate_links and self.correlation_status is CorrelationStatus.LINKED:
            raise ValueError(
                "a suggestion must not be reported alongside an automatic link"
            )
        return self


class TicketDetailEnvelope(_Base):
    """One ticket detail: live DevRev data, durable review, and evidence.

    Every component is optional and each failure is explicit. A DevRev outage
    yields ``ticket=None`` with ``partial=True``; it never erases ``review``
    and never lets the envelope claim completeness.
    """

    ticket_ref: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    ticket: Optional[DevRevTicketDetail] = Field(default=None)
    review: Optional[TicketReview] = Field(default=None)
    timeline: Optional[ClassifiedTimelinePage] = Field(default=None)
    evidence: TicketEvidenceSummary = Field(default_factory=TicketEvidenceSummary)
    partial: bool = Field(default=False)
    cache_state: CacheState = Field(default=CacheState.FRESH)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)
    diagnostics: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)

    @model_validator(mode="after")
    def _missing_live_data_is_always_partial(self) -> TicketDetailEnvelope:
        if self.ticket is None and not self.partial:
            raise ValueError("an envelope without live ticket data is partial by definition")
        return self


# =====================================================================
# Durable ticket-associated RAG executions
# =====================================================================


class DevRevHydrationStatus(str, Enum):
    """State of the bounded DevRev enrichment for one persisted execution."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DevRevAuthorizationStatus(str, Enum):
    """Whether DevRev has validated this execution's ticket scope.

    Persist-first records remain quarantined until ``works.get`` proves both
    existence and configured scope.  ``denied`` is reserved for an explicit
    not-found or scope decision; outages and configuration failures never
    become an authorization verdict.
    """

    QUARANTINED = "quarantined"
    AUTHORIZED = "authorized"
    DENIED = "denied"


class TicketEvaluationRun(_Base):
    """One immutable RAG event plus mutable, retryable DevRev hydration state."""

    execution_id: str = Field(..., min_length=3, max_length=160)
    event: TicketEvaluationEvent = Field(...)
    event_digest: Sha256Hex = Field(...)
    review_id: Optional[Sha256Hex] = Field(default=None)
    devrev_work_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    devrev_display_id: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_ID_LENGTH)
    ticket_snapshot: Optional[DevRevTicketSummary] = Field(default=None)
    ticket_detail_snapshot: Optional[DevRevTicketDetail] = Field(default=None)
    hydration_status: DevRevHydrationStatus = Field(default=DevRevHydrationStatus.PENDING)
    authorization_status: DevRevAuthorizationStatus = Field(
        default=DevRevAuthorizationStatus.QUARANTINED
    )
    hydration_attempts: StrictInt = Field(default=0, ge=0)
    hydration_retryable: bool = Field(default=True)
    hydration_error_code: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    last_hydration_attempt_at: Optional[AwareDatetime] = Field(default=None)
    next_hydration_attempt_at: Optional[AwareDatetime] = Field(default=None)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _infer_authorized_legacy_success(cls, value: Any) -> Any:
        """Read pre-authorization-state successful records without widening them."""
        if not isinstance(value, Mapping) or "authorization_status" in value:
            return value
        hydration = value.get("hydration_status")
        if hydration in {
            DevRevHydrationStatus.SUCCEEDED,
            DevRevHydrationStatus.SUCCEEDED.value,
        }:
            return {**value, "authorization_status": DevRevAuthorizationStatus.AUTHORIZED}
        return value

    @model_validator(mode="after")
    def _identity_and_hydration_are_consistent(self) -> TicketEvaluationRun:
        if self.execution_id != self.event.execution_id:
            raise ValueError("stored execution_id must match the immutable event")
        if self.event_digest != self.event.canonical_digest():
            raise ValueError("event_digest must match the immutable event")
        if self.hydration_status is DevRevHydrationStatus.SUCCEEDED:
            if not all((self.review_id, self.devrev_work_id, self.devrev_display_id)):
                raise ValueError("successful hydration requires linked DevRev identifiers")
            if self.hydration_error_code or self.next_hydration_attempt_at:
                raise ValueError("successful hydration cannot retain retry state")
            if self.authorization_status is not DevRevAuthorizationStatus.AUTHORIZED:
                raise ValueError("successful hydration must authorize the DevRev scope")
        if self.authorization_status is DevRevAuthorizationStatus.AUTHORIZED:
            if self.hydration_status is not DevRevHydrationStatus.SUCCEEDED:
                raise ValueError("authorized DevRev scope requires successful hydration")
        if self.authorization_status is DevRevAuthorizationStatus.DENIED:
            if self.hydration_status is not DevRevHydrationStatus.FAILED:
                raise ValueError("denied DevRev scope requires a failed hydration")
            if self.hydration_retryable or self.next_hydration_attempt_at is not None:
                raise ValueError("denied DevRev scope cannot retain retry state")
        return self


class TicketEvaluationSummary(_Base):
    """Bounded list projection; one row always means one persisted RAG run."""

    execution_id: str = Field(..., min_length=3, max_length=160)
    invocation_id: str = Field(..., min_length=3, max_length=160)
    job_id: str = Field(..., min_length=1, max_length=128)
    inquiry_index: StrictInt = Field(..., ge=0, le=99)
    attempt: Optional[StrictInt] = Field(
        default=None, ge=1, le=2_147_483_647,
    )
    lease_epoch: Optional[StrictInt] = Field(
        default=None, ge=1, le=2_147_483_647,
    )
    ticket_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    devrev_work_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    devrev_display_id: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_ID_LENGTH)
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)
    route: EvaluationRoute = Field(...)
    status: EvaluationStatus = Field(...)
    hydration_status: DevRevHydrationStatus = Field(...)
    classification_reasoning: str = Field(..., min_length=1, max_length=8_000)
    generated_answer_excerpt: Optional[str] = Field(default=None, max_length=500)
    occurred_at: AwareDatetime = Field(...)
    review: Optional[TicketReviewSummary] = Field(default=None)

    @classmethod
    def of(
        cls,
        run: TicketEvaluationRun,
        review: Optional[TicketReview] = None,
    ) -> TicketEvaluationSummary:
        answer = run.event.answer
        return cls(
            execution_id=run.execution_id,
            invocation_id=run.event.invocation_id or run.execution_id,
            job_id=run.event.job_id,
            inquiry_index=run.event.inquiry_index,
            attempt=run.event.attempt,
            lease_epoch=run.event.lease_epoch,
            ticket_id=run.event.ticket_id,
            devrev_work_id=run.devrev_work_id,
            devrev_display_id=run.devrev_display_id,
            title=(run.ticket_snapshot.title if run.ticket_snapshot else None),
            route=run.event.route,
            status=run.event.status,
            hydration_status=run.hydration_status,
            classification_reasoning=run.event.classification.reasoning,
            generated_answer_excerpt=(answer[:500] if answer else None),
            occurred_at=run.event.occurred_at,
            review=TicketReviewSummary.of(review) if review else None,
        )


class TicketEvaluationDetailEnvelope(_Base):
    """Persisted execution, review, and its scope-validated ticket snapshot."""

    execution: TicketEvaluationRun = Field(...)
    review: Optional[TicketReview] = Field(default=None)
    ticket: Optional[DevRevTicketDetail] = Field(default=None)
    generated_answer: Optional[str] = Field(default=None, max_length=100_000)
    classification_reasoning: str = Field(..., min_length=1, max_length=8_000)
    outcome_reason: Optional[str] = Field(default=None, max_length=20_000)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    gaps: list[
        Annotated[str, Field(min_length=1, max_length=MAX_REASON_LENGTH)]
    ] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    source_articles: list[SourceReference] = Field(default_factory=list, max_length=50)
    chunk_evidence: list[ChunkEvidence] = Field(default_factory=list, max_length=20)
    model_metadata: dict[str, Any] = Field(default_factory=dict)
    timing_metadata: dict[str, Any] = Field(default_factory=dict)
    hydration_status: DevRevHydrationStatus = Field(...)
    partial: bool = Field(default=False)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)


# =====================================================================
# Evidence broker transport (shared by the console and the broker app)
# =====================================================================
#
# These models live here rather than in either service so the broker app can
# import them without reaching into the console repository, and so the
# console cannot drift from the envelope the broker actually returns.


class EvidenceSourceCollection(str, Enum):
    """The only ``(default)`` collections the broker may ever read."""

    EXECUTION_LOGS = "execution_logs"
    TICKET_EXECUTIONS = "ticket_executions"
    TICKET_JOBS = "ticket_jobs"


class MissingProvenance(str, Enum):
    """Explicit gaps. A gap is rendered as a gap, never inferred as fact."""

    INDEX_VERSION = "index_version"
    DEPLOYED_REVISION = "deployed_revision"
    PROMPT_TEMPLATE = "prompt_template"
    MODEL = "model"
    OBSERVED_CHUNKS = "observed_chunks"
    RESPONSE_HASH = "response_hash"
    SOURCE_ARTICLES = "source_articles"
    LEGACY_SCHEMA = "legacy_schema"


class RagEvidenceRecord(_Base):
    """One sanitized, allowlisted execution record.

    Deliberately absent: prompts, responses, chunk text, participant data,
    API keys, and any raw external identifier. ``evidence_reference`` is an
    opaque server digest, not a Firestore path a caller could dereference.
    """

    evidence_reference: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    evidence_digest: Sha256Hex = Field(...)
    source_collection: EvidenceSourceCollection = Field(...)
    schema_version: StrictInt = Field(default=LEGACY_EXECUTION_LOG_SCHEMA_VERSION, ge=0)
    occurred_at: Optional[AwareDatetime] = Field(default=None)
    endpoint: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    correlation_source: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.NONE)
    lookup_key_version: Optional[StrictInt] = Field(default=None, ge=1)
    ingress_key_version: Optional[StrictInt] = Field(default=None, ge=1)
    internal_job_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    request_id_hash: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    principal_hash: Optional[Sha256Hex] = Field(default=None)
    # Generation identity. ``RagProvenance`` predates Stage 4 and models the
    # retrieval/index side only, so these live here rather than being forced
    # into it: which model answered is evidence about the *execution*, not
    # about the index it read.
    model: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    provider: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    route: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    config_version: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    # Labelled at the field, not just in prose: a trace hash correlates one
    # rendered prompt and is never a template version.
    rendered_prompt_trace_sha256: Optional[Sha256Hex] = Field(default=None)
    deployed_commit_sha: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    deployed_image_digest: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    provenance: RagProvenance = Field(default_factory=RagProvenance)
    source_article_ids: list[str] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFS_PER_REVIEW
    )
    duration_ms: Optional[float] = Field(default=None, ge=0.0)
    failed: Optional[bool] = Field(default=None)
    missing: list[MissingProvenance] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class RagEvidenceEnvelope(_Base):
    """The broker's whole answer. Bounded, allowlisted, and self-describing."""

    correlation_status: CorrelationStatus = Field(default=CorrelationStatus.UNAVAILABLE)
    records: list[RagEvidenceRecord] = Field(
        default_factory=list, max_length=MAX_BROKER_RESULTS
    )
    result_digest: Sha256Hex = Field(...)
    key_versions_queried: list[StrictInt] = Field(
        default_factory=list, max_length=MAX_BROKER_KEY_VERSIONS
    )
    truncated: bool = Field(default=False)
    unavailable_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)

    @model_validator(mode="after")
    def _no_records_is_never_linked(self) -> RagEvidenceEnvelope:
        if not self.records and self.correlation_status is not CorrelationStatus.UNAVAILABLE:
            raise ValueError("an empty evidence envelope is always 'unavailable'")
        return self


class TicketEvidenceLookupRequest(_Base):
    """The broker's only request body.

    It carries one bounded, transient DevRev DON from the authenticated
    console service. The broker hashes it in memory and never persists,
    echoes, or logs it.
    """

    devrev_work_id: SecretStr = Field(...)
    max_results: StrictInt = Field(default=MAX_BROKER_RESULTS, ge=1, le=MAX_BROKER_RESULTS)

    @field_validator("devrev_work_id")
    @classmethod
    def _bounded_don(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip():
            raise ValueError("a DevRev work id is required")
        if len(raw) > MAX_ID_LENGTH:
            raise ValueError("the DevRev work id is too long")
        return value


# =====================================================================
# Closed mutation envelopes (master plan, "Closed mutation envelopes")
# =====================================================================
#
# Frozen here rather than in Stage 5/8/9 because those stages' commit
# allowlists cannot all reach this module, and because the plan's rule
# "Unknown envelope fields fail validation" needs a strict model to enforce
# it. Every envelope inherits `_Base`, hence `extra="forbid"`.
#
# Conventions shared by the batch envelopes:
#   * `expected_version` carries optimistic concurrency for agent routes that
#     do not use a browser ETag header;
#   * `lease_token` is a SecretStr so it cannot leak through logs, audit
#     records, or error bodies -- only its hash is ever persisted.


class ReviewRef(_Base):
    """A frozen `(review_id, review_version)` pair used to build a batch."""

    review_id: Sha256Hex = Field(..., frozen=True)
    review_version: StrictInt = Field(..., ge=1, frozen=True)


class SessionResponse(_Base):
    """`GET /session`: the verified user, role, and feature flags."""

    identity: ReviewerIdentity = Field(...)
    role: ReviewerRole = Field(...)
    csrf_token: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    csrf_expires_at: AwareDatetime = Field(...)
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class ErrorBody(_Base):
    """The stable public error shape. Never echoes a remote body or a trace."""

    code: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    message: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)
    request_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    current_version: Optional[StrictInt] = Field(default=None, ge=0)
    changed_at: Optional[AwareDatetime] = Field(default=None)


class ErrorResponse(_Base):
    """`{"error": {...}}` — the single envelope every failure maps to."""

    error: ErrorBody = Field(...)


class CreateReviewRequest(_Base):
    """Idempotently import/create the durable review for a DevRev ticket."""

    topic: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    legacy_type: Optional[str] = Field(default=None, max_length=MAX_LEGACY_TYPE_LENGTH)
    rating: Optional[Rating] = Field(default=None)
    comments: Optional[str] = Field(default=None, max_length=MAX_COMMENTS_LENGTH)


class CreateEvidenceLinkRequest(_Base):
    """Manual evidence link. Never accepts a caller-chosen execution ID.

    The caller may only present a short-lived broker candidate token that the
    server minted after a bounded broker lookup.
    """

    broker_candidate_token: str = Field(..., min_length=1, max_length=MAX_CURSOR_LENGTH)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class DeleteEvidenceLinkRequest(_Base):
    """Versioned unlink. `link_id` is a path parameter; version is `If-Match`."""

    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class CreateRemediationBatchRequest(_Base):
    """Freeze selected review/version pairs into a batch."""

    review_refs: list[ReviewRef] = Field(..., min_length=1, max_length=MAX_BATCH_REVIEWS)
    transition_to_planned: bool = Field(default=False)

    @model_validator(mode="after")
    def _reject_duplicate_reviews(self) -> CreateRemediationBatchRequest:
        seen = {ref.review_id for ref in self.review_refs}
        if len(seen) != len(self.review_refs):
            raise ValueError("review_refs must not contain a duplicate review_id")
        return self


class CreateRemediationBatchResponse(_Base):
    """The frozen batch, plus exactly which reviews the creation moved.

    The two id lists are reported rather than implied: ``transition_to_planned``
    is best-effort by design (a review already past ``planned``, or not yet
    triaged, is left alone), and a caller that could not tell which happened
    would have to re-read every review to find out.
    """

    batch: RemediationBatchView = Field(...)
    planned_review_ids: list[Sha256Hex] = Field(
        default_factory=list, max_length=MAX_BATCH_REVIEWS
    )
    unchanged_review_ids: list[Sha256Hex] = Field(
        default_factory=list, max_length=MAX_BATCH_REVIEWS
    )


#: The response header that carries a freshly minted lease token, exactly once.
#:
#: The token deliberately does **not** travel in the JSON body.
#: :class:`ClaimBatchResponse` keeps ``lease_token`` as a masked
#: :class:`SecretStr`, and ``TestClosedMutationEnvelopes`` pins that no dump of
#: that model ever renders the plaintext — an invariant worth more than the
#: convenience of one field, because it holds for every future code path that
#: serializes the model for a log, an audit record, or an error body. The one
#: place the plaintext is allowed to exist is this header, set by the claim route
#: and read by the CLI. The console's access log records method, route template,
#: status, duration, and a subject hash; it never records a response header.
LEASE_TOKEN_HEADER = "X-Tickets-Lease-Token"  # noqa: S105 - HTTP header name, not a token


class ClaimBatchResponse(_Base):
    """Returned once at claim time. Only the token's hash is persisted.

    ``lease_token`` is a masked :class:`SecretStr` here on purpose: the field
    documents that a token exists and belongs to this claim, while the plaintext
    reaches the caller through :data:`LEASE_TOKEN_HEADER`. See that constant for
    why the split is worth the small awkwardness.
    """

    batch: RemediationBatch = Field(...)
    lease_token: SecretStr = Field(...)
    lease_expires_at: AwareDatetime = Field(...)
    heartbeat_interval_s: StrictInt = Field(default=REMEDIATION_HEARTBEAT_S, ge=1)
    max_continuous_lease_s: StrictInt = Field(
        default=REMEDIATION_MAX_CONTINUOUS_LEASE_S, ge=1
    )


class HeartbeatBatchRequest(_Base):
    """Renew an active lease."""

    expected_version: StrictInt = Field(..., ge=1)
    lease_token: SecretStr = Field(...)


class MaterializeBatchRequest(_Base):
    """Lease- and version-bound request for bounded frozen records."""

    expected_version: StrictInt = Field(..., ge=1)
    lease_token: SecretStr = Field(...)
    include_conversation: bool = Field(default=False)


class MaterializeBatchResponse(_Base):
    """Bounded frozen records plus explicit drift and truncation markers."""

    batch_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    batch_version: StrictInt = Field(..., ge=1)
    items: list[RemediationBatchItem] = Field(default_factory=list, max_length=MAX_BATCH_REVIEWS)
    drifted_review_ids: list[Sha256Hex] = Field(
        default_factory=list, max_length=MAX_BATCH_REVIEWS
    )
    conversation_included: bool = Field(default=False)
    next_cursor: Optional[str] = Field(default=None, max_length=MAX_CURSOR_LENGTH)
    partial: bool = Field(default=False)
    truncated: bool = Field(default=False)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_WARNINGS)


class ReviewOutcome(_Base):
    """One agent-reported per-review result inside a batch patch."""

    review_id: Sha256Hex = Field(...)
    outcome: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)


class ReviewDecision(_Base):
    """One human verification decision inside a batch completion."""

    review_id: Sha256Hex = Field(...)
    decision: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    resolution: Optional[ReviewResolution] = Field(default=None)


class PatchBatchRequest(_Base):
    """Record plan/progress/results under version and lease checks."""

    expected_version: StrictInt = Field(..., ge=1)
    lease_token: SecretStr = Field(...)
    transition: Optional[BatchStatus] = Field(default=None)
    plan_artifact: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    branch: Optional[str] = Field(default=None, max_length=MAX_GIT_REF_LENGTH)
    commit_sha: Optional[str] = Field(default=None, pattern=GIT_COMMIT_SHA_PATTERN)
    uncommitted_reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)
    pr_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    changed_files: list[str] = Field(default_factory=list, max_length=MAX_CHANGED_FIELDS)
    test_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    per_review_outcomes: list[ReviewOutcome] = Field(
        default_factory=list, max_length=MAX_BATCH_REVIEWS
    )
    summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)

    @field_validator("pr_url")
    @classmethod
    def _https_only(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.startswith("https://"):
            raise ValueError("pr_url must use https")
        return value

    @field_validator("branch")
    @classmethod
    def _well_formed_branch(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validated_git_ref(value)

    @field_validator("changed_files")
    @classmethod
    def _repo_relative_changed_files(cls, value: list[str]) -> list[str]:
        return [validated_repo_path(item) for item in value]

    @model_validator(mode="after")
    def _reject_duplicate_review_outcomes(self) -> PatchBatchRequest:
        seen = {entry.review_id for entry in self.per_review_outcomes}
        if len(seen) != len(self.per_review_outcomes):
            raise ValueError("per_review_outcomes must not repeat a review_id")
        if self.commit_sha is not None and self.uncommitted_reason is not None:
            raise ValueError("supply a commit_sha or an uncommitted_reason, never both")
        return self


class ReleaseBatchRequest(_Base):
    """Atomic release with lease invalidation under fixed safety rules."""

    expected_version: StrictInt = Field(..., ge=1)
    lease_token: SecretStr = Field(...)
    disposition: str = Field(...)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)

    @field_validator("disposition")
    @classmethod
    def _closed_disposition(cls, value: str) -> str:
        if value not in {BatchStatus.READY.value, BatchStatus.BLOCKED.value}:
            raise ValueError("disposition must be 'ready' or 'blocked'")
        return value


class ReadyBatchRequest(_Base):
    """Versioned `draft`/`blocked` -> `ready` human transition."""

    expected_version: StrictInt = Field(..., ge=1)
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)


class StartVerificationRequest(_Base):
    """Independent `changes_proposed` -> `verifying` transition."""

    expected_version: StrictInt = Field(..., ge=1)
    independent_verifier_attestation: str = Field(
        ..., min_length=1, max_length=MAX_REASON_LENGTH
    )
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)


class CompleteBatchRequest(_Base):
    """Independent verified `verifying` -> `completed` transition."""

    expected_version: StrictInt = Field(..., ge=1)
    decision: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    verification_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    per_review_decisions: list[ReviewDecision] = Field(
        default_factory=list, max_length=MAX_BATCH_REVIEWS
    )
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)

    @model_validator(mode="after")
    def _resolved_decisions_need_evidence(self) -> CompleteBatchRequest:
        seen = {entry.review_id for entry in self.per_review_decisions}
        if len(seen) != len(self.per_review_decisions):
            raise ValueError("per_review_decisions must not repeat a review_id")
        # The agent's own test records are NOT accepted here. A completion that
        # could lean on them would let "the author says it works" close a review,
        # which is exactly what independent verification exists to prevent.
        resolving = [
            entry
            for entry in self.per_review_decisions
            if entry.resolution is not None
            and entry.resolution.outcome is ResolutionOutcome.FIXED
        ]
        if resolving and not self.verification_evidence:
            raise ValueError(
                "a 'fixed' review decision requires recorded verification evidence"
            )
        return self


class ExtendLeaseRequest(_Base):
    """Admin-only bounded extension after the continuous cap."""

    expected_version: StrictInt = Field(..., ge=1)
    additional_minutes: StrictInt = Field(..., ge=1, le=MAX_LEASE_EXTENSION_MINUTES)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class CancelBatchRequest(_Base):
    """Versioned, reasoned human cancellation."""

    expected_version: StrictInt = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


# =====================================================================
# State machines
# =====================================================================

_REVIEW_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.UNREVIEWED: frozenset({ReviewStatus.REVIEWED, ReviewStatus.BLOCKED}),
    ReviewStatus.REVIEWED: frozenset(
        {ReviewStatus.TRIAGED, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX}
    ),
    ReviewStatus.TRIAGED: frozenset(
        {ReviewStatus.PLANNED, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX}
    ),
    ReviewStatus.PLANNED: frozenset(
        {ReviewStatus.IN_PROGRESS, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX}
    ),
    ReviewStatus.IN_PROGRESS: frozenset(
        {ReviewStatus.CHANGES_PROPOSED, ReviewStatus.BLOCKED}
    ),
    ReviewStatus.CHANGES_PROPOSED: frozenset(
        {ReviewStatus.VERIFYING, ReviewStatus.IN_PROGRESS, ReviewStatus.BLOCKED}
    ),
    ReviewStatus.VERIFYING: frozenset(
        {ReviewStatus.RESOLVED, ReviewStatus.IN_PROGRESS, ReviewStatus.BLOCKED}
    ),
    ReviewStatus.BLOCKED: frozenset(
        {
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
            ReviewStatus.IN_PROGRESS,
            ReviewStatus.WONT_FIX,
        }
    ),
    ReviewStatus.RESOLVED: frozenset(),
    ReviewStatus.WONT_FIX: frozenset(),
}

TERMINAL_REVIEW_STATUSES = frozenset({ReviewStatus.RESOLVED, ReviewStatus.WONT_FIX})
_REVIEW_MUTATING_ROLES = frozenset(
    {ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR, ReviewerRole.ADMIN}
)


def allowed_review_transitions(status: ReviewStatus) -> frozenset[ReviewStatus]:
    """Return the closed set of statuses reachable from ``status``."""
    return _REVIEW_TRANSITIONS[status]


def assert_review_transition(
    current: ReviewStatus,
    target: ReviewStatus,
    *,
    actor_role: ReviewerRole,
    resolution: Optional[ReviewResolution] = None,
    admin_reopen: bool = False,
) -> None:
    """Validate one review status change, or raise :class:`InvalidReviewTransition`.

    ``admin_reopen`` is the *only* way out of a terminal status, it is
    admin-only, and it may target ``triaged`` alone. A terminal target requires
    a closed :class:`ReviewResolution` carrying either structured verification
    evidence or an explicit no-change reason.
    """
    if actor_role not in _REVIEW_MUTATING_ROLES:
        raise InvalidReviewTransition(
            f"role '{actor_role.value}' may not change a review status"
        )

    if current in TERMINAL_REVIEW_STATUSES:
        if not admin_reopen:
            raise InvalidReviewTransition(
                f"'{current.value}' is terminal; an admin must explicitly reopen it"
            )
        if actor_role is not ReviewerRole.ADMIN:
            raise InvalidReviewTransition("only an admin may reopen a terminal review")
        if target is not ReviewStatus.TRIAGED:
            raise InvalidReviewTransition("an admin reopen may only target 'triaged'")
        return

    if admin_reopen:
        raise InvalidReviewTransition(f"'{current.value}' is not terminal and cannot be reopened")

    if target not in _REVIEW_TRANSITIONS[current]:
        raise InvalidReviewTransition(
            f"'{current.value}' -> '{target.value}' is not an allowed review transition"
        )

    if target in TERMINAL_REVIEW_STATUSES:
        if resolution is None:
            raise InvalidReviewTransition(
                f"'{target.value}' requires a closed resolution object"
            )
        if not resolution.has_defensible_verification():
            raise InvalidReviewTransition(
                f"'{target.value}' requires structured verification evidence "
                "or an explicit no-change reason"
            )


_BATCH_TRANSITIONS: dict[BatchStatus, frozenset[BatchStatus]] = {
    BatchStatus.DRAFT: frozenset({BatchStatus.READY, BatchStatus.CANCELLED}),
    BatchStatus.READY: frozenset({BatchStatus.CLAIMED, BatchStatus.CANCELLED}),
    BatchStatus.CLAIMED: frozenset(
        {BatchStatus.PLANNING, BatchStatus.BLOCKED, BatchStatus.EXPIRED}
    ),
    BatchStatus.PLANNING: frozenset(
        {BatchStatus.IN_PROGRESS, BatchStatus.BLOCKED, BatchStatus.EXPIRED}
    ),
    BatchStatus.IN_PROGRESS: frozenset(
        {BatchStatus.CHANGES_PROPOSED, BatchStatus.BLOCKED, BatchStatus.EXPIRED}
    ),
    BatchStatus.CHANGES_PROPOSED: frozenset(
        {BatchStatus.VERIFYING, BatchStatus.IN_PROGRESS, BatchStatus.BLOCKED}
    ),
    BatchStatus.VERIFYING: frozenset(
        {BatchStatus.COMPLETED, BatchStatus.IN_PROGRESS, BatchStatus.BLOCKED}
    ),
    BatchStatus.BLOCKED: frozenset(
        {BatchStatus.READY, BatchStatus.CLAIMED, BatchStatus.CANCELLED}
    ),
    BatchStatus.EXPIRED: frozenset({BatchStatus.CLAIMED, BatchStatus.CANCELLED}),
    BatchStatus.COMPLETED: frozenset(),
    BatchStatus.CANCELLED: frozenset(),
}

# Only a lease-holding agent may author; only an independent human may verify.
_AGENT_ONLY_BATCH_EDGES = frozenset(
    {
        (BatchStatus.CLAIMED, BatchStatus.PLANNING),
        (BatchStatus.PLANNING, BatchStatus.IN_PROGRESS),
        (BatchStatus.IN_PROGRESS, BatchStatus.CHANGES_PROPOSED),
    }
)
_HUMAN_ONLY_BATCH_EDGES = frozenset(
    {
        (BatchStatus.CHANGES_PROPOSED, BatchStatus.VERIFYING),
        (BatchStatus.VERIFYING, BatchStatus.COMPLETED),
    }
)


def allowed_batch_transitions(status: BatchStatus) -> frozenset[BatchStatus]:
    """Return the closed set of batch statuses reachable from ``status``."""
    return _BATCH_TRANSITIONS[status]


def assert_batch_transition(
    current: BatchStatus,
    target: BatchStatus,
    *,
    actor_role: Optional[ReviewerRole] = None,
    has_lease: bool = False,
) -> None:
    """Validate one batch status change, or raise :class:`InvalidBatchTransition`.

    ``actor_role=None`` checks the edge only; supplying a role additionally
    enforces the agent-lease and independent-verifier separation.
    """
    if target not in _BATCH_TRANSITIONS[current]:
        raise InvalidBatchTransition(
            f"'{current.value}' -> '{target.value}' is not an allowed batch transition"
        )
    if actor_role is None:
        return

    edge = (current, target)
    if edge in _AGENT_ONLY_BATCH_EDGES:
        if actor_role is not ReviewerRole.AGENT:
            raise InvalidBatchTransition(
                f"only a claimed agent may perform '{current.value}' -> '{target.value}'"
            )
        if not has_lease:
            raise InvalidBatchTransition("a valid lease is required for this transition")
        return

    if edge in _HUMAN_ONLY_BATCH_EDGES:
        if actor_role not in {ReviewerRole.REVIEWER, ReviewerRole.ADMIN}:
            raise InvalidBatchTransition(
                "an independent reviewer or admin must perform "
                f"'{current.value}' -> '{target.value}'"
            )


#: The statuses a *human* may move a batch to. The agent's forward path
#: (``planning``/``in_progress``/``changes_proposed``) is deliberately absent:
#: an agent authors, a human decides, and neither borrows the other's edge.
HUMAN_BATCH_TARGETS = frozenset(
    {
        BatchStatus.READY,
        BatchStatus.CANCELLED,
        BatchStatus.VERIFYING,
        BatchStatus.COMPLETED,
        BatchStatus.IN_PROGRESS,
        BatchStatus.BLOCKED,
    }
)

#: What an agent may transition to through ``PATCH``. ``verifying`` and
#: ``completed`` are absent by construction, which is the whole
#: human-in-the-loop invariant: an agent that could reach either would be able
#: to declare its own work verified.
AGENT_BATCH_TARGETS = frozenset(
    {BatchStatus.PLANNING, BatchStatus.IN_PROGRESS, BatchStatus.CHANGES_PROPOSED}
)


def assert_batch_submission_complete(
    *,
    branch: Optional[str],
    commit_sha: Optional[str],
    uncommitted_reason: Optional[str],
    changed_files: Sequence[str],
    test_evidence: Sequence[VerificationEvidence],
    summary: Optional[str],
    per_review_outcomes: Sequence[ReviewOutcome],
    frozen_review_ids: Sequence[str],
) -> None:
    """Everything ``changes_proposed`` must carry, or raise.

    A submission is a handoff to a person who has to decide whether to trust it.
    Each requirement below is something that person cannot reconstruct later:
    which branch, which commit (or why there is none), which files, what was
    actually run, and a per-review verdict for every review that was frozen —
    silence about one review is indistinguishable from having missed it.
    """
    missing: list[str] = []
    if not branch:
        missing.append("a branch")
    if not commit_sha and not uncommitted_reason:
        missing.append("a commit_sha or an explicit uncommitted_reason")
    if not changed_files:
        missing.append("a changed-file list")
    if not test_evidence:
        missing.append("at least one test command/outcome record")
    if not (summary or "").strip():
        missing.append("a resolution summary")
    if missing:
        raise InvalidBatchTransition(
            "a submission to 'changes_proposed' requires " + ", ".join(missing)
        )

    reported = {entry.review_id for entry in per_review_outcomes}
    frozen = set(frozen_review_ids)
    unreported = sorted(frozen - reported)
    if unreported:
        raise InvalidBatchTransition(
            f"{len(unreported)} frozen review(s) carry no per-review outcome"
        )
    foreign = sorted(reported - frozen)
    if foreign:
        raise InvalidBatchTransition(
            "a per-review outcome names a review this batch did not freeze"
        )


def assert_independent_verifier(
    *,
    actor: ReviewerIdentity,
    actor_role: ReviewerRole,
    claimed_by: Optional[ReviewerIdentity],
    created_by: Optional[ReviewerIdentity] = None,
) -> None:
    """Refuse a verifier who authored the work, or raise.

    Compared on ``subject``, not on email: an email can be re-pointed at another
    principal, whereas the IAP subject is the stable identifier the console
    already hashes into its audit chain. ``created_by`` is *not* excluded — the
    remediator who selected the observations is allowed to verify the fix; the
    rule that matters is that whoever *made* the change cannot sign it off.
    """
    del created_by
    if actor_role not in {ReviewerRole.REVIEWER, ReviewerRole.ADMIN}:
        raise InvalidBatchTransition("only a reviewer or an admin may verify a batch")
    if actor_role is ReviewerRole.AGENT:  # pragma: no cover - unreachable above
        raise InvalidBatchTransition("an agent may never verify its own batch")
    if claimed_by is not None and claimed_by.subject == actor.subject:
        raise InvalidBatchTransition(
            "the identity that claimed this batch may not verify it"
        )


def can_assign_reviewer(
    *,
    actor_role: ReviewerRole,
    actor: ReviewerIdentity,
    target: ReviewerIdentity,
    current_assignee: Optional[ReviewerIdentity] = None,
) -> bool:
    """Return whether ``actor`` may set ``assigned_reviewer`` to ``target``.

    A reviewer may only self-assign an unassigned review; an admin may
    reassign freely. This is independent of the authenticated audit actor,
    which is recorded on the audit event regardless.
    """
    if actor_role is ReviewerRole.ADMIN:
        return True
    if actor_role not in {ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR}:
        return False
    if target.subject != actor.subject:
        return False
    return current_assignee is None or current_assignee.subject == actor.subject


# =====================================================================
# ETag / If-Match preconditions
# =====================================================================


def format_etag(version: int) -> str:
    """Render a durable version as a quoted strong ETag, e.g. ``"v3"``."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    return f'"v{version}"'


def parse_if_match(header: Optional[str]) -> int:
    """Parse a quoted ``"vN"`` ``If-Match`` header into its integer version.

    Raises :class:`MissingPreconditionError` (428) when absent and
    :class:`MalformedPreconditionError` (422) for anything that is not exactly
    a quoted, unpadded, positive ``vN``. Weak validators and ``*`` are refused
    because a blind overwrite is never acceptable here.
    """
    if header is None or not header.strip():
        raise MissingPreconditionError("If-Match is required")
    # Bound the header before parsing: an unbounded digit run would make
    # int() raise a raw ValueError (CPython's 4300-digit limit), which would
    # surface as a 500 instead of the contract's 422.
    if len(header) > MAX_IF_MATCH_HEADER_LENGTH:
        raise MalformedPreconditionError("If-Match is too long")
    match = _IF_MATCH_PATTERN.match(header)
    if match is None:
        raise MalformedPreconditionError('If-Match must be a quoted version such as "v3"')
    return int(match.group(1))


def ensure_if_match(header: Optional[str], *, current_version: int) -> int:
    """Parse and compare an ``If-Match`` header against the current version."""
    supplied = parse_if_match(header)
    if supplied != current_version:
        raise StalePreconditionError(
            "the record changed since it was loaded",
            supplied_version=supplied,
            current_version=current_version,
        )
    return supplied


def http_status_for_precondition_error(error: PreconditionError) -> int:
    """Map a precondition failure to its stable HTTP status.

    428 missing, 412 stale, 422 malformed. 409 stays reserved for a
    valid-version business/lease/idempotency conflict.
    """
    if isinstance(error, MissingPreconditionError):
        return 428
    if isinstance(error, StalePreconditionError):
        return 412
    if isinstance(error, MalformedPreconditionError):
        return 422
    raise TypeError("unknown precondition error")


# =====================================================================
# Console cursor authenticated encryption
# =====================================================================


def seal_cursor(
    key: bytes,
    payload: Mapping[str, Any],
    *,
    context: str,
    ttl_s: int = CURSOR_DEFAULT_TTL_S,
    now: Optional[datetime] = None,
    nonce: Optional[bytes] = None,
) -> str:
    """Authenticated-encrypt a cursor payload into an opaque console token.

    ``context`` is bound as AES-GCM associated data, so a token minted for one
    endpoint/direction/filter/subject cannot be replayed against another. The
    AEAD implementation comes from the already pinned ``cryptography`` runtime
    lock; there is deliberately no bespoke cipher here.

    An absolute expiry is sealed *inside* the ciphertext as ``_exp`` so it is
    covered by the authentication tag and cannot be extended by a caller.

    ``now`` and ``nonce`` exist so tests can be deterministic. Production
    callers must leave both ``None``: reusing a nonce with the same key breaks
    AES-GCM catastrophically.
    """
    if len(key) != CURSOR_AEAD_KEY_BYTES:
        raise ValueError(f"cursor key must be exactly {CURSOR_AEAD_KEY_BYTES} bytes")
    if ttl_s <= 0:
        raise ValueError("cursor ttl_s must be positive")
    if CURSOR_EXPIRY_KEY in payload:
        raise ValueError(f"{CURSOR_EXPIRY_KEY!r} is reserved for the cursor expiry")
    chosen = nonce if nonce is not None else os.urandom(CURSOR_NONCE_BYTES)
    if len(chosen) != CURSOR_NONCE_BYTES:
        raise ValueError(f"cursor nonce must be exactly {CURSOR_NONCE_BYTES} bytes")
    issued_at = _require_utc(now) if now is not None else utc_now()
    envelope = dict(payload)
    envelope[CURSOR_EXPIRY_KEY] = int((issued_at + timedelta(seconds=ttl_s)).timestamp())
    plaintext = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(chosen, plaintext, context.encode("utf-8"))
    token = base64.urlsafe_b64encode(chosen + ciphertext).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValueError("sealed cursor exceeds the canonical cursor length")
    return token


def open_cursor(
    key: bytes,
    token: str,
    *,
    context: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Authenticate and decrypt a console cursor token.

    Every failure mode — a non-canonical encoding, truncation, a tampered
    nonce/tag, the wrong key, a mismatched context, or an elapsed expiry —
    raises :class:`CursorError` with a message that reveals neither the
    plaintext nor the key.
    """
    if len(key) != CURSOR_AEAD_KEY_BYTES:
        raise ValueError(f"cursor key must be exactly {CURSOR_AEAD_KEY_BYTES} bytes")
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise CursorError("cursor token is not acceptable")
    # One cursor must have exactly one encoding. base64.urlsafe_b64decode
    # discards non-alphabet bytes, which would let whitespace-injected or
    # standard-alphabet variants decode to the same cursor.
    if _CURSOR_TOKEN_PATTERN.match(token) is None:
        raise CursorError("cursor token is not readable")
    try:
        raw = base64.b64decode(
            (token + "=" * (-len(token) % 4)).translate(_B64URL_TO_STD), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise CursorError("cursor token is not readable") from exc
    # Reject any non-canonical encoding by re-encoding and comparing. Unpadded
    # base64 leaves spare bits in the final character, so several distinct
    # tokens decode to identical bytes; without this check, flipping that last
    # character is a silent no-op and the token is malleable.
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token:
        raise CursorError("cursor token is not readable")
    if len(raw) < CURSOR_NONCE_BYTES + CURSOR_TAG_BYTES:
        raise CursorError("cursor token is not readable")
    nonce, ciphertext = raw[:CURSOR_NONCE_BYTES], raw[CURSOR_NONCE_BYTES:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, context.encode("utf-8"))
    except InvalidTag as exc:
        raise CursorError("cursor token failed authentication") from exc
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor token payload is not readable") from exc
    if not isinstance(decoded, dict):
        raise CursorError("cursor token payload is not an object")
    expires_at = decoded.pop(CURSOR_EXPIRY_KEY, None)
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise CursorError("cursor token is missing its expiry")
    current = _require_utc(now) if now is not None else utc_now()
    if current.timestamp() >= expires_at:
        raise CursorError("cursor token has expired")
    return decoded
