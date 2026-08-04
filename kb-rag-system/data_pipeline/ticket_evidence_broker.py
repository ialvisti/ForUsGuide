"""Bounded, read-only broker over existing RAG execution logs.

Firestore IAM is *database*-scoped, not collection-scoped: a service account
that can read production ``(default)`` can read all of it. This module is the
application-level narrowing that IAM cannot express. It is the only component
allowed to read ``(default)``, and it exposes exactly one operation:

    one bounded transient DON  →  one sanitized, allowlisted evidence envelope

Five properties are enforced here rather than trusted to a caller.

**Only versioned HMAC lookup.** The producer stored a keyed digest of the DON,
never the DON. The broker therefore receives the raw DON from the authenticated
console, computes candidate digests *in memory*, and queries by
``(lookup_key_version, ticket_lookup_hmac)``. It cannot answer "show me
everything" because it has no other query.

**Bounded keyring fan-out.** One indexed query per *active* numeric version,
capped. Rotation is explicit: an unknown or disabled version fails loudly
instead of silently degrading to "not found", which would look identical to a
genuinely uncorrelated ticket and would quietly retire evidence early.

**Bounded results.** A fixed maximum result count, and ``truncated`` is
reported rather than hidden.

**Allowlisted fields only.** Records are projected through
:class:`~api.ticket_review_models.RagEvidenceRecord`, which is
``extra="forbid"`` and carries no prompt, response, chunk text, participant
field, arbitrary log key, or credential.

**Legacy is unavailable, not inferred.** A document written before the Stage 4
schema has no lookup digest at all, so it is unreachable by construction — and
a ticket whose only evidence is legacy comes back ``unavailable`` with a
reason, never a guess.

Authorization is deliberately *not* here. Caller identity is checked at the app
boundary in ``api/tickets_evidence_broker_main.py``, so this module stays a
pure, testable query layer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from api.ticket_review_models import (
    MAX_BROKER_KEY_VERSIONS,
    MAX_BROKER_RESULTS,
    CorrelationStatus,
    CorrelationTrust,
    EvidenceSourceCollection,
    RagEvidenceEnvelope,
    RagEvidenceRecord,
    TicketEvidenceLookupRequest,
)
from api.tickets_console_config import EvidenceBrokerSettings, _secret_text
from data_pipeline.ticket_review_provenance import (
    bounded_key_versions,
    envelope_result_digest,
    parse_execution_document,
    record_content_digest,
    sha256_hex,
    ticket_lookup_hmac,
)

logger = logging.getLogger(__name__)

# The stored field paths the producer writes. Dotted because the provenance
# fragment is a sub-document; both are part of the composite index below.
LOOKUP_HMAC_FIELD = "provenance.correlation.ticket_lookup_hmac"
LOOKUP_VERSION_FIELD = "provenance.correlation.lookup_key_version"
OCCURRED_AT_FIELD = "timestamp"

# The only collections the broker may read, in the order it reads them.
# ``ticket_jobs`` is included because a server-created ticket-handler job is a
# defensible correlation source; it is queried by the same keyed digest and
# projected through the same sanitizing model as the other two.
BROKER_COLLECTIONS: tuple[EvidenceSourceCollection, ...] = (
    EvidenceSourceCollection.EXECUTION_LOGS,
    EvidenceSourceCollection.TICKET_EXECUTIONS,
    EvidenceSourceCollection.TICKET_JOBS,
)

# Warning tokens. A fixed vocabulary: these cross into an API envelope.
WARNING_RESULTS_TRUNCATED = "broker_results_truncated"
WARNING_COLLECTION_UNAVAILABLE = "broker_collection_unavailable"
WARNING_LEGACY_ONLY = "broker_legacy_records_only"

REASON_NO_EVIDENCE = "no_defensible_correlation_exists"
REASON_LEGACY_ONLY = "only_pre_provenance_records_exist"


class EvidenceBrokerError(Exception):
    """Base class for every broker failure."""


class LookupKeyringUnavailable(EvidenceBrokerError):
    """The broker has no usable active lookup key.

    Fail closed. Answering "no evidence" with no key configured would report a
    misconfiguration as a factual absence of evidence.
    """


class UnknownLookupKeyVersion(EvidenceBrokerError):
    """An allowed version has no key, or a supplied version is not allowed."""


class EvidenceBackend(Protocol):
    """The minimal read-only primitive the broker needs.

    Narrow on purpose: there is no ``scan``, no ``list_collection``, and no
    free-form ``where``, so no future caller can turn the broker into a general
    Firestore reader.
    """

    async def query_by_lookup(
        self,
        collection: EvidenceSourceCollection,
        *,
        lookup_key_version: int,
        ticket_lookup_hmac: str,
        limit: int,
    ) -> Sequence[tuple[str, Mapping[str, Any]]]:
        """Return at most ``limit`` ``(document_id, document)`` pairs."""
        ...


class InMemoryEvidenceBackend:
    """A deterministic backend for contract tests.

    It mirrors the Firestore backend's *only* access path: an equality match on
    both indexed fields. A test therefore cannot accidentally prove a behaviour
    that a collection scan would allow.
    """

    def __init__(self) -> None:
        self._data: dict[EvidenceSourceCollection, dict[str, Mapping[str, Any]]] = {
            collection: {} for collection in BROKER_COLLECTIONS
        }
        self.queries: list[tuple[str, int, str, int]] = []
        self.unavailable: set[EvidenceSourceCollection] = set()

    def put(
        self,
        collection: EvidenceSourceCollection,
        document_id: str,
        document: Mapping[str, Any],
    ) -> None:
        self._data[collection][document_id] = document

    async def query_by_lookup(
        self,
        collection: EvidenceSourceCollection,
        *,
        lookup_key_version: int,
        ticket_lookup_hmac: str,
        limit: int,
    ) -> Sequence[tuple[str, Mapping[str, Any]]]:
        self.queries.append(
            (collection.value, lookup_key_version, ticket_lookup_hmac, limit)
        )
        if collection in self.unavailable:
            raise RuntimeError("collection is unavailable")
        matched: list[tuple[str, Mapping[str, Any]]] = []
        for document_id, document in sorted(self._data[collection].items()):
            correlation = _nested(document, "provenance", "correlation")
            if correlation.get("ticket_lookup_hmac") != ticket_lookup_hmac:
                continue
            if correlation.get("lookup_key_version") != lookup_key_version:
                continue
            matched.append((document_id, document))
            if len(matched) >= limit:
                break
        return matched


class FirestoreEvidenceBackend:
    """Read-only Firestore access to ``(default)``, index-backed only.

    Every query filters on both indexed fields and applies a hard ``limit``, so
    a lookup can never turn into a whole-collection read. No write, delete, or
    transaction primitive is exposed.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def query_by_lookup(
        self,
        collection: EvidenceSourceCollection,
        *,
        lookup_key_version: int,
        ticket_lookup_hmac: str,
        limit: int,
    ) -> Sequence[tuple[str, Mapping[str, Any]]]:
        # Imported lazily so this module can be unit-tested, and the broker app
        # imported, without the Firestore SDK present.
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (
            self._client.collection(collection.value)
            .where(filter=FieldFilter(LOOKUP_VERSION_FIELD, "==", lookup_key_version))
            .where(filter=FieldFilter(LOOKUP_HMAC_FIELD, "==", ticket_lookup_hmac))
            .order_by(OCCURRED_AT_FIELD, direction="DESCENDING")
            .limit(limit)
        )
        rows: list[tuple[str, Mapping[str, Any]]] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if isinstance(data, Mapping):
                rows.append((snapshot.id, data))
        return rows


