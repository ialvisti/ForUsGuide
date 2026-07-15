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
    NextAction,
    PublicErrorCode,
    TicketJobRecord,
    TicketJobState,
    hash_idempotency_key,
    hash_tenant_id,
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
    "public_result", "private_diagnostics_ref", "tenant_id", "ticket_id",
    "forusbots_job_ids", "fault_plan",
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


class IdempotencyReceiptOrphaned(TicketJobError):
    """Receipt vigente cuyo control/tombstone falta: nunca se recrea."""


class IdempotencyTenantMismatch(TicketJobError):
    """La key pertenece a otro tenant del mismo principal: no replay."""


class StaleEnqueueGeneration(TicketJobError):
    """Una confirmación de Cloud Tasks corresponde a otra generación."""


def principal_hash(principal_id: str) -> str:
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:32]


def _canonical_tenant_hash(
    *, tenant_id: Optional[str], tenant_id_hash: Optional[str]
) -> Optional[str]:
    """Normaliza el binding sin confiar en un hash que contradiga el raw."""
    if tenant_id is None:
        return tenant_id_hash
    canonical = hash_tenant_id(str(tenant_id))
    if tenant_id_hash is not None and tenant_id_hash != canonical:
        raise TicketJobError("tenant_id_hash no coincide con tenant_id")
    return canonical


