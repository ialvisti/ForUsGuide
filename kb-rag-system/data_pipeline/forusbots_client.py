"""
ForusBots async client — submit + poll wrapper around the ForusBots RPA service.

ForusBots scraping is **asynchronous only**: every scrape endpoint returns
``202 {jobId}`` and the caller must poll ``GET /forusbot/jobs/:id`` until the job
reaches a terminal state (``succeeded`` / ``failed`` / ``canceled``). This client
encapsulates that contract with:

  * submit + poll with exponential backoff and jitter,
  * a concurrency semaphore (the ForusBots service has a small global
    ``maxConcurrency``, so we deliberately stay below it),
  * in-flight de-duplication so two callers asking for the same scrape share one
    job instead of enqueuing duplicates (the service does NOT de-dupe), and a
    short TTL result cache,
  * per-HTTP-call retry that never blindly re-submits a non-idempotent POST on an
    ambiguous timeout (a job may already have been created).

See ticket-handler-planning/stage-1-forusbots-client.md for the design notes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)


# ============================================================================
# Public types
# ============================================================================

class ForusBotsError(Exception):
    """Base class for all ForusBots client errors."""


class ForusBotsTimeout(ForusBotsError):
    """A scrape job did not reach a terminal state within ``max_wait_s``."""

    def __init__(self, job_id: str, max_wait_s: float):
        self.job_id = job_id
        self.max_wait_s = max_wait_s
        super().__init__(
            f"ForusBots job {job_id} did not finish within {max_wait_s:.0f}s"
        )


class ForusBotsJobFailed(ForusBotsError):
    """A scrape job reached a terminal ``failed`` / ``canceled`` state."""

    def __init__(self, job_id: str, state: str, error: Optional[str]):
        self.job_id = job_id
        self.state = state
        self.error = error
        super().__init__(f"ForusBots job {job_id} {state}: {error}")


@dataclass
class ScrapeResult:
    """Outcome of a successful scrape (submit + poll to ``succeeded``)."""

    job_id: str
    state: str                       # "succeeded" for returned results
    result: Optional[Dict[str, Any]]
    elapsed_seconds: Optional[float] = None
    queue_position: Optional[int] = None
    stages: List[str] = field(default_factory=list)


# Transport errors that are safe to retry on a NON-idempotent request because
# they happen before the request is put on the wire (so no job can have been
# created). Read/write timeouts are deliberately excluded for POSTs.
_PRESEND_SAFE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_TERMINAL_OK = "succeeded"
_TERMINAL_BAD = {"failed", "canceled"}


# ============================================================================
# Client
# ============================================================================

class ForusBotsClient:
    """Async client for the ForusBots scraping service."""

    def __init__(
        self,
        base_url: str,
        auth_token: str,
        *,
        poll_interval_s: float = 3.0,
        poll_backoff: float = 1.3,
        poll_max_interval_s: float = 10.0,
        max_wait_s: float = 200.0,
        http_read_timeout_s: float = 15.0,
        http_retries: int = 3,
        max_inflight: int = 2,
        result_cache_ttl_s: int = 180,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base = base_url.rstrip("/")
        self._headers = {
            "x-auth-token": auth_token,
            "Content-Type": "application/json",
        }
        self._poll_interval = poll_interval_s
        self._poll_backoff = poll_backoff
        self._poll_max_interval = poll_max_interval_s
        self._max_wait = max_wait_s
        self._http_retries = max(1, http_retries)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0, read=http_read_timeout_s, write=10.0, pool=5.0
            )
        )
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._inflight: Dict[str, "asyncio.Future[ScrapeResult]"] = {}
        self._result_cache: TTLCache = TTLCache(maxsize=256, ttl=result_cache_ttl_s)

    @classmethod
    def from_settings(cls, settings: Any, *, client: Optional[httpx.AsyncClient] = None) -> "ForusBotsClient":
        """Build a client from the application Settings object."""
        return cls(
            base_url=settings.FORUSBOTS_BASE_URL,
            auth_token=settings.FORUSBOTS_AUTH_TOKEN,
            poll_interval_s=settings.FORUSBOTS_POLL_INTERVAL_S,
            poll_backoff=settings.FORUSBOTS_POLL_BACKOFF,
            poll_max_interval_s=settings.FORUSBOTS_POLL_MAX_INTERVAL_S,
            max_wait_s=settings.FORUSBOTS_MAX_WAIT_S,
            http_read_timeout_s=settings.FORUSBOTS_HTTP_READ_TIMEOUT_S,
            max_inflight=settings.FORUSBOTS_MAX_INFLIGHT,
            result_cache_ttl_s=settings.FORUSBOTS_RESULT_CACHE_TTL_S,
            client=client,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public scrape API
    # ------------------------------------------------------------------

    async def scrape_participant(
        self,
        participant_id: str,
        modules: List[Dict[str, Any]],
        *,
        strict: bool = False,
        return_: str = "data",
    ) -> ScrapeResult:
        payload: Dict[str, Any] = {
            "participantId": participant_id,
            "modules": modules,
            "return": return_,
            "strict": strict,
            "timeoutMs": int(self._max_wait * 1000),
        }
        idem = self._idem_key("participant", participant_id, modules)
        return await self._deduped(
            idem, "/forusbot/scrape-participant", payload, label=f"participant:{participant_id}"
        )

    async def scrape_plan(
        self,
        plan_id: str,
        modules: List[Dict[str, Any]],
        *,
        strict: bool = False,
        return_: str = "data",
    ) -> ScrapeResult:
        payload: Dict[str, Any] = {
            "planId": plan_id,
            "modules": modules,
            "return": return_,
            "strict": strict,
            "timeoutMs": int(self._max_wait * 1000),
        }
        idem = self._idem_key("plan", plan_id, modules)
        return await self._deduped(
            idem, "/forusbot/scrape-plan", payload, label=f"plan:{plan_id}"
        )

    # ------------------------------------------------------------------
    # De-duplication + result cache
    # ------------------------------------------------------------------

    @staticmethod
    def _idem_key(kind: str, entity_id: str, modules: List[Dict[str, Any]]) -> str:
        raw = f"{kind}|{entity_id}|{json.dumps(modules, sort_keys=True)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _deduped(
        self, idem: str, path: str, payload: Dict[str, Any], *, label: str
    ) -> ScrapeResult:
        cached = self._result_cache.get(idem)
        if cached is not None:
            logger.info("[forusbots] %s served from result cache", label)
            return cached

        existing = self._inflight.get(idem)
        if existing is not None:
            logger.info("[forusbots] %s joined in-flight job (dedupe)", label)
            return await existing

        # Wrap the work in a single task that BOTH the originating caller and any
        # concurrent duplicate callers await. This guarantees the task's result
        # (or exception) is always retrieved — no orphaned futures.
        task: "asyncio.Task[ScrapeResult]" = asyncio.ensure_future(
            self._submit_and_poll(path, payload, label=label)
        )
        self._inflight[idem] = task
        try:
            result = await task
        finally:
            self._inflight.pop(idem, None)
        self._result_cache[idem] = result
        return result

    # ------------------------------------------------------------------
    # Submit + poll
    # ------------------------------------------------------------------

    async def _submit_and_poll(
        self, path: str, payload: Dict[str, Any], *, label: str
    ) -> ScrapeResult:
        async with self._semaphore:
            job_id, queue_position, estimate = await self._submit(path, payload, label=label)
            return await self._poll(job_id, queue_position, estimate, label=label)

    async def _submit(
        self, path: str, payload: Dict[str, Any], *, label: str
    ) -> tuple[str, Optional[int], Dict[str, Any]]:
        resp = await self._http_request(
            "POST", f"{self._base}{path}", json=payload, idempotent=False
        )
        self._raise_for_status(resp, context=f"submit {label}")
        body = resp.json()
        job_id = body.get("jobId")
        if not job_id:
            raise ForusBotsError(
                f"submit {label}: response missing jobId (status={resp.status_code})"
            )
        estimate = body.get("estimate") or {}
        cap = body.get("capacitySnapshot") or {}
        logger.info(
            "[forusbots] %s submitted job=%s queue=%s running=%s queued=%s",
            label, job_id, body.get("queuePosition"), cap.get("running"), cap.get("queued"),
        )
        return job_id, body.get("queuePosition"), estimate

    async def _poll(
        self,
        job_id: str,
        queue_position: Optional[int],
        estimate: Dict[str, Any],
        *,
        label: str,
    ) -> ScrapeResult:
        stages: List[str] = []
        poll_start = time.monotonic()

        # First wait: a freshly submitted job is never ready immediately, so hold
        # off proportional to the service's own estimate before the first poll.
        avg = float(estimate.get("avgDurationSeconds") or 0.0)
        first_wait = min(avg * 0.6, 30.0)
        if first_wait > 0:
            await asyncio.sleep(first_wait)

        interval = self._poll_interval
        deadline = time.monotonic() + self._max_wait

        while True:
            if time.monotonic() >= deadline:
                logger.warning("[forusbots] %s job=%s timed out", label, job_id)
                raise ForusBotsTimeout(job_id, self._max_wait)

            resp = await self._http_request(
                "GET", f"{self._base}/forusbot/jobs/{job_id}", idempotent=True
            )
            self._raise_for_status(resp, context=f"poll {label}")
            body = resp.json()
            state = body.get("state")

            stage = body.get("stage")
            if stage and (not stages or stages[-1] != stage):
                stages.append(stage)

            if state == _TERMINAL_OK:
                # The public job response does not include elapsedSeconds
                # (admin-only) — fall back to locally measured wall time.
                elapsed = body.get("elapsedSeconds")
                if elapsed is None:
                    elapsed = round(time.monotonic() - poll_start, 1)
                logger.info(
                    "[forusbots] %s job=%s succeeded (elapsed=%ss)",
                    label, job_id, elapsed,
                )
                return ScrapeResult(
                    job_id=job_id,
                    state=state,
                    # ForusBots job-status returns the payload as the body itself
                    # ({state, data: {<module>: {...}}, warnings, errors}); it does NOT
                    # wrap it under a "result" key. Fall back to the whole body so the
                    # normalizer always receives the {data: ...} envelope. The
                    # ``body.get("result")`` branch is kept for any proxy/legacy shape
                    # that does wrap the payload.
                    result=body.get("result") or body,
                    elapsed_seconds=elapsed,
                    queue_position=queue_position,
                    stages=stages,
                )
            if state in _TERMINAL_BAD:
                raise ForusBotsJobFailed(job_id, state, body.get("error"))

            await asyncio.sleep(max(0.0, interval + random.uniform(-0.5, 0.5)))
            interval = min(interval * self._poll_backoff, self._poll_max_interval)

    # ------------------------------------------------------------------
    # HTTP with retry
    # ------------------------------------------------------------------

    async def _http_request(
        self, method: str, url: str, *, json: Optional[Dict[str, Any]] = None, idempotent: bool
    ) -> httpx.Response:
        """Issue one HTTP call with bounded retry.

        Retries transient transport errors and 5xx/429 responses. For a
        non-idempotent request (a POST that may create a job) only pre-send
        transport errors are retried — a read/write timeout is NOT, because the
        request may already have reached the server.
        """
        delay = 0.5
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._http_retries + 1):
            try:
                resp = await self._client.request(
                    method, url, headers=self._headers, json=json
                )
            except httpx.TransportError as exc:
                retriable = idempotent or isinstance(exc, _PRESEND_SAFE)
                last_exc = exc
                if retriable and attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
                raise ForusBotsError(
                    f"{method} {url}: transport error {type(exc).__name__}: {exc}"
                ) from exc

            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = ForusBotsError(f"{method} {url}: HTTP {resp.status_code}")
                if attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
            return resp

        # Loop only exits via return or raise; this guards exhausted 5xx retries.
        raise last_exc or ForusBotsError(f"{method} {url}: exhausted retries")

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
        if resp.status_code < 400:
            return
        detail: Any
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ForusBotsError(f"{context}: HTTP {resp.status_code}: {detail}")
