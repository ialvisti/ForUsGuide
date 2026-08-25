"""Fail-closed timing and Cloud Tasks target configuration."""

from __future__ import annotations

import math

import pytest

from api.config import Settings, settings, validate_settings


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


def _evaluation_publisher_config(monkeypatch, **overrides):
    values = {
        "TICKET_EVALUATION_PUBLISH_ENABLED": True,
        "TICKET_EVALUATION_INGEST_URL": (
            "https://kb-ticket-evaluation-ingest.example.run.app"
        ),
        "TICKET_EVALUATION_INGEST_AUDIENCE": (
            "https://kb-ticket-evaluation-ingest.rag-kb-system.ticket.internal"
        ),
        "TICKET_EVALUATION_PUBLISHER_SERVICE_ACCOUNT": (
            "ticket-evaluation-publisher@rag-kb-system.iam.gserviceaccount.com"
        ),
        "TICKET_EVALUATION_PUBLISH_TIMEOUT_S": 10.0,
        "TICKET_EVALUATION_PUBLISH_BATCH_SIZE": 25,
        "TICKET_EVALUATION_OUTBOX_RETENTION_S": 86_400,
    }
    values.update(overrides)
    _development_baseline(monkeypatch)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)


def test_evaluation_publisher_has_an_explicit_enable_flag():
    assert "TICKET_EVALUATION_PUBLISH_ENABLED" in Settings.model_fields


def test_enabled_evaluation_publisher_requires_destination_and_identity(
    monkeypatch,
):
    _development_baseline(monkeypatch)
    monkeypatch.setattr(settings, "TICKET_EVALUATION_PUBLISH_ENABLED", True)
    monkeypatch.setattr(settings, "TICKET_EVALUATION_INGEST_URL", "")
    monkeypatch.setattr(settings, "TICKET_EVALUATION_INGEST_AUDIENCE", "")
    monkeypatch.setattr(
        settings, "TICKET_EVALUATION_PUBLISHER_SERVICE_ACCOUNT", ""
    )

    with pytest.raises(ValueError, match="TICKET_EVALUATION_INGEST_URL"):
        validate_settings()


def test_evaluation_identity_cannot_be_configured_while_publisher_is_disabled(
    monkeypatch,
):
    _evaluation_publisher_config(
        monkeypatch,
        TICKET_EVALUATION_PUBLISH_ENABLED=False,
    )

    with pytest.raises(ValueError, match="TICKET_EVALUATION_PUBLISH_ENABLED"):
        validate_settings()


@pytest.mark.parametrize(
    "missing",
    (
        "TICKET_EVALUATION_INGEST_URL",
        "TICKET_EVALUATION_INGEST_AUDIENCE",
        "TICKET_EVALUATION_PUBLISHER_SERVICE_ACCOUNT",
    ),
)
def test_evaluation_publisher_configuration_is_all_or_nothing(
    monkeypatch, missing,
):
    _evaluation_publisher_config(monkeypatch, **{missing: ""})

    with pytest.raises(ValueError, match=missing):
        validate_settings()


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"TICKET_EVALUATION_INGEST_URL": "http://unsafe.test"}, "HTTPS"),
        ({"TICKET_EVALUATION_INGEST_AUDIENCE": "audience with spaces"}, "AUDIENCE"),
        ({"TICKET_EVALUATION_PUBLISH_TIMEOUT_S": 0}, "TIMEOUT"),
        ({"TICKET_EVALUATION_PUBLISH_BATCH_SIZE": 26}, "BATCH"),
        ({"TICKET_EVALUATION_OUTBOX_RETENTION_S": 60}, "RETENTION"),
    ),
)
def test_evaluation_publisher_bounds_fail_closed(monkeypatch, overrides, message):
    _evaluation_publisher_config(monkeypatch, **overrides)

    with pytest.raises(ValueError, match=message):
        validate_settings()


def test_fast_publisher_timeout_exceeds_inline_hydration_without_regression(
    monkeypatch,
):
    assert Settings.model_fields[
        "TICKET_EVALUATION_PUBLISH_TIMEOUT_S"
    ].default == 10.0
    _evaluation_publisher_config(
        monkeypatch,
        TICKET_EVALUATION_PUBLISH_TIMEOUT_S=5.0,
    )

    with pytest.raises(ValueError, match="TIMEOUT"):
        validate_settings()


def test_reviewed_evaluation_publisher_configuration_is_valid(monkeypatch):
    _evaluation_publisher_config(monkeypatch)
    assert validate_settings() is True
