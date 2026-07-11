"""
Cola durable de ejecución de ticket jobs (Task 4 del plan).

El productor (POST handle-ticket) confirma el par record+task antes de
responder 202. Firestore y Cloud Tasks no comparten transacción, así que el
nombre de task es DETERMINÍSTICO (derivado del job_id): un retry del POST o
un reconciler puede re-invocar ``ensure_enqueued`` sin duplicar ejecución
(AlreadyExists ⇒ ya encolado).

Implementaciones:
- ``CloudTasksTicketQueue`` — producción. La request HTTP del task queda
  abierta mientras el worker ejecuta (Cloud Run mantiene CPU) y Cloud Tasks
  reintenta fallos. La cola limita dispatch GLOBAL (configurado en IaC según
  capacidad de ForusBots/cuotas LLM, no por instancia).
- ``InlineTicketQueue`` — dev/tests. Ejecuta el MISMO worker durable en un
  task local. Prohibida en producción (``validate_settings`` fail-closed):
  no sobrevive a restarts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class TicketQueueError(Exception):
    pass


def task_name_for_job(project: str, location: str, queue: str, job_id: str) -> str:
    return (
        f"projects/{project}/locations/{location}/queues/{queue}"
        f"/tasks/ticket-{job_id}"
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
    ):
        from google.cloud import tasks_v2  # import perezoso

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksAsyncClient()
        self._project = project
        self._location = location
        self._queue = queue
        self._worker_url = worker_url.rstrip("/")
        self._service_account = service_account

    async def ensure_enqueued(self, job_id: str) -> str:
        """Idempotente por nombre determinístico. Devuelve el task name."""
        from google.api_core import exceptions as gexc

        tasks_v2 = self._tasks_v2
        parent = self._client.queue_path(self._project, self._location, self._queue)
        name = task_name_for_job(self._project, self._location, self._queue, job_id)
        task = tasks_v2.Task(
            name=name,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self._worker_url}/internal/tasks/ticket-job",
                headers={"Content-Type": "application/json"},
                body=f'{{"job_id": "{job_id}"}}'.encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._service_account,
                    audience=self._worker_url,
                ),
            ),
        )
        try:
            created = await self._client.create_task(parent=parent, task=task)
            return created.name
        except gexc.AlreadyExists:
            # retry del productor / reconciler: el task ya existe — correcto.
            return name

    async def aclose(self) -> None:
        try:
            await self._client.transport.close()
        except Exception:  # pragma: no cover
            logger.exception("error cerrando CloudTasksAsyncClient")


class InlineTicketQueue:
    """Dev/tests: ejecuta el worker durable en el mismo proceso.

    Mantiene registro de tasks para shutdown limpio. NO usar en producción
    (no sobrevive a restarts); validate_settings lo impide.
    """

    def __init__(self, runner: Callable[[str], Awaitable[Any]]):
        self._runner = runner
        self._tasks: set = set()
        self._enqueued: set = set()

    async def ensure_enqueued(self, job_id: str) -> str:
        name = f"inline/ticket-{job_id}"
        if job_id in self._enqueued:
            return name
        self._enqueued.add(job_id)
        task = asyncio.create_task(self._runner(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return name

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
