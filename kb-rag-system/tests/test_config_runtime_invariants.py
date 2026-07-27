"""Fail-closed timing and Cloud Tasks target configuration."""

from __future__ import annotations

import math

import pytest

from api.config import settings, validate_settings


def _development_baseline(monkeypatch, **overrides) -> None:
    values = {
        "ENVIRONMENT": "development",
        "APP_ENV": "development",
        "APP_ROLE": "reconciler",
        "TICKET_HANDLER_MODE": "disabled",
        "TICKET_JOB_BACKEND": "memory",
        "TICKET_TASK_QUEUE": "inline",
        "TICKET_WORKER_REQUIRE_OIDC": True,
        "TICKET_INQUIRY_BUDGET_S": 300.0,
        "TICKET_TOTAL_BUDGET_S": 480.0,
        "TICKET_ATTEMPT_BUDGET_S": 480.0,
        "TICKET_JOB_DEADLINE_S": 2400,
        "TICKET_WORKER_LEASE_S": 90.0,
        "TICKET_WORKER_HEARTBEAT_S": 30.0,
        "TICKET_TASK_DISPATCH_DEADLINE_S": 540,
        "TICKET_ADMISSION_QUEUE_DELAY_CEILING_S": 300,
        "TICKET_V1_INLINE_WAIT_S": 3.0,
        "PARTICIPANT_PLAN_TIMEOUT_S": 5.0,
        "FORUSBOTS_POLL_INTERVAL_S": 3.0,
        "FORUSBOTS_POLL_BACKOFF": 1.3,
        "FORUSBOTS_POLL_MAX_INTERVAL_S": 10.0,
        "FORUSBOTS_MAX_WAIT_S": 200.0,
        "FORUSBOTS_HTTP_READ_TIMEOUT_S": 15.0,
        "FORUSBOTS_RESULT_CACHE_TTL_S": 180,
        "FORUSBOTS_MAX_INFLIGHT": 2,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"TICKET_WORKER_HEARTBEAT_S": math.nan}, "finite positive"),
        ({"TICKET_WORKER_HEARTBEAT_S": 31.0}, "heartbeat"),
        ({"TICKET_ATTEMPT_BUDGET_S": 540.0}, "dispatch"),
        ({"TICKET_JOB_DEADLINE_S": 500}, "job deadline"),
        ({"TICKET_INQUIRY_BUDGET_S": 481.0}, "inquiry budget"),
        ({"FORUSBOTS_POLL_INTERVAL_S": 11.0}, "poll interval"),
        ({"FORUSBOTS_HTTP_READ_TIMEOUT_S": 201.0}, "read timeout"),
        ({"FORUSBOTS_MAX_WAIT_S": 301.0}, "max wait"),
        ({"FORUSBOTS_POLL_BACKOFF": 0.9}, "poll backoff"),
    ),
)
def test_runtime_timing_invariants_fail_closed(monkeypatch, overrides, message):
    _development_baseline(monkeypatch, **overrides)
    with pytest.raises(ValueError, match=message):
        validate_settings()


def _staging_reconciler(monkeypatch, **overrides) -> None:
    values = {
        "ENVIRONMENT": "staging",
        "APP_ENV": "staging",
        "APP_ROLE": "reconciler",
        "TICKET_JOB_BACKEND": "firestore",
        "FIRESTORE_DATABASE": "ticket-staging",
        "TICKET_TASK_QUEUE": "cloudtasks",
        "GCP_PROJECT": "rag-kb-system",
        "CLOUD_TASKS_LOCATION": "us-central1",
        "CLOUD_TASKS_QUEUE": "ticket-jobs-staging",
        "TICKET_WORKER_URL": "https://worker-abc-uc.a.run.app",
        "TICKET_WORKER_AUDIENCE": (
            "https://kb-rag-ticket-worker-staging."
            "rag-kb-system.ticket.internal"
        ),
        "TICKET_WORKER_SERVICE_ACCOUNT": (
            "ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com"
        ),
        "TICKET_LLM_PRICING_JSON": (
            '{"pricing_as_of":"2026-07-21","source":"official",'
            '"models":{"openai:gpt-5.5":{'
            '"input_usd_per_million":5.0,'
            '"output_usd_per_million":30.0}}}'
        ),
    }
    values.update(overrides)
    _development_baseline(monkeypatch, **values)


@pytest.mark.parametrize(
    "worker_url",
    (
        "https://user:secret@worker.example.run.app",
        "https://worker.example.run.app/internal/tasks/ticket-job",
        "https://worker.example.run.app?token=secret",
        "https://worker.example.run.app#fragment",
        "http://worker.example.run.app",
    ),
)
def test_deployed_task_target_must_be_canonical_https_origin(
    monkeypatch, worker_url
):
    _staging_reconciler(monkeypatch, TICKET_WORKER_URL=worker_url)
    with pytest.raises(ValueError, match="origen HTTPS") as captured:
        validate_settings()
    assert "secret" not in str(captured.value)


def test_deployed_custom_audience_is_exact_for_environment_project_and_service(
    monkeypatch,
):
    _staging_reconciler(
        monkeypatch,
        TICKET_WORKER_AUDIENCE="https://attacker.example",
    )
    with pytest.raises(ValueError, match="custom audience exacta"):
        validate_settings()


def test_deployed_task_signer_is_exact_for_environment_and_project(monkeypatch):
    _staging_reconciler(
        monkeypatch,
        TICKET_WORKER_SERVICE_ACCOUNT=(
            "other@rag-kb-system.iam.gserviceaccount.com"
        ),
    )
    with pytest.raises(ValueError, match="task signer exacta"):
        validate_settings()


def test_reviewed_staging_task_target_contract_is_valid(monkeypatch):
    _staging_reconciler(monkeypatch)
    assert validate_settings() is True
