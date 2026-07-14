"""
Repositorio durable de ticket jobs + idempotencia (plan de finalización,
Tarea 5). Toda la lógica (reserva idempotente, cuotas atómicas, máquina de
estados, checkpoints, claims con fencing) vive AQUÍ dentro de una transacción
del backend; los backends sólo aportan la primitiva transaccional.

Separación control/payload (privacidad + retención):

- ``ticket_jobs/{job_id}``           control/tombstone SIN PII (estado, hashes,
  timestamps, lease/outbox/cuota). No terminal: sin TTL; terminal: retiene
  ``TICKET_IDEMPOTENCY_RETENTION_DAYS`` junto con su receipt para que GET
  devuelva 410 durante todo el horizonte de replay.
- ``ticket_job_payloads/{job_id}``   request, execution plan, checkpoints y
  resultado (PII mínima); ``expires_at`` nativo a 24h como fail-safe.
- ``ticket_idempotency_receipts/{principal_hash:key_hash}``  fingerprint +
  job_id, sin PII, TTL = retención (default 90d, nunca menor al horizonte
  acordado en Tarea 1).
- ``ticket_active_counters/{principal_hash}``  ``active_jobs`` SIN TTL
  mientras sea positivo; se elimina sólo al volver atómicamente a cero.
- ``ticket_rate_windows/{principal_hash:window}``  ventana fija durable con
  TTL posterior al horizonte de retry/replay.

Los timestamps se escriben SIEMPRE como ``datetime`` nativos (TTL de
Firestore); ``.isoformat()`` está prohibido en este módulo.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from data_pipeline.ticket_job_models import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    CreateOrGetOutcome,
    TicketJobRecord,
    TicketJobState,
    hash_idempotency_key,
    utcnow,
)

JOBS_COLLECTION = "ticket_jobs"
PAYLOADS_COLLECTION = "ticket_job_payloads"
RECEIPTS_COLLECTION = "ticket_idempotency_receipts"
COUNTERS_COLLECTION = "ticket_active_counters"
RATE_WINDOWS_COLLECTION = "ticket_rate_windows"

# Compat: nombre histórico usado por tests/tooling para localizar el índice
# idempotente. La colección real es RECEIPTS_COLLECTION.
IDEM_COLLECTION = RECEIPTS_COLLECTION

# Campos del record que viven en el documento de PAYLOAD (PII / volumen).
_PAYLOAD_FIELDS = frozenset({
    "request_payload", "execution_plan", "per_inquiry_status",
    "public_result", "private_diagnostics_ref",
})

_RATE_WINDOW_S = 60
_RATE_WINDOW_TTL = timedelta(hours=48)


class TicketJobError(Exception):
    pass


class JobNotFound(TicketJobError):
    pass


class InvalidStateTransition(TicketJobError):
    def __init__(self, current: TicketJobState, requested: TicketJobState):
        super().__init__(f"transición inválida: {current.value} → {requested.value}")
        self.current = current
        self.requested = requested


class QuotaExceeded(TicketJobError):
    """Cap de jobs activos por principal alcanzado (429 en el productor)."""

    def __init__(self, outstanding: int):
        super().__init__(f"jobs activos: {outstanding}")
        self.outstanding = outstanding


class RateWindowExceeded(TicketJobError):
    """Ventana de tasa durable agotada (429 con Retry-After)."""

    def __init__(self, retry_after_s: int):
        super().__init__(f"rate window agotada; retry en {retry_after_s}s")
        self.retry_after_s = retry_after_s


class StaleLeaseEpoch(TicketJobError):
    """Un worker con un lease_epoch viejo intentó escribir: queda fenced."""


def principal_hash(principal_id: str) -> str:
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:32]


class _TxnView:
    """Vista de una transacción in-memory: reads del snapshot committed,
    writes/deletes bufferizados que se aplican sólo si la función completa."""

    def __init__(self, data: Dict[str, Dict[str, dict]]):
        self._data = data
        self._staged: Dict[Tuple[str, str], Optional[dict]] = {}

    async def get(self, collection: str, doc_id: str) -> Optional[dict]:
        if (collection, doc_id) in self._staged:
            return copy.deepcopy(self._staged[(collection, doc_id)])
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, collection: str, doc_id: str, value: dict) -> None:
        self._staged[(collection, doc_id)] = copy.deepcopy(value)

    def delete(self, collection: str, doc_id: str) -> None:
        self._staged[(collection, doc_id)] = None

    def apply(self) -> None:
        for (collection, doc_id), value in self._staged.items():
            if value is None:
                self._data.get(collection, {}).pop(doc_id, None)
            else:
                self._data.setdefault(collection, {})[doc_id] = value


class InMemoryTicketJobBackend:
    """Backend transaccional en memoria (tests / desarrollo local)."""

    def __init__(self):
        self._data: Dict[str, Dict[str, dict]] = {}
        self._lock = asyncio.Lock()

    async def transact(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        async with self._lock:
            view = _TxnView(self._data)
            result = await fn(view)
            view.apply()
            return result

    async def get_doc(self, collection: str, doc_id: str) -> Optional[dict]:
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc) if doc is not None else None

    async def count_jobs(self, collection: str, principal_id: str,
                         states: list) -> int:
        wanted = set(states)
        return sum(
            1 for doc in self._data.get(collection, {}).values()
            if doc.get("principal_id") == principal_id
            and doc.get("state") in wanted
        )

    async def dump_all(self) -> Dict[str, Dict[str, dict]]:
        return copy.deepcopy(self._data)


class FirestoreTicketJobBackend:
    """Backend Firestore. Capa DELGADA: no contiene lógica de negocio.

    La base NOMBRADA es obligatoria (Tarea 5 Paso 3): staging usa
    ``ticket-staging`` y producción ``(default)``; la base — no un prefijo —
    es el límite de aislamiento. Verificado contra emulador/staging real,
    no por la suite unitaria local.
    """

    def __init__(self, project: Optional[str] = None,
                 collection_prefix: str = "",
                 database: Optional[str] = None):
        from google.cloud import firestore  # import perezoso

        if not database:
            raise ValueError(
                "FirestoreTicketJobBackend requiere una base nombrada "
                "explícita (FIRESTORE_DATABASE); '(default)' debe declararse, "
                "no asumirse"
            )
        self._firestore = firestore
        self._client = firestore.AsyncClient(
            project=project or None,
            database=None if database == "(default)" else database,
        )
        self._prefix = collection_prefix
        self.database = database

    def _col(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def transact(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        firestore = self._firestore
        client = self._client
        prefix = self._prefix

        class _FirestoreView:
            def __init__(self, txn):
                self._txn = txn
                self._writes: list = []

            async def get(self, collection: str, doc_id: str) -> Optional[dict]:
                ref = client.collection(f"{prefix}{collection}").document(doc_id)
                snap = await ref.get(transaction=self._txn)
                return snap.to_dict() if snap.exists else None

            def set(self, collection: str, doc_id: str, value: dict) -> None:
                ref = client.collection(f"{prefix}{collection}").document(doc_id)
                self._writes.append(("set", ref, value))

            def delete(self, collection: str, doc_id: str) -> None:
                ref = client.collection(f"{prefix}{collection}").document(doc_id)
                self._writes.append(("delete", ref, None))

            def flush(self) -> None:
                for op, ref, value in self._writes:
                    if op == "set":
                        self._txn.set(ref, value)
                    else:
                        self._txn.delete(ref)

        transaction = client.transaction()

        @firestore.async_transactional
        async def _run(txn):
            view = _FirestoreView(txn)
            result = await fn(view)
            view.flush()
            return result

        return await _run(transaction)

    async def get_doc(self, collection: str, doc_id: str) -> Optional[dict]:
        ref = self._client.collection(self._col(collection)).document(doc_id)
        snap = await ref.get()
        return snap.to_dict() if snap.exists else None

    async def count_jobs(self, collection: str, principal_id: str,
                         states: list) -> int:  # pragma: no cover - staging
        query = (
            self._client.collection(self._col(collection))
            .where("principal_id", "==", principal_id)
            .where("state", "in", states)
        )
        agg = await query.count().get()
        return int(agg[0][0].value)

    async def dump_all(self):  # pragma: no cover - sólo para tests in-memory
        raise NotImplementedError("dump_all es una utilidad del backend in-memory")


def _plain(value: Any) -> Any:
    """Enums → value; datetimes se PRESERVAN nativos (TTL de Firestore)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and value.__class__.__module__ != "builtins" \
            and not isinstance(value, datetime):
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int)):
            return enum_value
    return value


