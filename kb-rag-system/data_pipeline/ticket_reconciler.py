"""
Reconciliador automático del outbox Firestore→Cloud Tasks y de leases
vencidos (plan de finalización, Tarea 7 Paso 5). Se ejecuta con
``APP_ROLE=reconciler`` cada minuto vía Cloud Scheduler→Cloud Run Job:

    python -m data_pipeline.ticket_reconciler --once --batch-size=25

Responsabilidades (todas idempotentes y tolerantes a dos reconciliadores
concurrentes gracias al recovery lock por job — separado del lease de
ejecución que debe reclamar el worker):

1. re-enqueue por generación de outbox ``pending``;
2. lease vencido → fencear al worker viejo (epoch+1), running→queued,
   generación nueva;
3. terminalizar jobs sin recuperación posible y liberar su slot exactamente
   una vez (lo garantiza el repositorio);
4. terminalizar ``job_deadline_at`` vencido o payload ausente sin recrear
   efectos (las tasks tardías reciben 2xx del worker por generación stale);
5. emitir métricas sanitizadas (conteos, jamás payloads).

El exit code es 0 sólo si el lote se completó o no había trabajo. El batch
size (25) es configuración declarada y probada para ambos entornos; cambiarlo
exige plan/revisión de capacidad. La CLI (scripts/requeue_ticket_job.py)
queda reservada para incidentes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Callable, Dict, Optional, Protocol, Sequence

from api import metrics as ticket_metrics

from data_pipeline.ticket_job_models import (
    TERMINAL_STATES,
    NextAction,
    PublicErrorCode,
    TicketJobState,
    utcnow,
)
from data_pipeline.ticket_job_repository import (
    InvalidStateTransition,
    JobNotFound,
    Document,
    StaleEnqueueGeneration,
    StaleLeaseEpoch,
    TicketJobRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 25
ENQUEUED_RECHECK_AFTER_S = 60.0


class ReconcilerQueue(Protocol):
    async def ensure_enqueued(self, job_id: str, generation: int = 0) -> str: ...

    async def task_exists(self, job_id: str, generation: int = 0) -> bool: ...

    async def aclose(self) -> None: ...


class TicketReconciler:

    def __init__(
        self,
        repo: TicketJobRepository,
        queue: ReconcilerQueue,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        owner: Optional[str] = None,
        metrics_hook: Optional[Callable[..., None]] = None,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.batch_size = batch_size
        self.owner = owner or f"reconciler-{uuid.uuid4().hex[:10]}"
        self._metrics_hook = metrics_hook

    def _metric(self, name: str, **labels: int) -> None:
        if self._metrics_hook is not None:
            try:
                self._metrics_hook(name, **labels)
            except Exception:  # noqa: BLE001 - métricas jamás rompen reparación
                logger.error("metrics hook falló")
        for reason, value in labels.items():
            try:
                ticket_metrics.emit(
                    "ticket_reconciler_count", value, reason=reason
                )
            except (TypeError, ValueError):
                logger.error("reconciler metric rejected by telemetry schema")

    async def _emit_active_gauges(self, observed_at: datetime) -> None:
        """Emit exact post-reconciliation global gauges.

        The bounded scan page is never used as a proxy for global state.  The
        repository performs a count aggregation plus an oldest-record query.
        """
        try:
            active, oldest_created_at = await self.repo.active_job_stats()
            oldest_age_s = 0.0
            if oldest_created_at is not None:
                oldest_age_s = max(
                    0.0, (observed_at - oldest_created_at).total_seconds()
                )
            ticket_metrics.emit("ticket_jobs_active", active)
            ticket_metrics.emit(
                "ticket_jobs_oldest_age_seconds", oldest_age_s
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - observability never blocks repair
            logger.error("active job gauge collection failed")

    async def run_once(self) -> Dict[str, int]:
        """Un lote acotado. Devuelve conteos sanitizados por categoría."""
        counts = {"scanned": 0, "requeued_outbox": 0, "fenced_leases": 0,
                  "deadline_terminalized": 0, "payload_expired": 0,
                  "skipped_locked": 0, "errors": 0}
        docs = await self.repo.scan_control_docs(limit=self.batch_size)
        now = utcnow()
        for job_id, control in docs:
            counts["scanned"] += 1
            try:
                state = control.get("state")
                if state in {s.value for s in TERMINAL_STATES}:
                    continue
                if not await self.repo.acquire_recovery_lock(
                        job_id, owner=self.owner):
                    counts["skipped_locked"] += 1
                    continue

                # 4a) deadline ABSOLUTO vencido → terminaliza sin efectos
                deadline = control.get("job_deadline_at")
                if deadline is not None and now > deadline:
                    await self._terminalize(
                        job_id, TicketJobState.TIMEOUT,
                        PublicErrorCode.TOTAL_JOB_TIMEOUT.value,
                        control=control,
                        observed_at=now,
                        expected_deadline_at=deadline,
                    )
                    counts["deadline_terminalized"] += 1
                    continue

                # 4b) payload ausente no terminal → expired_payload, libera y
                # NO reejecuta (no queda nada que ejecutar)
                _record, payload_present = (
                    await self.repo.get_with_payload_state(job_id)
                )
                if not payload_present:
                    await self._terminalize(
                        job_id, TicketJobState.FAILED, "EXPIRED_PAYLOAD",
                        control=control,
                        observed_at=now,
                        require_payload_missing=True,
                    )
                    counts["payload_expired"] += 1
                    continue

                # 2) lease vencido → fence + requeue con generación nueva
                lease_expiry = control.get("lease_expires_at")
                if state == TicketJobState.RUNNING.value \
                        and lease_expiry is not None and now > lease_expiry:
                    generation = await self.repo.fence_and_requeue(
                        job_id,
                        recovery_owner=self.owner,
                        expected_lease_epoch=control.get("lease_epoch", 0),
                        expected_lease_expires_at=lease_expiry,
                        observed_at=now,
                    )
                    if generation is not None:
                        name = await self.queue.ensure_enqueued(
                            job_id, generation)
                        await self.repo.mark_enqueued(
                            job_id, name, expected_generation=generation,
                        )
                        counts["fenced_leases"] += 1
                    continue

                # 1) outbox pending → re-enqueue por generación
                if control.get("enqueue_state") == "pending" \
                        and state == TicketJobState.QUEUED.value:
                    generation = control.get("enqueue_generation", 0)
                    name = await self.queue.ensure_enqueued(job_id, generation)
                    await self.repo.mark_enqueued(
                        job_id, name, expected_generation=generation,
                    )
                    counts["requeued_outbox"] += 1
                    continue

                # A task confirmed in the past can later be ACKed/deleted or
                # exhaust retries before it ever claims the job. Recheck only
                # stale QUEUED records, bounded by this page. A live task is
                # read-only; genuine NotFound burns a new generation by CAS.
                if control.get("enqueue_state") == "enqueued" \
                        and state == TicketJobState.QUEUED.value:
                    updated_at = control.get("updated_at")
                    try:
                        stale_for_s = (
                            (now - updated_at).total_seconds()
                            if isinstance(updated_at, datetime)
                            else ENQUEUED_RECHECK_AFTER_S
                        )
                    except TypeError:
                        stale_for_s = ENQUEUED_RECHECK_AFTER_S
                    if stale_for_s < ENQUEUED_RECHECK_AFTER_S:
                        continue
                    generation = control.get("enqueue_generation", 0)
                    if await self.queue.task_exists(job_id, generation):
                        continue
                    generation = await self.repo.bump_enqueue_generation(
                        job_id,
                        expected_generation=generation,
                        expected_state=TicketJobState.QUEUED,
                    )
                    name = await self.queue.ensure_enqueued(job_id, generation)
                    await self.repo.mark_enqueued(
                        job_id, name, expected_generation=generation,
                    )
                    counts["requeued_outbox"] += 1
            except (JobNotFound, InvalidStateTransition, StaleLeaseEpoch,
                    StaleEnqueueGeneration):
                # otro reconciliador/worker llegó primero: benigno
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
                logger.error("reconciler falló reparando un ticket job")
        self._metric("ticket_reconciler_run", **counts)
        await self._emit_active_gauges(utcnow())
        return counts

    async def _terminalize(
        self,
        job_id: str,
        state: TicketJobState,
        code: str,
        *,
        control: Document,
        observed_at: datetime,
        expected_deadline_at: Optional[datetime] = None,
        require_payload_missing: bool = False,
    ) -> None:
        expected_state = control.get("state")
        expected_epoch = control.get("lease_epoch", 0)
        if not isinstance(expected_state, str):
            raise StaleLeaseEpoch(
                f"job {job_id}: control sin estado válido"
            )
        if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int):
            raise StaleLeaseEpoch(
                f"job {job_id}: control sin lease_epoch válido"
            )
        await self.repo.terminalize_recovery(
            job_id,
            state=state,
            recovery_owner=self.owner,
            expected_state=expected_state,
            expected_lease_epoch=expected_epoch,
            next_action=NextAction.USE_LEGACY_OR_HUMAN,
            public_error_code=code,
            retryable=False,
            current_step="done",
            observed_at=observed_at,
            expected_deadline_at=expected_deadline_at,
            require_payload_missing=require_payload_missing,
        )


def _build_from_settings() -> tuple[TicketJobRepository, ReconcilerQueue]:
    """Construcción para el Run Job batch (APP_ROLE=reconciler). No inicia
    Uvicorn ni sirve endpoints."""
    from api.config import settings, validate_settings

    validate_settings()
    if settings.APP_ROLE != "reconciler":
        raise SystemExit("el entrypoint batch exige APP_ROLE=reconciler")

    from data_pipeline.ticket_job_repository import (
        FirestoreTicketJobBackend,
        TicketJobRepository,
    )
    from data_pipeline.ticket_task_queue import CloudTasksTicketQueue

    repo = TicketJobRepository(
        FirestoreTicketJobBackend(
            project=settings.GCP_PROJECT or None,
            collection_prefix=settings.FIRESTORE_TICKET_COLLECTION_PREFIX,
            database=settings.FIRESTORE_DATABASE,
        ),
        retention_days=settings.TICKET_IDEMPOTENCY_RETENTION_DAYS,
        max_outstanding=settings.TICKET_MAX_OUTSTANDING_JOBS,
        rate_limit_per_minute=settings.RATE_LIMIT_HANDLE_TICKET,
    )
    queue = CloudTasksTicketQueue(
        project=settings.GCP_PROJECT,
        location=settings.CLOUD_TASKS_LOCATION,
        queue=settings.CLOUD_TASKS_QUEUE,
        worker_url=settings.TICKET_WORKER_URL,
        worker_audience=settings.TICKET_WORKER_AUDIENCE,
        service_account=settings.TICKET_WORKER_SERVICE_ACCOUNT,
        dispatch_deadline_s=settings.TICKET_TASK_DISPATCH_DEADLINE_S,
        generation_bumper=None,
    )
    queue._generation_bumper = repo.bump_enqueue_generation
    return repo, queue


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconciliador batch de ticket jobs (Cloud Run Job)")
    parser.add_argument("--once", action="store_true", required=True,
                        help="ejecuta exactamente un lote y termina")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    repo, queue = _build_from_settings()
    reconciler = TicketReconciler(repo, queue, batch_size=args.batch_size)

    async def _run() -> int:
        try:
            counts = await reconciler.run_once()
            # exit 0 sólo si completó el lote o no había trabajo
            return 0 if counts["errors"] == 0 else 1
        finally:
            await queue.aclose()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
