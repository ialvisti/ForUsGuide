"""
In-process ticket-job store for the hybrid /handle-ticket contract.

The slow (generate_response) path runs as a background asyncio task; the POST
returns a ``ticket_job_id`` and the client polls ``GET /api/v1/tickets/{id}``.
Jobs are short-lived (minutes) and TTL-evicted. This store is per-process, so
the service must run with a single worker (see Dockerfile); for multi-worker a
Firestore-backed store would be needed instead.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from cachetools import TTLCache


@dataclass
class TicketJob:
    ticket_job_id: str
    state: str                                   # queued|running|succeeded|partial|failed|timeout
    created_monotonic: float
    outcomes: List[Any] = field(default_factory=list)        # InquiryOutcome dataclasses
    forusbots_job_ids: List[str] = field(default_factory=list)
    total_inquiries: Optional[int] = None
    error: Optional[str] = None


class TicketJobStore:
    """TTL-bounded in-memory store of ticket jobs."""

    def __init__(self, ttl_s: int = 1800, maxsize: int = 2048):
        self._jobs: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl_s)

    def create(self) -> TicketJob:
        job = TicketJob(
            ticket_job_id=uuid.uuid4().hex,
            state="queued",
            created_monotonic=time.monotonic(),
        )
        self._jobs[job.ticket_job_id] = job
        return job

    def get(self, job_id: str) -> Optional[TicketJob]:
        return self._jobs.get(job_id)

    def set_state(self, job_id: str, **changes: Any) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        self._jobs[job_id] = job  # refresh TTL/position
