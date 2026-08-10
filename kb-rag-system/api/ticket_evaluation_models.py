"""Bounded wire contract for ticket-associated RAG execution evidence.

The event contains explicit, inspectable system rationale.  It does not model
or imply provider hidden chain-of-thought.  Instances are safe to serialize to
Firestore and to deliver to the private ticket-evaluation ingestion service.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from data_pipeline.durable_document import validate_durable_document


SCHEMA_VERSION = "1.0"
MAX_EVENT_SIZE_BYTES = 400 * 1024
MAX_EVENT_DEPTH = 12

_SENSITIVE_KEYS = frozenset({
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "devrev_pat",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secrets",
    "serverless_authorization",
    "token",
    "x_api_key",
    "x_forus_workload_authorization",
})

_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_cookie",
    "_credential",
    "_password",
    "_pat",
    "_private_key",
    "_secret",
    "_token",
)


class EvaluationRoute(str, Enum):
    KNOWLEDGE_QUESTION = "knowledge_question"
    GENERATE_RESPONSE = "generate_response"


class EvaluationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClassificationRationale(_StrictModel):
    route: EvaluationRoute
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=8_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _safe_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(value)
        return value


class SourceReference(_StrictModel):
    article_id: Optional[str] = Field(default=None, max_length=512)
    article_title: Optional[str] = Field(default=None, max_length=1_000)
    title: Optional[str] = Field(default=None, max_length=1_000)
    url: Optional[str] = Field(default=None, max_length=2_048)
    chunk_types_used: Optional[str] = Field(default=None, max_length=1_000)
    relevance: Optional[str] = Field(default=None, max_length=2_000)
    used_info: Optional[bool] = None
    max_score: Optional[float] = None

    @model_validator(mode="after")
    def _has_identity(self) -> "SourceReference":
        if not any((self.article_id, self.article_title, self.title, self.url)):
            raise ValueError("source reference requires an identity")
        return self


class ChunkEvidence(_StrictModel):
    chunk_id: str = Field(min_length=1, max_length=512)
    source_id: Optional[str] = Field(default=None, max_length=512)
    article_id: Optional[str] = Field(default=None, max_length=512)
    article_title: Optional[str] = Field(default=None, max_length=1_000)
    chunk_type: Optional[str] = Field(default=None, max_length=128)
    chunk_tier: Optional[str] = Field(default=None, max_length=128)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview: str = Field(default="", max_length=2_000)
    score: Optional[float] = None


class EvaluationError(_StrictModel):
    code: str = Field(min_length=1, max_length=128)
    retryable: bool = False


class TicketEvaluationEvent(_StrictModel):
    """One immutable, idempotently-addressed RAG execution."""

    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^1\.0$")
    execution_id: str = Field(min_length=3, max_length=160)
    # ``execution_id`` remains the route/storage key for compatibility.  New
    # producers make it invocation-scoped; legacy ``job:index`` events are
    # still readable and receive the same value through this explicit alias.
    invocation_id: Optional[str] = Field(default=None, min_length=3,
                                         max_length=160)
    job_id: str = Field(min_length=1, max_length=128,
                        pattern=r"^[A-Za-z0-9_-]+$")
    inquiry_index: int = Field(ge=0, le=99)
    attempt: Optional[int] = Field(default=None, ge=1, le=2_147_483_647)
    lease_epoch: Optional[int] = Field(default=None, ge=1,
                                       le=2_147_483_647)
    ticket_id: str = Field(min_length=1, max_length=256)
    tenant_id: Optional[str] = Field(default=None, max_length=256)
    route: EvaluationRoute
    status: EvaluationStatus
    occurred_at: AwareDatetime
    inquiry: str = Field(min_length=1, max_length=20_000)
    topic: str = Field(min_length=1, max_length=256)
    classification: ClassificationRationale
    answer: Optional[str] = Field(default=None, max_length=100_000)
    structured_response: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceReference] = Field(default_factory=list, max_length=50)
    chunks: list[ChunkEvidence] = Field(default_factory=list, max_length=20)
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)
    error: Optional[EvaluationError] = None

    @field_validator(
        "structured_response",
        "diagnostics",
        "retrieval_metadata",
        "correlation",
    )
    @classmethod
    def _safe_map(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(value)
        return value

    @model_validator(mode="after")
    def _consistent_identity_and_route(self) -> "TicketEvaluationEvent":
        legacy_id = f"{self.job_id}:{self.inquiry_index}"
        if self.attempt is None and self.lease_epoch is None:
            expected = legacy_id
        elif self.attempt is not None and self.lease_epoch is not None:
            expected = rag_invocation_id(
                self.job_id,
                self.inquiry_index,
                lease_epoch=self.lease_epoch,
                attempt=self.attempt,
            )
        else:
            raise ValueError(
                "attempt and lease_epoch must be present together"
            )
        if self.execution_id != expected:
            raise ValueError(
                "execution_id must use the deterministic invocation identity"
            )
        if self.invocation_id is None:
            self.invocation_id = self.execution_id
        elif self.invocation_id != self.execution_id:
            raise ValueError("invocation_id must equal execution_id")
        if self.classification.route != self.route:
            raise ValueError("classification route must match execution route")
        document = self.model_dump(mode="python")
        validate_durable_document(
            document,
            max_depth=MAX_EVENT_DEPTH,
            max_size_bytes=MAX_EVENT_SIZE_BYTES,
        )
        return self

    @property
    def event_id(self) -> str:
        """Compatibility alias for systems that call the id an event id."""
        return self.execution_id

    def canonical_json(self) -> str:
        """Stable wire representation used for idempotency/conflict digests."""
        # Preserve the exact pre-journal digest for already persisted
        # ``job:index`` outbox events.  New invocation-scoped events bind the
        # three additive identity fields into their immutable digest.
        excluded = (
            {"invocation_id", "attempt", "lease_epoch"}
            if self.attempt is None and self.lease_epoch is None
            else None
        )
        return json.dumps(
            self.model_dump(mode="json", exclude=excluded),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_document(self) -> dict[str, Any]:
        """Firestore representation with native (TTL-safe) datetimes."""
        excluded = (
            {"invocation_id", "attempt", "lease_epoch"}
            if self.attempt is None and self.lease_epoch is None
            else None
        )
        return self.model_dump(mode="python", exclude=excluded)


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = _normalized_key(str(raw_key))
            if normalized in _SENSITIVE_KEYS or normalized.endswith(
                _SENSITIVE_KEY_SUFFIXES
            ):
                raise ValueError("event contains a sensitive key")
            _reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child)


def _normalized_key(value: str) -> str:
    """Normalize header/vendor/camelCase spellings before secret checks."""
    with_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", with_boundaries).strip("_").lower()


def chunk_evidence_from_result(raw: Mapping[str, Any]) -> ChunkEvidence:
    """Reduce a retrieved chunk to a bounded preview plus content digest."""
    content = str(raw.get("content") or raw.get("content_preview") or "")
    preview = str(raw.get("content_preview") or content[:2_000])[:2_000]
    return ChunkEvidence(
        chunk_id=str(raw.get("chunk_id") or ""),
        source_id=_optional_text(raw.get("source_id")),
        article_id=_optional_text(raw.get("article_id")),
        article_title=_optional_text(raw.get("article_title")),
        chunk_type=_optional_text(raw.get("chunk_type")),
        chunk_tier=_optional_text(raw.get("chunk_tier")),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        preview=preview,
        score=_optional_float(raw.get("score")),
    )


def rag_invocation_id(
    job_id: str,
    inquiry_index: int,
    *,
    lease_epoch: int,
    attempt: int,
) -> str:
    """Return a deterministic, URL-compatible identity for one real call."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("job_id is not usable for an invocation identity")
    if not 0 <= inquiry_index <= 99:
        raise ValueError("inquiry_index is not usable")
    for name, value in (("lease_epoch", lease_epoch), ("attempt", attempt)):
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 1 <= value <= 2_147_483_647:
            raise ValueError(f"{name} is not usable")
    value = f"{job_id}-e{lease_epoch}-a{attempt}:{inquiry_index}"
    if len(value) > 160:
        raise ValueError("invocation identity is too long")
    return value


