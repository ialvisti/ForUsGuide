"""Deployable observability contracts for Task 11.

These tests inspect the Terraform source because a comment or runbook entry
must never satisfy the gate when the corresponding metric, alert, or dashboard
resource is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

from api import metrics as ticket_metrics


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
RUNBOOK = (
    REPO_ROOT
    / "kb-rag-system"
    / "Development Docs"
    / "HANDLE_TICKET_RUNBOOK.md"
)
PRODUCTION_IMPORTS = (
    REPO_ROOT / "infra" / "terraform" / "live" / "production" / "imports.tf"
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


def _dashboard_definition(text: str) -> str:
    start = text.index("ticket_operations_dashboard_json")
    end = text.index('\nresource "google_monitoring_dashboard"', start)
    return text[start:end].replace('\\"', '"')


def test_logging_metrics_are_real_environment_scoped_and_privacy_safe() -> None:
    monitoring = _read(MONITORING)
    required = {
        "poll_not_found",
        "poll_gone",
        "accepted_total",
        "terminal_total",
        "terminal_incorrect",
        "terminal_failed",
        "terminal_partial",
        "terminal_internal_error",
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

    # Identifier labels are deliberately absent: only the bounded categorical
    # labels emitted by api.metrics may become Monitoring labels.
    for forbidden in ("job_hash", "trace_id", "participant_id", "plan_id"):
        assert forbidden not in "\n".join(
            line
            for line in monitoring.splitlines()
            if "label_extractors" in line or "REGEXP_EXTRACT" in line
        )
    assert 'resource.labels.service_name="${var.producer_service_name}"' in monitoring
    assert 'resource.labels.service_name="${var.worker_service_name}"' in monitoring
    assert 'resource.labels.job_name="${var.reconciler_job_name}"' in monitoring

    manual = _resource(
        monitoring, "google_logging_metric", "manual_reconciliation"
    )
    assert "ticket_metric_event" in manual
    assert "ticket_manual_reconciliation_required" in manual


def test_worker_metrics_use_one_canonical_cloud_logging_representation() -> None:
    monitoring = _read(MONITORING)
    locals_block = monitoring[:monitoring.index(
        'check "ticket_monitoring_notification_channels"'
    )]

    assert locals_block.count('labels.python_logger="ticket_metrics"') == 2


def test_canonical_run_metrics_read_json_message_not_text_payload() -> None:
    monitoring = _read(MONITORING)
    canonical_metrics = (
        "poll_not_found",
        "poll_gone",
        "terminal_total",
        "terminal_incorrect",
        "terminal_failed",
        "terminal_partial",
        "terminal_internal_error",
        "manual_reconciliation",
        "forusbots_failure",
        "pinecone_circuit_open",
        "queue_delay",
        "step_latency",
        "result_count",
        "forusbots_count",
        "forusbots_circuit",
        "pinecone_retry",
        "pinecone_circuit",
        "llm_parse",
        "llm_fallback",
        "llm_tokens",
        "llm_cost",
        "n8n_poll",
    )
    for resource_name in canonical_metrics:
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert "jsonPayload.message" in block
        assert "textPayload" not in block
        if "value_extractor" in block:
            assert "REGEXP_EXTRACT(jsonPayload.message" in block

    for resource_name in (
        "reconciler_run",
        "reconciler_fenced_leases",
        "reconciler_errors",
        "deadline_terminalized",
        "jobs_active",
        "jobs_oldest_age",
        "reconciler_duration",
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert "textPayload" in block
        assert "jsonPayload.message" not in block


def test_reconciler_action_counters_ignore_zero_value_heartbeats() -> None:
    monitoring = _read(MONITORING)
    for resource_name in (
        "reconciler_fenced_leases",
        "reconciler_errors",
        "deadline_terminalized",
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert "value" in block
        assert "[1-9][0-9]*" in block


def test_failed_partial_and_internal_error_have_new_private_safe_signals() -> None:
    monitoring = _read(MONITORING)
    metrics = {
        "terminal_failed": ('"state":"failed"',),
        "terminal_partial": ('"state":"partial"',),
        "terminal_internal_error": (
            '"metric":"ticket_inquiry_terminal"',
            '"code":"INTERNAL_ERROR"',
        ),
    }
    for resource_name, tokens in metrics.items():
        block = _resource(monitoring, "google_logging_metric", resource_name)
        expected_metric = (
            "ticket_inquiry_terminal"
            if resource_name == "terminal_internal_error"
            else "ticket_job_terminal"
        )
        assert f'"metric":"{expected_metric}"' in block
        assert "${local.worker_log_filter}" in block
        if resource_name == "terminal_internal_error":
            assert 'route = "REGEXP_EXTRACT' in block
        else:
            assert "label_extractors" not in block
        for token in tokens:
            assert token in block
        for forbidden in ("job_hash", "trace_id", "participant_id", "plan_id"):
            assert forbidden not in block

    policies = {
        "ticket_terminal_failed": "terminal_failed",
        "ticket_terminal_partial": "terminal_partial",
        "ticket_terminal_internal_error": "terminal_internal_error",
    }
    for policy_name, metric_name in policies.items():
        block = _resource(
            monitoring, "google_monitoring_alert_policy", policy_name
        )
        assert f"google_logging_metric.{metric_name}.name" in block
        assert re.search(r"threshold_value\s*=\s*0(?:\.0)?", block)
        assert re.search(r'duration\s*=\s*"0s"', block)
        assert "notification_channels = var.notification_channels" in block

    internal_policy = _resource(
        monitoring,
        "google_monitoring_alert_policy",
        "ticket_terminal_internal_error",
    )
    assert 'group_by_fields      = ["metric.label.route"]' in internal_policy


def test_accepted_to_terminal_ratio_is_declared_and_alerted() -> None:
    monitoring = _read(MONITORING)
    accepted = _resource(
        monitoring, "google_logging_metric", "accepted_total"
    )
    policy = _resource(
        monitoring,
        "google_monitoring_alert_policy",
        "ticket_accepted_terminal_ratio",
    )

    assert '${local.producer_log_filter}' in accepted
    assert '"metric":"ticket_job_accepted"' in accepted
    assert "google_logging_metric.terminal_total.name" in policy
    assert "google_logging_metric.accepted_total.name" in policy
    assert 'comparison         = "COMPARISON_LT"' in policy
    assert re.search(r"threshold_value\s*=\s*0\.99", policy)
    assert "denominator_filter" in policy
    assert "notification_channels = var.notification_channels" in policy


def test_oldest_active_job_alert_uses_the_absolute_job_sla() -> None:
    block = _resource(
        _read(MONITORING),
        "google_monitoring_alert_policy",
        "ticket_oldest_active_job",
    )

    assert re.search(r"threshold_value\s*=\s*2400", block)
    assert re.search(r'duration\s*=\s*"0s"', block)
    assert "120s" not in block


def test_task11_structured_signals_are_backed_by_log_metrics() -> None:
    monitoring = _read(MONITORING)
    required = {
        "queue_delay": "ticket_queue_delay_seconds",
        "jobs_active": "ticket_jobs_active",
        "jobs_oldest_age": "ticket_jobs_oldest_age_seconds",
        "reconciler_duration": "ticket_reconciler_duration_seconds",
        "step_latency": "ticket_step_latency_seconds",
        "result_count": "ticket_result_count",
        "forusbots_count": "ticket_forusbots_count",
        "forusbots_circuit": "ticket_forusbots_circuit_count",
        "pinecone_retry": "ticket_pinecone_retry_count",
        "pinecone_circuit": "ticket_pinecone_circuit_count",
        "llm_parse": "ticket_llm_parse_count",
        "llm_fallback": "ticket_llm_fallback_count",
        "llm_tokens": "ticket_llm_tokens",
        "llm_cost": "ticket_llm_cost_usd",
        "n8n_poll": "ticket_n8n_poll_count",
    }

    for resource_name, event_name in required.items():
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert "ticket_metric_event" in block
        assert f'"metric":"{event_name}"' in block
        assert "local.metric_prefix" in block

    for resource_name in (
        "queue_delay",
        "jobs_active",
        "jobs_oldest_age",
        "reconciler_duration",
        "step_latency",
        "llm_tokens",
        "llm_cost",
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert "value_extractor" in block
        assert "value" in block

    # Log-based counter metrics count matching entries and therefore omit a
    # value extractor. Google Cloud only accepts value extraction for
    # DELTA/DISTRIBUTION log-based metrics.
    for resource_name in (
        "reconciler_run",
        "reconciler_fenced_leases",
        "reconciler_errors",
        "deadline_terminalized",
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert re.search(r'value_type\s*=\s*"INT64"', block)
        assert "value_extractor" not in block

    for resource_name in (
        "queue_delay",
        "jobs_active",
        "jobs_oldest_age",
        "reconciler_duration",
        "step_latency",
        "llm_tokens",
        "llm_cost",
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert re.search(r'metric_kind\s*=\s*"DELTA"', block)
        assert re.search(r'value_type\s*=\s*"DISTRIBUTION"', block)
        assert "bucket_options" in block

    # Stable low-cardinality dimensions are useful operationally. Raw IDs are
    # intentionally not extracted into metric labels.
    step = _resource(monitoring, "google_logging_metric", "step_latency")
    assert 'step = "REGEXP_EXTRACT' in step
    assert 'code = "REGEXP_EXTRACT' in step
    for resource_name, label in (
        ("result_count", "reason"),
        ("forusbots_count", "step"),
        ("forusbots_count", "code"),
        ("forusbots_circuit", "state"),
        ("pinecone_retry", "reason"),
        ("pinecone_circuit", "state"),
        ("llm_parse", "code"),
        ("llm_fallback", "code"),
        ("llm_tokens", "reason"),
        ("n8n_poll", "state"),
    ):
        block = _resource(monitoring, "google_logging_metric", resource_name)
        assert f'{label} = "REGEXP_EXTRACT' in block


def test_old_log_formats_cannot_silently_feed_dependency_or_reconciler_metrics() -> None:
    monitoring = _read(MONITORING)

    for name in (
        "reconciler_run",
        "reconciler_fenced_leases",
        "reconciler_errors",
        "deadline_terminalized",
        "forusbots_failure",
        "pinecone_circuit_open",
    ):
        block = _resource(monitoring, "google_logging_metric", name)
        assert "ticket_metric_event" in block

    assert "reconciler_metric ticket_reconciler_run" not in monitoring
    assert "'fenced_leases'" not in monitoring
    assert "'errors'" not in monitoring
    assert "'deadline_terminalized'" not in monitoring
    assert 'textPayload =~ "ForusBots' not in monitoring
    assert 'textPayload:"circuito Pinecone abierto"' not in monitoring


def test_filters_and_extractors_match_the_real_compact_runtime_event(caplog) -> None:
    with caplog.at_level("INFO", logger="ticket_metrics"):
        ticket_metrics.emit(
            "ticket_step_latency_seconds",
            1,
            step="retrieve",
            code="success",
        )

    event = caplog.records[-1].getMessage()
    block = _resource(
        _read(MONITORING), "google_logging_metric", "step_latency"
    )
    for exact_token in (
        '"metric":"ticket_step_latency_seconds"',
        '"step":"retrieve"',
        '"code":"success"',
        '"value":1.0',
    ):
        assert exact_token in event

    assert '"metric":"ticket_step_latency_seconds"' in block
    assert '\\\\"value\\\\":' in block
    assert '\\\\"step\\\\":\\\\"([a-z_]+)\\\\"' in block
    assert '\\\\"code\\\\":\\\\"([a-z_]+)\\\\"' in block
    assert '"metric": "ticket_step_latency_seconds"' not in block

    caplog.clear()
    with caplog.at_level("INFO", logger="ticket_metrics"):
        ticket_metrics.emit("ticket_jobs_active", 1)
    count_event = caplog.records[-1].getMessage()
    count_block = _resource(
        _read(MONITORING), "google_logging_metric", "jobs_active"
    )
    assert '"metric":"ticket_jobs_active"' in count_event
    assert '"value":1' in count_event
    assert '"value":1.0' not in count_event
    assert re.search(r'value_type\s*=\s*"DISTRIBUTION"', count_block)


def test_distribution_alerts_and_charts_convert_to_numeric_percentiles() -> None:
    monitoring = _read(MONITORING)
    oldest = _resource(
        monitoring, "google_monitoring_alert_policy", "ticket_oldest_active_job"
    )
    cost = _resource(
        monitoring, "google_monitoring_alert_policy", "ticket_llm_cost_budget"
    )
    dashboard = _dashboard_definition(monitoring)

    assert 'per_series_aligner   = "ALIGN_PERCENTILE_99"' in oldest
    assert 'per_series_aligner   = "ALIGN_PERCENTILE_99"' in cost
    assert dashboard.count('perSeriesAligner = "ALIGN_PERCENTILE_99"') >= 3

    queue_depth = dashboard[
        dashboard.index("cloudtasks.googleapis.com/queue/depth"):
        dashboard.index("cloudtasks.googleapis.com/queue/task_attempt_delays")
    ]
    assert 'perSeriesAligner = "ALIGN_MAX"' in queue_depth


def test_dashboard_api_normalization_does_not_cause_perpetual_drift() -> None:
    monitoring = _read(MONITORING)
    definition = _dashboard_definition(monitoring)
    dashboard = _resource(
        monitoring, "google_monitoring_dashboard", "ticket_operations"
    )

    assert len(re.findall(r'targetAxis\s*=\s*"Y1"', definition)) == len(
        re.findall(r'plotType\s*=\s*"LINE"', definition)
    )
    assert "xPos = 0" not in definition
    assert "yPos = 0" not in definition
    assert "ignore_changes" not in dashboard
    assert "replace_triggered_by" not in dashboard


def test_forusbots_open_circuit_is_alerted_and_visible() -> None:
    monitoring = _read(MONITORING)
    alert = _resource(
        monitoring,
        "google_monitoring_alert_policy",
        "ticket_forusbots_reconciliation",
    )
    dashboard = _dashboard_definition(monitoring)

    assert "google_logging_metric.forusbots_circuit.name" in alert
    assert 'metric.label.state="open"' in alert
    assert "notification_channels = var.notification_channels" in alert
    assert "ForUsBots submit/poll/ambiguous and circuit" in dashboard


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
        "ticket_accepted_terminal_ratio",
        "ticket_terminal_failed",
        "ticket_terminal_partial",
        "ticket_terminal_internal_error",
        "ticket_queue_backlog",
        "worker_5xx_ratio",
        "producer_auth_failure_ratio",
        "ticket_lease_fencing",
        "ticket_oldest_active_job",
        "ticket_reconciler_health",
        "ticket_forusbots_reconciliation",
        "ticket_pinecone_circuit",
        "ticket_task_delivery_deadline",
        "ticket_billable_time_budget",
        "ticket_llm_cost_budget",
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


def test_notification_gate_fails_closed_when_services_are_active() -> None:
    monitoring = _read(MONITORING)

    assert 'check "ticket_monitoring_notification_channels"' in monitoring
    assert "!local.create_services" in monitoring
    assert "length(var.notification_channels) >= 2" in monitoring
    assert "distinct(var.notification_channels)" in monitoring
    assert "notificationChannels/[0-9]+" in monitoring
    assert "monitoring_policy_count = local.create_services ? 1 : 0" in monitoring


def test_legacy_high_error_rate_policy_is_imported_and_disabled() -> None:
    monitoring = _read(MONITORING)
    imports = _read(PRODUCTION_IMPORTS)
    legacy = _resource(
        monitoring, "google_monitoring_alert_policy", "legacy_high_error_rate"
    )

    assert 'display_name = "KB RAG High Error Rate (neutralized)"' in legacy
    assert re.search(r"enabled\s*=\s*false", legacy)
    assert re.search(r"count\s*=\s*var\.env == \"production\" \? 1 : 0", legacy)
    assert "15030298849808887870" in imports
    assert (
        "module.production.google_monitoring_alert_policy.legacy_high_error_rate[0]"
        in imports
    )


def test_runbook_uses_only_audited_requeue_and_safe_queue_operations() -> None:
    runbook = _read(RUNBOOK)

    assert "scripts.requeue_ticket_job" in runbook
    assert "--job-id JOB --operator" in runbook
    assert "gcloud tasks queues pause ticket-jobs-prod" in runbook
    assert "gcloud tasks queues resume ticket-jobs-prod" in runbook
    assert "marcar el doc `state=cancelled`" not in runbook
    assert "editar Firestore" not in runbook
    assert "KB RAG High Error Rate (neutralized)" in runbook


def test_runbook_records_remote_preflight_blockers_as_hard_gates() -> None:
    runbook = _read(RUNBOOK)

    assert "DELETE_PROTECTION_DISABLED" in runbook
    assert "Cloud Tasks API deshabilitada" in runbook
    assert "ForusBots sigue en HTTP" in runbook
    assert "versiones `latest`" in runbook
    assert "00048-bkc" in runbook and "disabled" in runbook
    assert "NO activar servicios" in runbook


def test_dashboard_covers_runtime_queue_recovery_and_dependencies() -> None:
    monitoring = _read(MONITORING)
    dashboard = _dashboard_definition(monitoring)

    for title in (
        "Worker requests by response class",
        "Queue depth and dispatch delay",
        "Terminal outcomes",
        "Poll 404 and 410",
        "Lease fencing and reconciler errors",
        "ForusBots/manual reconciliation and Pinecone circuit",
        "Task delivery failures and deadline terminalizations",
        "Billable worker instance time",
        "Active jobs and oldest age",
        "Application queue delay",
        "Step latency by step and code",
        "Partial, truncated and unprocessed results",
        "ForUsBots submit/poll/ambiguous and circuit",
        "Pinecone retry and circuit state",
        "LLM parse and fallback",
        "LLM tokens and estimated cost",
        "n8n poll state",
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
        "ticket_accepted_terminal_ratio",
        "ticket_terminal_failed",
        "ticket_terminal_partial",
        "ticket_terminal_internal_error",
        "ticket_queue_backlog",
        "worker_5xx_ratio",
        "producer_auth_failure_ratio",
        "ticket_lease_fencing",
        "ticket_oldest_active_job",
        "ticket_reconciler_health",
        "ticket_forusbots_reconciliation",
        "ticket_pinecone_circuit",
        "ticket_task_delivery_deadline",
        "ticket_billable_time_budget",
        "ticket_llm_cost_budget",
    ):
        assert alert in drill

    assert "Cloud Tasks no ofrece una DLQ nativa" in drill
    assert "dos canales" in drill
    assert "job activo >2400s" in drill
    assert "lease + gracia" not in drill
    assert "job_hash" in drill and "trace_id" in drill
    assert "participant_id" not in drill
    assert "plan_id" not in drill
