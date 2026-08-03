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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    cast,
)

from google.cloud.firestore_v1.base_query import FieldFilter

from data_pipeline.durable_document import validate_durable_document
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
from data_pipeline.forusbots_contract import FORUSBOTS_IDEMPOTENCY_CONTRACT

JOBS_COLLECTION = "ticket_jobs"
PAYLOADS_COLLECTION = "ticket_job_payloads"
RECEIPTS_COLLECTION = "ticket_idempotency_receipts"
COUNTERS_COLLECTION = "ticket_active_counters"
RATE_WINDOWS_COLLECTION = "ticket_rate_windows"
RECONCILER_STATE_COLLECTION = "ticket_reconciler_state"

_ACTIVE_SCAN_CURSOR_ID = "active_jobs"

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

Document = dict[str, Any]
CollectionData = dict[str, dict[str, Document]]
ScanPage = list[tuple[str, Document]]
TxnResult = TypeVar("TxnResult")


class TransactionView(Protocol):
    """Primitiva mínima compartida por las transacciones memory/Firestore."""

    async def get(self, collection: str, doc_id: str) -> Optional[Document]: ...

    def set(self, collection: str, doc_id: str, value: Document) -> None: ...

    def delete(self, collection: str, doc_id: str) -> None: ...


class TicketJobBackend(Protocol):
    """Contrato tipado del backend; la lógica de negocio no depende del SDK."""

    async def transact(
        self,
        fn: Callable[[TransactionView], Awaitable[TxnResult]],
    ) -> TxnResult: ...

    async def get_doc(
        self, collection: str, doc_id: str
    ) -> Optional[Document]: ...

    async def scan_collection(
        self,
        collection: str,
        limit: int = 100,
        *,
        states: Optional[list[str]] = None,
        start_after: Optional[str] = None,
    ) -> ScanPage: ...

    async def active_job_stats(
        self, collection: str, states: list[str]
    ) -> tuple[int, Optional[datetime]]: ...


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


@dataclass(frozen=True)
class ForusBotsOperationDecision:
    action: str
    external_job_id: Optional[str] = None


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
    *, receipt: Document, control: Document, expected_hash: Optional[str]
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

    def __init__(self, data: CollectionData):
        self._data = data
        self._staged: Dict[Tuple[str, str], Optional[Document]] = {}

    async def get(self, collection: str, doc_id: str) -> Optional[Document]:
        if (collection, doc_id) in self._staged:
            return copy.deepcopy(self._staged[(collection, doc_id)])
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, collection: str, doc_id: str, value: Document) -> None:
        validate_durable_document(value)
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

    def __init__(self) -> None:
        self._data: CollectionData = {}
        self._lock = asyncio.Lock()

    async def transact(
        self,
        fn: Callable[[TransactionView], Awaitable[TxnResult]],
    ) -> TxnResult:
        async with self._lock:
            view = _TxnView(self._data)
            result = await fn(view)
            view.apply()
            return result

    async def get_doc(
        self, collection: str, doc_id: str
    ) -> Optional[Document]:
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc) if doc is not None else None

    async def count_jobs(
        self, collection: str, principal_id: str, states: list[str]
    ) -> int:
        wanted = set(states)
        return sum(
            1 for doc in self._data.get(collection, {}).values()
            if doc.get("principal_id") == principal_id
            and doc.get("state") in wanted
        )

    async def dump_all(self) -> CollectionData:
        return copy.deepcopy(self._data)

    async def scan_collection(
        self,
        collection: str,
        limit: int = 100,
        *,
        states: Optional[list[str]] = None,
        start_after: Optional[str] = None,
    ) -> ScanPage:
        docs = self._data.get(collection, {})
        wanted = set(states) if states is not None else None
        eligible = sorted(
            (doc_id, doc) for doc_id, doc in docs.items()
            if wanted is None or doc.get("state") in wanted
        )
        if start_after is not None:
            eligible = [item for item in eligible if item[0] > start_after]
        return [(doc_id, copy.deepcopy(doc))
                for doc_id, doc in eligible[:limit]]

    async def active_job_stats(
        self, collection: str, states: list[str]
    ) -> tuple[int, Optional[datetime]]:
        wanted = set(states)
        active = [
            doc for doc in self._data.get(collection, {}).values()
            if doc.get("state") in wanted
        ]
        created: list[datetime] = []
        for document in active:
            created_at = document.get("created_at")
            if isinstance(created_at, datetime):
                created.append(created_at)
        return len(active), min(created) if created else None


