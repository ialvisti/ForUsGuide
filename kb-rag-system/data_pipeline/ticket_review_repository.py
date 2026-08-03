"""Durable persistence for the /tickets review console (Stage 3).

Every business invariant lives in :class:`TicketReviewRepository`, inside a
backend transaction. The backends are deliberately thin: they supply the
transaction primitive, single-document reads, and the small set of bounded
queries the master query grammar allows. That is the same split that
``ticket_job_repository`` uses, so the in-memory backend and the Firestore
backend share one observable contract.

Storage contract (four separate lifetimes, never conflated):

* ``ticket_reviews/{review_id}`` durable structured judgment. No DevRev title,
  no message body, no participant name. 730-day ``retention_expires_at``,
  refreshed on every write, plus ``legal_hold``.
* ``ticket_reviews/{review_id}/audit_events/{event_id}`` application
  append-only, hash-chained ledger. 2,555-day retention. The repository
  exposes no update or delete path for these.
* ``devrev_message_cache`` / ``ticket_console_cache`` / ``ticket_import_staging``
  / ``idempotency_keys`` disposable documents with a native ``expires_at`` TTL.
  Firestore TTL deletion is asynchronous and therefore cleanup-only, so an
  elapsed document is already absent at every application boundary here.
* ``remediation_batches`` / ``ticket_imports`` / ``ticket_exports`` durable
  operational records with the same 730/2,555-day split as reviews.

The named database is the isolation boundary. A collection prefix is not, and
``(default)`` is never a fallback: see
:func:`api.tickets_console_config.resolve_tickets_firestore_database`.

Timestamps are always written as native ``datetime`` objects: a serialized
string silently breaks Firestore TTL. ``.isoformat()`` is banned in this module
for stored values.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, TypeVar, Union, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from api.ticket_review_models import (
    AUDIT_RETENTION_DAYS,
    CACHE_TTL_S,
    DEFAULT_PAGE_SIZE,
    FIRESTORE_MAX_DOCUMENT_BYTES,
    GENESIS_EVENT_HASH,
    IDEMPOTENCY_TTL_S,
    IMPORT_STAGING_TTL_S,
    MAX_ATTACHMENTS,
    MAX_BATCH_REVIEWS,
    MAX_CSV_ROWS,
    MAX_DISPLAY_ID_LENGTH,
    MAX_ID_LENGTH,
    MAX_LEASE_EXTENSION_MINUTES,
    MAX_MESSAGE_BODY_LENGTH,
    MAX_METADATA_VALUE_LENGTH,
    MAX_PAGE_SIZE,
    MAX_REASON_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TOPIC_LENGTH,
    MESSAGE_CACHE_TTL_S,
    REMEDIATION_LEASE_S,
    REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    REVIEW_RETENTION_DAYS,
    SCHEMA_VERSION,
    AuditEvent,
    BatchLease,
    BatchStatus,
    CorrelationTrust,
    CursorError,
    EvidenceLink,
    ImportState,
    ImportStatus,
    RemediationBatch,
    RemediationBatchItem,
    ReviewerIdentity,
    ReviewerRole,
    ReviewPatch,
    ReviewRef,
    ReviewStatus,
    Sha256Hex,
    StrictInt,
    TERMINAL_REVIEW_STATUSES,
    TicketImport,
    TicketImportRow,
    TicketReview,
    VerificationEvidence,
    assert_batch_transition,
    assert_import_transition,
    assert_review_transition,
    can_assign_reviewer,
    compute_audit_event_hash,
    open_cursor,
    review_id_for_devrev_work,
    seal_cursor,
    utc_now,
)
from api.ticket_review_models import (
    InvalidBatchTransition as ContractInvalidBatchTransition,
)
from api.ticket_review_models import (
    InvalidImportTransition as ContractInvalidImportTransition,
)
from api.ticket_review_models import (
    InvalidReviewTransition as ContractInvalidReviewTransition,
)
from api.tickets_console_config import (
    DEFAULT_FIRESTORE_DATABASE,
    STRICT_ENVIRONMENTS,
    resolve_tickets_firestore_database,
)

# =====================================================================
# Collections (master plan, "Firestore layout"). Frozen names.
# =====================================================================

REVIEWS_COLLECTION = "ticket_reviews"
AUDIT_EVENTS_SUBCOLLECTION = "audit_events"
EVIDENCE_LINKS_SUBCOLLECTION = "evidence_links"
BATCHES_COLLECTION = "remediation_batches"
BATCH_ITEMS_SUBCOLLECTION = "items"
BATCH_EVENTS_SUBCOLLECTION = "events"
IMPORTS_COLLECTION = "ticket_imports"
IMPORT_ROWS_SUBCOLLECTION = "rows"
EXPORTS_COLLECTION = "ticket_exports"
GLOBAL_AUDIT_EVENTS_COLLECTION = "ticket_console_audit_events"
DEVREV_MESSAGE_CACHE_COLLECTION = "devrev_message_cache"
CONSOLE_CACHE_COLLECTION = "ticket_console_cache"
IMPORT_STAGING_COLLECTION = "ticket_import_staging"
IDEMPOTENCY_KEYS_COLLECTION = "idempotency_keys"

# Disposable collections, and only these, declare a native TTL field.
TTL_FIELD = "expires_at"
TTL_COLLECTIONS = frozenset(
    {
        CONSOLE_CACHE_COLLECTION,
        DEVREV_MESSAGE_CACHE_COLLECTION,
        IMPORT_STAGING_COLLECTION,
        IDEMPOTENCY_KEYS_COLLECTION,
    }
)
DURABLE_PRODUCT_COLLECTIONS = (
    REVIEWS_COLLECTION,
    BATCHES_COLLECTION,
    IMPORTS_COLLECTION,
    EXPORTS_COLLECTION,
)
RETENTION_FIELD = "retention_expires_at"
# While a legal hold is in force the product clock is PARKED under this name and
# `retention_expires_at` is removed from the document entirely. A held document
# must be invisible to the purge scan, not merely skipped by it: a missing field
# is excluded from every Firestore index, whereas an untouchable document that
# keeps an old `retention_expires_at` would sit at the head of the ascending
# scan order forever and starve every other candidate out of a bounded run.
HELD_RETENTION_FIELD = "retention_hold_expires_at"
LEGAL_HOLD_FIELD = "legal_hold"

# Bookkeeping fields the repository maintains on a durable parent so that the
# hash chain can be extended in O(1) inside the same transaction as the write.
CHAIN_HEAD_FIELD = "audit_chain_head"
CHAIN_COUNT_FIELD = "audit_event_count"
# A LIVE parent tracks its ledger's expiry under this name; a tombstone tracks
# it as `ledger_expires_at`. The two names are deliberately different so that a
# single-field range scan on `ledger_expires_at` returns tombstones and nothing
# else, with no composite index and no doc_kind filter.
LEDGER_EXPIRY_FIELD = "audit_ledger_expires_at"
TOMBSTONE_LEDGER_EXPIRY_FIELD = "ledger_expires_at"
DOC_KIND_FIELD = "doc_kind"

# The global ledger has no product parent, so its chain head lives in one
# reserved, content-free document. It is never returned as an event and never
# deleted by retention.
GLOBAL_CHAIN_HEAD_DOC_ID = "__global_chain_head__"

PURGED_TOMBSTONE_KIND = "purged_tombstone"
CHAIN_HEAD_KIND = "chain_head"
# Exactly the six values the master plan allows in a purged parent, plus the
# kind discriminator. Nothing else may survive a 730-day product purge.
TOMBSTONE_FIELDS = frozenset(
    {
        DOC_KIND_FIELD,
        "parent_id_hash",
        "schema_version",
        "purged_at",
        "ledger_expires_at",
        LEGAL_HOLD_FIELD,
        CHAIN_HEAD_FIELD,
    }
)

# The master query grammar: a status set plus at most one of these facets.
ALLOWED_REVIEW_FACETS = frozenset(
    {
        "assigned_reviewer.email",
        "topic",
        "rating",
        "observation_type",
        "remediation_target",
        "severity",
    }
)

# Cursor domains. The associated-data string binds a token to one cursor type
# and version, so a token minted for one listing never opens as another.
REVIEW_CURSOR_SCHEMA_VERSION = 1
REVIEW_LIST_CURSOR_CONTEXT = "tickets-firestore:reviews:list:v1"
BATCH_ITEMS_CURSOR_CONTEXT = "tickets-firestore:batch-items:v1"
AUDIT_EVENTS_CURSOR_CONTEXT = "tickets-firestore:audit-events:v1"
EVIDENCE_LINKS_CURSOR_CONTEXT = "tickets-firestore:evidence-links:v1"
IMPORT_ROWS_CURSOR_CONTEXT = "tickets-firestore:import-rows:v1"

# Firestore platform limits. The document limit is a canonical Stage 1 value;
# the write-count ceiling is Firestore's documented per-transaction/per-batch
# limit and exists here so a batch can never be built past it.
FIRESTORE_MAX_WRITES_PER_TRANSACTION = 500
DEFAULT_RETENTION_MAX_DOCUMENTS = 200
MAX_RETENTION_MAX_DOCUMENTS = 5_000

Document = dict[str, Any]
# A document path alternates collection and document ids and therefore always
# has an even length. Retention relies on that: a delete always names an exact
# document, never a collection.
DocPath = tuple[str, ...]
TxnResult = TypeVar("TxnResult")
ModelT = TypeVar("ModelT", bound=BaseModel)


# =====================================================================
# Typed errors. The API layer needs failures it can map; nothing here
# swallows an exception and returns None.
# =====================================================================


class ReviewRepositoryError(Exception):
    """Base class for every review-repository failure."""


class ReviewNotFound(ReviewRepositoryError):
    """No durable review exists for the requested id."""


class ReviewVersionConflict(ReviewRepositoryError):
    """A mutation carried a stale ``expected_version``.

    Only safe metadata travels: the current version and the time it changed.
    Never another reviewer's unsaved content.
    """

    def __init__(
        self,
        message: str = "the review changed since it was loaded",
        *,
        supplied_version: Optional[int] = None,
        current_version: Optional[int] = None,
        changed_at: Optional[datetime] = None,
        review_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.supplied_version = supplied_version
        self.current_version = current_version
        self.changed_at = changed_at
        self.review_id = review_id


class InvalidReviewTransition(ReviewRepositoryError, ContractInvalidReviewTransition):
    """A review status change is not permitted by the closed table.

    It deliberately inherits both taxonomies: Stage 1 already shipped
    ``InvalidReviewTransition``, and the API layer must be able to catch either
    name without the two forking.
    """


class InvalidBatchTransition(ReviewRepositoryError, ContractInvalidBatchTransition):
    """A remediation-batch status change is not permitted."""


class InvalidImportTransition(ReviewRepositoryError, ContractInvalidImportTransition):
    """A ticket-import status change is not permitted."""


class ReviewIdentityConflict(ReviewRepositoryError):
    """A stored review belongs to a different DevRev work item."""


class NotAuthorized(ReviewRepositoryError):
    """The authenticated actor's role may not perform this mutation."""


class UnsupportedFilterCombination(ReviewRepositoryError):
    """The requested filter is outside the master query grammar.

    The API maps this to ``422 unsupported_filter_combination``. It is raised
    before any backend call so an unindexed query is never issued.
    """


class IdempotencyConflict(ReviewRepositoryError):
    """An idempotency key was reused for a different request."""


class EvidenceCandidateRejected(ReviewRepositoryError):
    """A broker evidence candidate is absent, foreign, expired, or replayed.

    The message never discloses whether another ticket's evidence exists.
    """


class BatchNotFound(ReviewRepositoryError):
    """No remediation batch exists for the requested id."""


class BatchVersionConflict(ReviewRepositoryError):
    """A batch mutation carried a stale ``expected_version``."""

    def __init__(
        self,
        message: str = "the batch changed since it was loaded",
        *,
        supplied_version: Optional[int] = None,
        current_version: Optional[int] = None,
        changed_at: Optional[datetime] = None,
    ) -> None:
        super().__init__(message)
        self.supplied_version = supplied_version
        self.current_version = current_version
        self.changed_at = changed_at


class BatchAlreadyClaimed(ReviewRepositoryError):
    """Another agent holds a live lease, or a token would be re-minted."""


class BatchLeaseLost(ReviewRepositoryError):
    """The lease token, owner, or window no longer authorizes this write."""


class BatchReleaseRefused(ReviewRepositoryError):
    """Releasing to ``ready`` is unsafe: durable work exists or versions drifted."""


class LeaseExtensionRefused(ReviewRepositoryError):
    """The bounded, one-time, admin-only lease extension is not available."""


class BatchContractViolation(ReviewRepositoryError):
    """A batch would be empty, duplicated, oversized, or over a byte limit."""


class RetentionRefused(ReviewRepositoryError):
    """A retention run was asked for something it must not do."""


# =====================================================================
# Small helpers
# =====================================================================


def sha256_hex(value: str) -> str:
    """Lowercase hex SHA-256 of the NFC-normalized UTF-8 text."""
    return hashlib.sha256(unicodedata.normalize("NFC", value).encode("utf-8")).hexdigest()


# The Stage 1 state machines raise the Stage 1 contract errors. The repository
# re-raises its own dual-taxonomy classes so the API layer gets one hierarchy it
# can map, and so `except ReviewRepositoryError` really does catch everything
# this module can fail with.


def _assert_review_transition(*args: Any, **kwargs: Any) -> None:
    try:
        assert_review_transition(*args, **kwargs)
    except ContractInvalidReviewTransition as exc:
        raise InvalidReviewTransition(str(exc)) from exc


def _assert_batch_transition(*args: Any, **kwargs: Any) -> None:
    try:
        assert_batch_transition(*args, **kwargs)
    except ContractInvalidBatchTransition as exc:
        raise InvalidBatchTransition(str(exc)) from exc


def _assert_import_transition(*args: Any, **kwargs: Any) -> None:
    try:
        assert_import_transition(*args, **kwargs)
    except ContractInvalidImportTransition as exc:
        raise InvalidImportTransition(str(exc)) from exc


def normalize_display_id(value: str) -> str:
    """Normalize a DevRev display id for the exact-lookup index.

    One ticket must have exactly one stored form, otherwise the single-field
    equality lookup silently misses.
    """
    normalized = unicodedata.normalize("NFC", value).strip().upper()
    if not normalized:
        raise ValueError("devrev_display_id is required")
    return normalized


def canonical_json(payload: Any) -> str:
    """The one canonical JSON encoding used for every digest in this module."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def item_set_digest(refs: Iterable[ReviewRef]) -> str:
    """Digest of the frozen ``(review_id, review_version)`` set."""
    return _digest(sorted([[ref.review_id, ref.review_version] for ref in refs]))


def review_index_declarations() -> list[dict[str, Any]]:
    """The only composite review indexes the query grammar can use.

    One queue index plus exactly one index per allowed facet. No Cartesian
    product, no title or prefix tokens, no multi-facet index.
    """
    tail = [
        {"fieldPath": "updated_at", "order": "DESCENDING"},
        {"fieldPath": "review_id", "order": "ASCENDING"},
    ]
    declarations: list[dict[str, Any]] = [
        {
            "collectionGroup": REVIEWS_COLLECTION,
            "queryScope": "COLLECTION",
            "fields": [{"fieldPath": "status", "order": "ASCENDING"}, *tail],
        }
    ]
    for facet in sorted(ALLOWED_REVIEW_FACETS):
        declarations.append(
            {
                "collectionGroup": REVIEWS_COLLECTION,
                "queryScope": "COLLECTION",
                "fields": [
                    {"fieldPath": "status", "order": "ASCENDING"},
                    {"fieldPath": facet, "order": "ASCENDING"},
                    *tail,
                ],
            }
        )
    return declarations


def canonical_ttl_declarations() -> list[dict[str, Any]]:
    """The four, and only four, TTL field overrides the console declares."""
    return [
        {"collectionGroup": collection, "fieldPath": TTL_FIELD, "ttl": True}
        for collection in sorted(TTL_COLLECTIONS)
    ]


def _plain(value: Any) -> Any:
    """Enums become their value; datetimes stay native for Firestore TTL."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value
    enum_value = getattr(value, "value", None)
    if enum_value is not None and value.__class__.__module__ != "builtins":
        if isinstance(enum_value, (str, int)):
            return enum_value
    return value