def build_ticket_evaluation_seed(
    record: Any,
    index: int,
    *,
    route: str,
    invocation_id: str,
    lease_epoch: int,
    attempt: int,
) -> dict[str, Any]:
    """Capture enough immutable context to audit an abandoned invocation.

    The seed deliberately contains no answer.  If the process disappears
    after the provider call, recovery can therefore publish an honest failed
    attempt without reconstructing or inventing provider output.
    """
    if route not in {item.value for item in EvaluationRoute}:
        raise ValueError("invocation seed requires a RAG route")
    expected_id = rag_invocation_id(
        str(getattr(record, "job_id")),
        index,
        lease_epoch=lease_epoch,
        attempt=attempt,
    )
    if invocation_id != expected_id:
        raise ValueError("invocation seed identity does not match")

    ticket_id = _optional_text(getattr(record, "ticket_id", None))
    if ticket_id is None:
        request_payload = getattr(record, "request_payload", None) or {}
        ticket_payload = request_payload.get("ticket") or {}
        if isinstance(ticket_payload, Mapping):
            ticket_id = _optional_text(ticket_payload.get("ticket_id"))
    if ticket_id is None:
        raise ValueError("ticket-associated invocation needs a ticket id")

    plan = getattr(record, "execution_plan", None) or {}
    classifications = plan.get("classifications") or []
    classification_raw = (
        classifications[index]
        if isinstance(classifications, list) and index < len(classifications)
        and isinstance(classifications[index], Mapping)
        else {}
    )
    planned_inquiries = plan.get("inquiries") or []
    planned_inquiry = (
        planned_inquiries[index]
        if isinstance(planned_inquiries, list) and index < len(planned_inquiries)
        and isinstance(planned_inquiries[index], Mapping)
        else {}
    )
    metadata = classification_raw.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    seed: dict[str, Any] = {
        "invocation_id": invocation_id,
        "job_id": str(getattr(record, "job_id")),
        "inquiry_index": index,
        "attempt": attempt,
        "lease_epoch": lease_epoch,
        "ticket_id": ticket_id,
        "tenant_id": _optional_text(getattr(record, "tenant_id", None)),
        "route": route,
        "inquiry": (
            _optional_text(planned_inquiry.get("inquiry"))
            or f"Ticket inquiry {index + 1}"
        ),
        "topic": _optional_text(planned_inquiry.get("topic")) or "general",
        "classification": {
            "route": route,
            "confidence": _optional_float(classification_raw.get("confidence")),
            "reasoning": (
                _optional_text(classification_raw.get("reasoning"))
                or "Classifier rationale unavailable."
            ),
            "metadata": metadata,
        },
        "correlation": {
            "request_fingerprint": getattr(record, "request_fingerprint", ""),
        },
    }
    for field in ("trace_id", "principal_id"):
        value = _optional_text(getattr(record, field, None))
        if value is not None:
            seed["correlation"][field] = value
    _reject_sensitive_keys(seed)
    validate_durable_document(seed, max_depth=MAX_EVENT_DEPTH)
    return seed