class FirestoreTicketJobBackend:
    """Backend Firestore. Capa DELGADA: no contiene lógica de negocio.

    La base NOMBRADA es obligatoria (Tarea 5 Paso 3): staging usa
    ``ticket-staging`` y producción ``(default)``; la base — no un prefijo —
    es el límite de aislamiento. Verificado contra emulador/staging real,
    no por la suite unitaria local.
    """

    def __init__(
        self,
        project: Optional[str] = None,
        collection_prefix: str = "",
        database: Optional[str] = None,
    ) -> None:
        import google.cloud.firestore as firestore  # import perezoso

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

    async def transact(
        self,
        fn: Callable[[TransactionView], Awaitable[TxnResult]],
    ) -> TxnResult:
        firestore = self._firestore
        client = self._client
        prefix = self._prefix

        class _FirestoreView:
            def __init__(self, txn: Any) -> None:
                self._txn = txn
                self._writes: list[tuple[str, Any, Optional[Document]]] = []

            async def get(
                self, collection: str, doc_id: str
            ) -> Optional[Document]:
                ref = client.collection(f"{prefix}{collection}").document(doc_id)
                snap = await ref.get(transaction=self._txn)
                raw = snap.to_dict() if snap.exists else None
                return raw

            def set(
                self, collection: str, doc_id: str, value: Document
            ) -> None:
                validate_durable_document(value)
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

    async def get_doc(
        self, collection: str, doc_id: str
    ) -> Optional[Document]:
        ref = self._client.collection(self._col(collection)).document(doc_id)
        snap = await ref.get()
        raw = snap.to_dict() if snap.exists else None
        return raw

    async def count_jobs(
        self, collection: str, principal_id: str, states: list[str]
    ) -> int:  # pragma: no cover - staging
        query = (
            self._client.collection(self._col(collection))
            .where(filter=FieldFilter("principal_id", "==", principal_id))
            .where(filter=FieldFilter("state", "in", states))
        )
        agg = await cast(Any, query.count()).get()
        return int(agg[0][0].value)

    async def dump_all(
        self,
    ) -> CollectionData:  # pragma: no cover - sólo para tests in-memory
        raise NotImplementedError("dump_all es una utilidad del backend in-memory")

    async def scan_collection(
        self,
        collection: str,
        limit: int = 100,
        *,
        states: Optional[list[str]] = None,
        start_after: Optional[str] = None,
    ) -> ScanPage:  # pragma: no cover
        query: Any = self._client.collection(self._col(collection))
        if states is not None:
            query = query.where(filter=FieldFilter("state", "in", states))
        query = query.order_by("__name__")
        if start_after is not None:
            query = query.start_after({"__name__": start_after})
        query = query.limit(limit)
        out: ScanPage = []
        async for snap in query.stream():
            out.append((snap.id, cast(Document, snap.to_dict())))
        return out

    async def active_job_stats(
        self, collection: str, states: list[str]
    ) -> tuple[int, Optional[datetime]]:  # pragma: no cover - staging
        query: Any = self._client.collection(self._col(collection)).where(
            filter=FieldFilter("state", "in", states)
        )
        aggregation = await cast(Any, query.count()).get()
        count = int(aggregation[0][0].value)
        if count == 0:
            return 0, None
        oldest_query = query.order_by("created_at").limit(1)
        async for snapshot in oldest_query.stream():
            raw = cast(Document, snapshot.to_dict())
            created_at = raw.get("created_at")
            return count, created_at if isinstance(created_at, datetime) else None
        return count, None


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


def _record_to_doc(record: TicketJobRecord) -> Document:
    """Documento COMPLETO con timestamps nativos (mode='python'); nunca
    ``mode='json'``/``isoformat()``: los strings rompen el TTL (bloqueo 2)."""
    return cast(Document, _plain(record.model_dump(mode="python")))


def split_record(record: TicketJobRecord) -> Tuple[Document, Document]:
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


_IMMUTABLE_EFFECT_CHECKPOINT_KEYS = (
    "forusbots_submit_intent",
    "forusbots_submit_intent_epoch",
    "forusbots_submit_intent_worker",
    "forusbots_submit_intents",
    "forusbots_external_jobs",
)


def _merge_effect_checkpoint(
    existing: Optional[Document], entry: Document, *, index: int
) -> Document:
    """Merge an inquiry result without erasing durable side-effect evidence."""
    merged = {**entry, "index": index}
    if not existing:
        return merged
    for key in _IMMUTABLE_EFFECT_CHECKPOINT_KEYS:
        if key in existing:
            merged[key] = copy.deepcopy(existing[key])
    return merged