def _assert_idempotency_tenant_binding(
    *, receipt: dict, control: dict, expected_hash: Optional[str]
) -> None:
    """Fail closed para receipts nuevos y legados antes de devolver un job.

    Los receipts previos a este campo se enlazan mediante el hash estable del
    control/tombstone. Si receipt y control discrepan, tampoco se replaya.
    """
    receipt_hash = receipt.get("tenant_id_hash")
    control_hash = _canonical_tenant_hash(
        tenant_id=control.get("tenant_id"),
        tenant_id_hash=control.get("tenant_id_hash"),
    )
    if receipt_hash is not None and control_hash != receipt_hash:
        raise IdempotencyTenantMismatch(
            "receipt y control tienen bindings de tenant distintos"
        )
    observed_hash = receipt_hash if receipt_hash is not None else control_hash
    if observed_hash != expected_hash:
        raise IdempotencyTenantMismatch(
            "la Idempotency-Key pertenece a otro tenant"
        )


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

    async def scan_collection(self, collection: str, limit: int = 100,
                              *, states: Optional[list[str]] = None) -> list:
        docs = self._data.get(collection, {})
        wanted = set(states) if states is not None else None
        eligible = (
            (doc_id, doc) for doc_id, doc in docs.items()
            if wanted is None or doc.get("state") in wanted
        )
        return [(doc_id, copy.deepcopy(doc))
                for doc_id, doc in list(eligible)[:limit]]


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

    async def scan_collection(self, collection: str, limit: int = 100,
                              *, states: Optional[list[str]] = None
                              ) -> list:  # pragma: no cover
        query = self._client.collection(self._col(collection))
        if states is not None:
            query = query.where("state", "in", states)
        query = query.limit(limit)
        out = []
        async for snap in query.stream():
            out.append((snap.id, snap.to_dict()))
        return out


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
        candidate_tenant_hash = _canonical_tenant_hash(
            tenant_id=candidate.tenant_id,
            tenant_id_hash=candidate.tenant_id_hash,
        )
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
                        try:
                            _assert_idempotency_tenant_binding(
                                receipt=receipt,
                                control=control,
                                expected_hash=candidate_tenant_hash,
                            )
                        except IdempotencyTenantMismatch:
                            # Mantiene el contrato transaccional existente:
                            # cualquier binding incompatible es CONFLICT y no
                            # consume cuota ni crea/replaya un job.
                            return None, CreateOrGetOutcome.CONFLICT
                        payload = await view.get(PAYLOADS_COLLECTION,
                                                 receipt["job_id"])
                        return (_record_to_doc(_join(control, payload)),
                                CreateOrGetOutcome.REPLAYED)
                    raise IdempotencyReceiptOrphaned(
                        f"receipt huérfano para job {receipt['job_id']}"
                    )

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
                    "tenant_id_hash": candidate_tenant_hash,
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
        tenant_id: Optional[str],
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
            raise IdempotencyReceiptOrphaned(
                f"receipt huérfano para job {receipt['job_id']}"
            )
        _assert_idempotency_tenant_binding(
            receipt=receipt,
            control=control,
            expected_hash=_canonical_tenant_hash(
                tenant_id=tenant_id,
                tenant_id_hash=None,
            ),
        )
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
            now = utcnow()
            if expected_lease_epoch is not None:
                if record.state in TERMINAL_STATES:
                    raise StaleLeaseEpoch(
                        f"job {job_id} terminal: escritura rechazada"
                    )
                if record.lease_epoch != expected_lease_epoch:
                    raise StaleLeaseEpoch(
                        f"lease_epoch actual {record.lease_epoch} != "
                        f"esperado {expected_lease_epoch}"
                    )
                if record.lease_owner is None \
                        or record.lease_expires_at is None \
                        or now >= record.lease_expires_at:
                    raise StaleLeaseEpoch(
                        f"job {job_id}: lease vencido o sin owner"
                    )
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
                    updates["lease_owner"] = None
                    updates["lease_expires_at"] = None
                    updates["claimed_by"] = None
                    updates["claimed_at"] = None
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
            if record.state in TERMINAL_STATES:
                raise StaleLeaseEpoch(
                    f"job {job_id} terminal: checkpoint rechazado"
                )
            if lease_epoch is not None:
                if record.lease_epoch != lease_epoch:
                    raise StaleLeaseEpoch(
                        f"checkpoint con epoch {lease_epoch}; actual "
                        f"{record.lease_epoch}"
                    )
                now = utcnow()
                if record.lease_owner is None \
                        or record.lease_expires_at is None \
                        or now >= record.lease_expires_at:
                    raise StaleLeaseEpoch(
                        f"job {job_id}: lease vencido o sin owner"
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

    async def reserve_forusbots_submit_intent(
        self,
        job_id: str,
        index: int,
        *,
        worker_id: str,
        lease_epoch: int,
        route: str,
    ) -> bool:
        """Persiste el intent ANTES del primer POST de una inquiry.

        Devuelve ``True`` sólo al primer owner/epoch que lo reserva. Si un
        intent ya existe devuelve ``False`` y nunca lo borra: sin contrato de
        idempotencia/reconciliación upstream no se puede distinguir entre un
        POST que no salió y uno que creó un job cuya respuesta se perdió.

        La comprobación del lease y la escritura del intent comparten la
        transacción, tanto en memoria como en Firestore.
        """

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            if payload is None:
                raise TicketJobError(
                    f"job {job_id}: payload ausente; submit bloqueado"
                )
            record = _join(control, payload)
            now = utcnow()
            if record.state in TERMINAL_STATES \
                    or record.lease_epoch != lease_epoch \
                    or record.lease_owner != worker_id \
                    or record.lease_expires_at is None \
                    or now >= record.lease_expires_at:
                raise StaleLeaseEpoch(
                    f"job {job_id}: lease perdido antes del intent ForusBots"
                )

            existing = next(
                (entry for entry in record.per_inquiry_status
                 if entry.get("index") == index),
                None,
            )
            if existing and existing.get("forusbots_submit_intent") is True:
                return False

            statuses = [
                entry for entry in record.per_inquiry_status
                if entry.get("index") != index
            ]
            statuses.append({
                "index": index,
                "route": route,
                "execution_status": "running",
                "participant_reply_safe": False,
                "forusbots_submit_intent": True,
                "forusbots_submit_intent_epoch": lease_epoch,
            })
            statuses.sort(key=lambda entry: entry.get("index", 0))
            merged = record.model_copy(update={
                "per_inquiry_status": statuses,
                "updated_at": now,
            })
            new_control, new_payload = split_record(merged)
            view.set(JOBS_COLLECTION, job_id, new_control)
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return True

        return await self.backend.transact(_txn)

    # ------------------------------------------------------------------
    # Claims de worker con fencing por epoch (Tarea 6 Paso 4a)
    # ------------------------------------------------------------------

    async def assert_active_lease(self, job_id: str, *, worker_id: str,
                                  lease_epoch: int) -> None:
        """Fail-closed antes de un efecto externo.

        Coincidir sólo por epoch no basta: un job puede haber terminalizado o
        el lease puede haber vencido sin que otro worker lo haya reclamado.
        """
        control = await self.backend.get_doc(JOBS_COLLECTION, job_id)
        if control is None:
            raise StaleLeaseEpoch(f"job {job_id}: control ausente")
        record = _doc_to_record(control)
        if record.state in TERMINAL_STATES:
            raise StaleLeaseEpoch(f"job {job_id}: estado terminal")
        if record.lease_epoch != lease_epoch \
                or record.lease_owner != worker_id:
            raise StaleLeaseEpoch(
                f"job {job_id}: owner/epoch del lease no coincide"
            )
        if record.lease_expires_at is None \
                or utcnow() >= record.lease_expires_at:
            raise StaleLeaseEpoch(f"job {job_id}: lease vencido")

    async def claim(self, job_id: str, *, worker_id: str,
                    lease_s: float = 90.0,
                    expected_generation: Optional[int] = None) -> Optional[int]:
        """Claim transaccional. Devuelve el ``lease_epoch`` nuevo si este
        worker puede ejecutar el job, o None si el claim no procede.

        Cuando el caller es una Cloud Task, ``expected_generation`` fencea
        dentro de ESTA misma transacción el outbox y el lease. El pre-check
        HTTP es sólo una optimización: una tombstone/requeue concurrente no
        puede permitir que una task vieja reclame la generación nueva.

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
            if expected_generation is not None \
                    and record.enqueue_generation != expected_generation:
                raise StaleEnqueueGeneration(
                    f"generación de enqueue actual {record.enqueue_generation} "
                    f"!= esperada {expected_generation}"
                )
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
            now = utcnow()
            if record.state in TERMINAL_STATES \
                    or record.lease_epoch != lease_epoch \
                    or record.lease_owner != worker_id \
                    or record.lease_expires_at is None \
                    or now >= record.lease_expires_at:
                return False
            control["lease_expires_at"] = now + timedelta(seconds=lease_s)
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return True

        return await self.backend.transact(_txn)

    async def mark_enqueued(
        self,
        job_id: str,
        task_name: str,
        *,
        expected_generation: Optional[int] = None,
    ) -> TicketJobRecord:
        """Confirma el enqueue sólo para la generación que creó ``task_name``.

        La inferencia mantiene seguros los call sites existentes; aceptar una
        confirmación sin generación permitiría que una task vieja ocultase un
        outbox ``pending`` más nuevo.
        """
        if expected_generation is None:
            try:
                expected_generation = int(task_name.rsplit("-g", 1)[1])
            except (IndexError, ValueError) as exc:
                raise TicketJobError(
                    "task_name no contiene una generación verificable"
                ) from exc

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(control)
            if record.enqueue_generation != expected_generation:
                raise StaleEnqueueGeneration(
                    f"generación de enqueue actual {record.enqueue_generation} "
                    f"!= esperada {expected_generation}"
                )
            control["enqueue_state"] = "enqueued"
            control["task_name"] = task_name
            control["updated_at"] = utcnow()
            view.set(JOBS_COLLECTION, job_id, control)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            return _record_to_doc(_join(control, payload))

        return _doc_to_record(await self.backend.transact(_txn))

    async def bump_enqueue_generation(self, job_id: str) -> int:
        """Incremento TRANSACCIONAL de la generación de enqueue (Tarea 7
        Paso 3): tras una tombstone o un requeue administrativo, el nombre
        de task anterior queda quemado y sólo la generación nueva ejecuta."""

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(control)
            if record.state in TERMINAL_STATES:
                raise TicketJobError(
                    f"job {job_id} es terminal: no se re-encola")
            new_generation = record.enqueue_generation + 1
            control["enqueue_generation"] = new_generation
            control["enqueue_state"] = "pending"
            control["updated_at"] = utcnow()
            view.set(JOBS_COLLECTION, job_id, control)
            return new_generation

        return await self.backend.transact(_txn)

    async def acquire_recovery_lock(self, job_id: str, *, owner: str,
                                    lock_s: float = 120.0) -> bool:
        """Lock del RECONCILIADOR, separado del lease de ejecución del worker
        (Tarea 7 Paso 5): reparar outbox/leases sin poseer el lease que debe
        reclamar el worker. Tolera dos reconciliadores concurrentes."""

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return False
            record = _doc_to_record(control)
            now = utcnow()
            held = (
                record.recovery_lock_owner is not None
                and record.recovery_lock_expires_at is not None
                and now <= record.recovery_lock_expires_at
                and record.recovery_lock_owner != owner
            )
            if held:
                return False
            control["recovery_lock_owner"] = owner
            control["recovery_lock_expires_at"] = now + timedelta(seconds=lock_s)
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return True

        return await self.backend.transact(_txn)

    async def fence_and_requeue(
        self,
        job_id: str,
        *,
        recovery_owner: Optional[str] = None,
        expected_lease_epoch: Optional[int] = None,
        expected_lease_expires_at: Optional[datetime] = None,
        observed_at: Optional[datetime] = None,
    ) -> Optional[int]:
        """Repara un lease vencido (Tarea 7 Paso 5.2): incrementa
        ``lease_epoch`` para fencear al worker viejo, limpia owner/expiry,
        transiciona running→queued y quema una generación nueva. El
        reconciliador NO conserva el lease de ejecución.

        Cuando se pasan precondiciones del snapshot, todas se comprueban en
        la misma transacción. Un heartbeat posterior al scan invalida el CAS
        y no puede ser pisado por el reconciliador.
        """

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return None
            record = _doc_to_record(control)
            if record.state in TERMINAL_STATES:
                return None
            now = utcnow()
            if recovery_owner is not None and (
                record.recovery_lock_owner != recovery_owner
                or record.recovery_lock_expires_at is None
                or now > record.recovery_lock_expires_at
            ):
                raise StaleLeaseEpoch(
                    f"job {job_id}: recovery lock perdido"
                )
            if expected_lease_epoch is not None \
                    and record.lease_epoch != expected_lease_epoch:
                raise StaleLeaseEpoch(
                    f"job {job_id}: lease_epoch cambió desde el scan"
                )
            if expected_lease_expires_at is not None:
                if record.state != TicketJobState.RUNNING \
                        or record.lease_expires_at != expected_lease_expires_at:
                    raise StaleLeaseEpoch(
                        f"job {job_id}: lease cambió desde el scan"
                    )
                cutoff = observed_at or now
                if cutoff <= record.lease_expires_at:
                    raise StaleLeaseEpoch(
                        f"job {job_id}: lease aún vigente"
                    )
            control["lease_epoch"] = record.lease_epoch + 1
            control["lease_owner"] = None
            control["lease_expires_at"] = None
            control["claimed_by"] = None
            control["claimed_at"] = None
            if record.state == TicketJobState.RUNNING:
                control["state"] = TicketJobState.QUEUED.value
            control["enqueue_generation"] = record.enqueue_generation + 1
            control["enqueue_state"] = "pending"
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return control["enqueue_generation"]

        return await self.backend.transact(_txn)

    async def _stage_terminalization(
        self,
        view,
        job_id: str,
        record: TicketJobRecord,
        payload: Optional[dict],
        *,
        state: TicketJobState,
        next_action: Any,
        public_error_code: str,
        retryable: bool,
        current_step: str,
        now: datetime,
    ) -> dict:
        """Escrituras comunes de terminalización dentro de la tx llamante."""
        updates: Dict[str, Any] = {
            "state": state,
            "next_action": next_action,
            "public_error_code": public_error_code,
            "retryable": retryable,
            "current_step": current_step,
            "lease_epoch": record.lease_epoch + 1,
            "lease_owner": None,
            "lease_expires_at": None,
            "claimed_by": None,
            "claimed_at": None,
            "recovery_lock_owner": None,
            "recovery_lock_expires_at": None,
            "completed_at": record.completed_at or now,
            "updated_at": now,
        }
        if record.created_at is not None:
            updates["elapsed_s"] = round(
                (now - record.created_at).total_seconds(), 2
            )
        merged = record.model_copy(update=updates)
        new_control, new_payload = split_record(merged)
        new_control["expires_at"] = now + self._retention

        if merged.idempotency_key_hash:
            receipt = await view.get(
                RECEIPTS_COLLECTION, merged.idempotency_key_hash,
            )
            if receipt is not None:
                receipt["state"] = state.value
                receipt["expires_at"] = now + self._retention
                view.set(
                    RECEIPTS_COLLECTION,
                    merged.idempotency_key_hash,
                    receipt,
                )

        if not record.active_slot_released:
            new_control["active_slot_released"] = True
            p_hash = principal_hash(record.principal_id)
            counter = await view.get(COUNTERS_COLLECTION, p_hash)
            if counter is not None:
                counter["active_jobs"] = max(
                    0, counter["active_jobs"] - 1,
                )
                counter["updated_at"] = now
                if counter["active_jobs"] == 0:
                    view.delete(COUNTERS_COLLECTION, p_hash)
                else:
                    view.set(COUNTERS_COLLECTION, p_hash, counter)

        view.set(JOBS_COLLECTION, job_id, new_control)
        if payload is not None:
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
        return _record_to_doc(_join(new_control, new_payload))

    async def terminalize_if_unrecoverable(
        self,
        job_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[TicketJobRecord]:
        """Lazy terminalization atómica para GET/status.

        Si el job ya es terminal o aún es recuperable, devuelve el record sin
        mutarlo. Si venció el deadline absoluto o desapareció su payload,
        revalida la causa dentro de la transacción, fencea y libera la cuota.
        La autorización del principal/tenant debe hacerse antes de llamarlo.
        """
        observed_at = now or utcnow()

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return None
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            record = _join(control, payload)
            if record.state in TERMINAL_STATES:
                return _record_to_doc(record)

            if record.job_deadline_at is not None \
                    and record.job_deadline_at <= observed_at:
                state = TicketJobState.TIMEOUT
                code = PublicErrorCode.TOTAL_JOB_TIMEOUT.value
            elif payload is None:
                state = TicketJobState.FAILED
                code = "EXPIRED_PAYLOAD"
            else:
                return _record_to_doc(record)

            return await self._stage_terminalization(
                view,
                job_id,
                record,
                payload,
                state=state,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=code,
                retryable=False,
                current_step="done",
                now=observed_at,
            )

        doc = await self.backend.transact(_txn)
        return _doc_to_record(doc) if doc is not None else None

    async def terminalize_recovery(
        self,
        job_id: str,
        *,
        state: TicketJobState,
        recovery_owner: str,
        expected_state: str,
        expected_lease_epoch: int,
        next_action: Any,
        public_error_code: str,
        retryable: bool,
        current_step: str,
        observed_at: datetime,
        expected_deadline_at: Optional[datetime] = None,
        require_payload_missing: bool = False,
    ) -> TicketJobRecord:
        """Fencea y terminaliza en UNA transacción con CAS del snapshot.

        No existe estado intermedio QUEUED reclamable entre el fence y el
        terminal. Las condiciones que justifican la recuperación (deadline o
        payload ausente) se vuelven a validar dentro de la transacción.
        """
        if state not in TERMINAL_STATES:
            raise TicketJobError(
                f"terminalize_recovery exige estado terminal, recibió {state}"
            )

        async def _txn(view):
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            record = _join(control, payload)
            now = utcnow()
            if record.state in TERMINAL_STATES:
                raise StaleLeaseEpoch(f"job {job_id}: ya terminal")
            if record.recovery_lock_owner != recovery_owner \
                    or record.recovery_lock_expires_at is None \
                    or now > record.recovery_lock_expires_at:
                raise StaleLeaseEpoch(
                    f"job {job_id}: recovery lock perdido"
                )
            if record.state.value != expected_state \
                    or record.lease_epoch != expected_lease_epoch:
                raise StaleLeaseEpoch(
                    f"job {job_id}: snapshot de estado/epoch obsoleto"
                )
            if expected_deadline_at is not None and (
                record.job_deadline_at != expected_deadline_at
                or observed_at < expected_deadline_at
            ):
                raise StaleLeaseEpoch(
                    f"job {job_id}: deadline cambió o aún no venció"
                )
            if require_payload_missing and payload is not None:
                raise StaleLeaseEpoch(
                    f"job {job_id}: payload reapareció desde el scan"
                )

            return await self._stage_terminalization(
                view,
                job_id,
                record,
                payload,
                state=state,
                next_action=next_action,
                public_error_code=public_error_code,
                retryable=retryable,
                current_step=current_step,
                now=now,
            )

        return _doc_to_record(await self.backend.transact(_txn))

    async def scan_control_docs(self, limit: int = 100) -> list:
        """Lote activo; los tombstones no consumen el límite ni causan
        starvation permanente de reparaciones posteriores."""
        return await self.backend.scan_collection(
            JOBS_COLLECTION,
            limit,
            states=[TicketJobState.QUEUED.value, TicketJobState.RUNNING.value],
        )

    async def count_active(self, principal_id: str) -> int:
        """Jobs no-terminales del principal desde el contador transaccional
        (Tarea 5 Paso 2): la verdad durable, no un count() no atómico."""
        counter = await self.backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal_id))
        return int((counter or {}).get("active_jobs", 0))
