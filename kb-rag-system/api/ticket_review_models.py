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
from collections.abc import Mapping
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
IMPORT_STAGING_TTL_S = 7 * 24 * 60 * 60
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
MAX_CSV_ROWS = 10_000
MAX_JSON_REQUEST_BYTES = 1 * 1024 * 1024
MAX_CSV_REQUEST_BYTES = 10 * 1024 * 1024
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
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"

# Cursor authenticated encryption.
CURSOR_AEAD_KEY_BYTES = 32
CURSOR_NONCE_BYTES = 12
CURSOR_TAG_BYTES = 16
# Console cursors are short-lived by contract; the window matches the live
# list/detail cache so a token cannot outlive the data it points at.
CURSOR_DEFAULT_TTL_S = CACHE_TTL_S
CURSOR_EXPIRY_KEY = "_exp"


# =====================================================================
# Errors
# =====================================================================


class TicketReviewContractError(Exception):
    """Base class for every Stage 1 contract violation."""


class InvalidReviewTransition(TicketReviewContractError):
    """A review status change is not permitted by the closed table."""


class InvalidBatchTransition(TicketReviewContractError):
    """A remediation-batch status change is not permitted."""


class InvalidImportTransition(TicketReviewContractError):
    """A ticket-import status change is not permitted."""


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

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# \Z rather than $: in Python `$` also matches just before a trailing newline,
# which would accept '"v3"\n' as a valid strong validator.
_IF_MATCH_PATTERN = re.compile(r'^"v([1-9][0-9]*)"\Z')
# The console cursor alphabet is exactly unpadded base64url. Anything else --
# embedded whitespace, the standard '+'/'/' alphabet, stray padding -- is
# rejected rather than silently normalized, so one cursor has one encoding.
_CURSOR_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\Z")
_B64URL_TO_STD = str.maketrans("-_", "+/")


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


class ImportStatus(str, Enum):
    UPLOADED = "uploaded"
    PLANNED = "planned"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    PARTIAL = "partial"
    REVERSING = "reversing"
    REVERSED = "reversed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


# =====================================================================
# Identity
# =====================================================================


class ReviewerIdentity(_Base):
    """An application identity resolved from a verified IAP assertion.

    This is *not* the audit actor. Audit actors are recorded independently on
    every :class:`AuditEvent`, and the legacy CSV reviewer name lives in
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
    legacy_reviewer_display_name: Optional[str] = Field(
        default=None, max_length=MAX_DISPLAY_NAME_LENGTH
    )
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
    plan_artifact: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    branch: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    commit_sha: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    pr_url: Optional[str] = Field(default=None, max_length=MAX_URL_LENGTH)
    # The master plan's Firestore contract requires the batch parent to store
    # "bounded test evidence" alongside the plan/branch/commit references.
    test_evidence: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    verification_summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    outcome: Optional[BatchOutcome] = Field(default=None)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


# =====================================================================
# Sheet CSV import
# =====================================================================


class TicketImportRow(_Base):
    """One planned/applied CSV row. Carries the expected review version."""

    row_number: StrictInt = Field(..., ge=1)
    raw_ticket_id: str = Field(..., min_length=1, max_length=MAX_DISPLAY_ID_LENGTH)
    review_id: Optional[Sha256Hex] = Field(default=None)
    topic: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    legacy_type: Optional[str] = Field(default=None, max_length=MAX_LEGACY_TYPE_LENGTH)
    rating: Optional[Rating] = Field(default=None)
    legacy_reviewer_display_name: Optional[str] = Field(
        default=None, max_length=MAX_DISPLAY_NAME_LENGTH
    )
    comments: Optional[str] = Field(default=None, max_length=MAX_COMMENTS_LENGTH)
    expected_review_version: Optional[StrictInt] = Field(default=None, ge=1)
    error_code: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    error_message: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)


class TicketImport(_Base):
    """Durable import summary and reversal plan."""

    import_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    schema_version: str = Field(default=SCHEMA_VERSION, max_length=MAX_TOPIC_LENGTH)
    status: ImportStatus = Field(default=ImportStatus.UPLOADED)
    file_sha256: Sha256Hex = Field(...)
    plan_sha256: Optional[Sha256Hex] = Field(default=None)
    created_by: ReviewerIdentity = Field(...)
    total_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    applied_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    failed_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    conflicted_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    reversed_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


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


class ClaimBatchResponse(_Base):
    """Returned once at claim time. Only the token's hash is persisted."""

    batch: RemediationBatch = Field(...)
    lease_token: SecretStr = Field(...)
    lease_expires_at: AwareDatetime = Field(...)


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
    branch: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    commit_sha: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
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


