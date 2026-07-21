"""Fail-closed contracts for structured ticket telemetry."""

from __future__ import annotations

import json
import logging
import math

import pytest

from api import metrics


_INTEGER_METRICS = {
    "ticket_jobs_active",
    "ticket_reconciler_count",
    "ticket_result_count",
    "ticket_forusbots_count",
    "ticket_pinecone_retry_count",
    "ticket_pinecone_circuit_count",
    "ticket_llm_parse_count",
    "ticket_llm_fallback_count",
    "ticket_llm_tokens",
    "ticket_n8n_poll_count",
}


def _event(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("ticket_metric_event ")
    )
    return json.loads(message.removeprefix("ticket_metric_event "))


def test_emit_rejects_unknown_metric_and_unsafe_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ticket_metrics")

    for metric_name, value in (
        ("ticket_text_Jane_Doe", 1),
        ("ticket_jobs_active", -1),
        ("ticket_jobs_active", math.inf),
        ("ticket_jobs_active", math.nan),
        ("ticket_jobs_active", 10_000_001),
    ):
        with pytest.raises(ValueError):
            metrics.emit(metric_name, value)

    assert "Jane Doe" not in caplog.text
    assert "ticket_metric_event" not in caplog.text


def test_emit_sanitizes_label_values_and_optional_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "Jane Doe, 123 Main Street, participant-158948"
    caplog.set_level(logging.INFO, logger="ticket_metrics")

    metrics.emit(
        "ticket_job_terminal",
        1,
        state="failed",
        code=sentinel,
        job_hash=sentinel,
        trace_id=sentinel,
        untrusted=sentinel,
    )

    event = _event(caplog)
    assert event == {
        "metric": "ticket_job_terminal",
        "value": 1,
        "labels": {"code": "unknown", "state": "failed"},
    }
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    ("metric_name", "labels"),
    (
        ("ticket_queue_delay_seconds", {"code": "observed"}),
        ("ticket_jobs_active", {}),
        ("ticket_jobs_oldest_age_seconds", {}),
        ("ticket_reconciler_count", {"reason": "fenced_leases"}),
        (
            "ticket_step_latency_seconds",
            {"step": "retrieve", "code": "success"},
        ),
        ("ticket_result_count", {"reason": "truncated"}),
        (
            "ticket_forusbots_count",
            {"step": "participant", "code": "poll_success"},
        ),
        ("ticket_pinecone_retry_count", {"reason": "rate_limit"}),
        ("ticket_pinecone_circuit_count", {"state": "open"}),
        ("ticket_llm_parse_count", {"code": "success"}),
        ("ticket_llm_fallback_count", {"code": "used"}),
        ("ticket_llm_tokens", {"reason": "input"}),
        ("ticket_llm_cost_usd", {}),
        ("ticket_n8n_poll_count", {"state": "running"}),
    ),
)
def test_observability_catalog_emits_only_closed_schema(
    metric_name: str,
    labels: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ticket_metrics")

    metrics.emit(metric_name, 1, **labels)

    event = _event(caplog)
    expected_value = 1 if metric_name in _INTEGER_METRICS else 1.0
    assert event == {
        "metric": metric_name,
        "value": expected_value,
        "labels": labels,
    }


def test_integer_and_distribution_metric_json_types_are_stable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ticket_metrics")
    metrics.emit("ticket_llm_tokens", 7.0, reason="input")
    token_event = _event(caplog)
    assert token_event["value"] == 7
    assert isinstance(token_event["value"], int)

    caplog.clear()
    metrics.emit("ticket_queue_delay_seconds", 7, code="observed")
    delay_event = _event(caplog)
    assert delay_event["value"] == 7.0
    assert isinstance(delay_event["value"], float)


def test_emit_accepts_only_sha256_job_hash_and_opaque_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ticket_metrics")
    digest = "a" * 64
    trace_id = "123e4567-e89b-42d3-a456-426614174000"

    metrics.emit(
        "ticket_job_terminal",
        1,
        state="succeeded",
        code="none",
        job_hash=digest,
        trace_id=trace_id,
    )

    event = _event(caplog)
    assert event["job_hash"] == digest
    assert event["trace_id"] == trace_id


@pytest.mark.parametrize(
    "caller_value",
    [
        "John.Smith",
        "123-45-6789",
        "account:participant-158948",
        "ABCDEF0123456789ABCDEF0123456789",
    ],
)
def test_emit_never_logs_caller_controlled_noncanonical_trace_id(
    caller_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ticket_metrics")

    metrics.emit(
        "ticket_job_terminal",
        1,
        state="failed",
        code="INTERNAL_ERROR",
        trace_id=caller_value,
    )

    assert "trace_id" not in _event(caplog)
    assert caller_value not in caplog.text


def test_increment_rejects_unknown_names_and_sanitizes_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "Jane Doe participant-158948"
    caplog.set_level(logging.INFO, logger="ticket_metrics")

    with pytest.raises(ValueError):
        metrics.increment(sentinel)
    metrics.increment("ticket_jobs_terminal", state=sentinel)

    assert sentinel not in caplog.text
    assert "ticket_jobs_terminal{state=unknown}" in caplog.text
