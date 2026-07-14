"""
Modelos del job durable de tickets (Task 3 del plan de remediación).

El documento de job vive en un repositorio compartido (Firestore en
producción, in-memory en tests/dev) para que ``202 + polling`` sobreviva a
múltiples instancias, restarts y despliegues de Cloud Run. Los estados y
``next_action`` son enums CERRADOS: n8n nunca debe interpretar strings
arbitrarios.

Reglas de datos:
- Nunca se almacena la API key ni la idempotency key raw (sólo hashes).
- ``public_result`` se minimiza (sin ``used_chunks.content`` completos).
- ``request_payload`` contiene texto del ticket (PII): su retención la
  gobierna ``expires_at`` (TTL de Firestore, ver runbook).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "2.0"

# Retención por defecto: debe superar el máximo retry/poll de n8n con margen.
DEFAULT_RETENTION_S = 24 * 3600


class TicketJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({
    TicketJobState.SUCCEEDED,
    TicketJobState.PARTIAL,
    TicketJobState.FAILED,
    TicketJobState.TIMEOUT,
    TicketJobState.CANCELLED,
})

# QUEUED←RUNNING permite re-encolar un intento interrumpido (retry de Cloud
# Tasks tras crash). Un estado terminal no admite ninguna transición.
VALID_TRANSITIONS: Dict[TicketJobState, frozenset] = {
    TicketJobState.QUEUED: frozenset({
        TicketJobState.RUNNING, TicketJobState.CANCELLED, TicketJobState.FAILED,
    }),
    TicketJobState.RUNNING: frozenset({
        TicketJobState.QUEUED, TicketJobState.SUCCEEDED, TicketJobState.PARTIAL,
        TicketJobState.FAILED, TicketJobState.TIMEOUT, TicketJobState.CANCELLED,
    }),
    TicketJobState.SUCCEEDED: frozenset(),
    TicketJobState.PARTIAL: frozenset(),
    TicketJobState.FAILED: frozenset(),
    TicketJobState.TIMEOUT: frozenset(),
    TicketJobState.CANCELLED: frozenset(),
}


class NextAction(str, Enum):
    SEND_PARTICIPANT_REPLY = "send_participant_reply"
    POLL = "poll"
    USE_LEGACY = "use_legacy"
    USE_LEGACY_OR_HUMAN = "use_legacy_or_human"
    HUMAN_REVIEW = "human_review"
    RETRY = "retry"


class CreateOrGetOutcome(str, Enum):
    CREATED = "created"
    REPLAYED = "replayed"
    CONFLICT = "conflict"


# Códigos de error públicos machine-readable (invariante 6: un fallo técnico
# nunca se disfraza de needs_more_info).
class PublicErrorCode(str, Enum):
    INQUIRY_TIMEOUT = "INQUIRY_TIMEOUT"
    TOTAL_JOB_TIMEOUT = "TOTAL_JOB_TIMEOUT"
    FORUSBOTS_TIMEOUT = "FORUSBOTS_TIMEOUT"
    FORUSBOTS_FAILED = "FORUSBOTS_FAILED"
    FORUSBOTS_NEEDS_RECONCILIATION = "FORUSBOTS_NEEDS_RECONCILIATION"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_FAILURE = "LLM_FAILURE"
    PINECONE_TRANSIENT_FAILURE = "PINECONE_TRANSIENT_FAILURE"
    PLAN_SCRAPE_FAILED = "PLAN_SCRAPE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    WORKER_CANCELLED = "WORKER_CANCELLED"
    UNPROCESSED_INQUIRIES = "UNPROCESSED_INQUIRIES"


class TicketJobRecord(BaseModel):
    """Documento durable de un ticket job (colección ``ticket_jobs``)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    principal_id: str
    tenant_id: Optional[str] = None
    ticket_id: Optional[str] = None

    idempotency_key_hash: Optional[str] = None
    request_fingerprint: str
    schema_version: str = SCHEMA_VERSION
    api_version: str = "v1"

    state: TicketJobState = TicketJobState.QUEUED
    next_action: NextAction = NextAction.POLL
    attempt: int = 0
    current_step: Optional[str] = None
    mode: Optional[str] = None

    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Duración congelada al llegar a terminal (invariante HT-26).
    elapsed_s: Optional[float] = None

    total_inquiries: Optional[int] = None
    processed_inquiries: int = 0
    unprocessed_inquiries: int = 0

    per_inquiry_status: List[Dict[str, Any]] = Field(default_factory=list)
    public_result: Optional[Dict[str, Any]] = None
    private_diagnostics_ref: Optional[str] = None
    forusbots_job_ids: List[str] = Field(default_factory=list)

    public_error_code: Optional[str] = None
    retryable: Optional[bool] = None
    trace_id: Optional[str] = None

    enqueue_state: str = "pending"          # pending | enqueued
    task_name: Optional[str] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None

    # Fencing de lease (Tarea 6 Paso 4a): cada claim incrementa lease_epoch;
    # toda escritura condicional incluye el epoch y un intento viejo que
    # despierta después de perder el lease queda fenced.
    lease_epoch: int = 0
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None

    # Outbox por generaciones (Tarea 7 Paso 3): los nombres de task son
    # ticket-{job_id}-g{generation}; una tombstone fuerza generación nueva.
    enqueue_generation: int = 0

    # Deadline ABSOLUTO del job (Tarea 7 Paso 1): accepted+2400s; worker,
    # reconciliador y GET terminalizan por CAS después de vencido.
    job_deadline_at: Optional[datetime] = None

    # Liberación exactamente-una-vez del slot de cuota (Tarea 5 Paso 2).
    active_slot_released: bool = False

    # Lock de recuperación del reconciliador (Tarea 7 Paso 5) — separado del
    # lease de ejecución del worker.
    recovery_lock_owner: Optional[str] = None
    recovery_lock_expires_at: Optional[datetime] = None

    # Plan de ejecución persistido UNA vez antes de efectos externos
    # (Tarea 6 Paso 3): inquiries extraídas/normalizadas, clasificaciones,
    # decisiones de gating y conteos. Un retry lo reutiliza; nunca re-extrae.
    execution_plan: Optional[Dict[str, Any]] = None

    # Payload necesario para que el worker ejecute (contiene PII; retención
    # gobernada por expires_at). Nunca incluye la API key ni la idem key raw.
    request_payload: Optional[Dict[str, Any]] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def fingerprint_request(payload: Dict[str, Any]) -> str:
    """SHA-256 del JSON canónico del request, excluyendo campos volátiles.

    La idempotency key no forma parte de la identidad del payload (viaja en
    el header y en el índice idempotente); incluirla haría imposible detectar
    'misma key, payload distinto'.
    """
    clean = {k: v for k, v in payload.items() if k != "idempotency_key"}
    return hashlib.sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def hash_idempotency_key(principal_id: str, idempotency_key: str,
                         api_version: str = "v1") -> str:
    """Scope del índice idempotente: (principal, key, api_version). El valor
    raw de la key nunca se persiste."""
    raw = f"{principal_id}|{api_version}|{idempotency_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_job_record(
    *,
    principal_id: str,
    request_fingerprint: str,
    total_inquiries: Optional[int] = None,
    retention_s: int = DEFAULT_RETENTION_S,
    **overrides: Any,
) -> TicketJobRecord:
    now = utcnow()
    from datetime import timedelta
    base: Dict[str, Any] = dict(
        job_id=uuid.uuid4().hex,
        principal_id=principal_id,
        request_fingerprint=request_fingerprint,
        state=TicketJobState.QUEUED,
        next_action=NextAction.POLL,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=retention_s),
        total_inquiries=total_inquiries,
    )
    base.update(overrides)
    return TicketJobRecord(**base)
