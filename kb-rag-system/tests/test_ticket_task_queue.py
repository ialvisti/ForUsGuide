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
        assert a.endswith("/tasks/ticket-abc123")

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