def _validated_external_job_id(value: str) -> str:
    """Validate an opaque upstream identifier without echoing it on failure."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(char) < 32 for char in value)
    ):
        raise TicketJobError("ForUsBots devolvió un identificador inválido")
    return value


def build_validated_inquiry_checkpoint(
    record: TicketJobRecord,
    index: int,
    entry: Document,
) -> TicketJobRecord:
    """Build and validate the complete control/payload checkpoint documents."""
    existing = next(
        (status for status in record.per_inquiry_status
         if status.get("index") == index),
        None,
    )
    statuses = [
        status for status in record.per_inquiry_status
        if status.get("index") != index
    ]
    statuses.append(_merge_effect_checkpoint(existing, entry, index=index))
    statuses.sort(key=lambda status: status.get("index", 0))
    processed = sum(
        1 for status in statuses
        if status.get("execution_status") not in (None, "pending", "running")
    )
    merged = record.model_copy(update={
        "per_inquiry_status": statuses,
        "processed_inquiries": processed,
        "updated_at": utcnow(),
    })
    control, payload = split_record(merged)
    validate_durable_document(control)
    validate_durable_document(payload)
    return merged


def _live_payload(
    payload: Optional[Document], observed_at: Optional[datetime] = None,
) -> Optional[Document]:
    """Return payload only before its logical privacy deadline.

    Firestore TTL deletion is asynchronous and therefore cleanup-only. A
    physically present document with a missing, malformed or elapsed
    ``expires_at`` is absent at every application boundary.
    """
    if payload is None:
        return None
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, datetime):
        return None
    try:
        if expires_at <= (observed_at or utcnow()):
            return None
    except TypeError:
        # Naive/aware timestamp mismatch is malformed and fails closed.
        return None
    return payload


def _join(
    control: Document,
    payload: Optional[Document],
    observed_at: Optional[datetime] = None,
) -> TicketJobRecord:
    payload = _live_payload(payload, observed_at)
    merged = dict(control)
    if payload:
        for key in _PAYLOAD_FIELDS:
            if key in payload:
                merged[key] = payload[key]
        if payload.get("expires_at") is not None:
            merged["expires_at"] = payload["expires_at"]
    return TicketJobRecord.model_validate(merged)


def _doc_to_record(doc: Document) -> TicketJobRecord:
    return TicketJobRecord.model_validate(doc)


class TicketJobRepository:
    """Operaciones de negocio sobre jobs durables. Stateless: puede haber
    una instancia por proceso/instancia de Cloud Run compartiendo backend."""

    def __init__(
        self,
        backend: TicketJobBackend,
        *,
        retention_days: int = 90,
        max_outstanding: int = 25,
        rate_limit_per_minute: int = 0,
    ) -> None:
        self.backend = backend
        self._retention = timedelta(days=max(retention_days, 90))
        self._max_outstanding = max_outstanding
        self._rate_limit = rate_limit_per_minute
        # Load-shedding local por el documento caliente de cuota/ventana. La
        # transacción Firestore sigue siendo la autoridad entre instancias;
        # este single-flight evita que hasta 80 requests de una misma instancia
        # entren juntas a bloquear el mismo counter/receipt y agoten los cinco
        # reintentos nativos del SDK.
        self._admission_locks_guard = asyncio.Lock()
        self._admission_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    async def _retain_admission_lock(self, principal: str) -> asyncio.Lock:
        async with self._admission_locks_guard:
            current = self._admission_locks.get(principal)
            if current is None:
                lock, users = asyncio.Lock(), 0
            else:
                lock, users = current
            self._admission_locks[principal] = (lock, users + 1)
            return lock

    async def _release_admission_lock(
        self, principal: str, lock: asyncio.Lock,
    ) -> None:
        async with self._admission_locks_guard:
            current = self._admission_locks.get(principal)
            if current is None or current[0] is not lock:
                raise RuntimeError("admission lock registry lost ownership")
            users = current[1] - 1
            if users == 0:
                del self._admission_locks[principal]
            else:
                self._admission_locks[principal] = (lock, users)

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

        async def _txn(
            view: TransactionView,
        ) -> tuple[Optional[Document], CreateOrGetOutcome]:
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
                        return (_record_to_doc(_join(control, payload, now)),
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
            return (_record_to_doc(_join(control, payload, now)),
                    CreateOrGetOutcome.CREATED)

        admission_lock = await self._retain_admission_lock(p_hash)
        try:
            async with admission_lock:
                doc, outcome = await self.backend.transact(_txn)
        finally:
            await self._release_admission_lock(p_hash, admission_lock)
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
        observed_at = utcnow()
        live_payload = _live_payload(payload, observed_at)
        return (
            _join(control, live_payload, observed_at),
            live_payload is not None,
        )

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
        async def _txn(view: TransactionView) -> tuple[Document, bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            now = utcnow()
            live_payload = _live_payload(payload, now)
            record = _join(control, live_payload, now)
            if live_payload is None and record.state not in TERMINAL_STATES:
                terminal = await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    payload,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                return terminal, True
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
            if "forusbots_job_ids" in updates:
                proposed_ids = updates["forusbots_job_ids"]
                if not isinstance(proposed_ids, list):
                    raise TicketJobError(
                        "forusbots_job_ids debe ser una lista"
                    )
                # External receipts are monotonic effect evidence.  A worker
                # can terminalize from a snapshot taken just before a late
                # submit observer commits, so replacement here would erase
                # the only top-level reconciliation handle.
                merged_ids = list(record.forusbots_job_ids)
                for proposed_id in proposed_ids:
                    valid_id = _validated_external_job_id(proposed_id)
                    if valid_id not in merged_ids:
                        merged_ids.append(valid_id)
                updates["forusbots_job_ids"] = merged_ids
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
            # Un payload lógica/físicamente expirado NO se recrea, incluso si
            # el caller intentó introducir nuevos cambios de payload.
            if live_payload is not None:
                view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            elif payload is not None:
                view.delete(PAYLOADS_COLLECTION, job_id)
            return _record_to_doc(
                _join(new_control, new_payload, now)
            ), False

        doc, payload_expired = await self.backend.transact(_txn)
        if payload_expired:
            raise StaleLeaseEpoch(
                f"job {job_id}: payload expirado o ausente"
            )
        return _doc_to_record(doc)

    async def record_inquiry_result(self, job_id: str, index: int,
                                    entry: Dict[str, Any],
                                    *, lease_epoch: Optional[int] = None
                                    ) -> TicketJobRecord:
        """Checkpoint por inquiry: persiste inmediatamente (HT-08). Con
        ``lease_epoch`` la escritura es condicional: un worker fenced no
        puede checkpointear (Tarea 6 Paso 4a)."""

        async def _txn(
            view: TransactionView,
        ) -> tuple[Optional[Document], bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            now = utcnow()
            live_payload = _live_payload(payload, now)
            record = _join(control, live_payload, now)
            if live_payload is None:
                if record.state not in TERMINAL_STATES:
                    terminal = await self._stage_terminalization(
                        view,
                        job_id,
                        record,
                        payload,
                        state=TicketJobState.FAILED,
                        next_action=NextAction.USE_LEGACY_OR_HUMAN,
                        public_error_code="EXPIRED_PAYLOAD",
                        retryable=False,
                        current_step="done",
                        now=now,
                    )
                    return terminal, True
                return None, True
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
                if record.lease_owner is None \
                        or record.lease_expires_at is None \
                        or now >= record.lease_expires_at:
                    raise StaleLeaseEpoch(
                        f"job {job_id}: lease vencido o sin owner"
                    )
            merged = build_validated_inquiry_checkpoint(record, index, entry)
            new_control, new_payload = split_record(merged)
            if record.state in TERMINAL_STATES and control.get("expires_at"):
                new_control["expires_at"] = control["expires_at"]
            view.set(JOBS_COLLECTION, job_id, new_control)
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return _record_to_doc(
                _join(new_control, new_payload, now)
            ), False

        doc, payload_expired = await self.backend.transact(_txn)
        if payload_expired or doc is None:
            raise StaleLeaseEpoch(
                f"job {job_id}: payload expirado o ausente"
            )
        return _doc_to_record(doc)

    async def prepare_forusbots_operation(
        self,
        job_id: str,
        index: int,
        *,
        operation: str,
        request_fingerprint: str,
        worker_id: str,
        lease_epoch: int,
        route: str,
        idempotency_contract: Optional[str] = None,
    ) -> ForusBotsOperationDecision:
        """Atomically decide submit, resume, or manual reconciliation."""
        if operation not in {"participant", "plan"}:
            raise TicketJobError("operación ForUsBots inválida")
        if idempotency_contract not in {
            None,
            FORUSBOTS_IDEMPOTENCY_CONTRACT,
        }:
            raise TicketJobError("contrato idempotente ForUsBots inválido")
        if (
            not isinstance(request_fingerprint, str)
            or len(request_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in request_fingerprint)
        ):
            raise TicketJobError("fingerprint ForUsBots inválido")

        async def _txn(
            view: TransactionView,
        ) -> tuple[ForusBotsOperationDecision, bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            now = utcnow()
            live_payload = _live_payload(payload, now)
            if live_payload is None:
                return ForusBotsOperationDecision("reconcile"), True
            record = _join(control, live_payload, now)
            if record.state in TERMINAL_STATES \
                    or record.lease_epoch != lease_epoch \
                    or record.lease_owner != worker_id \
                    or record.lease_expires_at is None \
                    or now >= record.lease_expires_at:
                raise StaleLeaseEpoch(
                    f"job {job_id}: lease perdido antes de ForUsBots"
                )

            existing = next(
                (entry for entry in record.per_inquiry_status
                 if entry.get("index") == index),
                None,
            )
            intents = list((existing or {}).get("forusbots_submit_intents") or [])
            external_jobs = list(
                (existing or {}).get("forusbots_external_jobs") or []
            )
            external = next(
                (item for item in external_jobs
                 if isinstance(item, dict)
                 and item.get("operation") == operation),
                None,
            )
            matching_intent = next(
                (intent for intent in intents
                 if isinstance(intent, dict)
                 and intent.get("operation") in {"all", operation}),
                None,
            )

            def persist_intents(
                updated_intents: list[dict[str, Any]],
            ) -> None:
                checkpoint = {
                    **(existing or {}),
                    "index": index,
                    "route": route,
                    "execution_status": "running",
                    "participant_reply_safe": False,
                    "forusbots_submit_intent": True,
                    "forusbots_submit_intent_epoch": lease_epoch,
                    "forusbots_submit_intent_worker": worker_id,
                    "forusbots_submit_intents": updated_intents,
                }
                statuses = [
                    entry for entry in record.per_inquiry_status
                    if entry.get("index") != index
                ]
                statuses.append(checkpoint)
                statuses.sort(key=lambda entry: entry.get("index", 0))
                merged = record.model_copy(update={
                    "per_inquiry_status": statuses,
                    "updated_at": now,
                })
                new_control, new_payload = split_record(merged)
                view.set(JOBS_COLLECTION, job_id, new_control)
                view.set(PAYLOADS_COLLECTION, job_id, new_payload)

            if matching_intent is not None:
                stored_fingerprint = matching_intent.get("request_fingerprint")
                if stored_fingerprint not in {None, request_fingerprint}:
                    return ForusBotsOperationDecision("reconcile"), False
                if external is not None:
                    external_id = external.get("job_id")
                    if isinstance(external_id, str) and external_id:
                        return ForusBotsOperationDecision(
                            "resume", external_id,
                        ), False
                if (
                    idempotency_contract != FORUSBOTS_IDEMPOTENCY_CONTRACT
                    or matching_intent.get("idempotency_contract")
                    != idempotency_contract
                    or matching_intent.get("operation") != operation
                ):
                    return ForusBotsOperationDecision("reconcile"), False
                refreshed_intents = [
                    {
                        **intent,
                        "lease_epoch": lease_epoch,
                        "worker_id": worker_id,
                    }
                    if intent is matching_intent else intent
                    for intent in intents
                ]
                persist_intents(refreshed_intents)
                return ForusBotsOperationDecision("submit"), False

            if existing and existing.get("forusbots_submit_intent") is True \
                    and not intents:
                # A singular legacy intent cannot be assigned safely to one
                # of the two possible operations.
                return ForusBotsOperationDecision("reconcile"), False
            if external is not None:
                # Effect evidence without a matching reservation is corrupt;
                # never poll or submit automatically.
                return ForusBotsOperationDecision("reconcile"), False

            new_intent: Dict[str, Any] = {
                "operation": operation,
                "lease_epoch": lease_epoch,
                "worker_id": worker_id,
                "request_fingerprint": request_fingerprint,
            }
            if idempotency_contract is not None:
                new_intent["idempotency_contract"] = idempotency_contract
            intents.append(new_intent)
            persist_intents(intents)
            return ForusBotsOperationDecision("submit"), False

        decision, payload_expired = await self.backend.transact(_txn)
        if payload_expired:
            raise StaleLeaseEpoch(
                f"job {job_id}: payload expirado o ausente"
            )
        return decision

    async def reserve_forusbots_submit_intent(
        self,
        job_id: str,
        index: int,
        *,
        worker_id: str,
        lease_epoch: int,
        route: str,
        operation: Optional[str] = None,
    ) -> bool:
        """Persiste el intent ANTES del primer POST de una inquiry.

        Devuelve ``True`` sólo al primer owner/epoch que lo reserva. Si un
        intent ya existe devuelve ``False`` y nunca lo borra: sin contrato de
        idempotencia/reconciliación upstream no se puede distinguir entre un
        POST que no salió y uno que creó un job cuya respuesta se perdió.

        La comprobación del lease y la escritura del intent comparten la
        transacción, tanto en memoria como en Firestore.
        """

        normalized_operation = operation or "all"
        if normalized_operation not in {"all", "participant", "plan"}:
            raise TicketJobError("operación ForUsBots inválida")

        async def _txn(view: TransactionView) -> tuple[bool, bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            now = utcnow()
            live_payload = _live_payload(payload, now)
            record = _join(control, live_payload, now)
            if live_payload is None:
                if record.state not in TERMINAL_STATES:
                    await self._stage_terminalization(
                        view,
                        job_id,
                        record,
                        payload,
                        state=TicketJobState.FAILED,
                        next_action=NextAction.USE_LEGACY_OR_HUMAN,
                        public_error_code="EXPIRED_PAYLOAD",
                        retryable=False,
                        current_step="done",
                        now=now,
                    )
                return False, True
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
            existing_intents = list(
                (existing or {}).get("forusbots_submit_intents") or []
            )
            if existing and existing.get("forusbots_submit_intent") is True \
                    and not existing_intents:
                # Legacy intent without operation: the POST outcome is
                # ambiguous, so no operation may be submitted again.
                return False, False
            if any(
                isinstance(intent, dict)
                and intent.get("operation") in {
                    "all", normalized_operation,
                }
                for intent in existing_intents
            ) or (normalized_operation == "all" and existing_intents):
                return False, False

            existing_intents.append({
                "operation": normalized_operation,
                "lease_epoch": lease_epoch,
                "worker_id": worker_id,
            })

            statuses = [
                entry for entry in record.per_inquiry_status
                if entry.get("index") != index
            ]
            checkpoint = {
                **(existing or {}),
                "index": index,
                "route": route,
                "execution_status": "running",
                "participant_reply_safe": False,
                "forusbots_submit_intent": True,
                "forusbots_submit_intent_epoch": lease_epoch,
                "forusbots_submit_intent_worker": worker_id,
                "forusbots_submit_intents": existing_intents,
            }
            statuses.append(checkpoint)
            statuses.sort(key=lambda entry: entry.get("index", 0))
            merged = record.model_copy(update={
                "per_inquiry_status": statuses,
                "updated_at": now,
            })
            new_control, new_payload = split_record(merged)
            view.set(JOBS_COLLECTION, job_id, new_control)
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return True, False

        reserved, payload_expired = await self.backend.transact(_txn)
        if payload_expired:
            raise StaleLeaseEpoch(
                f"job {job_id}: payload expirado o ausente"
            )
        return reserved

    async def record_forusbots_external_job(
        self,
        job_id: str,
        index: int,
        *,
        operation: str,
        external_job_id: str,
        worker_id: str,
        lease_epoch: int,
    ) -> TicketJobRecord:
        """Persist a confirmed upstream job ID as monotonic effect evidence.

        This receipt runs immediately after the upstream ``202``.  It binds to
        the durable pre-submit reservation, worker and epoch, but deliberately
        does not require the lease to remain live: lease expiry in the narrow
        post-submit race must not discard the only handle that can reconcile
        the already-created external job.
        """
        if operation not in {"participant", "plan"}:
            raise TicketJobError("operación ForUsBots inválida")
        external_job_id = _validated_external_job_id(external_job_id)

        async def _txn(view: TransactionView) -> tuple[Optional[Document], bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            now = utcnow()
            live_payload = _live_payload(payload, now)
            if live_payload is None:
                return None, True
            record = _join(control, live_payload, now)
            existing = next(
                (entry for entry in record.per_inquiry_status
                 if entry.get("index") == index),
                None,
            )
            if existing is None:
                raise TicketJobError(
                    "falta la reserva durable previa de ForUsBots"
                )

            intents = list(existing.get("forusbots_submit_intents") or [])
            reservation_matches = any(
                isinstance(intent, dict)
                and intent.get("operation") in {"all", operation}
                and intent.get("lease_epoch") == lease_epoch
                and intent.get("worker_id") == worker_id
                for intent in intents
            )
            if not reservation_matches:
                reservation_matches = (
                    not intents
                    and existing.get("forusbots_submit_intent") is True
                    and existing.get("forusbots_submit_intent_epoch") == lease_epoch
                    and existing.get("forusbots_submit_intent_worker") == worker_id
                )
            if not reservation_matches:
                raise StaleLeaseEpoch(
                    "el recibo ForUsBots no coincide con su reserva durable"
                )

            external_jobs = list(existing.get("forusbots_external_jobs") or [])
            current_for_operation = next(
                (item for item in external_jobs
                 if isinstance(item, dict)
                 and item.get("operation") == operation),
                None,
            )
            if current_for_operation is not None:
                if current_for_operation.get("job_id") != external_job_id:
                    raise TicketJobError(
                        "conflicto de identificador ForUsBots para la operación"
                    )
            else:
                external_jobs.append({
                    "operation": operation,
                    "job_id": external_job_id,
                })

            checkpoint = {
                **existing,
                "forusbots_external_jobs": external_jobs,
            }
            statuses = [
                entry for entry in record.per_inquiry_status
                if entry.get("index") != index
            ]
            statuses.append(checkpoint)
            statuses.sort(key=lambda entry: entry.get("index", 0))

            job_ids = list(record.forusbots_job_ids)
            if external_job_id not in job_ids:
                job_ids.append(external_job_id)
            merged = record.model_copy(update={
                "per_inquiry_status": statuses,
                "forusbots_job_ids": job_ids,
                "updated_at": now,
            })
            new_control, new_payload = split_record(merged)
            if record.state in TERMINAL_STATES and control.get("expires_at"):
                new_control["expires_at"] = control["expires_at"]
            view.set(JOBS_COLLECTION, job_id, new_control)
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
            return _record_to_doc(_join(new_control, new_payload, now)), False

        doc, payload_expired = await self.backend.transact(_txn)
        if payload_expired or doc is None:
            raise StaleLeaseEpoch(
                f"job {job_id}: payload expirado o ausente"
            )
        return _doc_to_record(doc)

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
        payload = await self.backend.get_doc(PAYLOADS_COLLECTION, job_id)
        if _live_payload(payload) is None:
            raise StaleLeaseEpoch(f"job {job_id}: payload expirado o ausente")
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

        async def _txn(view: TransactionView) -> Optional[int]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return None
            record = _doc_to_record(control)
            now = utcnow()
            if record.state in TERMINAL_STATES:
                return None
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            live_payload = _live_payload(payload, now)
            if live_payload is None:
                await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    None,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                if payload is not None:
                    view.delete(PAYLOADS_COLLECTION, job_id)
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

        async def _txn(view: TransactionView) -> bool:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return False
            record = _doc_to_record(control)
            now = utcnow()
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            live_payload = _live_payload(payload, now)
            if record.state not in TERMINAL_STATES \
                    and live_payload is None:
                await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    None,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                if payload is not None:
                    view.delete(PAYLOADS_COLLECTION, job_id)
                return False
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

        async def _txn(view: TransactionView) -> Document:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(control)
            now = utcnow()
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            live_payload = _live_payload(payload, now)
            if record.state not in TERMINAL_STATES \
                    and live_payload is None:
                terminal = await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    None,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                if payload is not None:
                    view.delete(PAYLOADS_COLLECTION, job_id)
                return terminal
            if record.enqueue_generation != expected_generation:
                raise StaleEnqueueGeneration(
                    f"generación de enqueue actual {record.enqueue_generation} "
                    f"!= esperada {expected_generation}"
                )
            control["enqueue_state"] = "enqueued"
            control["task_name"] = task_name
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return _record_to_doc(_join(control, live_payload, now))

        return _doc_to_record(await self.backend.transact(_txn))

    async def bump_enqueue_generation(
        self,
        job_id: str,
        *,
        expected_generation: Optional[int] = None,
        expected_state: Optional[TicketJobState] = None,
    ) -> int:
        """Incremento TRANSACCIONAL de la generación de enqueue (Tarea 7
        Paso 3): tras una tombstone o un requeue administrativo, el nombre
        de task anterior queda quemado y sólo la generación nueva ejecuta."""

        async def _txn(view: TransactionView) -> tuple[Optional[int], bool]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            record = _doc_to_record(control)
            if record.state in TERMINAL_STATES:
                raise TicketJobError(
                    f"job {job_id} es terminal: no se re-encola")
            if expected_generation is not None \
                    and record.enqueue_generation != expected_generation:
                raise StaleEnqueueGeneration(
                    f"job {job_id}: generación cambió antes del requeue"
                )
            if expected_state is not None and record.state != expected_state:
                raise StaleEnqueueGeneration(
                    f"job {job_id}: estado cambió antes del requeue"
                )
            now = utcnow()
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            if _live_payload(payload, now) is None:
                await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    None,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                if payload is not None:
                    view.delete(PAYLOADS_COLLECTION, job_id)
                return None, True
            new_generation = record.enqueue_generation + 1
            control["enqueue_generation"] = new_generation
            control["enqueue_state"] = "pending"
            control["updated_at"] = now
            view.set(JOBS_COLLECTION, job_id, control)
            return new_generation, False

        generation, payload_expired = await self.backend.transact(_txn)
        if payload_expired or generation is None:
            raise TicketJobError(
                f"job {job_id}: payload expirado; no se re-encola"
            )
        return generation

    async def acquire_recovery_lock(self, job_id: str, *, owner: str,
                                    lock_s: float = 120.0) -> bool:
        """Lock del RECONCILIADOR, separado del lease de ejecución del worker
        (Tarea 7 Paso 5): reparar outbox/leases sin poseer el lease que debe
        reclamar el worker. Tolera dos reconciliadores concurrentes."""

        async def _txn(view: TransactionView) -> bool:
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

        async def _txn(view: TransactionView) -> Optional[int]:
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
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            if _live_payload(payload, now) is None:
                await self._stage_terminalization(
                    view,
                    job_id,
                    record,
                    None,
                    state=TicketJobState.FAILED,
                    next_action=NextAction.USE_LEGACY_OR_HUMAN,
                    public_error_code="EXPIRED_PAYLOAD",
                    retryable=False,
                    current_step="done",
                    now=now,
                )
                if payload is not None:
                    view.delete(PAYLOADS_COLLECTION, job_id)
                return None
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
            return int(control["enqueue_generation"])

        return await self.backend.transact(_txn)

    async def _stage_terminalization(
        self,
        view: TransactionView,
        job_id: str,
        record: TicketJobRecord,
        payload: Optional[Document],
        *,
        state: TicketJobState,
        next_action: Any,
        public_error_code: str,
        retryable: bool,
        current_step: str,
        now: datetime,
    ) -> Document:
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
        live_payload = _live_payload(payload, now)
        if live_payload is not None:
            view.set(PAYLOADS_COLLECTION, job_id, new_payload)
        elif payload is not None:
            view.delete(PAYLOADS_COLLECTION, job_id)
        return _record_to_doc(_join(new_control, new_payload, now))

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

        async def _txn(view: TransactionView) -> Optional[Document]:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                return None
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            live_payload = _live_payload(payload, observed_at)
            record = _join(control, live_payload, observed_at)
            if record.state in TERMINAL_STATES:
                return _record_to_doc(record)

            if record.job_deadline_at is not None \
                    and record.job_deadline_at <= observed_at:
                state = TicketJobState.TIMEOUT
                code = PublicErrorCode.TOTAL_JOB_TIMEOUT.value
            elif live_payload is None:
                state = TicketJobState.FAILED
                code = "EXPIRED_PAYLOAD"
            else:
                return _record_to_doc(record)

            terminal = await self._stage_terminalization(
                view,
                job_id,
                record,
                live_payload,
                state=state,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=code,
                retryable=False,
                current_step="done",
                now=observed_at,
            )
            if payload is not None and live_payload is None:
                view.delete(PAYLOADS_COLLECTION, job_id)
            return terminal

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

        async def _txn(view: TransactionView) -> Document:
            control = await view.get(JOBS_COLLECTION, job_id)
            if control is None:
                raise JobNotFound(job_id)
            payload = await view.get(PAYLOADS_COLLECTION, job_id)
            live_payload = _live_payload(payload, observed_at)
            record = _join(control, live_payload, observed_at)
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
            if require_payload_missing and live_payload is not None:
                raise StaleLeaseEpoch(
                    f"job {job_id}: payload reapareció desde el scan"
                )

            terminal = await self._stage_terminalization(
                view,
                job_id,
                record,
                live_payload,
                state=state,
                next_action=next_action,
                public_error_code=public_error_code,
                retryable=retryable,
                current_step=current_step,
                now=now,
            )
            if payload is not None and live_payload is None:
                view.delete(PAYLOADS_COLLECTION, job_id)
            return terminal

        return _doc_to_record(await self.backend.transact(_txn))

    async def scan_control_docs(self, limit: int = 100) -> ScanPage:
        """Página activa ordenada por ID con cursor durable y CAS.

        Un Run Job nuevo continúa después de la página examinada por la
        ejecución anterior. Al llegar al final vuelve al principio; jobs
        saludables o temporalmente lockeados no pueden ocupar para siempre el
        primer ``limit`` y ocultar reparaciones posteriores.
        """
        cursor_doc = await self.backend.get_doc(
            RECONCILER_STATE_COLLECTION, _ACTIVE_SCAN_CURSOR_ID,
        )
        cursor = (cursor_doc or {}).get("last_job_id")
        docs = await self.backend.scan_collection(
            JOBS_COLLECTION,
            limit,
            states=[TicketJobState.QUEUED.value, TicketJobState.RUNNING.value],
            start_after=cursor,
        )
        if not docs and cursor is not None:
            docs = await self.backend.scan_collection(
                JOBS_COLLECTION,
                limit,
                states=[
                    TicketJobState.QUEUED.value,
                    TicketJobState.RUNNING.value,
                ],
            )

        next_cursor = docs[-1][0] if docs else None

        async def _advance_cursor(view: TransactionView) -> None:
            current = await view.get(
                RECONCILER_STATE_COLLECTION, _ACTIVE_SCAN_CURSOR_ID,
            )
            if (current or {}).get("last_job_id") != cursor:
                return
            view.set(
                RECONCILER_STATE_COLLECTION,
                _ACTIVE_SCAN_CURSOR_ID,
                {"last_job_id": next_cursor, "updated_at": utcnow()},
            )

        if next_cursor != cursor:
            await self.backend.transact(_advance_cursor)
        return docs

    async def count_active(self, principal_id: str) -> int:
        """Jobs no-terminales del principal desde el contador transaccional
        (Tarea 5 Paso 2): la verdad durable, no un count() no atómico."""
        counter = await self.backend.get_doc(
            COUNTERS_COLLECTION, principal_hash(principal_id))
        return int((counter or {}).get("active_jobs", 0))

    async def active_job_stats(self) -> tuple[int, Optional[datetime]]:
        """Exact global active count and oldest creation timestamp.

        This is intentionally a backend aggregation/query, not a reconciler
        page statistic: the durable cursor scans only one bounded page per run
        and therefore cannot produce a truthful global gauge on its own.
        """
        return await self.backend.active_job_stats(
            JOBS_COLLECTION,
            [TicketJobState.QUEUED.value, TicketJobState.RUNNING.value],
        )