class ExtendLeaseRequest(_Base):
    """Admin-only bounded extension after the continuous cap."""

    expected_version: StrictInt = Field(..., ge=1)
    additional_minutes: StrictInt = Field(..., ge=1, le=MAX_LEASE_EXTENSION_MINUTES)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class CancelBatchRequest(_Base):
    """Versioned, reasoned human cancellation."""

    expected_version: StrictInt = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class ImportRowIssue(_Base):
    """One bounded row-level error or conflict. Never the whole row."""

    row_number: StrictInt = Field(..., ge=1)
    field: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    code: str = Field(..., min_length=1, max_length=MAX_TOPIC_LENGTH)
    message: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)


class ImportDryRunResponse(_Base):
    """The result of dry-running a bounded legacy CSV body."""

    import_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    file_sha256: Sha256Hex = Field(...)
    plan_sha256: Sha256Hex = Field(...)
    total_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    creatable_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    updatable_rows: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    errors: list[ImportRowIssue] = Field(default_factory=list, max_length=MAX_CSV_ROWS)
    conflicts: list[ImportRowIssue] = Field(default_factory=list, max_length=MAX_CSV_ROWS)


class ImportApplyOrReverseRequest(_Base):
    """Apply/resume or reverse/resume a versioned chunk.

    ``resume_cursor`` is a signed console cursor, never a raw offset.
    """

    plan_sha256: Sha256Hex = Field(...)
    approval_confirmed: bool = Field(...)
    resume_cursor: Optional[str] = Field(default=None, max_length=MAX_CURSOR_LENGTH)

    @field_validator("approval_confirmed")
    @classmethod
    def _must_be_confirmed(cls, value: bool) -> bool:
        if not value:
            raise ValueError("approval_confirmed must be true")
        return value


class ImportChunkResponse(_Base):
    """The result of one 100-row apply/reverse chunk."""

    import_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    status: ImportStatus = Field(...)
    completed: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    failed: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    conflicted: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    next_cursor: Optional[str] = Field(default=None, max_length=MAX_CURSOR_LENGTH)


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


_IMPORT_TRANSITIONS: dict[ImportStatus, frozenset[ImportStatus]] = {
    ImportStatus.UPLOADED: frozenset(
        {ImportStatus.PLANNED, ImportStatus.FAILED, ImportStatus.CANCELLED}
    ),
    ImportStatus.PLANNED: frozenset(
        {ImportStatus.APPROVED, ImportStatus.FAILED, ImportStatus.CANCELLED}
    ),
    ImportStatus.APPROVED: frozenset({ImportStatus.APPLYING, ImportStatus.CANCELLED}),
    ImportStatus.APPLYING: frozenset(
        {ImportStatus.APPLIED, ImportStatus.PARTIAL, ImportStatus.FAILED}
    ),
    ImportStatus.PARTIAL: frozenset(
        {ImportStatus.APPLYING, ImportStatus.REVERSING, ImportStatus.CANCELLED}
    ),
    ImportStatus.APPLIED: frozenset({ImportStatus.REVERSING}),
    ImportStatus.REVERSING: frozenset(
        {ImportStatus.REVERSED, ImportStatus.PARTIAL, ImportStatus.FAILED}
    ),
    ImportStatus.FAILED: frozenset(
        {
            ImportStatus.PLANNED,
            ImportStatus.APPLYING,
            ImportStatus.REVERSING,
            ImportStatus.CANCELLED,
        }
    ),
    ImportStatus.REVERSED: frozenset(),
    ImportStatus.CANCELLED: frozenset(),
}


def allowed_import_transitions(status: ImportStatus) -> frozenset[ImportStatus]:
    """Return the closed set of import statuses reachable from ``status``."""
    return _IMPORT_TRANSITIONS[status]


def assert_import_transition(
    current: ImportStatus,
    target: ImportStatus,
    *,
    reason: Optional[str] = None,
) -> None:
    """Validate one import status change, or raise :class:`InvalidImportTransition`.

    Recovery out of ``failed`` always requires an explicit admin reason.
    """
    if target not in _IMPORT_TRANSITIONS[current]:
        raise InvalidImportTransition(
            f"'{current.value}' -> '{target.value}' is not an allowed import transition"
        )
    if current is ImportStatus.FAILED and not (reason and reason.strip()):
        raise InvalidImportTransition(
            "leaving 'failed' requires an explicit admin reason"
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
