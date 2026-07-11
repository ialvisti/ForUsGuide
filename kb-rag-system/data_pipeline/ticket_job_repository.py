"""
Repositorio durable de ticket jobs + idempotencia (Task 3 del plan).

Toda la lógica (reserva idempotente, conflictos de fingerprint, máquina de
estados, checkpoints por inquiry, claims de worker) vive AQUÍ y se ejecuta
dentro de una transacción del backend. Los backends sólo aportan la
primitiva transaccional:

- ``InMemoryTicketJobBackend`` — tests/desarrollo; un ``asyncio.Lock``
  serializa las transacciones con writes bufferizados.
- ``FirestoreTicketJobBackend`` — producción; capa delgada sobre
  ``google.cloud.firestore.AsyncClient`` (misma semántica: reads primero,
  writes al commit). No se verifica en tests locales (documentado en el
  plan); el contrato se prueba contra el backend in-memory.

La función transaccional puede reintentarse (Firestore): debe ser pura
respecto de la vista que recibe.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
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
IDEM_COLLECTION = "ticket_idempotency"


class TicketJobError(Exception):
    pass


class JobNotFound(TicketJobError):
    pass


class InvalidStateTransition(TicketJobError):
    def __init__(self, current: TicketJobState, requested: TicketJobState):
        super().__init__(f"transición inválida: {current.value} → {requested.value}")
        self.current = current
        self.requested = requested


class _TxnView:
    """Vista de una transacción in-memory: reads del snapshot committed,
    writes bufferizados que se aplican sólo si la función completa."""

    def __init__(self, data: Dict[str, Dict[str, dict]]):
        self._data = data
        self._staged: Dict[Tuple[str, str], dict] = {}

    async def get(self, collection: str, doc_id: str) -> Optional[dict]:
        if (collection, doc_id) in self._staged:
            return copy.deepcopy(self._staged[(collection, doc_id)])
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, collection: str, doc_id: str, value: dict) -> None:
        self._staged[(collection, doc_id)] = copy.deepcopy(value)

    def apply(self) -> None:
        for (collection, doc_id), value in self._staged.items():
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

    async def dump_all(self) -> Dict[str, Dict[str, dict]]:
        return copy.deepcopy(self._data)


class FirestoreTicketJobBackend:
    """Backend Firestore. Capa DELGADA: no contiene lógica de negocio.

    NO verificado por la suite local (requiere emulador/servicio); el
    contrato se prueba vía ``InMemoryTicketJobBackend``.
    """

    def __init__(self, project: Optional[str] = None,
                 collection_prefix: str = ""):
        from google.cloud import firestore  # import perezoso

        self._firestore = firestore
        self._client = firestore.AsyncClient(project=project or None)
        self._prefix = collection_prefix

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
                self._writes.append((ref, value))

            def flush(self) -> None:
                for ref, value in self._writes:
                    self._txn.set(ref, value)

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

    async def dump_all(self):  # pragma: no cover - sólo para tests in-memory
        raise NotImplementedError("dump_all es una utilidad del backend in-memory")


def _record_to_doc(record: TicketJobRecord) -> dict:
    return record.model_dump(mode="json")


def _doc_to_record(doc: dict) -> TicketJobRecord:
    return TicketJobRecord.model_validate(doc)


class TicketJobRepository:
    """Operaciones de negocio sobre jobs durables. Stateless: puede haber
    una instancia por proceso/instancia de Cloud Run compartiendo backend."""

    def __init__(self, backend):
        self.backend = backend

    # ------------------------------------------------------------------
    # Creación + idempotencia
    # ------------------------------------------------------------------

    async def create_or_get(
        self,
        *,
        principal_id: str,
        idempotency_key: Optional[str],
        request_fingerprint: str,
        candidate: TicketJobRecord,
    ) -> Tuple[Optional[TicketJobRecord], CreateOrGetOutcome]:
        """Reserva transaccional ANTES de cualquier LLM/scrape.

        - key nueva → crea job + índice idempotente → CREATED
        - key conocida + mismo fingerprint → job existente → REPLAYED
        - key conocida + fingerprint distinto → (None, CONFLICT)
        - sin key → siempre crea (sin índice)
        """
        idem_hash = (
            hash_idempotency_key(principal_id, idempotency_key,
                                 candidate.api_version)
            if idempotency_key else None
        )
        if candidate.principal_id != principal_id:
            raise TicketJobError("candidate.principal_id no coincide con el caller")
        candidate = candidate.model_copy(update={"idempotency_key_hash": idem_hash})

        async def _txn(view):
            if idem_hash is not None:
                existing = await view.get(IDEM_COLLECTION, idem_hash)
                if existing is not None:
                    if existing.get("request_fingerprint") != request_fingerprint:
                        return None, CreateOrGetOutcome.CONFLICT
                    job = await view.get(JOBS_COLLECTION, existing["job_id"])
                    if job is not None:
                        return job, CreateOrGetOutcome.REPLAYED
                    # índice huérfano (job expirado): recrear
            view.set(JOBS_COLLECTION, candidate.job_id, _record_to_doc(candidate))
            if idem_hash is not None:
                view.set(IDEM_COLLECTION, idem_hash, {
                    "job_id": candidate.job_id,
                    "request_fingerprint": request_fingerprint,
                    "principal_id": principal_id,
                    "created_at": utcnow().isoformat(),
                    "expires_at": candidate.expires_at.isoformat()
                    if candidate.expires_at else None,
                })
            return _record_to_doc(candidate), CreateOrGetOutcome.CREATED

        doc, outcome = await self.backend.transact(_txn)
        return (_doc_to_record(doc) if doc is not None else None), outcome

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    async def get(self, job_id: str) -> Optional[TicketJobRecord]:
        doc = await self.backend.get_doc(JOBS_COLLECTION, job_id)
        return _doc_to_record(doc) if doc is not None else None

    async def get_authorized(self, job_id: str,
                             principal_id: str) -> Optional[TicketJobRecord]:
        """None tanto si no existe como si pertenece a otro principal; el
        endpoint decide 404 vs 403 comparando con ``get``."""
        record = await self.get(job_id)
        if record is None or record.principal_id != principal_id:
            return None
        return record

    # ------------------------------------------------------------------
    # Actualización con máquina de estados
    # ------------------------------------------------------------------

    async def update(self, job_id: str, *, state: Optional[TicketJobState] = None,
                     **changes: Any) -> TicketJobRecord:
        async def _txn(view):
            doc = await view.get(JOBS_COLLECTION, job_id)
            if doc is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(doc)
            now = utcnow()
            updates: Dict[str, Any] = dict(changes)
            if state is not None and state != record.state:
                if state not in VALID_TRANSITIONS[record.state]:
                    raise InvalidStateTransition(record.state, state)
                updates["state"] = state
                if state == TicketJobState.RUNNING and record.started_at is None:
                    updates["started_at"] = now
                if state in TERMINAL_STATES and record.completed_at is None:
                    updates["completed_at"] = now
                    if record.created_at is not None:
                        updates["elapsed_s"] = round(
                            (now - record.created_at).total_seconds(), 2
                        )
            updates["updated_at"] = now
            merged = record.model_copy(update=updates)
            view.set(JOBS_COLLECTION, job_id, _record_to_doc(merged))
            return _record_to_doc(merged)

        return _doc_to_record(await self.backend.transact(_txn))

    async def record_inquiry_result(self, job_id: str, index: int,
                                    entry: Dict[str, Any]) -> TicketJobRecord:
        """Checkpoint por inquiry: persiste inmediatamente para que un timeout
        o crash posterior no borre resultados ya completados (HT-08)."""

        async def _txn(view):
            doc = await view.get(JOBS_COLLECTION, job_id)
            if doc is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(doc)
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
            view.set(JOBS_COLLECTION, job_id, _record_to_doc(merged))
            return _record_to_doc(merged)

        return _doc_to_record(await self.backend.transact(_txn))

    # ------------------------------------------------------------------
    # Claims de worker (delivery at-least-once)
    # ------------------------------------------------------------------

    async def claim(self, job_id: str, *, worker_id: str,
                    lease_s: float = 900.0) -> bool:
        """Claim transaccional. True si este worker puede ejecutar el job.

        Reglas: un job QUEUED sin claim (o con lease vencido, o re-entrega al
        mismo worker) es reclamable; cualquier otra cosa devuelve False para
        tolerar el delivery at-least-once de Cloud Tasks sin doble ejecución.
        """

        async def _txn(view):
            doc = await view.get(JOBS_COLLECTION, job_id)
            if doc is None:
                return False
            record = _doc_to_record(doc)
            now = utcnow()
            if record.state in TERMINAL_STATES:
                return False
            lease_expired = (
                record.claimed_at is not None
                and (now - record.claimed_at).total_seconds() > lease_s
            )
            claimable = (
                record.claimed_by is None
                or record.claimed_by == worker_id
                or lease_expired
                or record.state == TicketJobState.QUEUED and record.claimed_by is None
            )
            if not claimable:
                return False
            updates = {
                "claimed_by": worker_id,
                "claimed_at": now,
                "attempt": record.attempt + 1,
                "updated_at": now,
            }
            if record.state == TicketJobState.QUEUED:
                updates["state"] = TicketJobState.RUNNING
                if record.started_at is None:
                    updates["started_at"] = now
            merged = record.model_copy(update=updates)
            view.set(JOBS_COLLECTION, job_id, _record_to_doc(merged))
            return True

        return await self.backend.transact(_txn)

    async def mark_enqueued(self, job_id: str, task_name: str) -> TicketJobRecord:
        return await self.update(job_id, enqueue_state="enqueued",
                                 task_name=task_name)
