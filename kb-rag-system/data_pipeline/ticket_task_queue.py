"""
Cola durable de ejecución de ticket jobs (plan de finalización, Tarea 7).

El productor confirma el par record+task antes de responder 202. Firestore y
Cloud Tasks no comparten transacción, así que el nombre de task es
DETERMINÍSTICO por job y GENERACIÓN: ``ticket-{job_id}-g{generation}``.

Semántica de ``AlreadyExists`` (bloqueo 7 del plan): el nombre repetido NO
prueba que la task viva — Cloud Tasks conserva una tombstone tras completar/
eliminar una task. La cola sonda ``get_task``:

- encontrada  → replay activo, éxito benigno;
- NotFound    → tombstone: se incrementa la generación (transaccionalmente si
  hay bumper del repositorio) y se crea el nombre NUEVO;
- otro error  → se propaga: el productor deja ``enqueue_state=pending`` y
  responde 503, nunca un 202 falso.

Cada task lleva ``dispatch_deadline`` EXPLÍCITO (540s) y un body autenticado
por OIDC ``{job_id, enqueue_generation}`` que el worker compara antes del
lease. El rol IAM queda limitado a tasks.create/tasks.get/queues.get: este
módulo no usa operaciones admin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, cast

logger = logging.getLogger(__name__)


class TicketQueueError(Exception):
    pass


class TicketQueueEstimationError(TicketQueueError):
    """Queue capacity cannot be read safely, so new admission must stop."""


def task_name_for_job(project: str, location: str, queue: str, job_id: str,
                      generation: int = 0) -> str:
    return (
        f"projects/{project}/locations/{location}/queues/{queue}"
        f"/tasks/ticket-{job_id}-g{generation}"
    )


class CloudTasksTicketQueue:
    """Encola el job hacia el worker HTTP con OIDC. Thin wrapper: la lógica
    de reintentos/backoff/dispatch vive en la cola de Cloud Tasks."""

    def __init__(
        self,
        *,
        project: str,
        location: str,
        queue: str,
        worker_url: str,
        service_account: str,
        worker_audience: Optional[str] = None,
        dispatch_deadline_s: int = 540,
        generation_bumper: Optional[Callable[[str], Awaitable[int]]] = None,
    ) -> None:
        from google.cloud import tasks_v2, tasks_v2beta3  # import perezoso

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksAsyncClient()
        # La API v2 GA de GetQueue no expone Queue.stats ni acepta read_mask.
        # v2beta3 sí expone esos dos campos con el mismo permiso
        # cloudtasks.queues.get. El SDK está fijado por requirements.lock;
        # cualquier retirada/cambio de esa superficie hace que admisión falle
        # cerrada en vez de aceptar trabajo con una estimación inventada.
        self._stats_tasks_v2beta3 = tasks_v2beta3
        self._stats_client = tasks_v2beta3.CloudTasksAsyncClient()
        self._project = project
        self._location = location
        self._queue = queue
        self._worker_url = worker_url.rstrip("/")
        self._worker_audience = (worker_audience or worker_url).rstrip("/")
        self._service_account = service_account
        self._dispatch_deadline_s = dispatch_deadline_s
        # bump transaccional de la generación en el repositorio; sin bumper
        # (reconciler/CLI lo inyectan) se usa generation+1 y el caller debe
        # persistir la nueva generación.
        self._generation_bumper = generation_bumper

    def _build_task(self, job_id: str, generation: int) -> Any:
        from google.protobuf import duration_pb2

        tasks_v2 = self._tasks_v2
        name = task_name_for_job(self._project, self._location, self._queue,
                                 job_id, generation)
        return tasks_v2.Task(
            name=name,
            # deadline de despacho EXPLÍCITO (Tarea 7 Paso 2): coherente con
            # lease(90s)/heartbeat(30s)/presupuesto de intento(480s) < 540s
            dispatch_deadline=duration_pb2.Duration(
                seconds=self._dispatch_deadline_s),
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self._worker_url}/internal/tasks/ticket-job",
                headers={"Content-Type": "application/json"},
                body=(f'{{"job_id": "{job_id}", '
                      f'"enqueue_generation": {generation}}}').encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._service_account,
                    audience=self._worker_audience,
                ),
            ),
        )

    async def ensure_enqueued(self, job_id: str, generation: int = 0) -> str:
        """Idempotente por nombre determinístico + generación. Devuelve el
        task name activo. Ante tombstone crea la generación siguiente."""
        from google.api_core import exceptions as gexc

        parent = self._client.queue_path(self._project, self._location,
                                         self._queue)
        current = generation
        for _attempt in range(3):
            task = self._build_task(job_id, current)
            try:
                created = await self._client.create_task(parent=parent, task=task)
                return str(created.name)
            except gexc.AlreadyExists:
                # sonda: ¿replay activo o tombstone?
                try:
                    await self._client.get_task(name=task.name)
                    return str(task.name)     # task VIVA: replay benigno
                except gexc.NotFound:
                    # tombstone: la generación actual está quemada
                    if self._generation_bumper is not None:
                        current = await self._generation_bumper(job_id)
                    else:
                        current += 1
                    logger.warning(
                        "task tombstoned: creando generación g%d", current,
                    )
                    continue
        raise TicketQueueError(
            f"no se pudo encolar el job tras {current} generaciones"
        )

    async def task_exists(self, job_id: str, generation: int = 0) -> bool:
        """Read-only liveness check for a previously confirmed task.

        Only a genuine ``NotFound`` means the task can be repaired. IAM,
        deadline and transport failures propagate so reconciliation cannot
        mutate generation from an uncertain observation.
        """
        from google.api_core import exceptions as gexc

        task = self._build_task(job_id, generation)
        try:
            await self._client.get_task(name=task.name)
        except gexc.NotFound:
            return False
        return True

    async def estimated_queue_delay_s(self) -> float:
        """Estimate queued work / dispatch rate, failing closed if unknown."""
        try:
            from google.protobuf import field_mask_pb2

            request = self._stats_tasks_v2beta3.GetQueueRequest(
                name=self._stats_client.queue_path(
                    self._project, self._location, self._queue),
                read_mask=field_mask_pb2.FieldMask(paths=[
                    "stats.tasks_count",
                    "rate_limits.max_dispatches_per_second",
                ]),
            )
            queue = await self._stats_client.get_queue(request=request)
            stats = getattr(queue, "stats", None)
            tasks_count = int(getattr(stats, "tasks_count", 0) or 0)
            rate = float(getattr(
                getattr(queue, "rate_limits", None),
                "max_dispatches_per_second", 0) or 0)
            if rate <= 0:
                raise TicketQueueEstimationError(
                    "dispatch rate unavailable for queue admission")
            return max(0, tasks_count) / rate
        except TicketQueueEstimationError:
            raise
        except Exception as exc:  # noqa: BLE001 - admission remains fail-closed
            logger.warning(
                "Cloud Tasks queue stats unavailable (error_type=%s)",
                type(exc).__name__,
            )
            raise TicketQueueEstimationError(
                "queue stats unavailable for admission") from exc

    async def aclose(self) -> None:
        for client in (self._client, self._stats_client):
            try:
                close = cast(
                    Callable[[], Awaitable[None]], client.transport.close
                )
                await close()
            except Exception:  # pragma: no cover
                logger.error("error cerrando CloudTasksAsyncClient")


class InlineTicketQueue:
    """Dev/tests: ejecuta el worker durable en el mismo proceso.

    Mantiene registro de tasks para shutdown limpio. NO usar en producción
    (no sobrevive a restarts); validate_settings lo impide.
    """

    def __init__(self, runner: Callable[[str], Awaitable[Any]]) -> None:
        self._runner = runner
        self._tasks: set[asyncio.Future[Any]] = set()
        self._enqueued: set[tuple[str, int]] = set()

    async def ensure_enqueued(self, job_id: str, generation: int = 0) -> str:
        name = f"inline/ticket-{job_id}-g{generation}"
        key = (job_id, generation)
        if key in self._enqueued:
            return name
        self._enqueued.add(key)
        task: asyncio.Future[Any] = asyncio.ensure_future(self._runner(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return name

    async def estimated_queue_delay_s(self) -> float:
        return 0.0

    async def task_exists(self, job_id: str, generation: int = 0) -> bool:
        return (job_id, generation) in self._enqueued

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