def _to_doc(model: BaseModel, **envelope: Any) -> Document:
    """Serialize a model plus repository envelope fields.

    ``mode="python"`` keeps datetimes native. Envelope fields carry storage
    concerns the Stage 1 models deliberately do not model (retention on an
    ``extra="forbid"`` audit event, chain bookkeeping, TTL), so the round trip
    is intentionally asymmetric and :func:`_from_doc` drops them again.
    """
    doc = cast(Document, _plain(model.model_dump(mode="python")))
    doc.update({key: _plain(item) for key, item in envelope.items()})
    return doc


def _from_doc(model: type[ModelT], doc: Mapping[str, Any]) -> ModelT:
    """Validate a stored document back into its model, dropping the envelope."""
    fields = set(model.model_fields)
    return model.model_validate({k: v for k, v in doc.items() if k in fields})


def _document_bytes(doc: Mapping[str, Any]) -> int:
    """Approximate a document's stored size for the pre-write byte guard.

    Firestore's own accounting differs in detail; this is deliberately an
    over-estimate of the field values so the guard trips before the platform
    does, never after.
    """
    return len(json.dumps(doc, default=str, ensure_ascii=False).encode("utf-8"))


def _is_tombstone(doc: Mapping[str, Any]) -> bool:
    return doc.get(DOC_KIND_FIELD) == PURGED_TOMBSTONE_KIND


def _live(doc: Optional[Mapping[str, Any]], now: datetime) -> Optional[Mapping[str, Any]]:
    """Apply logical TTL expiry at the boundary.

    Firestore TTL deletion is asynchronous, so a physically present document
    with a missing, malformed, or elapsed ``expires_at`` is already absent.
    """
    if doc is None:
        return None
    expires_at = doc.get(TTL_FIELD)
    if not isinstance(expires_at, datetime):
        return None
    try:
        if expires_at <= now:
            return None
    except TypeError:  # pragma: no cover - naive/aware mismatch is malformed
        return None
    return doc


def _resolve_path(doc: Mapping[str, Any], dotted: str) -> Any:
    current: Any = doc
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


# =====================================================================
# Repository-local models
# =====================================================================


