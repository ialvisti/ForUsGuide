"""Pure provenance helpers for trusted RAG↔ticket correlation.

This module is deliberately I/O-free: no Firestore, no HTTP, no Pinecone, no
OpenAI, and no import of ``api.config``. Everything here is a deterministic
function over values, so the producer (``api/main.py``, ``api/ticket_worker.py``,
``data_pipeline/rag_engine.py``), the versioned ``ExecutionLogger`` schema, and
the read-only evidence broker can all share it without importing one another.

Three rules shape every function below.

**A gap is a gap.** Nothing here infers a correlation. Text or timestamp
similarity produces a *suggestion*, never :attr:`CorrelationStatus.LINKED`.
When the deployed index carries no version, :attr:`RagProvenance.index_version`
stays ``None`` and :attr:`MissingProvenance.INDEX_VERSION` is recorded rather
than a plausible-looking commit being borrowed from somewhere else.

**No raw external identifier survives.** A DevRev DON reaches
:func:`ticket_lookup_hmac` and :func:`verify_ingress_context` and leaves as a
keyed digest. Neither the DON nor either key is returned, formatted into an
error, or made reachable from a log record. That is why the lookup reference is
versioned: the raw DON is intentionally absent, so a rotated key can only be
matched by asking the broker for one candidate HMAC per active version.

**Two keys, two jobs.** The *ingress* key authenticates that the active n8n
workload really sent this correlation context. A distinct *lookup* key derives
the storage/query HMAC. Sharing one key would let anybody who can verify a
signature also mint lookup references for arbitrary tickets, so
``validate_producer_correlation_settings`` refuses equal keys and this module
never accepts one key where it wants the other.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from api.ticket_review_models import (
    CORRELATION_HMAC_VERSION,
    EXECUTION_LOG_SCHEMA_VERSION,
    INGRESS_SIGNATURE_MAX_SKEW_S,
    LEGACY_EXECUTION_LOG_SCHEMA_VERSION,
    MAX_BROKER_KEY_VERSIONS,
    MAX_EVIDENCE_REFS_PER_REVIEW,
    MAX_ID_LENGTH,
    MAX_LIST_ITEMS,
    CorrelationStatus,
    CorrelationTrust,
    EvidenceSourceCollection,
    MissingProvenance,
    ObservedChunkRef,
    RagEvidenceRecord,
    RagProvenance,
)

# =====================================================================
# Canonical identifiers
# =====================================================================

# The static prompt-template artifact. It identifies WHICH template shipped,
# and its SHA-256 pins the exact bytes. It is intentionally not derived from a
# rendered prompt: a rendered prompt contains participant text and changes on
# every request, so it can never be a template version.
PROMPT_TEMPLATE_ID = "kb_rag_ticket_response_v1"

# Correlation source labels. A closed vocabulary, because these strings cross
# into stored documents, API envelopes, and UI badges.
CORRELATION_SOURCE_N8N_SIGNED = "n8n_signed_ingress"
CORRELATION_SOURCE_INTERNAL_JOB = "internal_ticket_job"
CORRELATION_SOURCE_UNVERIFIED_HEADER = "unverified_client_header"

# Reasons a correlation could not be verified. Also a closed vocabulary: these
# are surfaced to operators, so they must never carry a DON, a key, or a body.
REASON_NO_CONTEXT = "no_correlation_context"
REASON_MISSING_SIGNATURE = "missing_ingress_signature"
REASON_UNKNOWN_KEY_VERSION = "unknown_ingress_key_version"
REASON_STALE_TIMESTAMP = "ingress_timestamp_outside_window"
REASON_BAD_SIGNATURE = "ingress_signature_mismatch"
REASON_MISSING_RECEIPT = "missing_idempotency_receipt"
REASON_LOOKUP_KEY_UNAVAILABLE = "lookup_key_unavailable"

# A server-minted ticket job id. api/main.py validates the same shape before it
# will even look a job up, so anything else is caller-controlled text and must
# not enter a log record.
_INTERNAL_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")

# Cloud Run injects K_REVISION/K_SERVICE itself. A commit or image digest is
# only ever read from an explicitly configured variable, because guessing one
# would attach a false "the index contents were at this commit" claim.
DEPLOY_REVISION_ENV = "K_REVISION"
DEPLOY_SERVICE_ENV = "K_SERVICE"
DEPLOY_COMMIT_ENV = "SERVICE_COMMIT_SHA"
DEPLOY_IMAGE_DIGEST_ENV = "SERVICE_IMAGE_DIGEST"

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")
_WHITESPACE_RUN = re.compile(r"\s+")


# =====================================================================
# Normalization and hashing
# =====================================================================


def normalize_for_hash(value: str) -> str:
    """Normalize text so semantically identical inputs hash identically.

    Unicode NFC composition plus whitespace collapsing and trimming. There is
    deliberately **no case folding**: article ids, DONs, and template ids are
    case-sensitive, so folding them would merge distinct identifiers, and a
    response hash must distinguish "NO" from "no".
    """
    if not isinstance(value, str):
        raise TypeError("only text can be normalized for hashing")
    return _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", value)).strip()


def sha256_hex(value: str) -> str:
    """SHA-256 of normalized text, lowercase hex."""
    return hashlib.sha256(normalize_for_hash(value).encode("utf-8")).hexdigest()


def response_sha256(response_text: str) -> str:
    """Hash a generated response so it can be matched without being stored."""
    return sha256_hex(response_text)


def request_digest(payload: Mapping[str, Any] | str) -> str:
    """A stable digest of the request the signature covers.

    Accepts either the already-canonicalized request string produced by the
    caller or a mapping, which is rendered with sorted keys so two equal
    payloads cannot disagree on key order.
    """
    if isinstance(payload, str):
        return sha256_hex(payload)
    parts = [f"{key}={payload[key]!s}" for key in sorted(payload)]
    return sha256_hex("\n".join(parts))


def normalized_don(devrev_work_id: str) -> str:
    """Normalize a DON before it is hashed.

    A DON is case-sensitive and contains ``/``, so normalization is limited to
    Unicode NFC and whitespace trimming. The value is validated for length here
    so an oversized identifier fails before it reaches an HMAC.
    """
    normalized = normalize_for_hash(devrev_work_id)
    if not normalized:
        raise ValueError("a DevRev work id is required")
    if len(normalized) > MAX_ID_LENGTH:
        raise ValueError("the DevRev work id is too long")
    return normalized


def ticket_lookup_hmac(lookup_key: bytes | str, devrev_work_id: str) -> str:
    """``HMAC-SHA256(lookup_key, normalized DON)`` as lowercase hex.

    Neither the key nor the DON appears in the return value, in any exception
    raised here, or anywhere else in this module. Rotation is handled by the
    caller pairing this digest with a numeric ``lookup_key_version``; the raw
    DON is intentionally not stored, so an old version can only be matched by
    recomputing its own candidate digest.
    """
    key = lookup_key.encode("utf-8") if isinstance(lookup_key, str) else lookup_key
    if not key:
        raise ValueError("a lookup key is required")
    message = normalized_don(devrev_work_id).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def workload_binding_hash(binding: str) -> str:
    """Hash the configured workload binding (e.g. the caller's SA identity)."""
    return sha256_hex(binding)


def principal_hash(principal: str) -> str:
    """Hash a verified caller principal so it can be correlated, not read."""
    return sha256_hex(principal)


def prompt_template_artifact(template_text: str) -> tuple[str, str]:
    """Return the static ``(prompt_template_id, prompt_template_sha256)`` pair.

    The digest pins the shipped template bytes. It is a build artifact
    identity, not a per-request value.
    """
    return PROMPT_TEMPLATE_ID, sha256_hex(template_text)


def rendered_prompt_trace_hash(rendered_prompt: str) -> str:
    """A debugging trace hash of one rendered prompt.

    **Not a semantic version.** Two functionally identical requests produce
    different values because the participant text differs, and a template
    change produces a different value for reasons unrelated to the template.
    Consumers must treat it as an opaque correlation aid only; the template
    version is :func:`prompt_template_artifact`.
    """
    return sha256_hex(rendered_prompt)


# =====================================================================
# Deployment metadata
# =====================================================================


@dataclass(frozen=True)
class DeploymentMetadata:
    """Safe runtime deployment identity. Absent fields stay ``None``."""

    revision: Optional[str] = None
    service: Optional[str] = None
    commit_sha: Optional[str] = None
    image_digest: Optional[str] = None

    @property
    def missing(self) -> list[MissingProvenance]:
        gaps: list[MissingProvenance] = []
        if self.revision is None:
            gaps.append(MissingProvenance.DEPLOYED_REVISION)
        return gaps


def deployment_metadata(env: Mapping[str, str]) -> DeploymentMetadata:
    """Read deployment identity from safe runtime variables only.

    ``K_REVISION``/``K_SERVICE`` are injected by Cloud Run. A commit SHA or
    image digest is read only from an explicitly configured variable; nothing
    is inferred, because claiming a revision that was not recorded is exactly
    the fabricated provenance this stage exists to prevent.
    """

    def _bounded(name: str) -> Optional[str]:
        raw = (env.get(name) or "").strip()
        if not raw or len(raw) > MAX_ID_LENGTH:
            return None
        return raw

    return DeploymentMetadata(
        revision=_bounded(DEPLOY_REVISION_ENV),
        service=_bounded(DEPLOY_SERVICE_ENV),
        commit_sha=_bounded(DEPLOY_COMMIT_ENV),
        image_digest=_bounded(DEPLOY_IMAGE_DIGEST_ENV),
    )


# =====================================================================
# n8n ingress signature verification
# =====================================================================


@dataclass(frozen=True)
class IngressSignatureContext:
    """The correlation context an authenticated producer request may carry.

    Every field is required. A partially supplied context is not a weaker
    verification, it is no verification: :func:`verify_ingress_context`
    returns an unverified result rather than signing whatever is present.
    """

    devrev_work_id: str
    timestamp: datetime
    request_sha256: str
    idempotency_key_sha256: str
    key_version: int
    signature: str


def canonical_ingress_bytes(context: IngressSignatureContext) -> bytes:
    """The exact bytes both sides sign.

    Newline-delimited and field-ordered so no field can be shifted into
    another (the classic length-extension-by-concatenation mistake). The
    leading version tag means a future canonicalization change cannot be
    confused with the current one.
    """
    return "\n".join(
        (
            f"v{CORRELATION_HMAC_VERSION}",
            str(int(context.timestamp.timestamp())),
            normalized_don(context.devrev_work_id),
            context.request_sha256,
            context.idempotency_key_sha256,
            str(int(context.key_version)),
        )
    ).encode("utf-8")


@dataclass(frozen=True)
class CorrelationOutcome:
    """What may be recorded about one execution's correlation, and why.

    ``trust`` is the whole point. Only a request that passed Cloud Run IAM
    *and* carried a valid, in-window, replay-protected ingress signature
    reaches :attr:`CorrelationTrust.VERIFIED_WORKLOAD`. A raw header or body
    field on its own can reach at most :attr:`CorrelationTrust.CANDIDATE`, and
    a candidate never becomes a stored link without a reviewer action.
    """

    trust: CorrelationTrust = CorrelationTrust.NONE
    status: CorrelationStatus = CorrelationStatus.UNAVAILABLE
    source: Optional[str] = None
    ticket_lookup_hmac: Optional[str] = None
    lookup_key_version: Optional[int] = None
    ingress_key_version: Optional[int] = None
    workload_binding_sha256: Optional[str] = None
    principal_sha256: Optional[str] = None
    reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.trust is CorrelationTrust.VERIFIED_WORKLOAD


def verify_ingress_context(
    context: Optional[IngressSignatureContext],
    *,
    ingress_keys: Mapping[int, bytes | str],
    lookup_key: Optional[bytes | str],
    lookup_key_version: Optional[int],
    now: datetime,
    idempotency_receipt_present: bool,
    workload_binding: Optional[str] = None,
    principal: Optional[str] = None,
    max_skew_s: int = INGRESS_SIGNATURE_MAX_SKEW_S,
) -> CorrelationOutcome:
    """Verify a producer correlation context, or explain why it is unverified.

    Verification is fail-closed and ordered so that no later check can be
    reached by a caller who failed an earlier one:

    1. a context must exist and be complete;
    2. its ``key_version`` must name a currently active ingress key — an
       unknown or retired version is an explicit failure, never a fallback to
       "try every key";
    3. its timestamp must sit inside ``±max_skew_s``;
    4. the signature must match in constant time;
    5. the existing durable idempotency receipt must be present, which is what
       actually stops a replay — a fresh timestamp and a valid signature are
       both replayable on their own.

    Only then is the lookup HMAC derived, with the *separate* lookup key. If
    that key is unavailable the outcome stays unverified: recording a
    verified trust level with no queryable reference would produce evidence
    nobody can ever find.
    """
    if context is None:
        return CorrelationOutcome(reason=REASON_NO_CONTEXT)
    if not context.signature or not _SHA256_HEX_RE.match(context.request_sha256 or ""):
        return CorrelationOutcome(reason=REASON_MISSING_SIGNATURE)
    if not _SHA256_HEX_RE.match(context.idempotency_key_sha256 or ""):
        return CorrelationOutcome(reason=REASON_MISSING_SIGNATURE)

    key = ingress_keys.get(int(context.key_version)) if context.key_version else None
    if not key:
        return CorrelationOutcome(reason=REASON_UNKNOWN_KEY_VERSION)

    if context.timestamp.tzinfo is None:
        return CorrelationOutcome(reason=REASON_STALE_TIMESTAMP)
    skew = abs((now - context.timestamp).total_seconds())
    if skew > max_skew_s:
        return CorrelationOutcome(reason=REASON_STALE_TIMESTAMP)

    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    expected = hmac.new(
        key_bytes, canonical_ingress_bytes(context), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, context.signature.strip().lower()):
        return CorrelationOutcome(reason=REASON_BAD_SIGNATURE)

    # A valid signature proves authorship, not freshness of *this* delivery.
    # The durable receipt is the replay boundary.
    if not idempotency_receipt_present:
        return CorrelationOutcome(reason=REASON_MISSING_RECEIPT)

    if not lookup_key or not lookup_key_version:
        return CorrelationOutcome(reason=REASON_LOOKUP_KEY_UNAVAILABLE)

    return CorrelationOutcome(
        trust=CorrelationTrust.VERIFIED_WORKLOAD,
        status=CorrelationStatus.LINKED,
        source=CORRELATION_SOURCE_N8N_SIGNED,
        ticket_lookup_hmac=ticket_lookup_hmac(lookup_key, context.devrev_work_id),
        lookup_key_version=int(lookup_key_version),
        ingress_key_version=int(context.key_version),
        workload_binding_sha256=(
            workload_binding_hash(workload_binding) if workload_binding else None
        ),
        principal_sha256=principal_hash(principal) if principal else None,
    )


def unverified_candidate(reason: str = REASON_MISSING_SIGNATURE) -> CorrelationOutcome:
    """The most an unauthenticated header or body field may ever produce.

    Trust is :attr:`CorrelationTrust.CANDIDATE` and status stays
    ``unavailable``: nothing is stored, nothing is queryable, and the console
    can only offer a reviewer a suggestion.
    """
    return CorrelationOutcome(
        trust=CorrelationTrust.CANDIDATE,
        status=CorrelationStatus.UNAVAILABLE,
        source=CORRELATION_SOURCE_UNVERIFIED_HEADER,
        reason=reason,
    )


def internal_job_correlation(
    job_id: str,
    *,
    lookup_key: Optional[bytes | str] = None,
    devrev_work_id: Optional[str] = None,
    lookup_key_version: Optional[int] = None,
) -> CorrelationOutcome:
    """Correlation for a ticket-handler job this service created itself.

    The job id is trusted only because the server minted it and it still
    matches the validated internal shape. Caller-supplied text that merely
    looks like an id is rejected, which is what keeps an attacker-chosen
    string out of the execution record.
    """
    validated = validated_internal_job_id(job_id)
    if validated is None:
        return CorrelationOutcome(reason=REASON_NO_CONTEXT)
    reference: Optional[str] = None
    version: Optional[int] = None
    if lookup_key and devrev_work_id and lookup_key_version:
        reference = ticket_lookup_hmac(lookup_key, devrev_work_id)
        version = int(lookup_key_version)
    return CorrelationOutcome(
        trust=CorrelationTrust.VERIFIED_WORKLOAD,
        status=CorrelationStatus.LINKED if reference else CorrelationStatus.UNAVAILABLE,
        source=CORRELATION_SOURCE_INTERNAL_JOB,
        ticket_lookup_hmac=reference,
        lookup_key_version=version,
    )


def validated_internal_job_id(job_id: Any) -> Optional[str]:
    """Return the job id only when it matches the server-minted shape."""
    if not isinstance(job_id, str):
        return None
    candidate = job_id.strip()
    return candidate if _INTERNAL_JOB_ID_RE.match(candidate) else None


# =====================================================================
# Bounded chunk references
# =====================================================================


def sanitize_chunk_refs(
    observed: Sequence[Mapping[str, Any]],
    *,
    namespace: str,
    limit: int = MAX_EVIDENCE_REFS_PER_REVIEW,
) -> tuple[list[ObservedChunkRef], bool]:
    """Project retrieval results into bounded, text-free chunk references.

    Returns ``(refs, truncated)``. Three deliberate choices:

    * ``content_sha256`` is computed here over the content that was actually
      read back at query time. The KB's stored ``content_hash`` is a truncated
      MD5 and cannot satisfy a SHA-256 contract, and re-indexing to add one is
      explicitly out of scope.
    * ``chunk_ordinal`` is the 0-based rank **within this observed result
      set**, not the KB's ``chunk_index``. That counter is 1-based and per
      chunker-run, so it is not a stable per-article position and must not be
      presented as one.
    * ``observed_vector_id`` is copied verbatim. This function never mints or
      reformats a vector id, and the reference does not claim the id is stable
      across a reindex.
    """
    refs: list[ObservedChunkRef] = []
    truncated = len(observed) > limit
    for ordinal, chunk in enumerate(observed[:limit]):
        if not isinstance(chunk, Mapping):
            truncated = True
            continue
        metadata = _mapping(chunk.get("metadata"))
        vector_id = _bounded_text(chunk.get("observed_vector_id"))
        vector_id = vector_id or _bounded_text(chunk.get("id"))
        vector_id = vector_id or _bounded_text(chunk.get("vector_id"))
        article_id = _bounded_text(chunk.get("article_id"))
        article_id = article_id or _bounded_text(metadata.get("article_id"))
        content = chunk.get("content")
        if not isinstance(content, str):
            content = metadata.get("content")
        # An already-sanitized reference carries its digest instead of the text
        # it was computed from, so re-reading one must not require the content.
        stored_digest = _hash(chunk.get("content_sha256"))
        if not vector_id or not article_id or not (isinstance(content, str) or stored_digest):
            # A reference we cannot defend is dropped, not guessed at.
            truncated = True
            continue
        # `raw_score` is preferred where it exists: the required-data path
        # overwrites `score` with a boosted value and stashes the model's own
        # similarity in `raw_score`, and auditable provenance wants the latter.
        raw = chunk.get("raw_score")
        refs.append(
            ObservedChunkRef(
                observed_vector_id=vector_id,
                article_id=article_id,
                content_sha256=(
                    stored_digest if stored_digest is not None else sha256_hex(content)
                ),
                chunk_ordinal=_reused_ordinal(chunk, ordinal),
                namespace=namespace,
                score=_safe_score(raw if raw is not None else chunk.get("score")),
            )
        )
    return refs, truncated


def _reused_ordinal(chunk: Mapping[str, Any], ordinal: int) -> int:
    """Keep an already-recorded ordinal; otherwise use the observed rank.

    Re-reading a stored reference must be idempotent, so a document that
    already carries ``chunk_ordinal`` keeps it rather than being renumbered by
    its position in a later, differently sized page.
    """
    stored = chunk.get("chunk_ordinal")
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        return ordinal
    return stored


def _bounded_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_ID_LENGTH:
        return None
    return candidate


def _safe_score(value: Any) -> Optional[float]:
    """A finite float, or nothing. NaN/inf must never reach a stored document."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


# =====================================================================
# The versioned, additive execution-log payload
# =====================================================================


@dataclass(frozen=True)
class ExecutionProvenance:
    """Everything Stage 4 adds to one execution record.

    Constructed by trusted server-side code and handed to
    :class:`~data_pipeline.execution_logger.ExecutionLogger` as an explicit
    argument. It is never scraped out of a request or response body: doing so
    is how a caller-supplied ``metadata.model`` or ``source_articles[].id``
    would smuggle participant text into a log document.
    """

    correlation: CorrelationOutcome = field(default_factory=CorrelationOutcome)
    internal_job_id: Optional[str] = None
    route: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    config_version: Optional[str] = None
    prompt_template_id: Optional[str] = None
    prompt_template_sha256: Optional[str] = None
    rendered_prompt_trace_sha256: Optional[str] = None
    deployment: DeploymentMetadata = field(default_factory=DeploymentMetadata)
    index_name: Optional[str] = None
    index_version: Optional[str] = None
    namespace: Optional[str] = None
    source_article_ids: tuple[str, ...] = ()
    observed_chunks: tuple[ObservedChunkRef, ...] = ()
    response_sha256: Optional[str] = None
    chunk_refs_truncated: bool = False

    def missing_provenance(self) -> list[MissingProvenance]:
        """The explicit gap list. Recorded, never silently filled in."""
        gaps: list[MissingProvenance] = []
        if self.index_version is None:
            gaps.append(MissingProvenance.INDEX_VERSION)
        if self.deployment.revision is None:
            gaps.append(MissingProvenance.DEPLOYED_REVISION)
        if not self.prompt_template_id or not self.prompt_template_sha256:
            gaps.append(MissingProvenance.PROMPT_TEMPLATE)
        if not self.model:
            gaps.append(MissingProvenance.MODEL)
        if not self.observed_chunks:
            gaps.append(MissingProvenance.OBSERVED_CHUNKS)
        if not self.response_sha256:
            gaps.append(MissingProvenance.RESPONSE_HASH)
        if not self.source_article_ids:
            gaps.append(MissingProvenance.SOURCE_ARTICLES)
        return gaps

    def as_document_fields(self) -> dict[str, Any]:
        """The allowlisted, additive document fragment.

        Every value is a hash, a bounded internal identifier, a closed enum
        value, or a number. There is no request body, response text, chunk
        text, participant field, prompt, token, or raw external id, and there
        is no passthrough of unknown keys.
        """
        return {
            "provenance_schema_version": EXECUTION_LOG_SCHEMA_VERSION,
            "correlation": {
                "ticket_lookup_hmac": self.correlation.ticket_lookup_hmac,
                "lookup_key_version": self.correlation.lookup_key_version,
                "ingress_key_version": self.correlation.ingress_key_version,
                "correlation_source": self.correlation.source,
                "correlation_trust": self.correlation.trust.value,
                "correlation_status": self.correlation.status.value,
                "workload_binding_sha256": self.correlation.workload_binding_sha256,
                "principal_sha256": self.correlation.principal_sha256,
                "hmac_version": CORRELATION_HMAC_VERSION,
            },
            "job": {"internal_job_id": validated_internal_job_id(self.internal_job_id)},
            "pipeline": {
                "route": self.route,
                "model": self.model,
                "provider": self.provider,
                "config_version": self.config_version,
                "prompt_template_id": self.prompt_template_id,
                "prompt_template_sha256": self.prompt_template_sha256,
                # Explicitly labelled: a trace hash is a correlation aid, and
                # a consumer must never read it as a template version.
                "rendered_prompt_trace_sha256": self.rendered_prompt_trace_sha256,
                "rendered_prompt_trace_is_not_a_version": True,
                "deployed_revision": self.deployment.revision,
                "deployed_service": self.deployment.service,
                "deployed_commit_sha": self.deployment.commit_sha,
                "deployed_image_digest": self.deployment.image_digest,
                "index_name": self.index_name,
                "index_version": self.index_version,
                "namespace": self.namespace,
            },
            "retrieval": {
                "source_article_ids": list(self.source_article_ids[:MAX_LIST_ITEMS]),
                "observed_chunks": [
                    {
                        "observed_vector_id": ref.observed_vector_id,
                        "article_id": ref.article_id,
                        "content_sha256": ref.content_sha256,
                        "chunk_ordinal": ref.chunk_ordinal,
                        "namespace": ref.namespace,
                        "score": ref.score,
                    }
                    for ref in self.observed_chunks[:MAX_LIST_ITEMS]
                ],
                "truncated": bool(self.chunk_refs_truncated),
            },
            "response_sha256": self.response_sha256,
            "missing_provenance": [gap.value for gap in self.missing_provenance()],
        }


# =====================================================================
# Reading both new and legacy execution documents
# =====================================================================


#: The keys :meth:`ExecutionProvenance.as_document_fields` produces. Used to
#: recognize a provenance fragment wherever the writer happened to put it.
_PROVENANCE_MARKERS = frozenset(
    {"provenance_schema_version", "correlation", "pipeline", "retrieval"}
)


def provenance_root(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """Locate the provenance fragment inside a stored execution document.

    :class:`~data_pipeline.execution_logger.ExecutionLogger` nests it under
    ``provenance``, which keeps the additive schema out of the pre-existing
    top-level keys. A flat fragment is also accepted so a caller that already
    holds ``as_document_fields()`` output can parse it directly without first
    wrapping it.
    """
    nested = doc.get("provenance")
    if isinstance(nested, Mapping) and (_PROVENANCE_MARKERS & nested.keys()):
        return nested
    if _PROVENANCE_MARKERS & doc.keys():
        return doc
    return {}


def execution_schema_version(doc: Mapping[str, Any]) -> int:
    """Return the document's provenance schema version.

    A legacy document written before Stage 4 has neither a provenance fragment
    nor a ``schema_version``, so it parses as
    :data:`LEGACY_EXECUTION_LOG_SCHEMA_VERSION` (0) rather than being treated
    as a corrupt v1 record. A malformed version reads as v0 for the same
    reason: an unreadable claim of v1 is not evidence of v1.
    """
    root = provenance_root(doc)
    for candidate in (
        root.get("provenance_schema_version"),
        doc.get("provenance_schema_version"),
        doc.get("schema_version"),
    ):
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
            continue
        return candidate
    return LEGACY_EXECUTION_LOG_SCHEMA_VERSION


def parse_execution_document(
    doc: Mapping[str, Any],
    *,
    source_collection: EvidenceSourceCollection,
    evidence_reference: str,
) -> RagEvidenceRecord:
    """Project one stored execution document into a sanitized evidence record.

    Handles v0 (legacy, no provenance) and v1 identically at the call site: a
    legacy document simply carries :attr:`MissingProvenance.LEGACY_SCHEMA` and
    empty provenance, which is how the console renders a historical gap as a
    gap.
    """
    version = execution_schema_version(doc)
    root = provenance_root(doc)
    correlation = _mapping(root.get("correlation"))
    pipeline = _mapping(root.get("pipeline"))
    retrieval = _mapping(root.get("retrieval"))

    trust = _enum(CorrelationTrust, correlation.get("correlation_trust"), CorrelationTrust.NONE)
    stored_chunks = [
        chunk
        for chunk in (retrieval.get("observed_chunks") or [])
        if isinstance(chunk, Mapping)
    ]
    chunks, _chunks_truncated = sanitize_chunk_refs(
        stored_chunks,
        namespace=_text(pipeline.get("namespace")) or "unknown",
        limit=MAX_LIST_ITEMS,
    )

    provenance = RagProvenance(
        correlation_status=_enum(
            CorrelationStatus,
            correlation.get("correlation_status"),
            CorrelationStatus.UNAVAILABLE,
        ),
        correlation_trust=trust,
        correlation_source=_text(correlation.get("correlation_source"), 80),
        missing_provenance=version == LEGACY_EXECUTION_LOG_SCHEMA_VERSION
        or bool(root.get("missing_provenance")),
        index_name=_text(pipeline.get("index_name")),
        index_version=_text(pipeline.get("index_version")),
        namespace=_text(pipeline.get("namespace")),
        deployed_revision=_text(pipeline.get("deployed_revision")),
        prompt_template_id=_text(pipeline.get("prompt_template_id")),
        prompt_template_sha256=_hash(pipeline.get("prompt_template_sha256")),
        response_sha256=_hash(root.get("response_sha256")),
        observed_chunks=chunks,
    )

    missing = [
        gap
        for gap in (
            _enum(MissingProvenance, value, None)
            for value in (root.get("missing_provenance") or [])
        )
        if gap is not None
    ]
    if version == LEGACY_EXECUTION_LOG_SCHEMA_VERSION:
        missing.append(MissingProvenance.LEGACY_SCHEMA)

    return RagEvidenceRecord(
        evidence_reference=evidence_reference,
        evidence_digest=sha256_hex(f"{source_collection.value}:{evidence_reference}"),
        source_collection=source_collection,
        schema_version=version,
        occurred_at=_aware(doc.get("timestamp")),
        endpoint=_text(doc.get("endpoint"), 80),
        correlation_source=provenance.correlation_source,
        correlation_trust=trust,
        lookup_key_version=_positive_int(correlation.get("lookup_key_version")),
        ingress_key_version=_positive_int(correlation.get("ingress_key_version")),
        internal_job_id=validated_internal_job_id(_mapping(root.get("job")).get("internal_job_id")),
        request_id_hash=_text(doc.get("request_id_hash")),
        principal_hash=_hash(correlation.get("principal_sha256")),
        model=_text(pipeline.get("model")),
        provider=_text(pipeline.get("provider"), 80),
        route=_text(pipeline.get("route"), 80),
        config_version=_text(pipeline.get("config_version")),
        rendered_prompt_trace_sha256=_hash(pipeline.get("rendered_prompt_trace_sha256")),
        deployed_commit_sha=_text(pipeline.get("deployed_commit_sha")),
        deployed_image_digest=_text(pipeline.get("deployed_image_digest")),
        provenance=provenance,
        source_article_ids=[
            article
            for article in (
                _bounded_text(value) for value in (retrieval.get("source_article_ids") or [])
            )
            if article is not None
        ][:MAX_LIST_ITEMS],
        duration_ms=_safe_score(doc.get("duration_ms")),
        failed=doc.get("failed") if isinstance(doc.get("failed"), bool) else None,
        missing=missing[:MAX_LIST_ITEMS],
    )


def record_content_digest(record: RagEvidenceRecord) -> str:
    """Digest one evidence record's sanitized *content*.

    Excludes ``evidence_digest`` itself, so the value is well defined, and is
    computed over a sorted, canonical rendering so field order cannot change
    it. This is what makes a manual-link candidate revalidatable: if the
    underlying record changes at all, the digest changes and a token minted
    against the old record no longer matches the current broker result.
    """
    payload = record.model_dump(mode="json", exclude={"evidence_digest"})
    return sha256_hex(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    )


def envelope_result_digest(references: Iterable[str]) -> str:
    """A stable digest of the exact record set the broker returned.

    The console binds a manual-link candidate token to this value, so a
    reviewer can only link evidence that was still present in the broker
    result they were actually shown.
    """
    return sha256_hex("\n".join(sorted(references)))


def bounded_key_versions(
    versions: Iterable[Any], *, limit: int = MAX_BROKER_KEY_VERSIONS
) -> list[int]:
    """Normalize an active-keyring version list into a bounded, sorted set.

    Fan-out is capped because the broker derives one candidate HMAC per active
    version and issues one indexed query per candidate; an unbounded keyring
    would turn a single lookup into an unbounded read of ``(default)``.
    """
    seen: set[int] = set()
    for value in versions:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            continue
        seen.add(value)
    return sorted(seen)[:limit]


# =====================================================================
# Small typed readers
# =====================================================================


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = MAX_ID_LENGTH) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate[:limit] if candidate else None


def _hash(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _SHA256_HEX_RE.match(candidate) else None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _enum(enum_cls: Any, value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default
