"""
Tests de la cola durable de ticket jobs (Task 4 del plan).

CloudTasksTicketQueue es una capa delgada sobre el SDK (verificada en
staging, no aquí); estos tests cubren el contrato local: nombres de task
determinísticos e idempotencia de ``ensure_enqueued`` en la cola inline.
"""

from __future__ import annotations

import asyncio

from data_pipeline.ticket_task_queue import InlineTicketQueue, task_name_for_job


class TestTaskNaming:

    def test_task_name_is_deterministic_per_job(self):
        a = task_name_for_job("proj", "us-central1", "ticket-jobs", "abc123")
        b = task_name_for_job("proj", "us-central1", "ticket-jobs", "abc123")
        assert a == b
        # los nombres incluyen la generación (Tarea 7 Paso 3)
        assert a.endswith("/tasks/ticket-abc123-g0")

    def test_different_jobs_get_different_tasks(self):
        a = task_name_for_job("proj", "us-central1", "ticket-jobs", "abc")
        b = task_name_for_job("proj", "us-central1", "ticket-jobs", "xyz")
        assert a != b


class TestInlineQueue:

    async def test_ensure_enqueued_runs_worker_once(self):
        runs = []

        async def runner(job_id):
            runs.append(job_id)

        queue = InlineTicketQueue(runner)
        name1 = await queue.ensure_enqueued("job-1")
        name2 = await queue.ensure_enqueued("job-1")   # retry del productor
        await asyncio.sleep(0.05)

        assert name1 == name2
        assert runs == ["job-1"], "un retry de enqueue no puede duplicar ejecución"

    async def test_aclose_cancels_pending_tasks(self):
        started = asyncio.Event()

        async def runner(job_id):
            started.set()
            await asyncio.sleep(30)

        queue = InlineTicketQueue(runner)
        await queue.ensure_enqueued("job-1")
        await asyncio.wait_for(started.wait(), timeout=2)
        await queue.aclose()
        await asyncio.sleep(0.05)
        assert not queue._tasks


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Paso 3) — dispatch deadline
# explícito, sonda de AlreadyExists, generaciones ante tombstones y
# fail-closed de configuración. RED hasta cerrar la Tarea 7.
# ---------------------------------------------------------------------------

import inspect
from unittest.mock import patch

import pytest

from data_pipeline import ticket_task_queue as ttq_module
from data_pipeline.ticket_task_queue import CloudTasksTicketQueue


class _FakeCloudTasksClient:
    """Fake del SDK: captura create_task y simula tombstones/replays."""

    def __init__(self, *, create_raises=None, get_task_result="live"):
        from google.api_core import exceptions as gexc
        self._gexc = gexc
        self.created = []
        self.create_calls = 0
        self.get_task_calls = 0
        self._create_raises = create_raises
        self._get_task_result = get_task_result
        self.transport = type("T", (), {"close": staticmethod(_noop_async)})()

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    async def create_task(self, parent=None, task=None):
        self.create_calls += 1
        if self._create_raises and self.create_calls == 1:
            raise self._create_raises
        self.created.append(task)
        return task

    async def get_task(self, name=None, **kw):
        self.get_task_calls += 1
        if self._get_task_result == "live":
            return object()
        raise self._gexc.NotFound("tombstoned")


async def _noop_async():
    return None


def _queue_with(fake):
    with patch("google.cloud.tasks_v2.CloudTasksAsyncClient",
               return_value=fake):
        return CloudTasksTicketQueue(
            project="rag-kb-system", location="us-central1",
            queue="ticket-jobs-staging",
            worker_url="https://worker.example.run.app",
            service_account="ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com",
        )


