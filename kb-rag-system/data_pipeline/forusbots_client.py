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
    job instead of enqueuing duplicates, plus a durable scoped idempotency key
    understood by the upstream service and a short TTL result cache,
  * bounded per-HTTP-call retry: scoped POSTs safely reuse their durable key,
    while legacy unscoped POSTs keep the conservative no-resubmit policy.

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
from typing import Any, Awaitable, Callable, Dict, List, Optional, cast
from urllib.parse import quote, urlsplit

import httpx
from cachetools import TTLCache  # type: ignore[import-untyped]

from api import metrics as ticket_metrics
from data_pipeline.forusbots_contract import derive_forusbots_idempotency_key

logger = logging.getLogger(__name__)


def _request_category(method: str, url: str) -> str:
    """Map a URL to a closed operation name without reflecting its path."""
    try:
        path = urlsplit(url).path
    except (TypeError, ValueError):
        return "unknown"
    if method == "GET" and path.endswith("/forusbot/health"):
        return "health"
    if method == "GET" and "/forusbot/jobs/" in path:
        return "poll"
    if method == "POST" and path.endswith("/forusbot/scrape-participant"):
        return "submit_participant"
    if method == "POST" and path.endswith("/forusbot/scrape-plan"):
        return "submit_plan"
    return "unknown"


# ============================================================================
# Public types
# ============================================================================

class ForusBotsError(Exception):
    """Base class for all ForusBots client errors."""

    code = "FORUSBOTS_ERROR"
    needs_reconciliation = False


class ForusBotsTimeout(ForusBotsError):
    """A scrape job did not reach a terminal state within ``max_wait_s``."""

    code = "FORUSBOTS_TIMEOUT"
    needs_reconciliation = True

    def __init__(self, job_id: str, max_wait_s: float):
        self.job_id = job_id
        self.max_wait_s = max_wait_s
        super().__init__(f"ForusBots poll timed out after {max_wait_s:.0f}s")


class ForusBotsJobFailed(ForusBotsError):
    """A scrape job reached a terminal ``failed`` / ``canceled`` state."""

    code = "FORUSBOTS_JOB_FAILED"

    def __init__(self, job_id: str, state: str, error: Any):
        self.job_id = job_id
        self.state = state
        allowed_upstream_codes = {
            "DURABLE_STATE_FAILED",
            "ENQUEUE_FAILED",
            "INTERRUPTED",
        }
        upstream_code = error.get("code") if isinstance(error, dict) else None
        self.upstream_code = (
            upstream_code if upstream_code in allowed_upstream_codes else None
        )
        self.needs_reconciliation = self.upstream_code in {
            "DURABLE_STATE_FAILED",
            "INTERRUPTED",
        }
        # ``error`` is an untrusted upstream body and can contain scraped PII.
        # Accept it to preserve the wire adapter's signature, but deliberately
        # neither retain nor interpolate it in the exception.  Every downstream
        # boundary receives only the closed machine-readable code.
        self.upstream_error_redacted = error is not None
        super().__init__(f"ForusBots job reached terminal failure ({self.code})")


class ForusBotsAmbiguousSubmit(ForusBotsError):
    """Un POST terminó sin confirmación: el job upstream PUDO haberse creado.

    Los submits scoped reintentan con una identidad durable upstream. Si se
    agota ese presupuesto, el caller conserva la operación para reconciliación.
    """

    code = "FORUSBOTS_AMBIGUOUS_SUBMIT"
    needs_reconciliation = True

    def __init__(self, method: str, path: str,
                 status_code: Optional[int] = None):
        self.status_code = status_code
        self.method = method if method in {"GET", "POST"} else "UNKNOWN"
        self.operation = path if path in {
            "health", "submit_participant", "submit_plan", "poll"
        } else "unknown"
        outcome = (f"HTTP {status_code}" if status_code is not None
                   else "transport completion unknown")
        super().__init__(
            f"{self.method} {self.operation}: {outcome} tras el submit — resultado "
            "ambiguo (el job pudo crearse); no se reintenta"
        )


