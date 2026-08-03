"""Closed-schema, privacy-safe telemetry for the durable ticket handler.

Each structured event is deliberately restricted to a reviewed metric name,
one finite non-negative numeric value and bounded enum labels.  Optional
correlation identifiers have strict opaque formats.  Untrusted ticket text,
participant/plan identifiers, upstream bodies and exception strings therefore
cannot become log-based metric content or high-cardinality labels.

The in-process counters exist only for tests and local diagnostics; Cloud
Logging/Monitoring remains the distributed source of truth.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger("ticket_metrics")

_counters: Counter[str] = Counter()

_UNKNOWN = "unknown"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Google Cloud Trace uses 32 lowercase hexadecimal characters. The API's own
# request IDs are UUIDv4. No other caller-controlled string is safe to mirror
# into logs under the guise of an opaque identifier.
_TRACE_ID_RE = re.compile(
    r"^(?:[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12})$"
)
_TICKET_EXECUTION_ACTIVE: ContextVar[bool] = ContextVar(
    "ticket_execution_active", default=False
)


@contextmanager
def ticket_execution_scope() -> Iterator[None]:
    """Mark shared RAG/LLM work as part of a durable ticket execution.

    Context variables propagate through awaited coroutines, child asyncio
    tasks and ``asyncio.to_thread``. Core endpoint traffic therefore cannot
    contaminate ticket rollout metrics merely by sharing implementation code.
    """
    token = _TICKET_EXECUTION_ACTIVE.set(True)
    try:
        yield
    finally:
        _TICKET_EXECUTION_ACTIVE.reset(token)


def ticket_execution_active() -> bool:
    return _TICKET_EXECUTION_ACTIVE.get()


@dataclass(frozen=True)
class _MetricSpec:
    maximum: float
    labels: Mapping[str, frozenset[str]]
    integer: bool = False


def _values(*values: str) -> frozenset[str]:
    return frozenset((*values, _UNKNOWN))


_TERMINAL_STATES = _values(
    "succeeded", "partial", "failed", "timeout", "cancelled"
)
_TICKET_ROUTES = _values(
    "knowledge_question", "generate_response", "needs_more_info"
)
_PUBLIC_CODES = _values(
    "none",
    "EXPIRED_PAYLOAD",
    "INQUIRY_TIMEOUT",
    "TOTAL_JOB_TIMEOUT",
    "FORUSBOTS_TIMEOUT",
    "FORUSBOTS_FAILED",
    "FORUSBOTS_NEEDS_RECONCILIATION",
    "LLM_TIMEOUT",
    "LLM_FAILURE",
    "UNSAFE_RETRIEVAL_QUERY",
    "PINECONE_TRANSIENT_FAILURE",
    "PLAN_SCRAPE_FAILED",
    "INTERNAL_ERROR",
    "WORKER_CANCELLED",
    "UNPROCESSED_INQUIRIES",
)

# Exact Task-11 catalog.  Counts share a deliberately generous but finite
# ceiling; durations and monetary values have tighter per-event bounds.
_COUNT_MAX = 10_000_000.0
_METRIC_SPECS: Mapping[str, _MetricSpec] = {
    "ticket_queue_delay_seconds": _MetricSpec(
        86_400.0, {"code": _values("observed", "unavailable", "rejected")}
    ),
    "ticket_jobs_active": _MetricSpec(_COUNT_MAX, {}, True),
    "ticket_jobs_oldest_age_seconds": _MetricSpec(2_678_400.0, {}),
    "ticket_reconciler_duration_seconds": _MetricSpec(600.0, {}),
    "ticket_reconciler_count": _MetricSpec(
        _COUNT_MAX,
        {
            "reason": _values(
                "scanned",
                "requeued_outbox",
                "fenced_leases",
                "deadline_terminalized",
                "payload_expired",
                "skipped_locked",
                "errors",
            )
        },
        True,
    ),
    "ticket_step_latency_seconds": _MetricSpec(
        86_400.0,
        {
            "step": _values(
                "validate",
                "retrieve",
                "generate",
                "participant",
                "plan",
                "delivery",
                "finalize",
            ),
            "code": _values(
                "success", "partial", "fallback", "timeout", "failed", "cancelled"
            ),
        },
    ),
    "ticket_phase_count": _MetricSpec(
        _COUNT_MAX,
        {
            "phase": _values(
                "handle_inquiry",
                "convert_outcome",
                "validate_durable_document",
                "persist_inquiry_result",
                "mark_terminal",
            )
        },
        True,
    ),
    "ticket_result_count": _MetricSpec(
        _COUNT_MAX,
        {"reason": _values("partial", "truncated", "unprocessed")},
        True,
    ),
    "ticket_forusbots_count": _MetricSpec(
        _COUNT_MAX,
        {
            "step": _values("participant", "plan"),
            "code": _values(
                "submit_success", "poll_success", "ambiguous", "failure", "timeout"
            ),
        },
        True,
    ),
    "ticket_forusbots_circuit_count": _MetricSpec(
        _COUNT_MAX, {"state": _values("open", "half_open", "closed")}, True
    ),
    "ticket_pinecone_retry_count": _MetricSpec(
        _COUNT_MAX,
        {"reason": _values("rate_limit", "timeout", "unavailable", "other")},
        True,
    ),
    "ticket_pinecone_circuit_count": _MetricSpec(
        _COUNT_MAX, {"state": _values("open", "half_open", "closed")}, True
    ),
    "ticket_llm_parse_count": _MetricSpec(
        _COUNT_MAX, {"code": _values("success", "failed")}, True
    ),
    "ticket_llm_fallback_count": _MetricSpec(
        _COUNT_MAX, {"code": _values("used", "not_used")}, True
    ),
    "ticket_llm_tokens": _MetricSpec(
        1_000_000_000.0, {"reason": _values("input", "output")}, True
    ),
    "ticket_llm_cost_usd": _MetricSpec(1_000_000.0, {}),
    "ticket_n8n_poll_count": _MetricSpec(
        _COUNT_MAX,
        {
            "state": _values(
                "queued", "running", "succeeded", "partial", "failed",
                "timeout", "cancelled",
            )
        },
        True,
    ),
    "ticket_job_terminal": _MetricSpec(
        _COUNT_MAX, {"state": _TERMINAL_STATES, "code": _PUBLIC_CODES}, True
    ),
    "ticket_job_accepted": _MetricSpec(
        _COUNT_MAX,
        {"mode": _values("shadow", "knowledge_only", "full")},
        True,
    ),
    "ticket_inquiry_terminal": _MetricSpec(
        _COUNT_MAX, {"route": _TICKET_ROUTES, "code": _PUBLIC_CODES}, True
    ),
    "ticket_manual_reconciliation_required": _MetricSpec(
        _COUNT_MAX, {"code": _values("manual_reconciliation")}, True
    ),
}

_COUNTER_SPECS: Mapping[str, Mapping[str, frozenset[str]]] = {
    name: {}
    for name in (
        "ticket_fault_plan_rejected",
        "ticket_fault_plan_accepted",
        "ticket_participant_plan_unavailable",
        "ticket_participant_plan_mismatch",
        "ticket_jobs_conflicted",
        "ticket_jobs_replayed",
        "ticket_jobs_accepted",
        "ticket_queue_delay_unavailable",
        "ticket_queue_delay_rejected",
        "ticket_rate_limited",
        "ticket_outstanding_capped",
        "ticket_poll_not_found",
        "ticket_poll_forbidden",
        "ticket_poll_gone",
        "ticket_stale_generation",
    )
}
_COUNTER_SPECS = {
    **_COUNTER_SPECS,
    "ticket_jobs_terminal": {"state": _TERMINAL_STATES},
}


def _safe_labels(
    supplied: Mapping[str, Any], expected: Mapping[str, frozenset[str]]
) -> dict[str, str]:
    missing = expected.keys() - supplied.keys()
    if missing:
        raise ValueError("missing required metric labels")
    safe: dict[str, str] = {}
    for key, allowed in expected.items():
        value = supplied.get(key)
        safe[key] = value if isinstance(value, str) and value in allowed else _UNKNOWN
    return safe


def increment(name: str, **labels: str) -> None:
    """Increment one reviewed legacy counter without accepting free text."""
    expected = _COUNTER_SPECS.get(name)
    if expected is None:
        raise ValueError("unknown ticket counter")
    safe = _safe_labels(labels, expected)
    key = name if not safe else (
        name + "{" + ",".join(f"{k}={v}" for k, v in sorted(safe.items())) + "}"
    )
    _counters[key] += 1
    logger.info("ticket_metric %s=%d", key, _counters[key])


def emit(
    metric: str,
    value: float,
    *,
    job_hash: Optional[str] = None,
    trace_id: Optional[str] = None,
    **labels: Any,
) -> None:
    """Emit one stable JSON event after closed-schema validation.

    Invalid optional identifiers are omitted rather than logged or reflected.
    Unknown label values become the bounded ``unknown`` enum.  Unknown metric
    names and invalid numeric values fail before anything is written.
    """
    spec = _METRIC_SPECS.get(metric)
    if spec is None:
        raise ValueError("unknown ticket metric")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric value must be numeric")
    numeric_float = float(value)
    if (
        not math.isfinite(numeric_float)
        or numeric_float < 0
        or numeric_float > spec.maximum
        or (spec.integer and not numeric_float.is_integer())
    ):
        raise ValueError("metric value outside reviewed bounds")
    numeric: int | float = int(numeric_float) if spec.integer else numeric_float

    event: dict[str, Any] = {
        "metric": metric,
        "value": numeric,
        "labels": _safe_labels(labels, spec.labels),
    }
    if isinstance(job_hash, str) and _SHA256_RE.fullmatch(job_hash):
        event["job_hash"] = job_hash
    if isinstance(trace_id, str) and _TRACE_ID_RE.fullmatch(trace_id):
        event["trace_id"] = trace_id
    logger.info(
        "ticket_metric_event %s",
        json.dumps(event, sort_keys=True, allow_nan=False, separators=(",", ":")),
    )


def snapshot() -> Dict[str, int]:
    return dict(_counters)
