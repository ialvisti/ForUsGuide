"""Deployable observability contracts for Task 11.

These tests inspect the Terraform source because a comment or runbook entry
must never satisfy the gate when the corresponding metric, alert, or dashboard
resource is absent.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING = (
    REPO_ROOT
    / "infra"
    / "terraform"
    / "modules"
    / "ticket_environment"
    / "monitoring.tf"
)
DRILL = (
    REPO_ROOT
    / "docs"
    / "verification"
    / "handle-ticket"
    / "11-incident-drill-template.md"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resource(text: str, kind: str, name: str) -> str:
    marker = f'resource "{kind}" "{name}"'
    start = text.index(marker)
    next_resource = text.find('\nresource "', start + len(marker))
    block = text[start:] if next_resource == -1 else text[start:next_resource]
    # HCL quoted strings escape the quotes that belong to Monitoring filters.
    return block.replace('\\"', '"')


def test_logging_metrics_are_real_environment_scoped_and_label_free() -> None:
    monitoring = _read(MONITORING)
    required = {
        "poll_not_found",
        "poll_gone",
        "terminal_total",
        "terminal_incorrect",
        "reconciler_run",
        "reconciler_fenced_leases",
        "reconciler_errors",
        "deadline_terminalized",
        "manual_reconciliation",
        "forusbots_failure",
        "pinecone_circuit_open",
    }

    for name in required:
        block = _resource(monitoring, "google_logging_metric", name)
        assert "local.metric_prefix" in block
        assert "filter" in block

    # Metric labels are deliberately absent: no raw job/upstream/customer ID
    # can become a high-cardinality Monitoring label.
    assert "label_extractors" not in monitoring
    assert 'labels {' not in monitoring
    assert 'resource.labels.service_name="${var.producer_service_name}"' in monitoring
    assert 'resource.labels.service_name="${var.worker_service_name}"' in monitoring
    assert 'resource.labels.job_name="${var.reconciler_job_name}"' in monitoring

    manual = _resource(
        monitoring, "google_logging_metric", "manual_reconciliation"
    )
    assert "ticket_metric_event" in manual
    assert "ticket_manual_reconciliation_required" in manual


def test_worker_5xx_policy_is_a_true_numerator_denominator_ratio() -> None:
    monitoring = _read(MONITORING)
    block = _resource(
        monitoring, "google_monitoring_alert_policy", "worker_5xx_ratio"
    )

    assert 'metric.label.response_code_class="5xx"' in block
    assert "denominator_filter" in block
    assert "denominator_aggregations" in block
    assert block.count('metric.type="run.googleapis.com/request_count"') >= 2
    assert block.count(
        'resource.label.service_name="${var.worker_service_name}"'
    ) >= 2
    assert re.search(r"threshold_value\s*=\s*0\.01", block)
    assert re.search(r'duration\s*=\s*"300s"', block)


def test_required_alerts_are_implemented_not_left_as_comments() -> None:
    monitoring = _read(MONITORING)
    required = {
        "ticket_poll_not_found",
        "ticket_poll_gone",
        "ticket_terminal_incorrect_ratio",
        "ticket_queue_backlog",
        "worker_5xx_ratio",
        "producer_auth_failure_ratio",
        "ticket_lease_fencing",
        "ticket_reconciler_health",
        "ticket_forusbots_reconciliation",
        "ticket_pinecone_circuit",
        "ticket_task_delivery_deadline",
        "ticket_billable_time_budget",
    }

    for name in required:
        block = _resource(monitoring, "google_monitoring_alert_policy", name)
        assert "notification_channels = var.notification_channels" in block
        assert "user_labels  = local.alert_labels" in block

    assert "environment = var.env" in monitoring

    assert "se omiten aquí" not in monitoring
    assert "pendientes de canal probado" not in monitoring


def test_queue_and_delivery_alerts_use_official_scoped_cloud_tasks_metrics() -> None:
    monitoring = _read(MONITORING)
    queue = _resource(
        monitoring, "google_monitoring_alert_policy", "ticket_queue_backlog"
    )
    delivery = _resource(
        monitoring,
        "google_monitoring_alert_policy",
        "ticket_task_delivery_deadline",
    )

    for block in (queue, delivery):
        assert 'resource.type="cloud_tasks_queue"' in block
        assert 'resource.label.queue_id="${var.queue_name}"' in block
        assert 'resource.label.location="${var.region}"' in block

    assert "cloudtasks.googleapis.com/queue/depth" in queue
    assert "cloudtasks.googleapis.com/queue/task_attempt_delays" in queue
    assert "threshold_value = 50" in queue
    assert "threshold_value = 120000" in queue
    assert "cloudtasks.googleapis.com/queue/task_attempt_count" in delivery
    assert (
        "logging.googleapis.com/user/"
        "${google_logging_metric.deadline_terminalized.name}"
    ) in delivery


def test_notification_gate_requires_zero_or_two_channels() -> None:
    monitoring = _read(MONITORING)

    assert 'check "ticket_monitoring_notification_channels"' in monitoring
    assert "length(var.notification_channels) == 0" in monitoring
    assert "length(var.notification_channels) >= 2" in monitoring


def test_dashboard_covers_runtime_queue_recovery_and_dependencies() -> None:
    monitoring = _read(MONITORING)
    dashboard = _resource(
        monitoring, "google_monitoring_dashboard", "ticket_operations"
    )

    for title in (
        "Worker requests by response class",
        "Queue depth and dispatch delay",
        "Terminal outcomes",
        "Poll 404 and 410",
        "Lease fencing and reconciler errors",
        "ForusBots/manual reconciliation and Pinecone circuit",
        "Task delivery failures and deadline terminalizations",
        "Billable worker instance time",
    ):
        assert title in dashboard

    for metric in (
        "run.googleapis.com/request_count",
        "cloudtasks.googleapis.com/queue/depth",
        "cloudtasks.googleapis.com/queue/task_attempt_delays",
        "cloudtasks.googleapis.com/queue/task_attempt_count",
        "run.googleapis.com/container/billable_instance_time",
    ):
        assert metric in dashboard


def test_incident_drill_maps_every_alert_and_cloud_tasks_dlq_semantics() -> None:
    drill = _read(DRILL)

    for alert in (
        "ticket_poll_not_found",
        "ticket_poll_gone",
        "ticket_terminal_incorrect_ratio",
        "ticket_queue_backlog",
        "worker_5xx_ratio",
        "producer_auth_failure_ratio",
        "ticket_lease_fencing",
        "ticket_reconciler_health",
        "ticket_forusbots_reconciliation",
        "ticket_pinecone_circuit",
        "ticket_task_delivery_deadline",
        "ticket_billable_time_budget",
    ):
        assert alert in drill

    assert "Cloud Tasks no ofrece una DLQ nativa" in drill
    assert "dos canales" in drill
    assert "job_hash" in drill and "trace_id" in drill
    assert "participant_id" not in drill
    assert "plan_id" not in drill