def build_ticket_evaluation_event(
    record: Any,
    index: int,
    entry: Mapping[str, Any],
    *,
    observed_at: datetime,
    invocation_id: Optional[str] = None,
    attempt: Optional[int] = None,
    lease_epoch: Optional[int] = None,
    event_seed: Optional[Mapping[str, Any]] = None,
) -> Optional[TicketEvaluationEvent]:
    """Build an audit event from a durable inquiry checkpoint.

    Returning ``None`` is intentional for non-RAG routes, unprocessed work,
    or jobs without a DevRev ticket identity.  The repository calls this
    inside the same transaction that stores the checkpoint.
    """
    route = entry.get("route")
    if route not in {route.value for route in EvaluationRoute}:
        return None
    execution_status = entry.get("execution_status")
    status_mapping = {
        "succeeded": EvaluationStatus.PARTIAL
        if entry.get("degraded") is True else EvaluationStatus.SUCCEEDED,
        "partial": EvaluationStatus.PARTIAL,
        "failed": EvaluationStatus.FAILED,
        "timeout": EvaluationStatus.TIMEOUT,
    }
    status = status_mapping.get(str(execution_status))
    if status is None:
        return None

    ticket_id = _optional_text(
        event_seed.get("ticket_id") if event_seed is not None else None
    ) or _optional_text(getattr(record, "ticket_id", None))
    if ticket_id is None and event_seed is None:
        request_payload = getattr(record, "request_payload", None) or {}
        ticket_payload = request_payload.get("ticket") or {}
        if isinstance(ticket_payload, Mapping):
            ticket_id = _optional_text(ticket_payload.get("ticket_id"))
    if ticket_id is None:
        return None

    plan = getattr(record, "execution_plan", None) or {}
    classifications = plan.get("classifications") or []
    seed_classification = (
        event_seed.get("classification") if event_seed is not None else None
    )
    classification_raw = (
        seed_classification
        if isinstance(seed_classification, Mapping)
        else classifications[index]
        if isinstance(classifications, list) and index < len(classifications)
        and isinstance(classifications[index], Mapping)
        else {}
    )
    reasoning = _optional_text(classification_raw.get("reasoning")) or (
        "Classifier rationale unavailable."
    )
    confidence = _optional_float(classification_raw.get("confidence"))
    classification_metadata = classification_raw.get("metadata")
    if not isinstance(classification_metadata, dict):
        classification_metadata = {}

    result = entry.get("result")
    if not isinstance(result, Mapping):
        result = {}
    planned_inquiries = plan.get("inquiries") or []
    planned_inquiry = (
        planned_inquiries[index]
        if isinstance(planned_inquiries, list) and index < len(planned_inquiries)
        and isinstance(planned_inquiries[index], Mapping)
        else {}
    )
    inquiry = (
        _optional_text(result.get("inquiry"))
        or _optional_text(
            event_seed.get("inquiry") if event_seed is not None else None
        )
        or _optional_text(planned_inquiry.get("inquiry"))
        or f"Ticket inquiry {index + 1}"
    )
    topic = (
        _optional_text(result.get("topic"))
        or _optional_text(
            event_seed.get("topic") if event_seed is not None else None
        )
        or _optional_text(planned_inquiry.get("topic"))
        or "general"
    )

    result_key = (
        "knowledge_answer"
        if route == EvaluationRoute.KNOWLEDGE_QUESTION.value
        else "generate_response"
    )
    response_block = result.get(result_key)
    if not isinstance(response_block, Mapping):
        response_block = {}
    structured_response = {result_key: dict(response_block)}
    answer = _answer_from_response(route, response_block)

    source_records: list[SourceReference] = []
    raw_sources = response_block.get("source_articles") or []
    if isinstance(raw_sources, list):
        for raw_source in raw_sources[:50]:
            if not isinstance(raw_source, Mapping):
                continue
            allowed = {
                key: raw_source.get(key)
                for key in SourceReference.model_fields
                if raw_source.get(key) is not None
            }
            try:
                source_records.append(SourceReference.model_validate(allowed))
            except ValueError:
                continue

    chunk_records: list[ChunkEvidence] = []
    evidence = entry.get("evaluation_evidence") or {}
    raw_chunks = evidence.get("chunks") if isinstance(evidence, Mapping) else []
    if isinstance(raw_chunks, list):
        for raw_chunk in raw_chunks[:20]:
            if not isinstance(raw_chunk, Mapping):
                continue
            allowed = {
                key: raw_chunk.get(key)
                for key in ChunkEvidence.model_fields
                if raw_chunk.get(key) is not None
            }
            try:
                chunk_records.append(ChunkEvidence.model_validate(allowed))
            except ValueError:
                continue

    result_diagnostics = result.get("diagnostics")
    diagnostics = (
        dict(result_diagnostics) if isinstance(result_diagnostics, dict) else {}
    )
    checkpoint_diagnostics = entry.get("diagnostics")
    if isinstance(checkpoint_diagnostics, Mapping):
        diagnostics.update(dict(checkpoint_diagnostics))
    checkpoint_state = {
        field: entry[field]
        for field in (
            "degraded",
            "execution_status",
            "manual_reconciliation_required",
            "participant_reply_safe",
            "scrape_status",
        )
        if entry.get(field) is not None
    }
    if checkpoint_state:
        diagnostics["checkpoint"] = checkpoint_state
    retrieval_metadata = response_block.get("metadata")
    if not isinstance(retrieval_metadata, dict):
        retrieval_metadata = {}
    seed_correlation = (
        event_seed.get("correlation") if event_seed is not None else None
    )
    correlation: dict[str, Any] = (
        dict(seed_correlation)
        if isinstance(seed_correlation, Mapping)
        else {
            "request_fingerprint": getattr(record, "request_fingerprint", ""),
        }
    )
    for field in ("trace_id", "principal_id"):
        value = _optional_text(getattr(record, field, None))
        if value is not None:
            correlation[field] = value
    external_ids = getattr(record, "forusbots_job_ids", None)
    if isinstance(external_ids, list) and external_ids:
        correlation["forusbots_job_ids"] = [str(item)[:512]
                                              for item in external_ids[:20]]

    error_raw = entry.get("error")
    error = None
    if isinstance(error_raw, Mapping) and _optional_text(error_raw.get("code")):
        error = EvaluationError(
            code=str(error_raw["code"]),
            retryable=error_raw.get("retryable") is True,
        )

    job_id = str(
        event_seed.get("job_id")
        if event_seed is not None else getattr(record, "job_id")
    )
    resolved_invocation_id = invocation_id or f"{job_id}:{index}"
    resolved_attempt = (
        attempt
        if attempt is not None else event_seed.get("attempt")
        if event_seed is not None else None
    )
    resolved_lease_epoch = (
        lease_epoch
        if lease_epoch is not None else event_seed.get("lease_epoch")
        if event_seed is not None else None
    )
    return TicketEvaluationEvent(
        execution_id=resolved_invocation_id,
        invocation_id=resolved_invocation_id,
        job_id=job_id,
        inquiry_index=index,
        attempt=resolved_attempt,
        lease_epoch=resolved_lease_epoch,
        ticket_id=ticket_id,
        tenant_id=(
            _optional_text(event_seed.get("tenant_id"))
            if event_seed is not None
            else _optional_text(getattr(record, "tenant_id", None))
        ),
        route=route,
        status=status,
        occurred_at=observed_at,
        inquiry=inquiry,
        topic=topic,
        classification=ClassificationRationale(
            route=route,
            confidence=confidence,
            reasoning=reasoning,
            metadata=classification_metadata,
        ),
        answer=answer,
        structured_response=structured_response,
        diagnostics=diagnostics,
        sources=source_records,
        chunks=chunk_records,
        retrieval_metadata=retrieval_metadata,
        correlation=correlation,
        error=error,
    )


def _answer_from_response(route: object, block: Mapping[str, Any]) -> Optional[str]:
    if route == EvaluationRoute.KNOWLEDGE_QUESTION.value:
        return _optional_text(block.get("answer"))
    response = block.get("response")
    if not isinstance(response, Mapping):
        return _optional_text(response)
    participant_response = response.get("response_to_participant")
    if participant_response is None:
        return None
    if isinstance(participant_response, str):
        return participant_response.strip() or None
    return json.dumps(
        participant_response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )[:100_000]


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ChunkEvidence",
    "ClassificationRationale",
    "EvaluationError",
    "EvaluationRoute",
    "EvaluationStatus",
    "SCHEMA_VERSION",
    "SourceReference",
    "TicketEvaluationEvent",
    "build_ticket_evaluation_event",
    "build_ticket_evaluation_seed",
    "chunk_evidence_from_result",
    "rag_invocation_id",
]