def _record_to_doc(record: TicketJobRecord) -> dict:
    """Documento COMPLETO con timestamps nativos (mode='python'); nunca
    ``mode='json'``/``isoformat()``: los strings rompen el TTL (bloqueo 2)."""
    return _plain(record.model_dump(mode="python"))


def split_record(record: TicketJobRecord) -> Tuple[dict, dict]:
    """(control_doc sin PII, payload_doc con PII/volumen)."""
    full = _record_to_doc(record)
    payload = {k: full.pop(k) for k in list(full) if k in _PAYLOAD_FIELDS}
    payload["job_id"] = record.job_id
    # el fail-safe de privacidad del payload es SIEMPRE 24h desde aceptación
    payload["expires_at"] = full.get("expires_at")
    # el control no terminal NO lleva TTL: expires_at se fija al terminalizar
    if full.get("state") not in {s.value for s in TERMINAL_STATES}:
        full["expires_at"] = None
    return full, payload


def _join(control: dict, payload: Optional[dict]) -> TicketJobRecord:
    merged = dict(control)
    if payload:
        for key in _PAYLOAD_FIELDS:
            if key in payload:
                merged[key] = payload[key]
        if payload.get("expires_at") is not None:
            merged["expires_at"] = payload["expires_at"]
    return TicketJobRecord.model_validate(merged)