class ForusBotsIdempotencyConflict(ForusBotsError):
    """The durable operation key already exists for a different request."""

    code = "FORUSBOTS_IDEMPOTENCY_CONFLICT"
    needs_reconciliation = True

    def __init__(self) -> None:
        super().__init__(
            "ForUsBots rejected a changed durable operation; "
            "manual reconciliation required"
        )


class ForusBotsPollFailed(ForusBotsError):
    """El submit confirmó un job ID, pero no se pudo observar su terminal."""

    code = "FORUSBOTS_POLL_FAILED"
    needs_reconciliation = True

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"ForusBots polling failed ({self.code}); requiere reconciliación")


class ForusBotsCircuitOpen(ForusBotsError):
    """The bounded in-process dependency circuit is currently open."""

    code = "FORUSBOTS_CIRCUIT_OPEN"

    def __init__(self) -> None:
        super().__init__("ForusBots circuit open; request rejected before submit")


class ForusBotsCheckpointFailed(ForusBotsError):
    """A confirmed upstream job could not be durably checkpointed."""

    code = "FORUSBOTS_CHECKPOINT_FAILED"
    needs_reconciliation = True

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(
            "ForUsBots job receipt could not be checkpointed; "
            "manual reconciliation required"
        )


SubmittedJobObserver = Callable[[str, str], Awaitable[None]]


@dataclass
class ScrapeResult:
    """Outcome of a successful scrape (submit + poll to ``succeeded``)."""

    job_id: str
    state: str                       # "succeeded" for returned results
    result: Optional[Dict[str, Any]]
    elapsed_seconds: Optional[float] = None
    queue_position: Optional[int] = None
    stages: List[str] = field(default_factory=list)


@dataclass
class _SubmitBoundary:
    """Whether a submit may already have reached upstream."""

    crossed: bool = False
    orphaned: bool = False


@dataclass
class _InflightScrape:
    """Shared scrape task plus the waiter and side-effect state it owns."""

    task: "asyncio.Task[ScrapeResult]"
    submit_boundary: _SubmitBoundary
    waiters: int = 0


# Transport errors proven safe to retry before the request reaches the wire.
# Read/write timeouts remain excluded only for legacy unscoped POSTs.
_PRESEND_SAFE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_TERMINAL_OK = "succeeded"
_TERMINAL_BAD = {"failed", "canceled"}
_LEGACY_HTTP_ORIGIN = "http://35.224.156.104:10000"