class TestCloudTasksContract:

    async def test_task_has_oidc_audience_and_explicit_dispatch_deadline(self):
        """Bloqueo 6 del plan: la task no fija dispatch_deadline; el default
        (10 min) es inconsistente con lease/retry. Debe ser explícito (540s)."""
        fake = _FakeCloudTasksClient()
        queue = _queue_with(fake)
        await queue.ensure_enqueued("job-dd-1")
        assert fake.created, "no se creó la task"
        task = fake.created[0]
        assert task.http_request.oidc_token.audience, "OIDC sin audiencia"
        assert task.http_request.oidc_token.service_account_email
        deadline_s = getattr(task.dispatch_deadline, "seconds", 0)
        assert deadline_s == 540, (
            f"dispatch_deadline={deadline_s}s; debe fijarse explícitamente en "
            "540s (Tarea 7 Paso 2)"
        )

    async def test_live_already_exists_is_benign(self):
        """AlreadyExists con task VIVA es replay benigno, pero hay que
        distinguirlo de una tombstone: la cola debe sondear get_task."""
        from google.api_core import exceptions as gexc
        fake = _FakeCloudTasksClient(create_raises=gexc.AlreadyExists("dup"),
                                     get_task_result="live")
        queue = _queue_with(fake)
        name = await queue.ensure_enqueued("job-ae-1")
        assert name, "ensure_enqueued no devolvió nombre"
        assert fake.get_task_calls >= 1, (
            "RED: tras AlreadyExists la cola no llamó get_task — no puede "
            "distinguir un replay activo de una tombstone (Tarea 7 Paso 3)"
        )

    async def test_tombstoned_task_name_uses_next_generation(self):
        """Bloqueo 7 del plan: una tombstone (create=AlreadyExists,
        get=NotFound) se confunde con una task activa y el job muere sin
        ejecutar. Debe crearse una generación nueva ticket-{job}-g{n+1}."""
        from google.api_core import exceptions as gexc
        fake = _FakeCloudTasksClient(create_raises=gexc.AlreadyExists("dup"),
                                     get_task_result="tombstone")
        queue = _queue_with(fake)
        name = await queue.ensure_enqueued("job-ts-1")
        assert fake.create_calls >= 2, (
            "RED: tras una tombstone no se intentó crear una generación "
            "nueva; el job queda encolado-fantasma para siempre"
        )
        assert "-g" in name.rsplit("/", 1)[-1], (
            f"el nombre {name!r} no incluye sufijo de generación"
        )


class TestQueueRoleSurface:

    def test_queue_role_allows_create_task_get_and_queue_get_but_not_admin(self):
        """El custom role queda limitado a tasks.create/tasks.get/queues.get;
        el código no puede depender de operaciones admin."""
        src = inspect.getsource(ttq_module)
        for forbidden in ("delete_task", "pause_queue", "purge_queue",
                          "resume_queue", "update_queue", "delete_queue",
                          "list_tasks"):
            assert forbidden not in src, (
                f"la cola usa {forbidden}: excede el rol queue-scoped"
            )
        assert "get_task" in src, (
            "RED: la cola nunca usa get_task; la sonda de AlreadyExists y el "
            "rol cloudtasks.tasks.get son parte del contrato (Tarea 7 Paso 3)"
        )


class TestProductionFailClosedConfig:

    @pytest.mark.parametrize("overrides,reason", [
        ({"TICKET_WORKER_SERVICE_ACCOUNT": ""}, "SA firmante vacía"),
        ({"TICKET_WORKER_REQUIRE_OIDC": False}, "OIDC desactivado"),
    ])
    def test_production_rejects_empty_worker_sa_or_oidc_disabled(
            self, monkeypatch, overrides, reason):
        """No existe una opción production sin OIDC ni sin SA firmante."""
        from api import config as config_module
        base = {
            "API_KEY": "k", "PINECONE_API_KEY": "p", "OPENAI_API_KEY": "o",
            "ENVIRONMENT": "production",
            "TICKET_HANDLER_MODE": "full",
            "FORUSBOTS_AUTH_TOKEN": "t",
            "FORUSBOTS_BASE_URL": "https://forusbots.example.com",
            "TICKET_JOB_BACKEND": "firestore",
            "TICKET_TASK_QUEUE": "cloudtasks",
            "TICKET_WORKER_URL": "https://worker.example.run.app",
            "TICKET_WORKER_SERVICE_ACCOUNT":
                "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com",
            "TICKET_WORKER_REQUIRE_OIDC": True,
        }
        base.update(overrides)
        for name, value in base.items():
            monkeypatch.setattr(config_module.settings, name, value)
        with pytest.raises(ValueError, match="."):
            config_module.validate_settings()
        del reason