def _doc_to_record(doc: dict) -> TicketJobRecord:
    return TicketJobRecord.model_validate(doc)


class TicketJobRepository:
    """Operaciones de negocio sobre jobs durables. Stateless: puede haber
    una instancia por proceso/instancia de Cloud Run compartiendo backend."""

    def __init__(self, backend, *,
                 retention_days: int = 90,
                 max_outstanding: int = 25,
                 rate_limit_per_minute: int = 0):
        self.backend = backend
        self._retention = timedelta(days=max(retention_days, 90))
        self._max_outstanding = max_outstanding
        self._rate_limit = rate_limit_per_minute

    # ------------------------------------------------------------------
    # Creación + idempotencia + cuotas (una sola transacción)
    # ------------------------------------------------------------------

    async def create_or_get(
        self,
        *,
        principal_id: str,
        idempotency_key: Optional[str],
        request_fingerprint: str,
        candidate: TicketJobRecord,
    ) -> Tuple[Optional[TicketJobRecord], CreateOrGetOutcome]:
        """Reserva transaccional ANTES de cualquier LLM/scrape (Tarea 5):

        1. resuelve la idempotencia primero;
        2. replay/conflict NO consume slot ni ventana;
        3. un job nuevo aplica rate window + cap de activos;
        4. incrementa contadores y crea control, payload y receipt juntos.
        """
        idem_hash = (
            hash_idempotency_key(principal_id, idempotency_key,
                                 candidate.api_version)
            if idempotency_key else None
        )
        if candidate.principal_id != principal_id:
            raise TicketJobError("candidate.principal_id no coincide con el caller")
        candidate = candidate.model_copy(update={"idempotency_key_hash": idem_hash})
        p_hash = principal_hash(principal_id)
        now = utcnow()

        async def _txn(view):
            # 1) idempotencia PRIMERO: un replay jamás paga cuota
            if idem_hash is not None:
                receipt = await view.get(RECEIPTS_COLLECTION, idem_hash)
                if receipt is not None:
                    if receipt.get("request_fingerprint") != request_fingerprint:
                        return None, CreateOrGetOutcome.CONFLICT
                    control = await view.get(JOBS_COLLECTION, receipt["job_id"])
                    if control is not None:
                        payload = await view.get(PAYLOADS_COLLECTION,
                                                 receipt["job_id"])
                        return (_record_to_doc(_join(control, payload)),
                                CreateOrGetOutcome.REPLAYED)
                    # receipt huérfano (control expirado): recrear

            # 2) cuotas atómicas SOLO para jobs nuevos
            if self._rate_limit > 0:
                window = int(now.timestamp()) // _RATE_WINDOW_S
                window_id = f"{p_hash}:{window}"
                rate_doc = await view.get(RATE_WINDOWS_COLLECTION, window_id)
                count = (rate_doc or {}).get("count", 0)
                if count >= self._rate_limit:
                    remaining = _RATE_WINDOW_S - (int(now.timestamp()) % _RATE_WINDOW_S)
                    raise RateWindowExceeded(max(1, remaining))
                view.set(RATE_WINDOWS_COLLECTION, window_id, {
                    "principal_hash": p_hash,
                    "window": window,
                    "count": count + 1,
                    "expires_at": now + _RATE_WINDOW_TTL,
                })

            counter = await view.get(COUNTERS_COLLECTION, p_hash) or {
                "principal_hash": p_hash, "active_jobs": 0,
            }
            if self._max_outstanding > 0 \
                    and counter["active_jobs"] >= self._max_outstanding:
                raise QuotaExceeded(counter["active_jobs"])

            # 3) creación conjunta: control + payload + receipt + contador
            control, payload = split_record(candidate)
            view.set(JOBS_COLLECTION, candidate.job_id, control)
            view.set(PAYLOADS_COLLECTION, candidate.job_id, payload)
            if idem_hash is not None:
                view.set(RECEIPTS_COLLECTION, idem_hash, {
                    "job_id": candidate.job_id,
                    "request_fingerprint": request_fingerprint,
                    "principal_hash": p_hash,
                    "api_version": candidate.api_version,
                    "state": candidate.state.value,
                    "created_at": now,
                    "expires_at": now + self._retention,
                })
            counter["active_jobs"] += 1
            counter["updated_at"] = now
            view.set(COUNTERS_COLLECTION, p_hash, counter)
            return (_record_to_doc(_join(control, payload)),
                    CreateOrGetOutcome.CREATED)

        doc, outcome = await self.backend.transact(_txn)
        return (_doc_to_record(doc) if doc is not None else None), outcome

    async def peek_idempotent(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        api_version: str,
        request_fingerprint: str,
    ) -> Tuple[str, Optional[TicketJobRecord]]:
        """Resolución de idempotencia ANTES de las cuotas (Tarea 4 Paso 5).
        Devuelve ("replay", record) | ("conflict", None) | ("new", None)."""
        idem_hash = hash_idempotency_key(principal_id, idempotency_key,
                                         api_version)
        receipt = await self.backend.get_doc(RECEIPTS_COLLECTION, idem_hash)
        if receipt is None:
            return "new", None
        if receipt.get("request_fingerprint") != request_fingerprint:
            return "conflict", None
        control = await self.backend.get_doc(JOBS_COLLECTION, receipt["job_id"])
        if control is None:
            return "new", None
        payload = await self.backend.get_doc(PAYLOADS_COLLECTION,
                                             receipt["job_id"])
        return "replay", _join(control, payload)

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    async def get(self, job_id: str) -> Optional[TicketJobRecord]:
        record, _present = await self.get_with_payload_state(job_id)
        return record

    async def get_with_payload_state(
        self, job_id: str
    ) -> Tuple[Optional[TicketJobRecord], bool]:
        """(record, payload_present). El control/tombstone sobrevive al
        payload durante todo el horizonte de retención: el endpoint responde
        410 cuando el payload expiró (Tarea 5 Paso 2)."""
        control = await self.backend.get_doc(JOBS_COLLECTION, job_id)
        if control is None:
            return None, False
        payload = await self.backend.get_doc(PAYLOADS_COLLECTION, job_id)
        return _join(control, payload), payload is not None

    async def get_authorized(self, job_id: str,
                             principal_id: str) -> Optional[TicketJobRecord]:
        """None tanto si no existe como si pertenece a otro principal; el
        endpoint decide 404 vs 403 comparando con ``get``."""
        record = await self.get(job_id)
        if record is None or record.principal_id != principal_id:
            return None
        return record

    # ------------------------------------------------------------------
    # Actualización con máquina de estados + liberación de cuota
    # ------------------------------------------------------------------

    async def update(self, job_id: str, *, state: Optional[TicketJobState] = None,
                     expected_lease_epoch: Optional[int] = None,
                     **changes: Any) -> TicketJobRecord:
        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            record = _join(control, payload)
            if expected_lease_epoch is not None \
                    and record.lease_epoch != expected_lease_epoch:
                raise StaleLeaseEpoch(
                    f"lease_epoch actual {record.lease_epoch} != "
                    f"esperado {expected_lease_epoch}"
                )
            now = utcnow()
            updates: Dict[str, Any] = dict(changes)
            terminalizing = False
            if state is not None and state != record.state:
                if state not in VALID_TRANSITIONS[record.state]:
                    raise InvalidStateTransition(record.state, state)
                updates["state"] = state
                if state == TicketJobState.RUNNING and record.started_at is None:
                    updates["started_at"] = now
                if state in TERMINAL_STATES and record.completed_at is None:
                    terminalizing = True
                    updates["completed_at"] = now
                    if record.created_at is not None:
                        updates["elapsed_s"] = round(
                            (now - record.created_at).total_seconds(), 2
                        )
            updates["updated_at"] = now
            merged = record.model_copy(update=updates)

            new_control, new_payload = split_record(merged)
            if terminalizing:
                # el control/tombstone terminal retiene el MISMO horizonte
                # que su receipt (GET → 410 durante todo el replay window)
                new_control["expires_at"] = now + self._retention
                if merged.idempotency_key_hash:
                    receipt = await view.get(RECEIPTS_COLLECTION,
                                             merged.idempotency_key_hash)
                    if receipt is not None:
                        receipt["state"] = merged.state.value
                        receipt["expires_at"] = now + self._retention
                        view.set(RECEIPTS_COLLECTION,
                                 merged.idempotency_key_hash, receipt)
                # liberación exactamente-una-vez del slot de cuota
                if not record.active_slot_released:
                    new_control["active_slot_released"] = True
                    p_hash = principal_hash(record.principal_id)
                    counter = await view.get(COUNTERS_COLLECTION, p_hash)
                    if counter is not None:
                        counter["active_jobs"] = max(
                            0, counter["active_jobs"] - 1)
                        counter["updated_at"] = now
                        if counter["active_jobs"] == 0:
                            view.delete(COUNTERS_COLLECTION, p_hash)
                        else:
                            view.set(COUNTERS_COLLECTION, p_hash, counter)
            elif record.state in TERMINAL_STATES and control.get("expires_at"):
                new_control["expires_at"] = control["expires_at"]

            view.set(JOBS_COLLECTION, job_id, new_control)
            # un payload ya expirado/eliminado NO se recrea (fail-safe de
            # privacidad); sólo se escribe si existía o si hay contenido nuevo
            has_payload_content = any(
                new_payload.get(k) for k in _PAYLOAD_FIELDS
            )
            if payload is not None or has_payload_content:
                view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return _record_to_doc(_join(new_control, new_payload))

        return _doc_to_record(await self.backend.transact(_txn))

    async def record_inquiry_result(self, job_id: str, index: int,
                                    entry: Dict[str, Any],
                                    *, lease_epoch: Optional[int] = None
                                    ) -> TicketJobRecord:
        """Checkpoint por inquiry: persiste inmediatamente (HT-08). Con
        ``lease_epoch`` la escritura es condicional: un worker fenced no
        puede checkpointear (Tarea 6 Paso 4a)."""

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            record = _join(control, payload)
            if lease_epoch is not None and record.lease_epoch != lease_epoch:
                raise StaleLeaseEpoch(
                    f"checkpoint con epoch {lease_epoch}; actual "
                    f"{record.lease_epoch}"
                )
            statuses = [s for s in record.per_inquiry_status
                        if s.get("index") != index]
            statuses.append({**entry, "index": index})
            statuses.sort(key=lambda s: s.get("index", 0))
            processed = sum(
                1 for s in statuses
                if s.get("execution_status") not in (None, "pending", "running")
            )
            merged = record.model_copy(update={
                "per_inquiry_status": statuses,
                "processed_inquiries": processed,
                "updated_at": utcnow(),
            })
            new_control, new_payload = split_record(merged)
            if record.state in TERMINAL_STATES and control.get("expires_at"):
                new_control["expires_at"] = control["expires_at"]
            view.set(JOBS_COLLECTION, job_id, new_control)
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return _record_to_doc(_join(new_control, new_payload))

        return _doc_to_record(await self.backend.transact(_txn))

    # ------------------------------------------------------------------
    # Claims de worker con fencing por epoch (Tarea 6 Paso 4a)
    # ------------------------------------------------------------------

    async def claim(self, job_id: str, *, worker_id: str,
                    lease_s: float = 90.0) -> Optional[int]:
        """Claim transaccional. Devuelve el ``lease_epoch`` nuevo si este
        worker puede ejecutar el job, o None si el claim no procede.

        Cada claim INCREMENTA ``lease_epoch``: un intento viejo que despierta
        después de perder su lease queda fenced en cualquier escritura
        condicional posterior."""

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return None
            record = _doc_to_record(control)
            now = utcnow()
            if record.state in TERMINAL_STATES:
                return None
            lease_expired = (
                record.lease_expires_at is not None
                and now > record.lease_expires_at
            ) or (
                record.lease_expires_at is None
                and record.claimed_at is not None
                and (now - record.claimed_at).total_seconds() > lease_s
            )
            claimable = (
                record.claimed_by is None
                or record.claimed_by == worker_id
                or lease_expired
            )
            if not claimable:
                return None
            new_epoch = record.lease_epoch + 1
            updates = {
                "claimed_by": worker_id,
                "claimed_at": now,
                "lease_epoch": new_epoch,
                "lease_owner": worker_id,
                "lease_expires_at": now + timedelta(seconds=lease_s),
                "attempt": record.attempt + 1,
                "updated_at": now,
            }
            if record.state == TicketJobState.QUEUED:
                updates["state"] = TicketJobState.RUNNING
                if record.started_at is None:
                    updates["started_at"] = now
            merged = record.model_copy(update=updates)
            new_control, _ = split_record(merged)
            # claim NO toca el payload: preserva checkpoints existentes
            new_control.pop("per_inquiry_status", None)
            view.set(JOBS_COLLECTION, job_id, new_control)
            return new_epoch

        return await self.backend.transact(_txn)

    async def renew_lease(self, job_id: str, *, worker_id: str,
                          lease_epoch: int, lease_s: float = 90.0) -> bool:
        """Heartbeat: renueva el lease sólo si owner+epoch siguen vigentes."""

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return False
            record = _doc_to_record(control)
            if record.lease_epoch != lease_epoch \
                    or record.lease_owner != worker_id:
                return False
            now = utcnow()
            control["lease_expires_at"] = now + timedelta(seconds=lease_s)
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return True

        return await self.backend.transact(_txn)

    async def mark_enqueued(self, job_id: str, task_name: str) -> TicketJobRecord:
        return await self.update(job_id, enqueue_state="enqueued",
                                 task_name=task_name)

    async def count_active(self, principal_id: str) -> int:
        """Jobs no-terminales del principal desde el contador transaccional
        (Tarea 5 Paso 2): la verdad durable, no un count() no atómico."""
        counter = await self.backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal_id))
        return int((counter or {}).get("active_jobs", 0))