def validate_forusbots_base_url(base_url: str) -> str:
    """Return a canonical reviewed ForUsBots origin.

    The deployed ForUsBots 2.5 contract is currently served from one legacy
    HTTP origin. Preserve that exact integration while continuing to reject
    every other plaintext origin and every URL containing credentials, paths,
    queries or fragments. Error text deliberately never echoes the untrusted
    value because it may contain embedded credentials.
    """
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ForusBotsError(
            "ForusBots base_url debe ser un origen canónico revisado"
        ) from None
    if (
        not isinstance(base_url, str)
        or base_url != base_url.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ForusBotsError(
            "ForusBots base_url debe ser un origen canónico revisado"
        )
    normalized = base_url.rstrip("/")
    if parsed.scheme == "http" and normalized != _LEGACY_HTTP_ORIGIN:
        raise ForusBotsError(
            "ForusBots base_url debe ser un origen canónico revisado"
        )
    # Accessing ``parsed.port`` above validates the port syntax.  Preserve an
    # explicit non-default port while normalizing the one permitted root path.
    _ = port
    return normalized


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
        circuit_failure_threshold: int = 3,
        circuit_reset_s: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base = validate_forusbots_base_url(base_url)
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
            ),
            # NUNCA seguir redirects automáticamente: un 3xx a otro host/
            # esquema filtraría el x-auth-token (Tarea 8 Paso 3). Explícito
            # para no depender del default de httpx.
            follow_redirects=False,
        )
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._inflight: Dict[str, _InflightScrape] = {}
        self._result_cache: TTLCache = TTLCache(maxsize=256, ttl=result_cache_ttl_s)
        self._circuit_failure_threshold = max(1, circuit_failure_threshold)
        self._circuit_reset_s = max(1.0, circuit_reset_s)
        self._circuit_failures = 0
        self._circuit_state = "closed"
        self._circuit_opened_at = 0.0
        self._half_open_inflight = False

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

    def requires_tls(self) -> bool:
        return self._base.startswith("https://")

    async def health(self) -> Dict[str, Any]:
        """Probe the documented health endpoint without participant data."""
        resp = await self._http_request(
            "GET", f"{self._base}/forusbot/health", idempotent=True)
        self._raise_for_status(resp, context="health")
        return {"status_code": resp.status_code, "tls": self.requires_tls()}

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
        dedupe_scope: Optional[str] = None,
        on_submitted: Optional[SubmittedJobObserver] = None,
    ) -> ScrapeResult:
        payload: Dict[str, Any] = {
            "participantId": participant_id,
            "modules": modules,
            "return": return_,
            "strict": strict,
            "timeoutMs": int(self._max_wait * 1000),
        }
        idem = self._idem_key(
            dedupe_scope, "participant", participant_id, modules,
        ) if dedupe_scope else None
        upstream_idempotency_key = self._upstream_idempotency_key(
            dedupe_scope, "participant",
        ) if dedupe_scope else None
        return await self._deduped(
            idem, "/forusbot/scrape-participant", payload,
            label="participant",
            upstream_idempotency_key=upstream_idempotency_key,
            on_submitted=on_submitted,
        )

    async def scrape_plan(
        self,
        plan_id: str,
        modules: List[Dict[str, Any]],
        *,
        strict: bool = False,
        return_: str = "data",
        dedupe_scope: Optional[str] = None,
        on_submitted: Optional[SubmittedJobObserver] = None,
    ) -> ScrapeResult:
        payload: Dict[str, Any] = {
            "planId": plan_id,
            "modules": modules,
            "return": return_,
            "strict": strict,
            "timeoutMs": int(self._max_wait * 1000),
        }
        idem = self._idem_key(
            dedupe_scope, "plan", plan_id, modules,
        ) if dedupe_scope else None
        upstream_idempotency_key = self._upstream_idempotency_key(
            dedupe_scope, "plan",
        ) if dedupe_scope else None
        return await self._deduped(
            idem, "/forusbot/scrape-plan", payload, label="plan",
            upstream_idempotency_key=upstream_idempotency_key,
            on_submitted=on_submitted,
        )

    async def resume_job(self, job_id: str, *, operation: str) -> ScrapeResult:
        """Resume polling a confirmed job ID without issuing another POST."""
        if operation not in {"participant", "plan"}:
            raise ForusBotsError("ForUsBots operation is invalid")
        if (
            not isinstance(job_id, str)
            or not job_id.strip()
            or len(job_id.encode("utf-8")) > 512
            or any(ord(char) < 32 for char in job_id)
        ):
            raise ForusBotsError("ForUsBots job identifier is invalid")
        async with self._semaphore:
            self._before_circuit_request()
            try:
                result = await self._poll(
                    job_id, None, {}, label=operation,
                )
            except asyncio.CancelledError:
                self._record_circuit_cancelled()
                raise
            except ForusBotsJobFailed:
                self._emit_metric(
                    "ticket_forusbots_count", step=operation, code="failure"
                )
                self._record_circuit_success()
                raise
            except ForusBotsTimeout:
                self._emit_metric(
                    "ticket_forusbots_count", step=operation, code="timeout"
                )
                self._record_circuit_failure()
                raise
            except ForusBotsPollFailed:
                self._emit_metric(
                    "ticket_forusbots_count", step=operation, code="failure"
                )
                self._record_circuit_failure()
                raise
            except Exception:
                self._emit_metric(
                    "ticket_forusbots_count", step=operation, code="failure"
                )
                self._record_circuit_failure()
                raise ForusBotsPollFailed(job_id) from None
            self._emit_metric(
                "ticket_forusbots_count", step=operation, code="poll_success"
            )
            self._record_circuit_success()
            return result

    # ------------------------------------------------------------------
    # De-duplication + result cache
    # ------------------------------------------------------------------

    @staticmethod
    def _idem_key(
        scope: str,
        kind: str,
        entity_id: str,
        modules: List[Dict[str, Any]],
    ) -> str:
        raw = (
            f"{scope}|{kind}|{entity_id}|"
            f"{json.dumps(modules, sort_keys=True)}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _upstream_idempotency_key(scope: str, operation: str) -> str:
        """Opaque identity for one durable upstream operation.

        Payload fields deliberately do not participate.  Reconstructing an
        operation with a different payload must reuse the same key so ForUsBots
        can reject the mismatch with 409 instead of creating a second job.
        """
        return derive_forusbots_idempotency_key(scope, operation)

    async def _deduped(
        self,
        idem: Optional[str],
        path: str,
        payload: Dict[str, Any],
        *,
        label: str,
        upstream_idempotency_key: Optional[str] = None,
        on_submitted: Optional[SubmittedJobObserver] = None,
    ) -> ScrapeResult:
        # A durable observer is fenced to one worker/lease. Never coalesce it
        # with another waiter: a new lease must get its own 202 callback even
        # when both POSTs resolve to the same upstream job through the stable
        # idempotency key.
        if on_submitted is not None:
            return await self._observed_submit(
                path,
                payload,
                label=label,
                upstream_idempotency_key=upstream_idempotency_key,
                on_submitted=on_submitted,
            )

        # El cliente vive como singleton de proceso. Sin un scope explícito no
        # existe una frontera segura para compartir datos de participante/plan,
        # por lo que se desactiva caché y coalescing (fail closed).
        if idem is None:
            return await self._submit_and_poll(
                path,
                payload,
                label=label,
                upstream_idempotency_key=upstream_idempotency_key,
                on_submitted=on_submitted,
            )

        cached = self._result_cache.get(idem)
        if cached is not None:
            logger.info("[forusbots] %s served from result cache", label)
            return cast(ScrapeResult, cached)

        entry = self._inflight.get(idem)
        if entry is not None:
            logger.info("[forusbots] %s joined in-flight job (dedupe)", label)
        else:
            # Shield isolates each waiter only after the submit boundary.  Before
            # that boundary, cancelling the final waiter also cancels the queued
            # work so it cannot acquire the semaphore and emit a late POST.
            submit_boundary = _SubmitBoundary()
            task = asyncio.create_task(self._submit_and_poll(
                path,
                payload,
                label=label,
                submit_boundary=submit_boundary,
                upstream_idempotency_key=upstream_idempotency_key,
                on_submitted=on_submitted,
            ))
            entry = _InflightScrape(
                task=task,
                submit_boundary=submit_boundary,
            )
            self._inflight[idem] = entry

            def _on_done(t: "asyncio.Task[ScrapeResult]") -> None:
                if self._inflight.get(idem) is entry:
                    self._inflight.pop(idem, None)
                if t.cancelled():
                    return
                exc = t.exception()  # retrieved: no orphaned-future warning
                if exc is None:
                    self._result_cache[idem] = t.result()

            task.add_done_callback(_on_done)

        entry.waiters += 1
        entry.submit_boundary.orphaned = False
        try:
            # A cancelled waiter never cancels work another waiter still owns.
            return await asyncio.shield(entry.task)
        finally:
            entry.waiters -= 1
            if entry.waiters == 0:
                entry.submit_boundary.orphaned = True
            if (
                entry.waiters == 0
                and not entry.submit_boundary.crossed
                and not entry.task.done()
            ):
                if self._inflight.get(idem) is entry:
                    self._inflight.pop(idem, None)
                entry.task.cancel()

    async def _observed_submit(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        label: str,
        upstream_idempotency_key: Optional[str],
        on_submitted: SubmittedJobObserver,
    ) -> ScrapeResult:
        """Run one lease-owned submit without sharing its durable observer."""
        submit_boundary = _SubmitBoundary()
        task = asyncio.create_task(self._submit_and_poll(
            path,
            payload,
            label=label,
            submit_boundary=submit_boundary,
            upstream_idempotency_key=upstream_idempotency_key,
            on_submitted=on_submitted,
        ))

        def _retrieve_outcome(done: "asyncio.Task[ScrapeResult]") -> None:
            if not done.cancelled():
                done.exception()

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            submit_boundary.orphaned = True
            if not submit_boundary.crossed and not task.done():
                task.cancel()
            if not task.done():
                task.add_done_callback(_retrieve_outcome)
            else:
                _retrieve_outcome(task)
            raise

    # ------------------------------------------------------------------
    # Submit + poll
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_metric(metric: str, value: float = 1, **labels: str) -> None:
        """Metrics are best-effort and the schema itself strips free text."""
        try:
            ticket_metrics.emit(metric, value, **labels)
        except (TypeError, ValueError):
            logger.warning("ForusBots metric rejected by telemetry schema")

    def _emit_circuit_state(self, state: str) -> None:
        self._emit_metric("ticket_forusbots_circuit_count", state=state)

    def _before_circuit_request(self) -> None:
        if self._circuit_state == "closed":
            return
        if self._circuit_state == "open":
            elapsed = time.monotonic() - self._circuit_opened_at
            if elapsed < self._circuit_reset_s:
                raise ForusBotsCircuitOpen()
            self._circuit_state = "half_open"
            self._half_open_inflight = True
            self._emit_circuit_state("half_open")
            return
        # Only one request may probe a half-open dependency.
        if self._half_open_inflight:
            raise ForusBotsCircuitOpen()
        self._half_open_inflight = True

    def _record_circuit_success(self) -> None:
        recovered = self._circuit_state != "closed"
        self._circuit_state = "closed"
        self._circuit_failures = 0
        self._half_open_inflight = False
        if recovered:
            self._emit_circuit_state("closed")

    def _record_circuit_failure(self) -> None:
        self._half_open_inflight = False
        self._circuit_failures += 1
        if (
            self._circuit_state == "half_open"
            or self._circuit_failures >= self._circuit_failure_threshold
        ):
            transition = self._circuit_state != "open"
            self._circuit_state = "open"
            self._circuit_opened_at = time.monotonic()
            if transition:
                self._emit_circuit_state("open")

    def _record_circuit_cancelled(self) -> None:
        if self._circuit_state == "half_open":
            self._circuit_state = "open"
            self._circuit_opened_at = time.monotonic()
        self._half_open_inflight = False

    async def _submit_and_poll(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        label: str,
        submit_boundary: Optional[_SubmitBoundary] = None,
        upstream_idempotency_key: Optional[str] = None,
        on_submitted: Optional[SubmittedJobObserver] = None,
    ) -> ScrapeResult:
        try:
            async with self._semaphore:
                # Requests can wait here while an earlier submit opens the
                # circuit. Re-check at the actual side-effect boundary so an
                # admitted backlog cannot continue posting into an outage.
                self._before_circuit_request()
                try:
                    job_id, queue_position, estimate = await self._submit(
                        path,
                        payload,
                        label=label,
                        submit_boundary=submit_boundary,
                        upstream_idempotency_key=upstream_idempotency_key,
                    )
                except ForusBotsAmbiguousSubmit:
                    self._emit_metric(
                        "ticket_forusbots_count", step=label, code="ambiguous"
                    )
                    raise
                except ForusBotsIdempotencyConflict:
                    self._emit_metric(
                        "ticket_forusbots_count", step=label, code="failure"
                    )
                    raise
                if on_submitted is not None:
                    observer = on_submitted

                    async def _persist_submitted_checkpoint() -> None:
                        await observer(label, job_id)

                    checkpoint_task: asyncio.Task[None] = asyncio.create_task(
                        _persist_submitted_checkpoint()
                    )
                    try:
                        await asyncio.shield(checkpoint_task)
                    except asyncio.CancelledError:
                        # Once the 202 crossed the boundary, finish the durable
                        # receipt before honoring cancellation.
                        try:
                            await checkpoint_task
                        except Exception:
                            raise ForusBotsCheckpointFailed(job_id) from None
                        raise
                    except Exception:
                        raise ForusBotsCheckpointFailed(job_id) from None
                self._emit_metric(
                    "ticket_forusbots_count", step=label, code="submit_success"
                )
                try:
                    result = await self._poll(
                        job_id, queue_position, estimate, label=label
                    )
                except asyncio.CancelledError:
                    raise
                except ForusBotsTimeout:
                    self._emit_metric(
                        "ticket_forusbots_count", step=label, code="timeout"
                    )
                    raise
                except (ForusBotsJobFailed, ForusBotsPollFailed):
                    self._emit_metric(
                        "ticket_forusbots_count", step=label, code="failure"
                    )
                    raise
                except Exception:
                    # The confirmed ID remains available only as a durable
                    # attribute.  Discard the raw exception chain at this
                    # boundary because reporters commonly serialize it.
                    self._emit_metric(
                        "ticket_forusbots_count", step=label, code="failure"
                    )
                    raise ForusBotsPollFailed(job_id) from None
                self._emit_metric(
                    "ticket_forusbots_count", step=label, code="poll_success"
                )
                self._record_circuit_success()
                return result
        except asyncio.CancelledError:
            self._record_circuit_cancelled()
            raise
        except (ForusBotsJobFailed, ForusBotsIdempotencyConflict):
            # A terminal business/data outcome or durable-key conflict proves
            # dependency availability. Neither should poison the global
            # availability circuit for unrelated tickets.
            self._record_circuit_success()
            raise
        except ForusBotsCheckpointFailed:
            # Upstream accepted the request; only the local durable receipt
            # failed.  Do not poison the dependency availability circuit.
            self._record_circuit_success()
            raise
        except ForusBotsCircuitOpen:
            # A fail-fast rejection is not a new dependency failure and must
            # not advance the failure count or reset the open interval.
            raise
        except ForusBotsError:
            self._record_circuit_failure()
            raise

    async def _submit(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        label: str,
        submit_boundary: Optional[_SubmitBoundary] = None,
        upstream_idempotency_key: Optional[str] = None,
    ) -> tuple[str, Optional[int], Dict[str, Any]]:
        resp = await self._http_request(
            "POST",
            f"{self._base}{path}",
            json=payload,
            idempotent=False,
            submit_boundary=submit_boundary,
            upstream_idempotency_key=upstream_idempotency_key,
        )
        self._raise_for_status(resp, context=f"submit {label}")
        try:
            body = resp.json()
            job_id = body.get("jobId")
        except (AttributeError, TypeError, ValueError):
            raise ForusBotsAmbiguousSubmit(
                "POST", f"submit_{label}", resp.status_code
            ) from None
        if not isinstance(job_id, str) or not job_id.strip():
            raise ForusBotsAmbiguousSubmit(
                "POST", f"submit_{label}", resp.status_code
            )
        estimate = body.get("estimate") or {}
        logger.info("[forusbots] %s submit accepted", label)
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
                logger.warning("[forusbots] %s poll timed out", label)
                raise ForusBotsTimeout(job_id, self._max_wait)

            encoded_job_id = quote(job_id, safe="")
            resp = await self._http_request(
                "GET",
                f"{self._base}/forusbot/jobs/{encoded_job_id}",
                idempotent=True,
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
                logger.info("[forusbots] %s poll succeeded", label)
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

            # Backoff jitter is deliberately non-cryptographic.
            await asyncio.sleep(
                max(0.0, interval + random.uniform(-0.5, 0.5))  # noqa: S311
            )
            interval = min(interval * self._poll_backoff, self._poll_max_interval)

    # ------------------------------------------------------------------
    # HTTP with retry
    # ------------------------------------------------------------------

    async def _http_request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        idempotent: bool,
        submit_boundary: Optional[_SubmitBoundary] = None,
        upstream_idempotency_key: Optional[str] = None,
    ) -> httpx.Response:
        """Issue one HTTP call with bounded retry.

        Retries transient transport errors and 5xx/429 responses. For a
        legacy unscoped submit only pre-send transport errors are retried. A
        scoped submit retries with the exact same durable key.
        """
        delay = 0.5
        operation = _request_category(method, url)
        keyed_submit = (
            not idempotent and upstream_idempotency_key is not None
        )
        request_headers = self._headers
        if upstream_idempotency_key is not None:
            if method != "POST" or operation not in {
                "submit_participant", "submit_plan",
            }:
                raise ForusBotsError(
                    "ForusBots idempotency key is only valid for submit"
                )
            # Never mutate the singleton header dictionary: participant and plan
            # submits may execute concurrently with different operation keys.
            request_headers = {
                **self._headers,
                "Idempotency-Key": upstream_idempotency_key,
            }
        last_failure: Optional[ForusBotsError] = None
        for attempt in range(1, self._http_retries + 1):
            try:
                # Set immediately before handing the non-idempotent request to
                # the transport.  Cancellation before this point is provably
                # side-effect free; cancellation after it is conservatively
                # treated as an ambiguous submit and the task is left running.
                if not idempotent and submit_boundary is not None:
                    submit_boundary.crossed = True
                resp = await self._client.request(
                    method, url, headers=request_headers, json=json
                )
            except httpx.TransportError as exc:
                presend_safe = isinstance(exc, _PRESEND_SAFE)
                retriable = idempotent or keyed_submit or presend_safe
                if (
                    not idempotent
                    and presend_safe
                    and submit_boundary is not None
                ):
                    # The transport proved that this attempt never reached the
                    # wire, so cancellation during backoff remains safe.
                    submit_boundary.crossed = False
                    if submit_boundary.orphaned:
                        # The only reason the shared task survived its final
                        # waiter was the previously ambiguous transport call.
                        # It is now proved side-effect-free, so a retry would be
                        # a brand-new late effect with no owner.
                        raise asyncio.CancelledError() from None
                last_failure = ForusBotsError(
                    f"{method} {operation}: transport unavailable"
                )
                if retriable and attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
                if not idempotent and not presend_safe:
                    raise ForusBotsAmbiguousSubmit(method, operation) from None
                raise last_failure from None

            if (
                resp.status_code == 409
                and upstream_idempotency_key is not None
                and not idempotent
            ):
                raise ForusBotsIdempotencyConflict() from None
            if resp.status_code >= 500:
                # HT-16: legacy POSTs remain non-repeatable. Scoped submits can
                # replay safely because every attempt carries the same key.
                if not idempotent and not keyed_submit:
                    raise ForusBotsAmbiguousSubmit(
                        method, operation, resp.status_code
                    )
                last_failure = ForusBotsError(
                    f"{method} {operation}: HTTP {resp.status_code}"
                )
                if attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
                if keyed_submit:
                    raise ForusBotsAmbiguousSubmit(
                        method, operation, resp.status_code
                    )
            elif resp.status_code == 408 and not idempotent:
                if keyed_submit and attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
                raise ForusBotsAmbiguousSubmit(
                    method, operation, resp.status_code
                )
            elif resp.status_code == 429:
                if not idempotent and not keyed_submit:
                    raise ForusBotsAmbiguousSubmit(
                        method, operation, resp.status_code
                    )
                last_failure = ForusBotsError(
                    f"{method} {operation}: HTTP {resp.status_code}"
                )
                if attempt < self._http_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
                    continue
                if keyed_submit:
                    raise ForusBotsAmbiguousSubmit(
                        method, operation, resp.status_code
                    )
            elif 300 <= resp.status_code < 400:
                # No se siguen redirects para requests autenticados: un 3xx a
                # otro host/esquema podría filtrar el token (Task 8 Step 3).
                raise ForusBotsError(
                    f"{method} {operation}: unexpected redirect HTTP {resp.status_code}"
                )
            return resp

        # Loop only exits via return or raise; this guards exhausted 5xx retries.
        raise last_failure or ForusBotsError(
            f"{method} {operation}: exhausted retries"
        ) from None

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
        """Errores sanitizados: nunca se incluye el body upstream (puede
        contener PII scrapeada); sólo status + tamaño (Task 8/HT-15)."""
        if resp.status_code < 400:
            return
        raise ForusBotsError(
            f"{context}: HTTP {resp.status_code} "
            f"(body de {len(resp.content or b'')} bytes suprimido)"
        )