class _RepoBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MutationContext(BaseModel):
    """The authenticated actor and request metadata for one unsafe operation.

    The actor is *not* the assigned reviewer: every audit event records this
    identity independently of ``TicketReview.assigned_reviewer``. Stage 4's
    service layer builds this and must import it from here rather than
    redefining the name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: ReviewerIdentity = Field(...)
    actor_role: ReviewerRole = Field(...)
    request_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    idempotency_key: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    reason_code: Optional[str] = Field(default=None, max_length=MAX_METADATA_VALUE_LENGTH)


class ReviewListQuery(_RepoBase):
    """The only shape the master query grammar accepts.

    ``title_contains`` exists so an accidental substring filter fails loudly
    instead of being silently dropped: there is no full-text search in the MVP.
    ``cursor`` is deliberately unbounded here so an oversized token fails as a
    :class:`~api.ticket_review_models.CursorError` rather than a validation
    error.
    """

    statuses: list[ReviewStatus] = Field(default_factory=list)
    facets: dict[str, Union[str, int]] = Field(default_factory=dict)
    title_contains: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)
    devrev_display_id: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_ID_LENGTH)
    updated_after: Optional[AwareDatetime] = Field(default=None)
    updated_before: Optional[AwareDatetime] = Field(default=None)
    include_reversed: bool = Field(default=False)
    page_size: StrictInt = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: Optional[str] = Field(default=None)


class ReviewPatchSpec(_RepoBase):
    """One independent review update inside a multi-review operation."""

    review_id: Sha256Hex = Field(...)
    patch: ReviewPatch = Field(...)
    expected_version: StrictInt = Field(..., ge=1)
    admin_reopen: bool = Field(default=False)


class ReviewPatchFailure(_RepoBase):
    """Why one spec of a multi-review update did not apply."""

    review_id: Sha256Hex = Field(...)
    code: str = Field(..., max_length=MAX_TOPIC_LENGTH)
    current_version: Optional[StrictInt] = Field(default=None, ge=0)


class MultiPatchResult(_RepoBase):
    """The outcome of a partial multi-review update.

    ``applied`` carries only the reviews that really changed, so an unaffected
    review can never be reported -- or stored -- as resolved.
    """

    applied: list[TicketReview] = Field(default_factory=list)
    conflicts: list[ReviewPatchFailure] = Field(default_factory=list)
    failures: list[ReviewPatchFailure] = Field(default_factory=list)


class EvidenceCandidate(_RepoBase):
    """A service-validated broker candidate.

    The repository never accepts a caller-chosen execution or reference id:
    only this object, which the service mints after a bounded broker lookup and
    revalidates against the current broker result.
    """

    review_id: Sha256Hex = Field(...)
    evidence_reference: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    evidence_digest: Sha256Hex = Field(...)
    broker_result_digest: Sha256Hex = Field(...)
    issued_to_subject: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    expires_at: AwareDatetime = Field(...)
    correlation_trust: CorrelationTrust = Field(default=CorrelationTrust.MANUAL_REVIEWER)
    source_url: Optional[str] = Field(default=None, max_length=2_048)

    @property
    def link_id(self) -> str:
        """Deterministic link id, so one candidate can only link once."""
        return sha256_hex(f"{self.review_id}:{self.evidence_digest}")


class DevRevMessageCacheEntry(_RepoBase):
    """One bounded, disposable DevRev timeline snapshot.

    This is the only place a raw message body or a DevRev title may live, and
    it always carries a TTL.
    """

    remote_entry_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    devrev_work_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    object_version: Optional[StrictInt] = Field(default=None, ge=0)
    remote_modified_at: Optional[AwareDatetime] = Field(default=None)
    body: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_BODY_LENGTH)
    body_type: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    author_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)
    visibility: Optional[str] = Field(default=None, max_length=MAX_TOPIC_LENGTH)
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @field_validator("attachments")
    @classmethod
    def _bounded_attachment_metadata(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > MAX_ID_LENGTH:
                raise ValueError("attachment metadata is too long")
        return value

    @property
    def entry_id_hash(self) -> str:
        """A DON contains '/', so the hashed id is the document id."""
        return sha256_hex(self.remote_entry_id)


class ConsoleCacheEntry(_RepoBase):
    """Disposable list/detail metadata. TTL only, never durable state."""

    cache_key: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)
    payload: dict[str, str] = Field(default_factory=dict)


class TicketExportSummary(_RepoBase):
    """Durable export metadata. Never the CSV body, never a ticket title."""

    export_id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    schema_version: str = Field(default=SCHEMA_VERSION, max_length=MAX_TOPIC_LENGTH)
    created_by: ReviewerIdentity = Field(...)
    row_count: StrictInt = Field(default=0, ge=0, le=MAX_CSV_ROWS)
    file_sha256: Sha256Hex = Field(...)
    filter_fingerprint: Sha256Hex = Field(...)
    retention_expires_at: Optional[AwareDatetime] = Field(default=None)
    legal_hold: bool = Field(default=False)
    version: StrictInt = Field(default=1, ge=1)
    created_at: Optional[AwareDatetime] = Field(default=None)


class ImportRowSpec(_RepoBase):
    """One versioned import/reversal row."""

    row_number: StrictInt = Field(..., ge=1)
    review_id: Sha256Hex = Field(...)
    expected_review_version: StrictInt = Field(..., ge=1)
    patch: ReviewPatch = Field(...)
    raw_ticket_id: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_ID_LENGTH)
    created_by_import: bool = Field(default=False)


class BatchClaim(_RepoBase):
    """The claim result. The raw lease token is returned once, never stored."""

    batch: RemediationBatch = Field(...)
    lease_expires_at: AwareDatetime = Field(...)
    reclaimed: bool = Field(default=False)


class AuditChainReport(_RepoBase):
    """Whether a ledger still recomputes to one linear chain."""

    intact: bool = Field(...)
    event_count: StrictInt = Field(default=0, ge=0)
    broken_event_id: Optional[str] = Field(default=None, max_length=MAX_ID_LENGTH)
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_LENGTH)


class RetentionPreview(_RepoBase):
    """What a bounded retention run would do. No content, ids only."""

    product_candidates: list[str] = Field(default_factory=list)
    ledger_candidates: list[str] = Field(default_factory=list)
    tombstone_candidates: list[str] = Field(default_factory=list)
    disposable_candidates: list[str] = Field(default_factory=list)
    skipped_legal_hold: list[str] = Field(default_factory=list)
    counts_by_collection: dict[str, int] = Field(default_factory=dict)
    truncated: bool = Field(default=False)


class RetentionReport(_RepoBase):
    """What a bounded retention run actually did."""

    tombstoned: list[str] = Field(default_factory=list)
    product_documents_deleted: list[str] = Field(default_factory=list)
    ledger_events_deleted: list[str] = Field(default_factory=list)
    parents_deleted: list[str] = Field(default_factory=list)
    disposable_deleted: list[str] = Field(default_factory=list)
    skipped_legal_hold: list[str] = Field(default_factory=list)
    truncated: bool = Field(default=False)


class CursorPageOf(_RepoBase):
    """A bounded page of repository records with an opaque forward cursor."""

    items: list[Any] = Field(default_factory=list)
    next_cursor: Optional[str] = Field(default=None)
    page_size: StrictInt = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


# =====================================================================
# Backend contract
# =====================================================================


class TransactionView(Protocol):
    """The minimal primitive both backends share."""

    async def get(self, path: DocPath) -> Optional[Document]: ...

    def set(self, path: DocPath, value: Document) -> None: ...

    def delete(self, path: DocPath) -> None: ...


class TicketReviewBackend(Protocol):
    """Typed backend contract. Business logic never touches the SDK."""

    server_timestamp: Any

    async def transact(
        self, fn: Callable[[TransactionView], Awaitable[TxnResult]]
    ) -> TxnResult: ...

    async def get_doc(self, path: DocPath) -> Optional[Document]: ...

    async def query_reviews(
        self,
        *,
        statuses: Sequence[str],
        facet: Optional[str] = None,
        facet_value: Any = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        start_after: Optional[tuple[datetime, str]] = None,
    ) -> list[tuple[str, Document]]: ...

    async def query_reviews_by_display_id(
        self, display_id: str, *, limit: int = 2
    ) -> list[tuple[str, Document]]: ...

    async def list_subcollection(
        self,
        parent: DocPath,
        subcollection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]: ...

    async def list_collection(
        self,
        collection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]: ...

    async def scan_by_field(
        self,
        collection: str,
        *,
        field: str,
        before: datetime,
        limit: int = DEFAULT_RETENTION_MAX_DOCUMENTS,
        ) -> list[tuple[str, Document]]: ...


# =====================================================================
# In-memory backend
# =====================================================================


class _MemoryTxnView:
    """Reads see the committed snapshot; writes apply only on success."""

    def __init__(self, data: dict[DocPath, Document]) -> None:
        self._data = data
        self._staged: dict[DocPath, Optional[Document]] = {}

    async def get(self, path: DocPath) -> Optional[Document]:
        if path in self._staged:
            staged = self._staged[path]
            return copy.deepcopy(staged) if staged is not None else None
        doc = self._data.get(path)
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, path: DocPath, value: Document) -> None:
        if len(path) % 2 != 0:
            raise ValueError("a document path must name an exact document")
        self._staged[path] = copy.deepcopy(value)

    def delete(self, path: DocPath) -> None:
        if len(path) % 2 != 0:
            raise ValueError("a document path must name an exact document")
        self._staged[path] = None

    def apply(self) -> list[DocPath]:
        deleted: list[DocPath] = []
        for path, value in self._staged.items():
            if value is None:
                self._data.pop(path, None)
                deleted.append(path)
            else:
                self._data[path] = value
        return deleted

    @property
    def write_count(self) -> int:
        return len(self._staged)


class InMemoryTicketReviewBackend:
    """Transactional in-memory backend for the contract tests.

    Transactions are serialized, which is the strictest reading of Firestore's
    serializable isolation, so a contract proven here cannot be looser than the
    emulator's.
    """

    server_timestamp: Any = None

    def __init__(self) -> None:
        self._data: dict[DocPath, Document] = {}
        self._lock = asyncio.Lock()
        self.deleted_paths: list[DocPath] = []
        self.max_writes_in_one_transaction = 0

    async def transact(
        self, fn: Callable[[TransactionView], Awaitable[TxnResult]]
    ) -> TxnResult:
        async with self._lock:
            view = _MemoryTxnView(self._data)
            result = await fn(view)
            if view.write_count > FIRESTORE_MAX_WRITES_PER_TRANSACTION:
                raise BatchContractViolation(
                    "a transaction may not exceed "
                    f"{FIRESTORE_MAX_WRITES_PER_TRANSACTION} writes"
                )
            self.max_writes_in_one_transaction = max(
                self.max_writes_in_one_transaction, view.write_count
            )
            self.deleted_paths.extend(view.apply())
            return result

    async def get_doc(self, path: DocPath) -> Optional[Document]:
        doc = self._data.get(path)
        return copy.deepcopy(doc) if doc is not None else None

    async def query_reviews(
        self,
        *,
        statuses: Sequence[str],
        facet: Optional[str] = None,
        facet_value: Any = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        start_after: Optional[tuple[datetime, str]] = None,
    ) -> list[tuple[str, Document]]:
        wanted = set(statuses)
        rows: list[tuple[str, Document]] = []
        for path, doc in self._data.items():
            if len(path) != 2 or path[0] != REVIEWS_COLLECTION or _is_tombstone(doc):
                continue
            if doc.get("status") not in wanted:
                continue
            if facet is not None and _resolve_path(doc, facet) != facet_value:
                continue
            updated_at = doc.get("updated_at")
            if not isinstance(updated_at, datetime):
                continue
            if updated_after is not None and updated_at < updated_after:
                continue
            if updated_before is not None and updated_at > updated_before:
                continue
            if start_after is not None:
                last_at, last_id = start_after
                if updated_at > last_at or (updated_at == last_at and path[1] <= last_id):
                    continue
            rows.append((path[1], copy.deepcopy(doc)))
        rows.sort(key=lambda row: (-row[1]["updated_at"].timestamp(), row[0]))
        return rows[:limit]

    async def query_reviews_by_display_id(
        self, display_id: str, *, limit: int = 2
    ) -> list[tuple[str, Document]]:
        rows = [
            (path[1], copy.deepcopy(doc))
            for path, doc in self._data.items()
            if len(path) == 2
            and path[0] == REVIEWS_COLLECTION
            and not _is_tombstone(doc)
            and doc.get("devrev_display_id") == display_id
        ]
        rows.sort(key=lambda row: row[0])
        return rows[:limit]

    async def list_subcollection(
        self,
        parent: DocPath,
        subcollection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]:
        prefix = (*parent, subcollection)
        rows = [
            (path[-1], copy.deepcopy(doc))
            for path, doc in self._data.items()
            if len(path) == len(prefix) + 1 and path[:-1] == prefix
        ]
        if order_by is None:
            rows.sort(key=lambda row: row[0])
        else:
            # Firestore appends __name__ as the implicit final sort key, so an
            # ordered query is a total order, and it omits documents that lack
            # the ordered field. Mirror both exactly.
            rows = [row for row in rows if row[1].get(order_by) is not None]
            rows.sort(key=lambda row: (row[1][order_by], row[0]))
        if start_after_id is not None:
            rows = [row for row in rows if row[0] > start_after_id]
        return rows[:limit]

    async def list_collection(
        self,
        collection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]:
        rows = [
            (path[1], copy.deepcopy(doc))
            for path, doc in self._data.items()
            if len(path) == 2 and path[0] == collection
        ]
        if order_by is None:
            rows.sort(key=lambda row: row[0])
        else:
            # Firestore omits documents that lack the ordered field entirely;
            # mirror that rather than inventing an ordering for them.
            rows = [row for row in rows if row[1].get(order_by) is not None]
            rows.sort(key=lambda row: (row[1][order_by], row[0]))
        if start_after_id is not None:
            rows = [row for row in rows if row[0] > start_after_id]
        return rows[:limit]

    async def scan_by_field(
        self,
        collection: str,
        *,
        field: str,
        before: datetime,
        limit: int = DEFAULT_RETENTION_MAX_DOCUMENTS,
    ) -> list[tuple[str, Document]]:
        rows: list[tuple[str, Document]] = []
        for path, doc in self._data.items():
            if len(path) != 2 or path[0] != collection:
                continue
            value = doc.get(field)
            if isinstance(value, datetime) and value <= before:
                rows.append((path[1], copy.deepcopy(doc)))
        rows.sort(key=lambda row: (row[1][field], row[0]))
        return rows[:limit]

    # -- test/inspection helpers (in-memory only) ----------------------

    async def dump_collection(self, collection: str) -> dict[str, Document]:
        return {
            path[1]: copy.deepcopy(doc)
            for path, doc in self._data.items()
            if len(path) == 2 and path[0] == collection
        }

    async def dump_subcollection(
        self, collection: str, doc_id: str, subcollection: str
    ) -> dict[str, Document]:
        prefix = (collection, doc_id, subcollection)
        return {
            path[3]: copy.deepcopy(doc)
            for path, doc in self._data.items()
            if len(path) == 4 and path[:3] == prefix
        }

    async def force_write(self, path: DocPath, doc: Document) -> None:
        """Tamper with storage directly, bypassing every invariant.

        Only a test may call this: it is how a mutated ledger is simulated.
        """
        self._data[tuple(path)] = copy.deepcopy(doc)


# =====================================================================
# Firestore backend
# =====================================================================


class FirestoreTicketReviewBackend:
    """Thin Firestore backend. No business logic lives here.

    The named database is mandatory and is the isolation boundary; there is no
    collection prefix and ``(default)`` is never assumed. Staging and
    production refuse ``(default)`` outright.
    """

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        database: str = "",
        environment: str = "production",
    ) -> None:
        import google.cloud.firestore as firestore  # lazy: keeps tests SDK-free

        resolved = resolve_tickets_firestore_database(database, environment=environment)
        if environment in STRICT_ENVIRONMENTS and resolved == DEFAULT_FIRESTORE_DATABASE:
            # Unreachable via the resolver; kept as a local fail-closed guard so
            # this class can never be the place the rule is lost.
            raise ValueError(
                "FIRESTORE_DATABASE must be a dedicated named database, never (default)"
            )
        self._firestore = firestore
        self._client = firestore.AsyncClient(project=project or None, database=resolved)
        self.database = resolved
        self.environment = environment
        self.server_timestamp = firestore.SERVER_TIMESTAMP

    def _ref(self, path: DocPath) -> Any:
        """Resolve an exact document path. No prefix is ever applied."""
        if len(path) % 2 != 0 or not path:
            raise ValueError("a document path must name an exact document")
        ref = self._client.collection(path[0]).document(path[1])
        for index in range(2, len(path), 2):
            ref = ref.collection(path[index]).document(path[index + 1])
        return ref

    async def transact(
        self, fn: Callable[[TransactionView], Awaitable[TxnResult]]
    ) -> TxnResult:
        firestore = self._firestore
        backend = self

        class _FirestoreView:
            def __init__(self, txn: Any) -> None:
                self._txn = txn
                self._writes: list[tuple[str, Any, Optional[Document]]] = []

            async def get(self, path: DocPath) -> Optional[Document]:
                snapshot = await backend._ref(path).get(transaction=self._txn)
                return cast(Optional[Document], snapshot.to_dict() if snapshot.exists else None)

            def set(self, path: DocPath, value: Document) -> None:
                self._writes.append(("set", backend._ref(path), value))

            def delete(self, path: DocPath) -> None:
                self._writes.append(("delete", backend._ref(path), None))

            def flush(self) -> None:
                if len(self._writes) > FIRESTORE_MAX_WRITES_PER_TRANSACTION:
                    raise BatchContractViolation(
                        "a transaction may not exceed "
                        f"{FIRESTORE_MAX_WRITES_PER_TRANSACTION} writes"
                    )
                for op, ref, value in self._writes:
                    if op == "set":
                        self._txn.set(ref, value)
                    else:
                        self._txn.delete(ref)

        transaction = self._client.transaction()
        transactional = cast(
            Callable[
                [Callable[[Any], Awaitable[TxnResult]]],
                Callable[[Any], Awaitable[TxnResult]],
            ],
            firestore.async_transactional,
        )

        @transactional
        async def _run(txn: Any) -> TxnResult:
            view = _FirestoreView(txn)
            result = await fn(view)
            view.flush()
            return result

        return await _run(transaction)

    async def get_doc(self, path: DocPath) -> Optional[Document]:
        snapshot = await self._ref(path).get()
        return cast(Optional[Document], snapshot.to_dict() if snapshot.exists else None)

    async def query_reviews(
        self,
        *,
        statuses: Sequence[str],
        facet: Optional[str] = None,
        facet_value: Any = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        start_after: Optional[tuple[datetime, str]] = None,
    ) -> list[tuple[str, Document]]:
        firestore = self._firestore
        query: Any = self._client.collection(REVIEWS_COLLECTION).where(
            filter=firestore.FieldFilter("status", "in", list(statuses))
        )
        if facet is not None:
            query = query.where(filter=firestore.FieldFilter(facet, "==", facet_value))
        if updated_after is not None:
            query = query.where(
                filter=firestore.FieldFilter("updated_at", ">=", updated_after)
            )
        if updated_before is not None:
            query = query.where(
                filter=firestore.FieldFilter("updated_at", "<=", updated_before)
            )
        query = query.order_by("updated_at", direction="DESCENDING").order_by("review_id")
        if start_after is not None:
            query = query.start_after({"updated_at": start_after[0], "review_id": start_after[1]})
        rows: list[tuple[str, Document]] = []
        async for snapshot in query.limit(limit).stream():
            rows.append((snapshot.id, cast(Document, snapshot.to_dict())))
        return rows

    async def query_reviews_by_display_id(
        self, display_id: str, *, limit: int = 2
    ) -> list[tuple[str, Document]]:
        firestore = self._firestore
        query = (
            self._client.collection(REVIEWS_COLLECTION)
            .where(filter=firestore.FieldFilter("devrev_display_id", "==", display_id))
            .limit(limit)
        )
        rows: list[tuple[str, Document]] = []
        async for snapshot in query.stream():
            rows.append((snapshot.id, cast(Document, snapshot.to_dict())))
        return rows

    async def list_subcollection(
        self,
        parent: DocPath,
        subcollection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]:
        collection = self._ref(parent).collection(subcollection)
        query: Any = collection
        if order_by is not None:
            # Firestore's implicit trailing __name__ sort key makes this a total
            # order without a second declared index.
            query = query.order_by(order_by)
        else:
            query = query.order_by("__name__")
        if start_after_id is not None:
            query = query.start_after(collection.document(start_after_id))
        rows: list[tuple[str, Document]] = []
        async for snapshot in query.limit(limit).stream():
            rows.append((snapshot.id, cast(Document, snapshot.to_dict())))
        return rows

    async def list_collection(
        self,
        collection: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Optional[str] = None,
        start_after_id: Optional[str] = None,
    ) -> list[tuple[str, Document]]:
        handle = self._client.collection(collection)
        # An ordered query gets Firestore's implicit trailing __name__ sort key,
        # so a single-field automatic index is enough for a total order.
        query: Any = handle.order_by(order_by) if order_by else handle.order_by("__name__")
        if start_after_id is not None:
            query = query.start_after(handle.document(start_after_id))
        rows: list[tuple[str, Document]] = []
        async for snapshot in query.limit(limit).stream():
            rows.append((snapshot.id, cast(Document, snapshot.to_dict())))
        return rows

    async def scan_by_field(
        self,
        collection: str,
        *,
        field: str,
        before: datetime,
        limit: int = DEFAULT_RETENTION_MAX_DOCUMENTS,
    ) -> list[tuple[str, Document]]:
        firestore = self._firestore
        query = (
            self._client.collection(collection)
            .where(filter=firestore.FieldFilter(field, "<=", before))
            .order_by(field)
            .limit(limit)
        )
        rows: list[tuple[str, Document]] = []
        async for snapshot in query.stream():
            rows.append((snapshot.id, cast(Document, snapshot.to_dict())))
        return rows


# =====================================================================
# Repository facade
# =====================================================================

_REVIEW_MUTATORS = frozenset(
    {ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR, ReviewerRole.ADMIN}
)
_ALL_REVIEW_STATUS_VALUES = tuple(status.value for status in ReviewStatus)
# Durable batch work that makes a release-to-ready unsafe.
_BATCH_WORK_FIELDS = (
    "plan_artifact",
    "branch",
    "commit_sha",
    "pr_url",
    "verification_summary",
)


class TicketReviewRepository:
    """Every review, audit, evidence, batch, import/export, and retention
    invariant, expressed once, over an interchangeable backend."""

    def __init__(
        self,
        backend: TicketReviewBackend,
        *,
        cursor_key: bytes,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        review_retention_days: int = REVIEW_RETENTION_DAYS,
        audit_retention_days: int = AUDIT_RETENTION_DAYS,
        cache_ttl_s: int = CACHE_TTL_S,
        message_cache_ttl_s: int = MESSAGE_CACHE_TTL_S,
        idempotency_ttl_s: int = IDEMPOTENCY_TTL_S,
        import_staging_ttl_s: int = IMPORT_STAGING_TTL_S,
        max_batch_reviews: int = MAX_BATCH_REVIEWS,
        lease_s: int = REMEDIATION_LEASE_S,
        max_continuous_lease_s: int = REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    ) -> None:
        self.backend = backend
        self._cursor_key = cursor_key
        self._clock = clock
        self._new_id = id_factory
        # Retention may be tightened by configuration but never below the
        # agreed contract, and the audit ledger always outlives product state.
        self._review_retention = timedelta(days=min(review_retention_days, REVIEW_RETENTION_DAYS))
        self._audit_retention = timedelta(days=min(audit_retention_days, AUDIT_RETENTION_DAYS))
        if self._audit_retention <= self._review_retention:
            raise ValueError("the audit ledger must outlive durable product state")
        self._cache_ttl = timedelta(seconds=cache_ttl_s)
        self._message_cache_ttl = timedelta(seconds=message_cache_ttl_s)
        self._idempotency_ttl = timedelta(seconds=idempotency_ttl_s)
        self._import_staging_ttl = timedelta(seconds=import_staging_ttl_s)
        self._max_batch_reviews = min(max_batch_reviews, MAX_BATCH_REVIEWS)
        self._lease = timedelta(seconds=lease_s)
        self._max_continuous_lease = timedelta(seconds=max_continuous_lease_s)

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        backend: Optional[TicketReviewBackend] = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> TicketReviewRepository:
        """Build a repository from validated console settings.

        ``backend`` is injectable so a test never constructs a Firestore client.
        """
        from api.tickets_console_config import decode_cursor_aead_key

        resolved_backend = backend or FirestoreTicketReviewBackend(
            project=settings.GCP_PROJECT,
            database=settings.FIRESTORE_DATABASE,
            environment=settings.ENVIRONMENT,
        )
        return cls(
            resolved_backend,
            cursor_key=decode_cursor_aead_key(settings.CURSOR_AEAD_KEY),
            clock=clock,
            id_factory=id_factory or (lambda: uuid.uuid4().hex),
            review_retention_days=settings.REVIEW_RETENTION_DAYS,
            audit_retention_days=settings.AUDIT_RETENTION_DAYS,
            cache_ttl_s=settings.CACHE_TTL_S,
            message_cache_ttl_s=settings.MESSAGE_CACHE_TTL_S,
            idempotency_ttl_s=settings.IDEMPOTENCY_TTL_S,
            import_staging_ttl_s=settings.IMPORT_STAGING_TTL_S,
            max_batch_reviews=settings.MAX_BATCH_REVIEWS,
            lease_s=settings.REMEDIATION_LEASE_S,
            max_continuous_lease_s=settings.REMEDIATION_MAX_CONTINUOUS_LEASE_S,
        )

    # ------------------------------------------------------------------
    # Internals: envelopes, audit append, idempotency
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _created_at(self, now: datetime) -> Any:
        """``SERVER_TIMESTAMP`` where the backend has one, else the clock."""
        sentinel = getattr(self.backend, "server_timestamp", None)
        return sentinel if sentinel is not None else now

    def _product_envelope(self, now: datetime, *, legal_hold: bool = False) -> dict[str, Any]:
        return {
            RETENTION_FIELD: now + self._review_retention,
            LEGAL_HOLD_FIELD: legal_hold,
        }

    def _stamp_product(self, doc: Document, now: datetime) -> Document:
        """Park or set the 730-day product clock according to the legal hold.

        Call this on every durable PARENT write, after ``legal_hold`` is set.
        """
        expiry = now + self._review_retention
        if bool(doc.get(LEGAL_HOLD_FIELD)):
            doc.pop(RETENTION_FIELD, None)
            doc[HELD_RETENTION_FIELD] = expiry
        else:
            doc.pop(HELD_RETENTION_FIELD, None)
            doc[RETENTION_FIELD] = expiry
        return doc

    def _ledger_envelope(self, now: datetime, *, legal_hold: bool = False) -> dict[str, Any]:
        return {
            RETENTION_FIELD: now + self._audit_retention,
            LEGAL_HOLD_FIELD: legal_hold,
            "created_at": self._created_at(now),
        }

    def _build_event(
        self,
        *,
        parent_kind: str,
        parent_id: str,
        event_type: str,
        context: MutationContext,
        now: datetime,
        previous_event_hash: str,
        previous_version: Optional[int] = None,
        new_version: Optional[int] = None,
        changed_fields: Optional[Sequence[str]] = None,
    ) -> AuditEvent:
        metadata: dict[str, str] = {}
        if context.reason_code:
            metadata["reason_code"] = context.reason_code
        event = AuditEvent(
            event_id=self._new_id(),
            parent_kind=parent_kind,
            parent_id=parent_id,
            event_type=event_type,
            actor_subject=context.actor.subject,
            actor_email=context.actor.email,
            actor_subject_hash=sha256_hex(context.actor.subject),
            request_id_hash=sha256_hex(context.request_id) if context.request_id else None,
            idempotency_key_hash=(
                sha256_hex(context.idempotency_key) if context.idempotency_key else None
            ),
            occurred_at_unix_us=int(now.timestamp() * 1_000_000),
            previous_version=previous_version,
            new_version=new_version,
            changed_fields=sorted(set(changed_fields or ())),
            metadata=metadata,
            previous_event_hash=previous_event_hash,
        )
        return event.model_copy(update={"event_hash": compute_audit_event_hash(event)})

    async def _append_event(
        self,
        view: TransactionView,
        *,
        ledger_path: DocPath,
        parent_doc: Optional[Document],
        parent_kind: str,
        parent_id: str,
        event_type: str,
        context: MutationContext,
        now: datetime,
        previous_version: Optional[int] = None,
        new_version: Optional[int] = None,
        changed_fields: Optional[Sequence[str]] = None,
    ) -> AuditEvent:
        """Append one event and advance the chain head on its parent.

        The head lives on the parent document that this same transaction
        writes, so two concurrent appends contend on one document and the chain
        stays linear -- there is no query-based head lookup to race.
        """
        head = GENESIS_EVENT_HASH
        if parent_doc is not None:
            head = parent_doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH
        event = self._build_event(
            parent_kind=parent_kind,
            parent_id=parent_id,
            event_type=event_type,
            context=context,
            now=now,
            previous_event_hash=head,
            previous_version=previous_version,
            new_version=new_version,
            changed_fields=changed_fields,
        )
        view.set(
            (*ledger_path, event.event_id),
            _to_doc(event, **self._ledger_envelope(now)),
        )
        if parent_doc is not None:
            parent_doc[CHAIN_HEAD_FIELD] = event.event_hash
            parent_doc[CHAIN_COUNT_FIELD] = int(parent_doc.get(CHAIN_COUNT_FIELD) or 0) + 1
            parent_doc[LEDGER_EXPIRY_FIELD] = now + self._audit_retention
        return event

    async def _append_global_event(
        self,
        view: TransactionView,
        *,
        parent_kind: str,
        parent_id: str,
        event_type: str,
        context: MutationContext,
        now: datetime,
        changed_fields: Optional[Sequence[str]] = None,
    ) -> AuditEvent:
        head_path = (GLOBAL_AUDIT_EVENTS_COLLECTION, GLOBAL_CHAIN_HEAD_DOC_ID)
        head_doc = await view.get(head_path) or {
            DOC_KIND_FIELD: CHAIN_HEAD_KIND,
            CHAIN_HEAD_FIELD: GENESIS_EVENT_HASH,
            CHAIN_COUNT_FIELD: 0,
        }
        event = self._build_event(
            parent_kind=parent_kind,
            parent_id=parent_id,
            event_type=event_type,
            context=context,
            now=now,
            previous_event_hash=head_doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
            changed_fields=changed_fields,
        )
        view.set(
            (GLOBAL_AUDIT_EVENTS_COLLECTION, event.event_id),
            _to_doc(event, **self._ledger_envelope(now)),
        )
        head_doc[CHAIN_HEAD_FIELD] = event.event_hash
        head_doc[CHAIN_COUNT_FIELD] = int(head_doc.get(CHAIN_COUNT_FIELD) or 0) + 1
        head_doc["updated_at"] = now
        view.set(head_path, head_doc)
        return event

    async def _claim_idempotency(
        self,
        view: TransactionView,
        context: MutationContext,
        *,
        operation: str,
        request: Any,
        now: datetime,
    ) -> tuple[Optional[Document], Optional[str], Optional[str]]:
        """Resolve the idempotency key before any quota, version, or write.

        Returns ``(replayed_record, key_hash, digest)``. A replay never
        re-increments a version or appends a second audit event; the same key
        with a different request digest is a conflict.
        """
        if not context.idempotency_key:
            return None, None, None
        key_hash = sha256_hex(context.idempotency_key)
        digest = _digest({"operation": operation, "request": request})
        stored = _live(await view.get((IDEMPOTENCY_KEYS_COLLECTION, key_hash)), now)
        if stored is None:
            return None, key_hash, digest
        if stored.get("request_digest") != digest:
            raise IdempotencyConflict(
                "this idempotency key was already used for a different request"
            )
        return dict(stored), key_hash, digest

    def _record_idempotency(
        self,
        view: TransactionView,
        *,
        key_hash: Optional[str],
        digest: Optional[str],
        operation: str,
        result: Mapping[str, Any],
        now: datetime,
    ) -> None:
        if key_hash is None or digest is None:
            return
        view.set(
            (IDEMPOTENCY_KEYS_COLLECTION, key_hash),
            {
                "key_hash": key_hash,
                "request_digest": digest,
                "operation": operation,
                "result": dict(result),
                "created_at": now,
                TTL_FIELD: now + self._idempotency_ttl,
            },
        )

    @staticmethod
    def _derive_context(context: MutationContext, suffix: str) -> MutationContext:
        """Give one fanned-out sub-operation its own idempotency key.

        A chunk or bulk request carries ONE Idempotency-Key but fans out into
        many independent per-record mutations. Reusing the key verbatim would let
        the first record store a receipt that every later record then collides
        with, so a 100-row chunk would apply exactly one row and report the rest
        as failures -- and no retry could ever repair it.
        """
        if not context.idempotency_key:
            return context
        return context.model_copy(
            update={"idempotency_key": f"{context.idempotency_key}:{suffix}"}
        )

    async def _load_review_doc(
        self, view: TransactionView, review_id: str
    ) -> Document:
        doc = await view.get((REVIEWS_COLLECTION, review_id))
        if doc is None or _is_tombstone(doc):
            raise ReviewNotFound("no review exists for that id")
        return doc

    @staticmethod
    def _assert_version(doc: Document, expected_version: Optional[int]) -> None:
        current = int(doc.get("version") or 0)
        if expected_version is None or expected_version == current:
            return
        raise ReviewVersionConflict(
            supplied_version=expected_version,
            current_version=current,
            changed_at=doc.get("updated_at"),
            review_id=doc.get("review_id"),
        )

    # ------------------------------------------------------------------
    # Reviews
    # ------------------------------------------------------------------

    async def create_or_get_review(
        self, review: TicketReview, *, context: MutationContext
    ) -> tuple[TicketReview, bool]:
        """Idempotently create the durable review for one DevRev work item."""
        expected_id = review_id_for_devrev_work(review.devrev_work_id)
        if review.review_id != expected_id:
            raise ReviewIdentityConflict(
                "review_id must be the deterministic digest of devrev_work_id"
            )
        if context.actor_role not in _REVIEW_MUTATORS:
            raise NotAuthorized(f"role '{context.actor_role.value}' may not create a review")
        now = self._now()
        candidate = review.model_copy(
            update={
                "devrev_display_id": normalize_display_id(review.devrev_display_id),
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
        )
        path = (REVIEWS_COLLECTION, candidate.review_id)

        async def _txn(view: TransactionView) -> tuple[Document, bool]:
            existing = await view.get(path)
            if existing is not None and not _is_tombstone(existing):
                if existing.get("devrev_work_id") != candidate.devrev_work_id:
                    raise ReviewIdentityConflict(
                        "that review id belongs to a different DevRev work item"
                    )
                return existing, False
            if existing is not None and _is_tombstone(existing):
                raise ReviewIdentityConflict("that review was purged and cannot be recreated")
            doc = _to_doc(candidate)
            doc.update(
                {
                    DOC_KIND_FIELD: "review",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: False,
                    CHAIN_HEAD_FIELD: GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: 0,
                }
            )
            self._stamp_product(doc, now)
            await self._append_event(
                view,
                ledger_path=(*path, AUDIT_EVENTS_SUBCOLLECTION),
                parent_doc=doc,
                parent_kind="review",
                parent_id=candidate.review_id,
                event_type="review_created",
                context=context,
                now=now,
                new_version=1,
            )
            view.set(path, doc)
            return doc, True

        doc, created = await self.backend.transact(_txn)
        return _from_doc(TicketReview, doc), created

    async def get_review(self, review_id: str) -> TicketReview:
        doc = await self.backend.get_doc((REVIEWS_COLLECTION, review_id))
        if doc is None or _is_tombstone(doc):
            raise ReviewNotFound("no review exists for that id")
        return _from_doc(TicketReview, doc)

    async def find_review_by_display_id(self, display_id: str) -> Optional[TicketReview]:
        """Exact normalized ticket-id lookup. Never combined with queue facets."""
        try:
            normalized = normalize_display_id(display_id)
        except ValueError as exc:
            # A blank search box is a rejected filter, not a 500: keep every
            # failure inside a hierarchy the API layer is told to catch.
            raise UnsupportedFilterCombination(
                "an exact ticket id lookup needs a non-empty ticket id"
            ) from exc
        rows = await self.backend.query_reviews_by_display_id(normalized, limit=2)
        if not rows:
            return None
        return _from_doc(TicketReview, rows[0][1])

    async def patch_review(
        self,
        review_id: str,
        patch: ReviewPatch,
        *,
        expected_version: Optional[int],
        context: MutationContext,
        admin_reopen: bool = False,
    ) -> TicketReview:
        """Apply one versioned review patch, transactionally with its audit event."""
        now = self._now()
        operation = "patch_review"
        request = {
            "review_id": review_id,
            "expected_version": expected_version,
            "admin_reopen": admin_reopen,
            "patch": patch.model_dump(mode="json", exclude_unset=True),
        }

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view, context, operation=operation, request=request, now=now
            )
            if replay is not None:
                return await self._load_review_doc(view, review_id)
            doc = await self._load_review_doc(view, review_id)
            current = _from_doc(TicketReview, doc)
            # Work with the model's own objects, never a dumped dict: the closed
            # state machine needs a real ReviewResolution to check evidence.
            updates = {field: getattr(patch, field) for field in patch.model_fields_set}

            target_status = updates.get("status")
            if admin_reopen or (target_status is not None and target_status != current.status):
                _assert_review_transition(
                    current.status,
                    target_status or current.status,
                    actor_role=context.actor_role,
                    resolution=updates.get("resolution") or current.resolution,
                    admin_reopen=admin_reopen,
                )
            if context.actor_role not in _REVIEW_MUTATORS:
                raise NotAuthorized(
                    f"role '{context.actor_role.value}' may not change a review"
                )
            target_reviewer = updates.get("assigned_reviewer")
            if target_reviewer is not None:
                if not can_assign_reviewer(
                    actor_role=context.actor_role,
                    actor=context.actor,
                    target=target_reviewer,
                    current_assignee=current.assigned_reviewer,
                ):
                    raise NotAuthorized("this actor may not assign that reviewer")

            self._assert_version(doc, expected_version)
            changed = [
                field for field, value in updates.items() if getattr(current, field) != value
            ]
            merged = current.model_copy(
                update={**updates, "version": current.version + 1, "updated_at": now}
            )
            if merged.status in TERMINAL_REVIEW_STATUSES and current.status != merged.status:
                merged = merged.model_copy(update={"resolved_at": now})
            if admin_reopen:
                merged = merged.model_copy(update={"resolved_at": None})

            new_doc = _to_doc(merged)
            new_doc.update(
                {
                    DOC_KIND_FIELD: "review",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: bool(doc.get(LEGAL_HOLD_FIELD, merged.legal_hold)),
                    CHAIN_HEAD_FIELD: doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            # Immutable identity and creation time never move.
            new_doc["review_id"] = doc["review_id"]
            new_doc["devrev_work_id"] = doc["devrev_work_id"]
            new_doc["devrev_display_id"] = doc["devrev_display_id"]
            new_doc["created_at"] = doc["created_at"]
            self._stamp_product(new_doc, now)
            self._stamp_product(new_doc, now)
            await self._append_event(
                view,
                ledger_path=(REVIEWS_COLLECTION, review_id, AUDIT_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="review",
                parent_id=review_id,
                event_type="review_reopened" if admin_reopen else "review_updated",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=changed,
            )
            view.set((REVIEWS_COLLECTION, review_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation=operation,
                result={"review_id": review_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(TicketReview, await self.backend.transact(_txn))

    async def patch_reviews(
        self, specs: Sequence[ReviewPatchSpec], *, context: MutationContext
    ) -> MultiPatchResult:
        """Apply independent review patches without cross-contamination.

        Each spec gets its own transaction, so a failure or version conflict in
        one can never mark an unaffected review resolved.
        """
        applied: list[TicketReview] = []
        conflicts: list[ReviewPatchFailure] = []
        failures: list[ReviewPatchFailure] = []
        for spec in specs:
            try:
                applied.append(
                    await self.patch_review(
                        spec.review_id,
                        spec.patch,
                        expected_version=spec.expected_version,
                        context=self._derive_context(context, f"review:{spec.review_id}"),
                        admin_reopen=spec.admin_reopen,
                    )
                )
            except ReviewVersionConflict as conflict:
                conflicts.append(
                    ReviewPatchFailure(
                        review_id=spec.review_id,
                        code="review_version_conflict",
                        current_version=conflict.current_version,
                    )
                )
            except ReviewRepositoryError as error:
                failures.append(
                    ReviewPatchFailure(
                        review_id=spec.review_id,
                        code=type(error).__name__,
                    )
                )
        return MultiPatchResult(applied=applied, conflicts=conflicts, failures=failures)

    async def mark_import_state(
        self,
        review_id: str,
        state: ImportState,
        *,
        expected_version: Optional[int],
        context: MutationContext,
        event_type: str = "review_import_state_changed",
    ) -> TicketReview:
        """Flip ``import_state`` without deleting anything."""
        return await self._simple_review_field_update(
            review_id,
            {"import_state": state.value},
            event_type=event_type,
            changed_fields=["import_state"],
            expected_version=expected_version,
            context=context,
        )

    async def set_legal_hold(
        self,
        review_id: str,
        legal_hold: bool,
        *,
        context: MutationContext,
        expected_version: Optional[int] = None,
    ) -> TicketReview:
        """Set or clear the legal hold that suppresses every purge."""
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may change a legal hold")
        return await self._simple_review_field_update(
            review_id,
            {LEGAL_HOLD_FIELD: legal_hold},
            event_type="review_legal_hold_set",
            changed_fields=[LEGAL_HOLD_FIELD],
            expected_version=expected_version,
            context=context,
        )

    async def _simple_review_field_update(
        self,
        review_id: str,
        updates: Mapping[str, Any],
        *,
        event_type: str,
        changed_fields: Sequence[str],
        expected_version: Optional[int],
        context: MutationContext,
    ) -> TicketReview:
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation=event_type,
                request={"review_id": review_id, "updates": _plain(dict(updates))},
                now=now,
            )
            if replay is not None:
                return await self._load_review_doc(view, review_id)
            doc = await self._load_review_doc(view, review_id)
            if context.actor_role not in _REVIEW_MUTATORS:
                raise NotAuthorized("this actor may not change a review")
            self._assert_version(doc, expected_version)
            current = _from_doc(TicketReview, doc)
            merged = current.model_copy(
                update={**updates, "version": current.version + 1, "updated_at": now}
            )
            new_doc = _to_doc(merged)
            new_doc.update(
                {
                    DOC_KIND_FIELD: "review",
                    RETENTION_FIELD: now + self._review_retention,
                    CHAIN_HEAD_FIELD: doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            new_doc["created_at"] = doc["created_at"]
            self._stamp_product(new_doc, now)
            await self._append_event(
                view,
                ledger_path=(REVIEWS_COLLECTION, review_id, AUDIT_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="review",
                parent_id=review_id,
                event_type=event_type,
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=changed_fields,
            )
            view.set((REVIEWS_COLLECTION, review_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation=event_type,
                result={"review_id": review_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(TicketReview, await self.backend.transact(_txn))

    # ------------------------------------------------------------------
    # Listing: master query grammar and sealed cursors
    # ------------------------------------------------------------------

    def _filter_fingerprint(self, query: ReviewListQuery) -> str:
        """A normalized, hashed filter identity. Carries no PII by construction."""
        facet = next(iter(sorted(query.facets)), None)
        return _digest(
            {
                "statuses": sorted(status.value for status in query.statuses),
                "facet": facet,
                "facet_value": None if facet is None else str(query.facets[facet]),
                "updated_after": (
                    None if query.updated_after is None else query.updated_after.timestamp()
                ),
                "updated_before": (
                    None if query.updated_before is None else query.updated_before.timestamp()
                ),
                "include_reversed": query.include_reversed,
            }
        )

    def _assert_supported(self, query: ReviewListQuery) -> None:
        """Reject anything outside the grammar before touching the backend."""
        if query.title_contains is not None:
            raise UnsupportedFilterCombination(
                "there is no substring or title search; use the exact ticket id"
            )
        if len(query.facets) > 1:
            raise UnsupportedFilterCombination("at most one queue facet is supported")
        for facet in query.facets:
            if facet not in ALLOWED_REVIEW_FACETS:
                raise UnsupportedFilterCombination("that field is not a supported facet")
        if query.devrev_display_id is not None and (
            query.facets
            or query.statuses
            or query.updated_after is not None
            or query.updated_before is not None
            or query.cursor is not None
        ):
            raise UnsupportedFilterCombination(
                "an exact ticket id lookup cannot be combined with queue filters"
            )

    async def encode_review_cursor(
        self,
        query: ReviewListQuery,
        *,
        last_updated_at: datetime,
        last_review_id: str,
    ) -> str:
        """Seal exactly the four allowed cursor fields."""
        return seal_cursor(
            self._cursor_key,
            {
                "v": REVIEW_CURSOR_SCHEMA_VERSION,
                "f": self._filter_fingerprint(query),
                "t": int(last_updated_at.timestamp() * 1_000_000),
                "i": last_review_id,
            },
            context=REVIEW_LIST_CURSOR_CONTEXT,
            now=self._now(),
        )

    async def decode_review_cursor_payload(self, token: str) -> dict[str, Any]:
        payload = open_cursor(
            self._cursor_key, token, context=REVIEW_LIST_CURSOR_CONTEXT, now=self._now()
        )
        if payload.get("v") != REVIEW_CURSOR_SCHEMA_VERSION:
            raise CursorError("cursor schema version is not supported")
        if not isinstance(payload.get("t"), int) or not isinstance(payload.get("i"), str):
            raise CursorError("cursor payload is not readable")
        return payload

    async def encode_batch_items_cursor(self, batch_id: str, *, last_review_id: str) -> str:
        return seal_cursor(
            self._cursor_key,
            {"v": REVIEW_CURSOR_SCHEMA_VERSION, "b": batch_id, "i": last_review_id},
            context=BATCH_ITEMS_CURSOR_CONTEXT,
            now=self._now(),
        )

    async def list_reviews(self, query: ReviewListQuery) -> CursorPageOf:
        """The master queue query: a status set plus at most one facet."""
        self._assert_supported(query)
        if query.devrev_display_id is not None:
            found = await self.find_review_by_display_id(query.devrev_display_id)
            return CursorPageOf(
                items=[] if found is None else [found], page_size=query.page_size
            )

        start_after: Optional[tuple[datetime, str]] = None
        if query.cursor is not None:
            payload = await self.decode_review_cursor_payload(query.cursor)
            if payload["f"] != self._filter_fingerprint(query):
                raise CursorError("cursor does not belong to this filter")
            start_after = (
                datetime.fromtimestamp(payload["t"] / 1_000_000, tz=timezone.utc),
                payload["i"],
            )

        facet = next(iter(sorted(query.facets)), None)
        statuses = (
            [status.value for status in query.statuses]
            if query.statuses
            else list(_ALL_REVIEW_STATUS_VALUES)
        )
        rows = await self.backend.query_reviews(
            statuses=statuses,
            facet=facet,
            facet_value=None if facet is None else query.facets[facet],
            updated_after=query.updated_after,
            updated_before=query.updated_before,
            limit=query.page_size + 1,
            start_after=start_after,
        )
        scanned = rows[: query.page_size]
        items: list[TicketReview] = []
        for _doc_id, doc in scanned:
            review = _from_doc(TicketReview, doc)
            # import_state is filtered in the application: adding it to the
            # query would need an index the grammar does not declare.
            if not query.include_reversed and review.import_state is ImportState.REVERSED:
                continue
            items.append(review)
        next_cursor = None
        if len(rows) > query.page_size and scanned:
            # The cursor advances by what was SCANNED, not by what survived the
            # application filter. Minting it from the last kept item would end
            # pagination on any page whose every row was filtered out -- one
            # reversed 100-row import chunk would then empty the whole queue.
            last = _from_doc(TicketReview, scanned[-1][1])
            next_cursor = await self.encode_review_cursor(
                query,
                last_updated_at=last.updated_at,
                last_review_id=last.review_id,
            )
        return CursorPageOf(items=items, next_cursor=next_cursor, page_size=query.page_size)

    # ------------------------------------------------------------------
    # Audit reads and chain verification
    # ------------------------------------------------------------------

    async def list_audit_events(
        self, review_id: str, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> CursorPageOf:
        rows = await self.backend.list_subcollection(
            (REVIEWS_COLLECTION, review_id),
            AUDIT_EVENTS_SUBCOLLECTION,
            limit=page_size,
            order_by="occurred_at_unix_us",
        )
        return CursorPageOf(
            items=[_from_doc(AuditEvent, doc) for _id, doc in rows], page_size=page_size
        )

    async def list_batch_events(
        self, batch_id: str, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> CursorPageOf:
        rows = await self.backend.list_subcollection(
            (BATCHES_COLLECTION, batch_id),
            BATCH_EVENTS_SUBCOLLECTION,
            limit=page_size,
            order_by="occurred_at_unix_us",
        )
        return CursorPageOf(
            items=[_from_doc(AuditEvent, doc) for _id, doc in rows], page_size=page_size
        )

    async def list_global_audit_events(
        self, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> CursorPageOf:
        rows = await self.backend.list_collection(
            GLOBAL_AUDIT_EVENTS_COLLECTION,
            limit=page_size + 1,
            order_by="occurred_at_unix_us",
        )
        # The reserved chain-head document is bookkeeping, never an event.
        events = [
            _from_doc(AuditEvent, doc)
            for doc_id, doc in rows
            if doc_id != GLOBAL_CHAIN_HEAD_DOC_ID
        ]
        return CursorPageOf(items=events[:page_size], page_size=page_size)

    @staticmethod
    def _verify_chain(events: Sequence[AuditEvent]) -> AuditChainReport:
        previous = GENESIS_EVENT_HASH
        for event in events:
            if event.previous_event_hash != previous:
                return AuditChainReport(
                    intact=False,
                    event_count=len(events),
                    broken_event_id=event.event_id,
                    reason="previous_event_hash does not match the chain head",
                )
            if event.event_hash != compute_audit_event_hash(event):
                return AuditChainReport(
                    intact=False,
                    event_count=len(events),
                    broken_event_id=event.event_id,
                    reason="event_hash does not recompute",
                )
            previous = event.event_hash
        return AuditChainReport(intact=True, event_count=len(events))

    async def verify_audit_chain(
        self, review_id: str, *, page_size: int = MAX_PAGE_SIZE
    ) -> AuditChainReport:
        page = await self.list_audit_events(review_id, page_size=page_size)
        return self._verify_chain(page.items)

    async def verify_global_audit_chain(
        self, *, page_size: int = MAX_PAGE_SIZE
    ) -> AuditChainReport:
        page = await self.list_global_audit_events(page_size=page_size)
        return self._verify_chain(page.items)

    # ------------------------------------------------------------------
    # Evidence links
    # ------------------------------------------------------------------

    async def create_evidence_link(
        self,
        review_id: str,
        *,
        candidate: EvidenceCandidate,
        reason: str,
        expected_version: Optional[int],
        context: MutationContext,
    ) -> tuple[EvidenceLink, TicketReview]:
        """Link sanitized evidence from a service-validated broker candidate."""
        if not isinstance(candidate, EvidenceCandidate):
            raise TypeError(
                "evidence linking accepts only a service-validated EvidenceCandidate"
            )
        if context.actor_role not in _REVIEW_MUTATORS:
            raise NotAuthorized("this role may not link evidence")
        now = self._now()
        operation = "create_evidence_link"
        request = {
            "review_id": review_id,
            "link_id": candidate.link_id,
            "evidence_digest": candidate.evidence_digest,
            "broker_result_digest": candidate.broker_result_digest,
            "reason": reason,
        }
        link_path = (REVIEWS_COLLECTION, review_id, EVIDENCE_LINKS_SUBCOLLECTION)

        async def _txn(view: TransactionView) -> tuple[Document, Document]:
            replay, key_hash, digest = await self._claim_idempotency(
                view, context, operation=operation, request=request, now=now
            )
            review_doc = await self._load_review_doc(view, review_id)
            existing = await view.get((*link_path, candidate.link_id))
            if replay is not None or (existing is not None and not existing.get("unlinked_at")):
                if existing is None:
                    raise EvidenceCandidateRejected("that evidence candidate is not linkable")
                return existing, review_doc
            # Fail without disclosing whether another ticket's evidence exists.
            if candidate.review_id != review_id:
                raise EvidenceCandidateRejected("that evidence candidate is not linkable")
            if candidate.issued_to_subject != context.actor.subject:
                raise EvidenceCandidateRejected("that evidence candidate is not linkable")
            if candidate.expires_at <= now:
                raise EvidenceCandidateRejected("that evidence candidate is not linkable")

            self._assert_version(review_doc, expected_version)
            # A retired link may be re-linked, but its version must keep moving
            # forward and the retraction it carried must not be erased: a client
            # holding the retired ETag has to be able to tell it was resurrected.
            retired = existing if existing is not None else {}
            link = EvidenceLink(
                link_id=candidate.link_id,
                review_id=review_id,
                evidence_reference=candidate.evidence_reference,
                evidence_digest=candidate.evidence_digest,
                reason=reason,
                linked_by=context.actor,
                correlation_trust=candidate.correlation_trust,
                source_url=candidate.source_url,
                linked_at=now,
                version=int(retired.get("version") or 0) + 1,
            )
            extra: dict[str, Any] = {"broker_result_digest": candidate.broker_result_digest}
            if retired.get("unlink_reason"):
                extra["previous_unlink_reason"] = retired["unlink_reason"]
                extra["previous_unlinked_at"] = retired.get("unlinked_at")
                extra["relinked_at"] = now
            link_doc = _to_doc(link, **extra, **self._product_envelope(now))
            view.set((*link_path, link.link_id), link_doc)

            current = _from_doc(TicketReview, review_doc)
            merged = current.model_copy(
                update={"version": current.version + 1, "updated_at": now}
            )
            new_review = _to_doc(merged)
            new_review.update(
                {
                    DOC_KIND_FIELD: "review",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: bool(review_doc.get(LEGAL_HOLD_FIELD, False)),
                    CHAIN_HEAD_FIELD: review_doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: review_doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            new_review["created_at"] = review_doc["created_at"]
            self._stamp_product(new_review, now)
            await self._append_event(
                view,
                ledger_path=(REVIEWS_COLLECTION, review_id, AUDIT_EVENTS_SUBCOLLECTION),
                parent_doc=new_review,
                parent_kind="review",
                parent_id=review_id,
                event_type="evidence_linked",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=["evidence_links"],
            )
            view.set((REVIEWS_COLLECTION, review_id), new_review)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation=operation,
                result={"review_id": review_id, "link_id": link.link_id},
                now=now,
            )
            return link_doc, new_review

        link_doc, review_doc = await self.backend.transact(_txn)
        return _from_doc(EvidenceLink, link_doc), _from_doc(TicketReview, review_doc)

    async def unlink_evidence_link(
        self,
        review_id: str,
        link_id: str,
        *,
        reason: str,
        expected_version: Optional[int],
        context: MutationContext,
    ) -> TicketReview:
        """Versioned, reasoned, audited unlink.

        The link document is retired in place rather than erased, so the
        reviewer's reason survives inside the same 730-day product window.
        """
        if context.actor_role not in _REVIEW_MUTATORS:
            raise NotAuthorized("this role may not unlink evidence")
        now = self._now()
        link_path = (REVIEWS_COLLECTION, review_id, EVIDENCE_LINKS_SUBCOLLECTION, link_id)

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="unlink_evidence_link",
                request={"review_id": review_id, "link_id": link_id, "reason": reason},
                now=now,
            )
            if replay is not None:
                return await self._load_review_doc(view, review_id)
            review_doc = await self._load_review_doc(view, review_id)
            link_doc = await view.get(link_path)
            if link_doc is None or link_doc.get("unlinked_at"):
                raise EvidenceCandidateRejected("that evidence link is not present")
            self._assert_version(review_doc, expected_version)
            link_doc.update(
                {
                    "unlinked_at": now,
                    "unlinked_by_subject_hash": sha256_hex(context.actor.subject),
                    "unlink_reason": reason,
                    "version": int(link_doc.get("version") or 1) + 1,
                    RETENTION_FIELD: now + self._review_retention,
                }
            )
            view.set(link_path, link_doc)

            current = _from_doc(TicketReview, review_doc)
            merged = current.model_copy(
                update={"version": current.version + 1, "updated_at": now}
            )
            new_review = _to_doc(merged)
            new_review.update(
                {
                    DOC_KIND_FIELD: "review",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: bool(review_doc.get(LEGAL_HOLD_FIELD, False)),
                    CHAIN_HEAD_FIELD: review_doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: review_doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            new_review["created_at"] = review_doc["created_at"]
            self._stamp_product(new_review, now)
            await self._append_event(
                view,
                ledger_path=(REVIEWS_COLLECTION, review_id, AUDIT_EVENTS_SUBCOLLECTION),
                parent_doc=new_review,
                parent_kind="review",
                parent_id=review_id,
                event_type="evidence_unlinked",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=["evidence_links"],
            )
            view.set((REVIEWS_COLLECTION, review_id), new_review)
            return new_review

        return _from_doc(TicketReview, await self.backend.transact(_txn))

    async def encode_evidence_links_cursor(self, review_id: str, *, last_link_id: str) -> str:
        return seal_cursor(
            self._cursor_key,
            {"v": REVIEW_CURSOR_SCHEMA_VERSION, "r": review_id, "i": last_link_id},
            context=EVIDENCE_LINKS_CURSOR_CONTEXT,
            now=self._now(),
        )

    async def list_evidence_links(
        self,
        review_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> CursorPageOf:
        """Page live evidence links.

        Retired links are filtered in the application, so the cursor advances by
        rows SCANNED. Without that, a review whose earliest-sorting links were
        all unlinked would report zero evidence and offer no way to page past
        them.
        """
        start_after_id: Optional[str] = None
        if cursor is not None:
            payload = open_cursor(
                self._cursor_key,
                cursor,
                context=EVIDENCE_LINKS_CURSOR_CONTEXT,
                now=self._now(),
            )
            if payload.get("r") != review_id:
                raise CursorError("cursor does not belong to this review")
            start_after_id = str(payload["i"])
        rows = await self.backend.list_subcollection(
            (REVIEWS_COLLECTION, review_id),
            EVIDENCE_LINKS_SUBCOLLECTION,
            limit=page_size + 1,
            start_after_id=start_after_id,
        )
        scanned = rows[:page_size]
        items = [
            _from_doc(EvidenceLink, doc)
            for _id, doc in scanned
            if not doc.get("unlinked_at")
        ]
        next_cursor = None
        if len(rows) > page_size and scanned:
            next_cursor = await self.encode_evidence_links_cursor(
                review_id, last_link_id=scanned[-1][0]
            )
        return CursorPageOf(items=items, next_cursor=next_cursor, page_size=page_size)

    # ------------------------------------------------------------------
    # Disposable caches
    # ------------------------------------------------------------------

    async def upsert_message_cache_entry(
        self, entry: DevRevMessageCacheEntry
    ) -> DevRevMessageCacheEntry:
        """Idempotent by hashed remote entry id; newer remote snapshot wins."""
        now = self._now()
        path = (DEVREV_MESSAGE_CACHE_COLLECTION, entry.entry_id_hash)

        def _rank(version: Any, modified: Any) -> tuple[int, float]:
            ordinal = int(version) if isinstance(version, int) and not isinstance(
                version, bool
            ) else -1
            stamp = modified.timestamp() if isinstance(modified, datetime) else float("-inf")
            return ordinal, stamp

        async def _txn(view: TransactionView) -> Document:
            existing = await view.get(path)
            if existing is not None:
                incoming = _rank(entry.object_version, entry.remote_modified_at)
                stored = _rank(existing.get("object_version"), existing.get("remote_modified_at"))
                if incoming <= stored and _live(existing, now) is not None:
                    return existing
            doc = _to_doc(
                entry,
                entry_id_hash=entry.entry_id_hash,
                cached_at=now,
                **{TTL_FIELD: now + self._message_cache_ttl},
            )
            view.set(path, doc)
            return doc

        doc = await self.backend.transact(_txn)
        return _from_doc(DevRevMessageCacheEntry, doc)

    async def get_message_cache_entry(
        self, remote_entry_id: str
    ) -> Optional[DevRevMessageCacheEntry]:
        doc = _live(
            await self.backend.get_doc(
                (DEVREV_MESSAGE_CACHE_COLLECTION, sha256_hex(remote_entry_id))
            ),
            self._now(),
        )
        return None if doc is None else _from_doc(DevRevMessageCacheEntry, doc)

    async def put_console_cache_entry(self, entry: ConsoleCacheEntry) -> ConsoleCacheEntry:
        now = self._now()
        doc = _to_doc(entry, **{TTL_FIELD: now + self._cache_ttl, "cached_at": now})

        async def _txn(view: TransactionView) -> Document:
            view.set((CONSOLE_CACHE_COLLECTION, sha256_hex(entry.cache_key)), doc)
            return doc

        return _from_doc(ConsoleCacheEntry, await self.backend.transact(_txn))

    async def get_console_cache_entry(self, cache_key: str) -> Optional[ConsoleCacheEntry]:
        doc = _live(
            await self.backend.get_doc((CONSOLE_CACHE_COLLECTION, sha256_hex(cache_key))),
            self._now(),
        )
        return None if doc is None else _from_doc(ConsoleCacheEntry, doc)

    # ------------------------------------------------------------------
    # Remediation batches
    # ------------------------------------------------------------------

    async def create_batch(
        self,
        *,
        review_refs: Sequence[Mapping[str, Any]],
        context: MutationContext,
    ) -> RemediationBatch:
        """Freeze unique ``(review_id, review_version)`` pairs into item docs."""
        if context.actor_role not in {ReviewerRole.ADMIN, ReviewerRole.REVIEWER}:
            raise NotAuthorized("this role may not create a remediation batch")
        refs = [ReviewRef.model_validate(dict(ref)) for ref in review_refs]
        if not refs:
            raise BatchContractViolation("a batch needs at least one review")
        if len({ref.review_id for ref in refs}) != len(refs):
            raise BatchContractViolation("a batch may not freeze a review twice")
        if len(refs) > self._max_batch_reviews:
            raise BatchContractViolation(
                f"a batch may not exceed {self._max_batch_reviews} reviews"
            )
        # One parent, one event, and one document per item must fit a single
        # transaction; refuse before any write rather than half-writing.
        if len(refs) + 2 > FIRESTORE_MAX_WRITES_PER_TRANSACTION:
            raise BatchContractViolation("a batch would exceed the transaction write limit")

        now = self._now()
        digest = item_set_digest(refs)
        operation = "create_batch"
        request = {"refs": sorted([[r.review_id, r.review_version] for r in refs])}

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, key_digest = await self._claim_idempotency(
                view, context, operation=operation, request=request, now=now
            )
            if replay is not None:
                stored = await view.get(
                    (BATCHES_COLLECTION, str(replay["result"]["batch_id"]))
                )
                if stored is not None:
                    return stored
            batch_id = self._new_id()
            items: list[tuple[ReviewRef, TicketReview]] = []
            for ref in refs:
                review_doc = await view.get((REVIEWS_COLLECTION, ref.review_id))
                if review_doc is None or _is_tombstone(review_doc):
                    raise ReviewNotFound("a frozen review does not exist")
                review = _from_doc(TicketReview, review_doc)
                if review.version != ref.review_version:
                    raise ReviewVersionConflict(
                        "a frozen review version no longer matches",
                        supplied_version=ref.review_version,
                        current_version=review.version,
                        changed_at=review.updated_at,
                        review_id=review.review_id,
                    )
                items.append((ref, review))

            batch = RemediationBatch(
                batch_id=batch_id,
                status=BatchStatus.DRAFT,
                created_by=context.actor,
                item_count=len(items),
                item_set_digest=digest,
                created_at=now,
                updated_at=now,
                version=1,
            )
            parent = _to_doc(batch)
            parent.update(
                {
                    DOC_KIND_FIELD: "remediation_batch",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: False,
                    CHAIN_HEAD_FIELD: GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: 0,
                    "lease_extensions_granted": 0,
                }
            )
            self._stamp_product(parent, now)
            if _document_bytes(parent) >= FIRESTORE_MAX_DOCUMENT_BYTES:
                raise BatchContractViolation(
                    "the batch parent would exceed the Firestore document limit"
                )
            item_docs: list[tuple[DocPath, Document]] = []
            for ref, review in items:
                item = RemediationBatchItem(
                    review_id=ref.review_id,
                    review_version=ref.review_version,
                    devrev_display_id=review.devrev_display_id,
                    observation_type=review.observation_type,
                    severity=review.severity,
                    remediation_target=review.remediation_target,
                )
                item_doc = _to_doc(item, **self._product_envelope(now))
                if _document_bytes(item_doc) >= FIRESTORE_MAX_DOCUMENT_BYTES:
                    raise BatchContractViolation(
                        "a frozen batch item would exceed the Firestore document limit"
                    )
                item_docs.append(
                    (
                        (BATCHES_COLLECTION, batch_id, BATCH_ITEMS_SUBCOLLECTION, ref.review_id),
                        item_doc,
                    )
                )
            for item_path, item_doc in item_docs:
                view.set(item_path, item_doc)
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=parent,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_created",
                context=context,
                now=now,
                new_version=1,
                changed_fields=["items", "item_set_digest"],
            )
            view.set((BATCHES_COLLECTION, batch_id), parent)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=key_digest,
                operation=operation,
                result={"batch_id": batch_id},
                now=now,
            )
            return parent

        return _from_doc(RemediationBatch, await self.backend.transact(_txn))

    async def get_batch(self, batch_id: str) -> RemediationBatch:
        doc = await self.backend.get_doc((BATCHES_COLLECTION, batch_id))
        if doc is None or _is_tombstone(doc):
            raise BatchNotFound("no remediation batch exists for that id")
        return _from_doc(RemediationBatch, doc)

    async def list_batch_items(
        self,
        batch_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> CursorPageOf:
        """Materialize frozen items through a bounded cursor, never one array."""
        start_after_id: Optional[str] = None
        if cursor is not None:
            payload = open_cursor(
                self._cursor_key, cursor, context=BATCH_ITEMS_CURSOR_CONTEXT, now=self._now()
            )
            if payload.get("b") != batch_id:
                raise CursorError("cursor does not belong to this batch")
            start_after_id = str(payload["i"])
        rows = await self.backend.list_subcollection(
            (BATCHES_COLLECTION, batch_id),
            BATCH_ITEMS_SUBCOLLECTION,
            limit=page_size + 1,
            start_after_id=start_after_id,
        )
        items = [_from_doc(RemediationBatchItem, doc) for _id, doc in rows[:page_size]]
        next_cursor = None
        if len(rows) > page_size and items:
            next_cursor = await self.encode_batch_items_cursor(
                batch_id, last_review_id=items[-1].review_id
            )
        return CursorPageOf(items=items, next_cursor=next_cursor, page_size=page_size)

    def _lease_or_none(self, doc: Document) -> Optional[BatchLease]:
        raw = doc.get("lease")
        return None if not raw else BatchLease.model_validate(raw)

    def _assert_batch_version(self, doc: Document, expected_version: Optional[int]) -> None:
        current = int(doc.get("version") or 0)
        if expected_version is None or expected_version == current:
            return
        raise BatchVersionConflict(
            supplied_version=expected_version,
            current_version=current,
            changed_at=doc.get("updated_at"),
        )

    def _assert_lease(
        self, doc: Document, *, lease_token: Optional[str], context: MutationContext, now: datetime
    ) -> BatchLease:
        lease = self._lease_or_none(doc)
        if lease is None:
            raise BatchLeaseLost("this batch holds no lease")
        if lease_token is None or lease.lease_token_hash != sha256_hex(lease_token):
            raise BatchLeaseLost("the lease token does not authorize this write")
        if lease.holder != context.actor.email:
            raise BatchLeaseLost("the lease belongs to another holder")
        if lease.expires_at <= now:
            raise BatchLeaseLost("the lease window has elapsed")
        return lease

    def _batch_doc(
        self, batch: RemediationBatch, previous: Document, now: datetime
    ) -> Document:
        doc = _to_doc(batch)
        doc.update(
            {
                DOC_KIND_FIELD: "remediation_batch",
                RETENTION_FIELD: now + self._review_retention,
                LEGAL_HOLD_FIELD: bool(previous.get(LEGAL_HOLD_FIELD, False)),
                CHAIN_HEAD_FIELD: previous.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                CHAIN_COUNT_FIELD: previous.get(CHAIN_COUNT_FIELD) or 0,
                "lease_extensions_granted": int(previous.get("lease_extensions_granted") or 0),
            }
        )
        if previous.get("lease_extension_expires_at") is not None:
            doc["lease_extension_expires_at"] = previous["lease_extension_expires_at"]
        doc["created_at"] = previous["created_at"]
        self._stamp_product(doc, now)
        return doc

    async def claim_batch(
        self, batch_id: str, *, lease_token: str, context: MutationContext
    ) -> BatchClaim:
        """Atomic, lease-based claim. Idempotent for the same agent and token."""
        if context.actor_role is not ReviewerRole.AGENT:
            raise NotAuthorized("only an agent may claim a remediation batch")
        now = self._now()

        async def _txn(view: TransactionView) -> tuple[Document, bool]:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="claim_batch",
                request={"batch_id": batch_id, "token_hash": sha256_hex(lease_token)},
                now=now,
            )
            if replay is not None:
                stored = await view.get((BATCHES_COLLECTION, batch_id))
                if stored is not None and not _is_tombstone(stored):
                    return stored, False
            doc = await view.get((BATCHES_COLLECTION, batch_id))
            if doc is None or _is_tombstone(doc):
                raise BatchNotFound("no remediation batch exists for that id")
            current = _from_doc(RemediationBatch, doc)
            lease = self._lease_or_none(doc)
            token_hash = sha256_hex(lease_token)
            if lease is not None and lease.expires_at > now:
                if (
                    lease.holder == context.actor.email
                    and lease.lease_token_hash == token_hash
                ):
                    return doc, False
                raise BatchAlreadyClaimed("this batch is already claimed")

            reclaimed = lease is not None
            if reclaimed:
                # Lease expiry never silently changes business status: record the
                # expiry, then make the expire/reclaim transition explicit.
                _assert_batch_transition(current.status, BatchStatus.EXPIRED)
                expired = current.model_copy(
                    update={"status": BatchStatus.EXPIRED, "updated_at": now}
                )
                expired_doc = self._batch_doc(expired, doc, now)
                await self._append_event(
                    view,
                    ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                    parent_doc=expired_doc,
                    parent_kind="batch",
                    parent_id=batch_id,
                    event_type="batch_lease_expired",
                    context=context,
                    now=now,
                    previous_version=current.version,
                    new_version=current.version,
                    changed_fields=["lease", "status"],
                )
                doc = expired_doc
                current = expired

            _assert_batch_transition(
                current.status, BatchStatus.CLAIMED, actor_role=None
            )
            new_lease = BatchLease(
                lease_token_hash=token_hash,
                holder=context.actor.email,
                acquired_at=now,
                expires_at=now + self._lease,
                last_heartbeat_at=now,
                continuous_since=now,
            )
            claimed = current.model_copy(
                update={
                    "status": BatchStatus.CLAIMED,
                    "lease": new_lease,
                    "version": current.version + 1,
                    "updated_at": now,
                }
            )
            new_doc = self._batch_doc(claimed, doc, now)
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_lease_reclaimed" if reclaimed else "batch_claimed",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=claimed.version,
                changed_fields=["lease", "status"],
            )
            view.set((BATCHES_COLLECTION, batch_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="claim_batch",
                result={"batch_id": batch_id, "version": claimed.version},
                now=now,
            )
            return new_doc, reclaimed

        doc, reclaimed = await self.backend.transact(_txn)
        batch = _from_doc(RemediationBatch, doc)
        assert batch.lease is not None
        return BatchClaim(
            batch=batch, lease_expires_at=batch.lease.expires_at, reclaimed=reclaimed
        )

    async def heartbeat_batch(
        self,
        batch_id: str,
        *,
        expected_version: int,
        lease_token: str,
        context: MutationContext,
    ) -> RemediationBatch:
        """Renew a live lease inside the bounded continuous window."""
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="heartbeat_batch",
                request={"batch_id": batch_id, "expected_version": expected_version},
                now=now,
            )
            if replay is not None:
                stored = await view.get((BATCHES_COLLECTION, batch_id))
                if stored is not None and not _is_tombstone(stored):
                    return stored
                    return stored
            doc = await view.get((BATCHES_COLLECTION, batch_id))
            if doc is None or _is_tombstone(doc):
                raise BatchNotFound("no remediation batch exists for that id")
            lease = self._assert_lease(doc, lease_token=lease_token, context=context, now=now)
            self._assert_batch_version(doc, expected_version)
            extension_until = doc.get("lease_extension_expires_at")
            if lease.continuous_cap_reached(at=now) and not (
                isinstance(extension_until, datetime) and now < extension_until
            ):
                raise BatchLeaseLost(
                    "the continuous lease cap is reached; an admin must extend it"
                )
            current = _from_doc(RemediationBatch, doc)
            renewed = lease.model_copy(
                update={
                    # acquired_at moves with the window so the bounded 15-minute
                    # lease invariant holds; continuous_since stays put so the
                    # two-hour cap still measures the whole continuous run.
                    "acquired_at": now,
                    "expires_at": now + self._lease,
                    "last_heartbeat_at": now,
                    "continuous_since": lease.continuous_since or lease.acquired_at,
                }
            )
            merged = current.model_copy(
                update={"lease": renewed, "version": current.version + 1, "updated_at": now}
            )
            new_doc = self._batch_doc(merged, doc, now)
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_heartbeat",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=["lease"],
            )
            view.set((BATCHES_COLLECTION, batch_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="heartbeat_batch",
                result={"batch_id": batch_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(RemediationBatch, await self.backend.transact(_txn))

    async def extend_lease(
        self,
        batch_id: str,
        *,
        expected_version: int,
        additional_minutes: int,
        reason: str,
        context: MutationContext,
    ) -> RemediationBatch:
        """One audited, bounded, admin-only extension after the two-hour cap."""
        if context.actor_role is not ReviewerRole.ADMIN:
            raise LeaseExtensionRefused("only an admin may extend a lease")
        if not 1 <= additional_minutes <= MAX_LEASE_EXTENSION_MINUTES:
            raise LeaseExtensionRefused(
                f"an extension must be between 1 and {MAX_LEASE_EXTENSION_MINUTES} minutes"
            )
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="extend_lease",
                request={
                    "batch_id": batch_id,
                    "expected_version": expected_version,
                    "additional_minutes": additional_minutes,
                },
                now=now,
            )
            if replay is not None:
                stored = await view.get((BATCHES_COLLECTION, batch_id))
                if stored is not None and not _is_tombstone(stored):
                    return stored
                    return stored
            doc = await view.get((BATCHES_COLLECTION, batch_id))
            if doc is None or _is_tombstone(doc):
                raise BatchNotFound("no remediation batch exists for that id")
            if int(doc.get("lease_extensions_granted") or 0) >= 1:
                raise LeaseExtensionRefused("this batch already received its one extension")
            lease = self._lease_or_none(doc)
            if lease is None:
                raise LeaseExtensionRefused("this batch holds no lease to extend")
            if lease.expires_at <= now:
                # An elapsed lease is reclaimed, never extended: extending it
                # would resurrect the old holder without the explicit
                # expire/reclaim transition and its audit events.
                raise LeaseExtensionRefused(
                    "the lease has elapsed; it must be reclaimed, not extended"
                )
            self._assert_batch_version(doc, expected_version)
            current = _from_doc(RemediationBatch, doc)
            renewed = lease.model_copy(
                update={
                    "acquired_at": now,
                    "expires_at": now + self._lease,
                    "last_heartbeat_at": now,
                    # continuous_since is deliberately NOT reset. The extension
                    # is a bounded window granted past the cap, so resetting it
                    # would silently hand out a whole fresh two-hour run and make
                    # additional_minutes meaningless.
                    "continuous_since": lease.continuous_since or lease.acquired_at,
                }
            )
            merged = current.model_copy(
                update={"lease": renewed, "version": current.version + 1, "updated_at": now}
            )
            new_doc = self._batch_doc(merged, doc, now)
            new_doc["lease_extensions_granted"] = 1
            new_doc["lease_extension_expires_at"] = now + timedelta(minutes=additional_minutes)
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_lease_extended",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=["lease"],
            )
            view.set((BATCHES_COLLECTION, batch_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="extend_lease",
                result={"batch_id": batch_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(RemediationBatch, await self.backend.transact(_txn))

    async def patch_batch(
        self,
        batch_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        transition: Optional[BatchStatus] = None,
        lease_token: Optional[str] = None,
        plan_artifact: Optional[str] = None,
        branch: Optional[str] = None,
        commit_sha: Optional[str] = None,
        pr_url: Optional[str] = None,
        test_evidence: Optional[Sequence[VerificationEvidence]] = None,
        verification_summary: Optional[str] = None,
    ) -> RemediationBatch:
        """Record plan, progress, or results under version and lease checks."""
        now = self._now()
        updates: dict[str, Any] = {}
        for field, value in (
            ("plan_artifact", plan_artifact),
            ("branch", branch),
            ("commit_sha", commit_sha),
            ("pr_url", pr_url),
            ("verification_summary", verification_summary),
        ):
            if value is not None:
                updates[field] = value
        if test_evidence is not None:
            updates["test_evidence"] = list(test_evidence)

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="patch_batch",
                request={
                    "batch_id": batch_id,
                    "expected_version": expected_version,
                    "transition": None if transition is None else transition.value,
                    "updates": _plain(dict(updates)),
                },
                now=now,
            )
            if replay is not None:
                stored = await view.get((BATCHES_COLLECTION, batch_id))
                if stored is not None and not _is_tombstone(stored):
                    return stored
                    return stored
            doc = await view.get((BATCHES_COLLECTION, batch_id))
            if doc is None or _is_tombstone(doc):
                raise BatchNotFound("no remediation batch exists for that id")
            current = _from_doc(RemediationBatch, doc)
            has_lease = False
            if context.actor_role is ReviewerRole.AGENT or lease_token is not None:
                self._assert_lease(doc, lease_token=lease_token, context=context, now=now)
                has_lease = True
            elif context.actor_role not in {ReviewerRole.ADMIN, ReviewerRole.REVIEWER}:
                raise NotAuthorized("this role may not change a remediation batch")
            self._assert_batch_version(doc, expected_version)
            if transition is not None and transition is not current.status:
                _assert_batch_transition(
                    current.status,
                    transition,
                    actor_role=context.actor_role,
                    has_lease=has_lease,
                )
                updates["status"] = transition
            merged = current.model_copy(
                update={**updates, "version": current.version + 1, "updated_at": now}
            )
            new_doc = self._batch_doc(merged, doc, now)
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_updated",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=sorted(updates),
            )
            view.set((BATCHES_COLLECTION, batch_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="patch_batch",
                result={"batch_id": batch_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(RemediationBatch, await self.backend.transact(_txn))

    async def release_batch(
        self,
        batch_id: str,
        *,
        expected_version: int,
        lease_token: str,
        disposition: BatchStatus,
        reason: str,
        context: MutationContext,
    ) -> RemediationBatch:
        """Atomically release a claim and invalidate the lease.

        ``ready`` is available only before durable plan/progress/results exist
        and while every frozen review version still matches; otherwise the
        batch must go to ``blocked`` with a reason. No release may leave an
        unleased ``claimed``/``planning``/``in_progress`` batch, which is why
        this edge is validated here rather than through the closed table (the
        table models the agent's forward path, not the release edge).
        """
        if disposition not in {BatchStatus.READY, BatchStatus.BLOCKED}:
            raise BatchReleaseRefused("a release disposition is 'ready' or 'blocked'")
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="release_batch",
                request={
                    "batch_id": batch_id,
                    "expected_version": expected_version,
                    "disposition": disposition.value,
                },
                now=now,
            )
            if replay is not None:
                stored = await view.get((BATCHES_COLLECTION, batch_id))
                if stored is not None and not _is_tombstone(stored):
                    return stored
                    return stored
            doc = await view.get((BATCHES_COLLECTION, batch_id))
            if doc is None or _is_tombstone(doc):
                raise BatchNotFound("no remediation batch exists for that id")
            self._assert_lease(doc, lease_token=lease_token, context=context, now=now)
            self._assert_batch_version(doc, expected_version)
            current = _from_doc(RemediationBatch, doc)
            if disposition is BatchStatus.READY:
                if any(getattr(current, field) for field in _BATCH_WORK_FIELDS) or (
                    current.test_evidence or current.outcome
                ):
                    raise BatchReleaseRefused(
                        "durable work exists; release to 'blocked' with a reason"
                    )
                # Frozen item documents are immutable once the batch exists, so
                # reading them through the client is safe; the review versions
                # they are compared against are read through the transaction
                # below. The page is bounded by this batch's OWN item_count, not
                # by the currently configured cap, which a later settings change
                # could otherwise lower below an already-frozen batch and
                # silently skip the drift check for the tail.
                items = await self.backend.list_subcollection(
                    (BATCHES_COLLECTION, batch_id),
                    BATCH_ITEMS_SUBCOLLECTION,
                    limit=current.item_count + 1,
                )
                if len(items) != current.item_count:
                    raise BatchReleaseRefused(
                        "cannot verify every frozen review; release to 'blocked'"
                    )
                for review_id, item_doc in items:
                    review_doc = await view.get((REVIEWS_COLLECTION, review_id))
                    if review_doc is None or _is_tombstone(review_doc):
                        raise BatchReleaseRefused("a frozen review is no longer available")
                    if int(review_doc.get("version") or 0) != int(item_doc["review_version"]):
                        raise BatchReleaseRefused(
                            "a frozen review version drifted; release to 'blocked'"
                        )
            merged = current.model_copy(
                update={
                    "status": disposition,
                    "lease": None,
                    "version": current.version + 1,
                    "updated_at": now,
                }
            )
            new_doc = self._batch_doc(merged, doc, now)
            new_doc["lease"] = None
            await self._append_event(
                view,
                ledger_path=(BATCHES_COLLECTION, batch_id, BATCH_EVENTS_SUBCOLLECTION),
                parent_doc=new_doc,
                parent_kind="batch",
                parent_id=batch_id,
                event_type="batch_released",
                context=context,
                now=now,
                previous_version=current.version,
                new_version=merged.version,
                changed_fields=["lease", "status"],
            )
            view.set((BATCHES_COLLECTION, batch_id), new_doc)
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="release_batch",
                result={"batch_id": batch_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(RemediationBatch, await self.backend.transact(_txn))

    # ------------------------------------------------------------------
    # Imports and exports
    # ------------------------------------------------------------------

    async def create_import(
        self, record: TicketImport, *, context: MutationContext
    ) -> TicketImport:
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may create an import")
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            path = (IMPORTS_COLLECTION, record.import_id)
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="create_import",
                request={"import_id": record.import_id, "file_sha256": record.file_sha256},
                now=now,
            )
            existing = await view.get(path)
            if replay is not None and existing is not None:
                return existing
            if existing is not None:
                raise ReviewRepositoryError("that import already exists")
            stored = record.model_copy(update={"created_at": now, "updated_at": now, "version": 1})
            doc = _to_doc(stored)
            doc.update(
                {
                    DOC_KIND_FIELD: "ticket_import",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: False,
                    CHAIN_HEAD_FIELD: GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: 0,
                }
            )
            self._stamp_product(doc, now)
            view.set(path, doc)
            await self._append_global_event(
                view,
                parent_kind="import",
                parent_id=sha256_hex(record.import_id),
                event_type="import_created",
                context=context,
                now=now,
            )
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="create_import",
                result={"import_id": record.import_id},
                now=now,
            )
            return doc

        return _from_doc(TicketImport, await self.backend.transact(_txn))

    async def get_import(self, import_id: str) -> TicketImport:
        doc = await self.backend.get_doc((IMPORTS_COLLECTION, import_id))
        if doc is None or _is_tombstone(doc):
            raise ReviewRepositoryError("no import exists for that id")
        return _from_doc(TicketImport, doc)

    async def patch_import(
        self,
        import_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        transition: Optional[ImportStatus] = None,
        reason: Optional[str] = None,
    ) -> TicketImport:
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may change an import")
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            path = (IMPORTS_COLLECTION, import_id)
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="patch_import",
                request={
                    "import_id": import_id,
                    "expected_version": expected_version,
                    "transition": None if transition is None else transition.value,
                },
                now=now,
            )
            doc = await view.get(path)
            if doc is None or _is_tombstone(doc):
                raise ReviewRepositoryError("no import exists for that id")
            if replay is not None:
                return doc
            current = _from_doc(TicketImport, doc)
            if int(doc.get("version") or 0) != expected_version:
                raise ReviewVersionConflict(
                    "the import changed since it was loaded",
                    supplied_version=expected_version,
                    current_version=int(doc.get("version") or 0),
                    changed_at=doc.get("updated_at"),
                )
            updates: dict[str, Any] = {}
            if transition is not None and transition is not current.status:
                _assert_import_transition(current.status, transition, reason=reason)
                updates["status"] = transition
            merged = current.model_copy(
                update={**updates, "version": current.version + 1, "updated_at": now}
            )
            new_doc = _to_doc(merged)
            new_doc.update(
                {
                    DOC_KIND_FIELD: "ticket_import",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: bool(doc.get(LEGAL_HOLD_FIELD, False)),
                    CHAIN_HEAD_FIELD: doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            new_doc["created_at"] = doc["created_at"]
            self._stamp_product(new_doc, now)
            view.set(path, new_doc)
            await self._append_global_event(
                view,
                parent_kind="import",
                parent_id=sha256_hex(import_id),
                event_type="import_updated",
                context=context,
                now=now,
                changed_fields=sorted(updates),
            )
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="patch_import",
                result={"import_id": import_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(TicketImport, await self.backend.transact(_txn))

    async def stage_import_rows(
        self, import_id: str, rows: Sequence[Mapping[str, Any]]
    ) -> int:
        """Store bounded parsed staging rows for seven days, never the CSV body."""
        now = self._now()
        if len(rows) > MAX_CSV_ROWS:
            raise ReviewRepositoryError("a staged chunk exceeds the canonical row limit")

        async def _txn(view: TransactionView) -> int:
            for row in rows:
                row_number = int(row["row_number"])
                view.set(
                    (IMPORT_STAGING_COLLECTION, f"{import_id}:{row_number:06d}"),
                    {
                        "import_id": import_id,
                        "row": {key: _plain(value) for key, value in row.items()},
                        "created_at": now,
                        TTL_FIELD: now + self._import_staging_ttl,
                    },
                )
            return len(rows)

        return await self.backend.transact(_txn)

    async def get_staged_import_rows(
        self, import_id: str, *, page_size: int = 100
    ) -> list[Document]:
        rows = await self.backend.scan_by_field(
            IMPORT_STAGING_COLLECTION,
            field=TTL_FIELD,
            before=self._now() + self._import_staging_ttl,
            limit=page_size,
        )
        now = self._now()
        return [
            doc
            for _id, doc in rows
            if doc.get("import_id") == import_id and _live(doc, now) is not None
        ]

    async def _write_import_rows(
        self,
        import_id: str,
        specs: Sequence[ImportRowSpec],
        *,
        context: MutationContext,
        reversing: bool,
    ) -> TicketImport:
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may apply or reverse an import")
        if len(specs) > 100:
            raise ReviewRepositoryError("an import chunk is at most 100 rows")
        now = self._now()
        applied = 0
        conflicted = 0
        failed = 0
        reversed_rows = 0
        row_docs: list[tuple[TicketImportRow, str]] = []

        for spec in specs:
            display_id: Optional[str] = spec.raw_ticket_id
            error_code: Optional[str] = None
            try:
                row_context = self._derive_context(context, f"row:{spec.row_number}")
                if reversing:
                    review = await self.mark_import_state(
                        spec.review_id,
                        ImportState.REVERSED,
                        expected_version=spec.expected_review_version,
                        context=row_context,
                        event_type="review_import_reversed",
                    )
                    reversed_rows += 1
                else:
                    review = await self.patch_review(
                        spec.review_id,
                        spec.patch,
                        expected_version=spec.expected_review_version,
                        context=row_context,
                    )
                    applied += 1
                display_id = display_id or review.devrev_display_id
            except ReviewVersionConflict as conflict:
                conflicted += 1
                error_code = "review_version_conflict"
                display_id = display_id or conflict.review_id
            except ReviewRepositoryError as error:
                failed += 1
                error_code = type(error).__name__
            row_docs.append(
                (
                    TicketImportRow(
                        row_number=spec.row_number,
                        raw_ticket_id=(display_id or "unavailable")[:MAX_DISPLAY_ID_LENGTH],
                        review_id=spec.review_id,
                        expected_review_version=spec.expected_review_version,
                        error_code=error_code,
                    ),
                    f"{spec.row_number:06d}",
                )
            )

        summary_context = self._derive_context(context, "summary")

        async def _txn(view: TransactionView) -> Document:
            path = (IMPORTS_COLLECTION, import_id)
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                summary_context,
                operation="import_chunk",
                request={
                    "import_id": import_id,
                    "reversing": reversing,
                    "rows": sorted(spec.row_number for spec in specs),
                },
                now=now,
            )
            doc = await view.get(path)
            if doc is None or _is_tombstone(doc):
                raise ReviewRepositoryError("no import exists for that id")
            if replay is not None:
                # A retried chunk must not add its counters a second time.
                return doc
            current = _from_doc(TicketImport, doc)
            for row, row_id in row_docs:
                view.set(
                    (*path, IMPORT_ROWS_SUBCOLLECTION, row_id),
                    _to_doc(row, **self._product_envelope(now)),
                )
            merged = current.model_copy(
                update={
                    "applied_rows": current.applied_rows + applied,
                    "conflicted_rows": current.conflicted_rows + conflicted,
                    "failed_rows": current.failed_rows + failed,
                    "reversed_rows": current.reversed_rows + reversed_rows,
                    "version": current.version + 1,
                    "updated_at": now,
                }
            )
            new_doc = _to_doc(merged)
            new_doc.update(
                {
                    DOC_KIND_FIELD: "ticket_import",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: bool(doc.get(LEGAL_HOLD_FIELD, False)),
                    CHAIN_HEAD_FIELD: doc.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
                    CHAIN_COUNT_FIELD: doc.get(CHAIN_COUNT_FIELD) or 0,
                }
            )
            new_doc["created_at"] = doc["created_at"]
            self._stamp_product(new_doc, now)
            view.set(path, new_doc)
            await self._append_global_event(
                view,
                parent_kind="import",
                parent_id=sha256_hex(import_id),
                event_type="import_rows_reversed" if reversing else "import_rows_applied",
                context=summary_context,
                now=now,
                changed_fields=["rows"],
            )
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="import_chunk",
                result={"import_id": import_id, "version": merged.version},
                now=now,
            )
            return new_doc

        return _from_doc(TicketImport, await self.backend.transact(_txn))

    async def apply_import_rows(
        self, import_id: str, specs: Sequence[ImportRowSpec], *, context: MutationContext
    ) -> TicketImport:
        """Apply a versioned chunk. Conflicts stay visible; nothing is deleted."""
        return await self._write_import_rows(
            import_id, specs, context=context, reversing=False
        )

    async def reverse_import_rows(
        self, import_id: str, specs: Sequence[ImportRowSpec], *, context: MutationContext
    ) -> TicketImport:
        """Reverse a versioned chunk by marking state, never by deleting."""
        return await self._write_import_rows(
            import_id, specs, context=context, reversing=True
        )

    async def list_import_rows(
        self, import_id: str, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> CursorPageOf:
        rows = await self.backend.list_subcollection(
            (IMPORTS_COLLECTION, import_id), IMPORT_ROWS_SUBCOLLECTION, limit=page_size
        )
        return CursorPageOf(
            items=[_from_doc(TicketImportRow, doc) for _id, doc in rows], page_size=page_size
        )

    async def create_export(
        self, summary: TicketExportSummary, *, context: MutationContext
    ) -> TicketExportSummary:
        """Record durable export metadata. Never the CSV body, never a title."""
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may export reviews")
        now = self._now()

        async def _txn(view: TransactionView) -> Document:
            path = (EXPORTS_COLLECTION, summary.export_id)
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                context,
                operation="create_export",
                request={"export_id": summary.export_id, "file_sha256": summary.file_sha256},
                now=now,
            )
            existing = await view.get(path)
            if replay is not None and existing is not None:
                return existing
            if existing is not None:
                raise ReviewRepositoryError("that export already exists")
            stored = summary.model_copy(update={"created_at": now, "version": 1})
            doc = _to_doc(stored)
            doc.update(
                {
                    DOC_KIND_FIELD: "ticket_export",
                    RETENTION_FIELD: now + self._review_retention,
                    LEGAL_HOLD_FIELD: False,
                }
            )
            self._stamp_product(doc, now)
            view.set(path, doc)
            await self._append_global_event(
                view,
                parent_kind="export",
                parent_id=sha256_hex(summary.export_id),
                event_type="export_created",
                context=context,
                now=now,
            )
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="create_export",
                result={"export_id": summary.export_id},
                now=now,
            )
            return doc

        return _from_doc(TicketExportSummary, await self.backend.transact(_txn))

    async def get_export(self, export_id: str) -> TicketExportSummary:
        doc = await self.backend.get_doc((EXPORTS_COLLECTION, export_id))
        if doc is None or _is_tombstone(doc):
            raise ReviewRepositoryError("no export exists for that id")
        return _from_doc(TicketExportSummary, doc)

    # ------------------------------------------------------------------
    # Retention: bounded, idempotent, non-cascading
    # ------------------------------------------------------------------

    @staticmethod
    def _bounded(max_documents: int) -> int:
        if max_documents < 1:
            raise RetentionRefused("a retention run must be allowed at least one document")
        return min(max_documents, MAX_RETENTION_MAX_DOCUMENTS)

    async def _retention_candidates(
        self, now: datetime, budget: int
    ) -> tuple[dict[str, list[tuple[str, Document]]], list[str], bool]:
        """Collect bounded candidates without ever scanning a whole database.

        A held document carries no ``retention_expires_at`` at all (the clock is
        parked under :data:`HELD_RETENTION_FIELD`), so it cannot appear here and
        cannot starve the bounded window. Holds are reported from their own
        bounded scan instead, purely so an operator can see them.
        """
        product: dict[str, list[tuple[str, Document]]] = {}
        holds: list[str] = []
        remaining = budget
        truncated = False
        for collection in DURABLE_PRODUCT_COLLECTIONS:
            if remaining <= 0:
                truncated = True
                break
            rows = await self.backend.scan_by_field(
                collection, field=RETENTION_FIELD, before=now, limit=remaining + 1
            )
            keep: list[tuple[str, Document]] = []
            for doc_id, doc in rows:
                if doc.get(LEGAL_HOLD_FIELD) is True:
                    # Defensive: a held document should not have reached the
                    # scan at all. Never count it against the window.
                    holds.append(doc_id)
                    continue
                if len(keep) >= remaining:
                    truncated = True
                    break
                keep.append((doc_id, doc))
            if keep:
                product[collection] = keep
                remaining -= len(keep)
        for collection in DURABLE_PRODUCT_COLLECTIONS:
            for doc_id, doc in await self.backend.scan_by_field(
                collection, field=HELD_RETENTION_FIELD, before=now, limit=budget
            ):
                if doc.get(LEGAL_HOLD_FIELD) is True and doc_id not in holds:
                    holds.append(doc_id)
        return product, holds, truncated

    async def preview_expired(
        self, *, max_documents: int = DEFAULT_RETENTION_MAX_DOCUMENTS
    ) -> RetentionPreview:
        """Report, without writing, what a bounded purge would touch."""
        budget = self._bounded(max_documents)
        now = self._now()
        product, holds, truncated = await self._retention_candidates(now, budget)

        ledger: list[str] = []
        for collection, rows in product.items():
            for doc_id, _doc in rows:
                for subcollection in self._ledger_subcollections(collection):
                    for event_id, event in await self.backend.list_subcollection(
                        (collection, doc_id), subcollection, limit=budget
                    ):
                        if self._ledger_expired(event, now):
                            ledger.append(event_id)

        tombstones: list[str] = []
        for collection in DURABLE_PRODUCT_COLLECTIONS:
            for doc_id, doc in await self.backend.scan_by_field(
                collection, field=TOMBSTONE_LEDGER_EXPIRY_FIELD, before=now, limit=budget
            ):
                if _is_tombstone(doc) and doc.get(LEGAL_HOLD_FIELD) is not True:
                    tombstones.append(doc_id)

        disposable: list[str] = []
        for collection in sorted(TTL_COLLECTIONS):
            for doc_id, _doc in await self.backend.scan_by_field(
                collection, field=TTL_FIELD, before=now, limit=budget
            ):
                disposable.append(doc_id)

        return RetentionPreview(
            product_candidates=[doc_id for rows in product.values() for doc_id, _ in rows],
            ledger_candidates=ledger,
            tombstone_candidates=tombstones,
            disposable_candidates=disposable,
            skipped_legal_hold=holds,
            counts_by_collection={
                collection: len(rows) for collection, rows in product.items()
            },
            truncated=truncated,
        )

    @staticmethod
    def _ledger_subcollections(collection: str) -> tuple[str, ...]:
        if collection == REVIEWS_COLLECTION:
            return (AUDIT_EVENTS_SUBCOLLECTION,)
        if collection == BATCHES_COLLECTION:
            return (BATCH_EVENTS_SUBCOLLECTION,)
        return ()

    @staticmethod
    def _product_subcollections(collection: str) -> tuple[str, ...]:
        if collection == REVIEWS_COLLECTION:
            return (EVIDENCE_LINKS_SUBCOLLECTION,)
        if collection == BATCHES_COLLECTION:
            return (BATCH_ITEMS_SUBCOLLECTION,)
        if collection == IMPORTS_COLLECTION:
            return (IMPORT_ROWS_SUBCOLLECTION,)
        return ()

    @staticmethod
    def _ledger_expired(event: Mapping[str, Any], now: datetime) -> bool:
        if event.get(LEGAL_HOLD_FIELD) is True:
            return False
        expires_at = event.get(RETENTION_FIELD)
        return isinstance(expires_at, datetime) and expires_at <= now

    async def purge_expired(
        self,
        *,
        run_id: str,
        context: MutationContext,
        max_documents: int = DEFAULT_RETENTION_MAX_DOCUMENTS,
    ) -> RetentionReport:
        """Bounded, idempotent purge that deletes only exact document ids.

        Order is fixed by contract: product parents become content-free
        tombstones while a younger ledger exists, product subcollections go by
        exact id, then expired ledger events by exact id, and only then the
        exact tombstone. There is no recursive delete anywhere in this module,
        and Firestore does not cascade, so a 730-day product purge can never
        shorten the 2,555-day audit contract.
        """
        if context.actor_role is not ReviewerRole.ADMIN:
            raise NotAuthorized("only an admin may run retention")
        budget = self._bounded(max_documents)
        now = self._now()
        report = RetentionReport()
        spent = 0

        product, holds, truncated = await self._retention_candidates(now, budget)
        report.skipped_legal_hold = holds

        for collection, rows in product.items():
            for doc_id, doc in rows:
                if spent >= budget:
                    truncated = True
                    break
                if _is_tombstone(doc):
                    continue
                # Product subcollections go BEFORE the parent becomes a
                # tombstone. A tombstone carries no retention_expires_at and so
                # is never re-found by this scan; tombstoning first would orphan
                # any leftover child if the run ran out of budget here.
                leftover = False
                for subcollection in self._product_subcollections(collection):
                    if spent >= budget:
                        leftover = True
                        break
                    deleted, more = await self._delete_subcollection_page(
                        collection, doc_id, subcollection, now, limit=budget - spent
                    )
                    spent += len(deleted)
                    report.product_documents_deleted.extend(deleted)
                    leftover = leftover or more
                if leftover or spent >= budget:
                    # Leave this parent intact so the next bounded run finds it
                    # again and finishes the job.
                    truncated = True
                    continue
                if await self._tombstone(collection, doc_id, doc, now):
                    spent += 1
                    report.tombstoned.append(doc_id)

        # Expired ledger events, by exact id, for every parent that has one.
        for collection in DURABLE_PRODUCT_COLLECTIONS:
            for subcollection in self._ledger_subcollections(collection):
                if spent >= budget:
                    truncated = True
                    break
                for doc_id, parent in await self.backend.scan_by_field(
                    collection, field=TOMBSTONE_LEDGER_EXPIRY_FIELD, before=now, limit=budget
                ):
                    if parent.get(LEGAL_HOLD_FIELD) is True:
                        if doc_id not in report.skipped_legal_hold:
                            report.skipped_legal_hold.append(doc_id)
                        continue
                    if spent >= budget:
                        truncated = True
                        break
                    deleted = await self._delete_expired_events(
                        collection, doc_id, subcollection, now, limit=budget - spent
                    )
                    spent += len(deleted)
                    report.ledger_events_deleted.extend(deleted)

        # Only now may an exact tombstone go, and only with an empty ledger.
        for collection in DURABLE_PRODUCT_COLLECTIONS:
            for doc_id, doc in await self.backend.scan_by_field(
                collection, field=TOMBSTONE_LEDGER_EXPIRY_FIELD, before=now, limit=budget
            ):
                if not _is_tombstone(doc) or doc.get(LEGAL_HOLD_FIELD) is True:
                    continue
                if spent >= budget:
                    truncated = True
                    break
                if await self._has_remaining_ledger(collection, doc_id):
                    continue
                if await self._delete_exact_tombstone(collection, doc_id, now):
                    spent += 1
                    report.parents_deleted.append(doc_id)

        # Expired global ledger events, by exact id.
        if spent < budget:
            for doc_id, doc in await self.backend.scan_by_field(
                GLOBAL_AUDIT_EVENTS_COLLECTION,
                field=RETENTION_FIELD,
                before=now,
                limit=budget - spent,
            ):
                if doc_id == GLOBAL_CHAIN_HEAD_DOC_ID or not self._ledger_expired(doc, now):
                    continue
                await self._delete_document((GLOBAL_AUDIT_EVENTS_COLLECTION, doc_id))
                spent += 1
                report.ledger_events_deleted.append(doc_id)

        # Disposable documents. Firestore TTL is cleanup-only and asynchronous,
        # so the facade sweeps elapsed ids explicitly too.
        for collection in sorted(TTL_COLLECTIONS):
            if spent >= budget:
                truncated = True
                break
            for doc_id, _doc in await self.backend.scan_by_field(
                collection, field=TTL_FIELD, before=now, limit=budget - spent
            ):
                if await self._delete_expired_disposable(collection, doc_id, now):
                    spent += 1
                    report.disposable_deleted.append(doc_id)

        changed = bool(
            report.tombstoned
            or report.product_documents_deleted
            or report.ledger_events_deleted
            or report.parents_deleted
            or report.disposable_deleted
        )
        if changed:
            await self._append_retention_event(run_id=run_id, context=context, now=now, report=report)
        report.truncated = truncated or spent >= budget
        return report

    async def _tombstone(
        self, collection: str, doc_id: str, doc: Document, now: datetime
    ) -> bool:
        """Replace one parent with a content-free tombstone, or decline.

        The candidate was found by a scan that ran outside any transaction, so
        the decision is re-taken here against a transactional read. Without
        that, a reviewer's save or an admin's legal hold committed between the
        scan and this write would be destroyed -- the write set would carry no
        read, so Firestore would have nothing to detect contention on.
        """

        async def _txn(view: TransactionView) -> bool:
            current = await view.get((collection, doc_id))
            if current is None or _is_tombstone(current):
                return False
            if current.get(LEGAL_HOLD_FIELD) is True:
                return False
            expires_at = current.get(RETENTION_FIELD)
            if not isinstance(expires_at, datetime) or expires_at > now:
                # Someone touched the record after the scan; its 730-day clock
                # restarted and it is no longer expired.
                return False
            tombstone = {
                DOC_KIND_FIELD: PURGED_TOMBSTONE_KIND,
                "parent_id_hash": sha256_hex(doc_id),
                "schema_version": SCHEMA_VERSION,
                "purged_at": now,
                TOMBSTONE_LEDGER_EXPIRY_FIELD: (
                    current.get(LEDGER_EXPIRY_FIELD) or (now + self._audit_retention)
                ),
                LEGAL_HOLD_FIELD: False,
                CHAIN_HEAD_FIELD: current.get(CHAIN_HEAD_FIELD) or GENESIS_EVENT_HASH,
            }
            if set(tombstone) != TOMBSTONE_FIELDS:  # pragma: no cover - guard
                raise RetentionRefused("the tombstone shape drifted from its contract")
            view.set((collection, doc_id), tombstone)
            return True

        return await self.backend.transact(_txn)

    async def _delete_subcollection_page(
        self, collection: str, doc_id: str, subcollection: str, now: datetime, *, limit: int
    ) -> tuple[list[str], bool]:
        """Delete one bounded page by exact id; report whether more remain."""
        bound = max(limit, 1)
        rows = await self.backend.list_subcollection(
            (collection, doc_id), subcollection, limit=bound + 1
        )
        candidates = [row_id for row_id, _doc in rows][:bound]
        more_remain = len(rows) > bound
        if not candidates:
            return [], more_remain

        async def _txn(view: TransactionView) -> list[str]:
            parent = await view.get((collection, doc_id))
            if parent is None or parent.get(LEGAL_HOLD_FIELD) is True:
                return []
            expires_at = parent.get(RETENTION_FIELD)
            if not _is_tombstone(parent) and (
                not isinstance(expires_at, datetime) or expires_at > now
            ):
                # The parent was touched after the scan, so its product state is
                # live again and its children must stay.
                return []
            deleted: list[str] = []
            for row_id in candidates:
                path = (collection, doc_id, subcollection, row_id)
                if await view.get(path) is None:
                    continue
                view.delete(path)
                deleted.append(row_id)
            return deleted

        return await self.backend.transact(_txn), more_remain

    async def _delete_expired_events(
        self,
        collection: str,
        doc_id: str,
        subcollection: str,
        now: datetime,
        *,
        limit: int,
    ) -> list[str]:
        rows = await self.backend.list_subcollection(
            (collection, doc_id), subcollection, limit=max(limit, 1)
        )
        candidates = [
            row_id for row_id, event in rows if self._ledger_expired(event, now)
        ][:limit]
        if not candidates:
            return []

        async def _txn(view: TransactionView) -> list[str]:
            # Re-decide inside the transaction: a legal hold placed after the
            # scan must still suppress the delete, and an event id must never be
            # removed on the strength of a stale snapshot.
            parent = await view.get((collection, doc_id))
            if parent is not None and parent.get(LEGAL_HOLD_FIELD) is True:
                return []
            deleted: list[str] = []
            for row_id in candidates:
                path = (collection, doc_id, subcollection, row_id)
                event = await view.get(path)
                if event is None or not self._ledger_expired(event, now):
                    continue
                view.delete(path)
                deleted.append(row_id)
            return deleted

        return await self.backend.transact(_txn)

    async def _has_remaining_ledger(self, collection: str, doc_id: str) -> bool:
        for subcollection in self._ledger_subcollections(collection):
            if await self.backend.list_subcollection(
                (collection, doc_id), subcollection, limit=1
            ):
                return True
        return False

    async def _delete_exact_tombstone(
        self, collection: str, doc_id: str, now: datetime
    ) -> bool:
        """Remove one exact tombstone, last, and only once its ledger is gone."""

        async def _txn(view: TransactionView) -> bool:
            current = await view.get((collection, doc_id))
            if current is None or not _is_tombstone(current):
                return False
            if current.get(LEGAL_HOLD_FIELD) is True:
                return False
            expires_at = current.get(TOMBSTONE_LEDGER_EXPIRY_FIELD)
            if not isinstance(expires_at, datetime) or expires_at > now:
                return False
            view.delete((collection, doc_id))
            return True

        return await self.backend.transact(_txn)

    async def _delete_expired_disposable(
        self, collection: str, doc_id: str, now: datetime
    ) -> bool:
        """Sweep one elapsed disposable document, re-checked transactionally."""

        async def _txn(view: TransactionView) -> bool:
            path = (collection, doc_id)
            current = await view.get(path)
            if current is None or _live(current, now) is not None:
                # It was refreshed after the scan; it is live again.
                return False
            view.delete(path)
            return True

        return await self.backend.transact(_txn)

    async def _delete_document(self, path: DocPath) -> None:
        async def _txn(view: TransactionView) -> None:
            if await view.get(path) is not None:
                view.delete(path)

        await self.backend.transact(_txn)

    async def _append_retention_event(
        self, *, run_id: str, context: MutationContext, now: datetime, report: RetentionReport
    ) -> None:
        """One content-free global event per retention run."""
        run_context = context.model_copy(
            update={"idempotency_key": f"retention:{run_id}", "reason_code": "retention"}
        )

        async def _txn(view: TransactionView) -> None:
            replay, key_hash, digest = await self._claim_idempotency(
                view,
                run_context,
                operation="purge_expired",
                request={"run_id": run_id},
                now=now,
            )
            if replay is not None:
                return
            await self._append_global_event(
                view,
                parent_kind="retention",
                parent_id=sha256_hex(run_id),
                event_type="retention_purged",
                context=run_context,
                now=now,
                changed_fields=["ledger_events", "product_documents", "tombstones"],
            )
            self._record_idempotency(
                view,
                key_hash=key_hash,
                digest=digest,
                operation="purge_expired",
                result={"run_id": sha256_hex(run_id)},
                now=now,
            )

        await self.backend.transact(_txn)


__all__ = [
    "ALLOWED_REVIEW_FACETS",
    "AUDIT_EVENTS_SUBCOLLECTION",
    "BATCHES_COLLECTION",
    "BATCH_EVENTS_SUBCOLLECTION",
    "BATCH_ITEMS_SUBCOLLECTION",
    "CHAIN_HEAD_FIELD",
    "CONSOLE_CACHE_COLLECTION",
    "DEVREV_MESSAGE_CACHE_COLLECTION",
    "EVIDENCE_LINKS_SUBCOLLECTION",
    "EXPORTS_COLLECTION",
    "FIRESTORE_MAX_DOCUMENT_BYTES",
    "FIRESTORE_MAX_WRITES_PER_TRANSACTION",
    "GLOBAL_AUDIT_EVENTS_COLLECTION",
    "GLOBAL_CHAIN_HEAD_DOC_ID",
    "IDEMPOTENCY_KEYS_COLLECTION",
    "IMPORTS_COLLECTION",
    "IMPORT_ROWS_SUBCOLLECTION",
    "IMPORT_STAGING_COLLECTION",
    "LEGAL_HOLD_FIELD",
    "PURGED_TOMBSTONE_KIND",
    "RETENTION_FIELD",
    "REVIEWS_COLLECTION",
    "REVIEW_LIST_CURSOR_CONTEXT",
    "TOMBSTONE_FIELDS",
    "TTL_COLLECTIONS",
    "TTL_FIELD",
    "AuditChainReport",
    "BatchAlreadyClaimed",
    "BatchClaim",
    "BatchContractViolation",
    "BatchLeaseLost",
    "BatchNotFound",
    "BatchReleaseRefused",
    "BatchVersionConflict",
    "ConsoleCacheEntry",
    "DevRevMessageCacheEntry",
    "EvidenceCandidate",
    "EvidenceCandidateRejected",
    "FirestoreTicketReviewBackend",
    "IdempotencyConflict",
    "ImportRowSpec",
    "InMemoryTicketReviewBackend",
    "InvalidBatchTransition",
    "InvalidImportTransition",
    "InvalidReviewTransition",
    "LeaseExtensionRefused",
    "MultiPatchResult",
    "MutationContext",
    "NotAuthorized",
    "RetentionPreview",
    "RetentionRefused",
    "RetentionReport",
    "ReviewIdentityConflict",
    "ReviewListQuery",
    "ReviewNotFound",
    "ReviewPatchFailure",
    "ReviewPatchSpec",
    "ReviewRepositoryError",
    "ReviewVersionConflict",
    "TicketExportSummary",
    "TicketReviewBackend",
    "TicketReviewRepository",
    "UnsupportedFilterCombination",
    "canonical_ttl_declarations",
    "item_set_digest",
    "normalize_display_id",
    "review_index_declarations",
    "sha256_hex",
]