def broker_index_declarations() -> list[dict[str, Any]]:
    """The composite indexes every broker query requires.

    Declared in code, and asserted by ``tests/test_ticket_evidence_broker.py``
    to cover every query this module can issue, because the canonical
    ``firestore.indexes.json`` mirror for ``(default)`` is compared field-for-
    field against the Terraform module — and Terraform is Stage 10's
    deliverable, not this stage's. Stage 10 mirrors these declarations; until
    then this function is the reviewable source of truth.
    """
    return [
        {
            "collectionGroup": collection.value,
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": LOOKUP_VERSION_FIELD, "order": "ASCENDING"},
                {"fieldPath": LOOKUP_HMAC_FIELD, "order": "ASCENDING"},
                {"fieldPath": OCCURRED_AT_FIELD, "order": "DESCENDING"},
            ],
            "__comment": (
                "evidence broker: versioned HMAC lookup, newest first, bounded "
                "by an explicit limit; never a collection scan"
            ),
        }
        for collection in BROKER_COLLECTIONS
    ]


class TicketEvidenceBroker:
    """Maps one DON to a bounded, sanitized provenance envelope."""

    def __init__(
        self,
        backend: EvidenceBackend,
        *,
        keyring: Mapping[int, bytes | str],
        active_versions: Sequence[int],
        max_results: int = MAX_BROKER_RESULTS,
        collections: Sequence[EvidenceSourceCollection] = BROKER_COLLECTIONS,
    ) -> None:
        versions = bounded_key_versions(active_versions, limit=MAX_BROKER_KEY_VERSIONS)
        if not versions:
            raise LookupKeyringUnavailable(
                "the evidence broker needs at least one active lookup key version"
            )
        # An allowed version with no key is a deployment error, and it must not
        # be skipped: silently ignoring it would make historical evidence
        # disappear without anybody being told a key was retired.
        missing = [version for version in versions if not keyring.get(version)]
        if missing:
            raise UnknownLookupKeyVersion(
                f"{len(missing)} active lookup key version(s) have no configured key"
            )
        self._backend = backend
        self._keyring = {
            version: (
                keyring[version].encode("utf-8")
                if isinstance(keyring[version], str)
                else keyring[version]
            )
            for version in versions
        }
        self._active_versions = versions
        self._max_results = max(1, min(int(max_results), MAX_BROKER_RESULTS))
        self._collections = tuple(
            collection for collection in collections if collection in BROKER_COLLECTIONS
        )

    @classmethod
    def from_settings(
        cls, settings: EvidenceBrokerSettings, backend: EvidenceBackend
    ) -> TicketEvidenceBroker:
        """Build a broker from the broker plane's own settings.

        The keyring is a numeric-version → key JSON map delivered as a Secret
        Manager version. Only versions in ``CORRELATION_ALLOWED_KEY_VERSIONS``
        are ever queried, so retiring a key is an explicit configuration change
        rather than a deletion.
        """
        import json

        raw = _secret_text(settings.CORRELATION_LOOKUP_KEYRING_JSON)
        if not raw:
            raise LookupKeyringUnavailable("the broker lookup keyring is not configured")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LookupKeyringUnavailable("the broker lookup keyring is not valid JSON") from exc
        if not isinstance(parsed, Mapping) or not parsed:
            raise LookupKeyringUnavailable("the broker lookup keyring must be a version map")

        keyring: dict[int, str] = {}
        for version, key in parsed.items():
            try:
                numeric = int(version)
            except (TypeError, ValueError):
                continue
            if isinstance(key, str) and key:
                keyring[numeric] = key
        return cls(
            backend,
            keyring=keyring,
            active_versions=settings.CORRELATION_ALLOWED_KEY_VERSIONS,
            max_results=settings.MAX_RESULTS,
        )

    @property
    def active_versions(self) -> tuple[int, ...]:
        return tuple(self._active_versions)

    async def lookup(self, request: TicketEvidenceLookupRequest) -> RagEvidenceEnvelope:
        """Resolve one DON into a bounded sanitized envelope.

        The DON lives only in this frame: it is read out of the ``SecretStr``,
        hashed once per active key version, and never assigned to a field, put
        in a log line, or included in an error.
        """
        devrev_work_id = request.devrev_work_id.get_secret_value()
        limit = min(int(request.max_results), self._max_results)

        records: list[RagEvidenceRecord] = []
        warnings: list[str] = []
        truncated = False

        for version in self._active_versions:
            digest = ticket_lookup_hmac(self._keyring[version], devrev_work_id)
            for collection in self._collections:
                if len(records) >= limit:
                    truncated = True
                    break
                remaining = limit - len(records)
                try:
                    rows = await self._backend.query_by_lookup(
                        collection,
                        lookup_key_version=version,
                        ticket_lookup_hmac=digest,
                        # One extra row so "there were more" is detectable
                        # without ever materializing an unbounded result set.
                        limit=min(remaining + 1, MAX_BROKER_RESULTS),
                    )
                except Exception as exc:  # noqa: BLE001 - one source must not fail the lookup
                    # A degraded source is reported as a gap, never as absence.
                    logger.warning(
                        "evidence broker source unavailable; collection=%s error_type=%s",
                        collection.value,
                        type(exc).__name__,
                    )
                    if WARNING_COLLECTION_UNAVAILABLE not in warnings:
                        warnings.append(WARNING_COLLECTION_UNAVAILABLE)
                    continue

                if len(rows) > remaining:
                    truncated = True
                    rows = rows[:remaining]

                for document_id, document in rows:
                    records.append(
                        _sanitized_record(collection, document_id, document)
                    )
            if len(records) >= limit:
                truncated = True
                break

        if truncated and WARNING_RESULTS_TRUNCATED not in warnings:
            warnings.append(WARNING_RESULTS_TRUNCATED)

        return _envelope(
            records,
            warnings=warnings,
            truncated=truncated,
            versions=self._active_versions,
        )


def _sanitized_record(
    collection: EvidenceSourceCollection,
    document_id: str,
    document: Mapping[str, Any],
) -> RagEvidenceRecord:
    """Project one stored document into the allowlisted evidence model.

    ``evidence_reference`` is a keyless digest of ``collection:document_id``, so
    it is stable enough for a reviewer to link and useless as a Firestore path.
    ``evidence_digest`` covers the sanitized *content*, which is what makes
    "revalidate against the current broker result" meaningful: if the record
    changes, the digest changes and a stale candidate token stops matching.
    """
    reference = sha256_hex(f"{collection.value}:{document_id}")
    record = parse_execution_document(
        document, source_collection=collection, evidence_reference=reference
    )
    return record.model_copy(update={"evidence_digest": record_content_digest(record)})


def _envelope(
    records: Sequence[RagEvidenceRecord],
    *,
    warnings: Sequence[str],
    truncated: bool,
    versions: Sequence[int],
) -> RagEvidenceEnvelope:
    """Build the envelope, deciding status from what was actually found."""
    collected = list(warnings)
    if not records:
        return RagEvidenceEnvelope(
            correlation_status=CorrelationStatus.UNAVAILABLE,
            records=[],
            result_digest=envelope_result_digest([]),
            key_versions_queried=list(versions),
            truncated=truncated,
            unavailable_reason=REASON_NO_EVIDENCE,
            warnings=collected,
        )

    # A record only counts as an automatic link when the producer recorded a
    # verified workload trust level. Anything else — a legacy document, an
    # unverified candidate — is evidence the console may show but must not
    # present as an established correlation.
    linked = any(
        record.correlation_trust is CorrelationTrust.VERIFIED_WORKLOAD for record in records
    )
    if not linked and WARNING_LEGACY_ONLY not in collected:
        collected.append(WARNING_LEGACY_ONLY)

    return RagEvidenceEnvelope(
        correlation_status=(
            CorrelationStatus.LINKED if linked else CorrelationStatus.UNAVAILABLE
        ),
        records=list(records),
        result_digest=envelope_result_digest(
            record.evidence_reference for record in records
        ),
        key_versions_queried=list(versions),
        truncated=truncated,
        unavailable_reason=None if linked else REASON_LEGACY_ONLY,
        warnings=collected,
    )


def _nested(document: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


__all__ = [
    "BROKER_COLLECTIONS",
    "LOOKUP_HMAC_FIELD",
    "LOOKUP_VERSION_FIELD",
    "OCCURRED_AT_FIELD",
    "REASON_LEGACY_ONLY",
    "REASON_NO_EVIDENCE",
    "WARNING_COLLECTION_UNAVAILABLE",
    "WARNING_LEGACY_ONLY",
    "WARNING_RESULTS_TRUNCATED",
    "EvidenceBackend",
    "EvidenceBrokerError",
    "FirestoreEvidenceBackend",
    "InMemoryEvidenceBackend",
    "LookupKeyringUnavailable",
    "TicketEvidenceBroker",
    "UnknownLookupKeyVersion",
    "broker_index_declarations",
]
