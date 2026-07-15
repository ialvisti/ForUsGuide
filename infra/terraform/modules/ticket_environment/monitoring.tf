# Observabilidad ejecutable por entorno (plan Tarea 11).
#
# Invariantes:
# - cada filtro queda acotado al service/job/queue del módulo;
# - las métricas basadas en logs no extraen labels: ningún identificador de
#   cliente, ticket, job o upstream entra en Monitoring;
# - cero canales permite planificar infraestructura antes del gate; cualquier
#   configuración no vacía exige dos canales on-call probados;
# - Cloud Tasks no tiene DLQ nativa: se alerta sobre intentos no-OK y sobre
#   terminalizaciones por deadline, que son las señales operativas reales.

locals {
  metric_prefix           = "ticket_${var.env}"
  monitoring_policy_count = length(var.notification_channels) > 0 ? 1 : 0

  producer_log_filter   = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.producer_service_name}"
  EOT
  worker_log_filter     = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.worker_service_name}"
  EOT
  reconciler_log_filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="${var.reconciler_job_name}"
  EOT

  alert_labels = {
    environment = var.env
    system      = "ticket-handler"
  }
}

check "ticket_monitoring_notification_channels" {
  assert {
    condition = (
      length(var.notification_channels) == 0 ||
      length(var.notification_channels) >= 2
    )
    error_message = "monitoring requiere cero canales antes del gate o al menos dos canales on-call probados."
  }
}

# ---------------------------------------------------------------------------
# Log-based metrics. Todos son counters/valores sin labels extraídos.
# ---------------------------------------------------------------------------

resource "google_logging_metric" "poll_not_found" {
  project     = var.project_id
  name        = "${local.metric_prefix}_poll_not_found"
  description = "Poll de ticket que devolvió 404; sin IDs ni payloads."
  filter      = <<-EOT
    ${local.producer_log_filter}
    textPayload:"ticket_metric ticket_poll_not_found"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "poll_gone" {
  project     = var.project_id
  name        = "${local.metric_prefix}_poll_gone"
  description = "Poll de ticket que devolvió 410 tras expirar payload."
  filter      = <<-EOT
    ${local.producer_log_filter}
    textPayload:"ticket_metric ticket_poll_gone"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "terminal_total" {
  project     = var.project_id
  name        = "${local.metric_prefix}_terminal_total"
  description = "Todos los jobs terminales observados por el worker."
  filter      = <<-EOT
    ${local.worker_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\": \"ticket_job_terminal\""
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "terminal_incorrect" {
  project     = var.project_id
  name        = "${local.metric_prefix}_terminal_incorrect"
  description = "Terminales partial/failed/timeout/cancelled."
  filter      = <<-EOT
    ${local.worker_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\": \"ticket_job_terminal\""
    (textPayload:"\"state\": \"partial\"" OR textPayload:"\"state\": \"failed\"" OR textPayload:"\"state\": \"timeout\"" OR textPayload:"\"state\": \"cancelled\"")
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "reconciler_run" {
  project     = var.project_id
  name        = "${local.metric_prefix}_reconciler_run"
  description = "Heartbeat de cada ejecución del reconciliador."
  filter      = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"reconciler_metric ticket_reconciler_run"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "reconciler_fenced_leases" {
  project         = var.project_id
  name            = "${local.metric_prefix}_reconciler_fenced_leases"
  description     = "Leases vencidos fenceados y reencolados."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"reconciler_metric ticket_reconciler_run"
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"'fenced_leases': ([0-9]+)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "reconciler_errors" {
  project         = var.project_id
  name            = "${local.metric_prefix}_reconciler_errors"
  description     = "Errores sanitizados reportados por el reconciliador."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"reconciler_metric ticket_reconciler_run"
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"'errors': ([0-9]+)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "deadline_terminalized" {
  project         = var.project_id
  name            = "${local.metric_prefix}_deadline_terminalized"
  description     = "Jobs terminalizados por deadline absoluto."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"reconciler_metric ticket_reconciler_run"
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"'deadline_terminalized': ([0-9]+)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "manual_reconciliation" {
  project     = var.project_id
  name        = "${local.metric_prefix}_manual_reconciliation"
  description = "Resultado técnico marcado para reconciliación manual."
  filter      = <<-EOT
    ${local.worker_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"ticket_manual_reconciliation_required"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "forusbots_failure" {
  project     = var.project_id
  name        = "${local.metric_prefix}_forusbots_failure"
  description = "Timeout/fallo de ForusBots, sin extraer el job ID upstream."
  filter      = <<-EOT
    ${local.worker_log_filter}
    textPayload =~ "ForusBots (participant|plan) (timeout|failed):"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "pinecone_circuit_open" {
  project     = var.project_id
  name        = "${local.metric_prefix}_pinecone_circuit_open"
  description = "Circuit breaker de Pinecone abierto."
  filter      = <<-EOT
    ${local.worker_log_filter}
    textPayload:"circuito Pinecone abierto"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# ---------------------------------------------------------------------------
# Alert policies. Se crean sólo cuando el root aporta canales; el check de
# arriba impide configurar exactamente un canal y creer que hay redundancia.
# ---------------------------------------------------------------------------

resource "google_monitoring_alert_policy" "ticket_poll_not_found" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] ticket poll 404 > 0 (5m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "poll 404 sin explicación"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.poll_not_found.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Runbook: HANDLE_TICKET_RUNBOOK.md. Verificar receipt/payload TTL antes de cualquier requeue."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_poll_gone" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] ticket poll 410 > 0 (5m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "poll 410 / payload expirado"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.poll_gone.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "410 es terminal: no recrear el job con la misma key; revisar TTL y watch de n8n."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_terminal_incorrect_ratio" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] incorrect terminal ratio > 10% (15m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "partial/failed/timeout/cancelled sobre terminales"
    condition_threshold {
      filter             = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_incorrect.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      denominator_filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_total.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.10
      duration           = "900s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Separar fallos técnicos de respuestas publicables; inspeccionar sólo code/state/trace_id sanitizados."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_queue_backlog" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] ticket queue backlog (10m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "queue depth > 50"
    condition_threshold {
      filter          = "metric.type=\"cloudtasks.googleapis.com/queue/depth\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.project_id=\"${var.project_id}\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 50
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  conditions {
    display_name = "p99 dispatch delay > 120s"
    condition_threshold {
      filter          = "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_delays\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.project_id=\"${var.project_id}\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 120000
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_PERCENTILE_99"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "No subir concurrencia a ciegas. Contener producer/cohort, revisar worker y usar pause/resume sólo si ejecutar es inseguro."
  }
  notification_channels = var.notification_channels
}

# Proporción real: numerador 5xx / denominador de todas las requests del
# worker, agregados del mismo modo a través de revisiones.
resource "google_monitoring_alert_policy" "worker_5xx_ratio" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] worker 5xx ratio > 1% (5m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "5xx / requests del worker"
    condition_threshold {
      filter             = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\" AND metric.label.response_code_class=\"5xx\""
      denominator_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.01
      duration           = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Contener cohort en n8n; no borrar Firestore. Correlacionar por trace_id, nunca por body."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "producer_auth_failure_ratio" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] producer auth failures > 5% (5m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "401/403 sobre requests del producer"
    condition_threshold {
      filter             = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\" AND (metric.label.response_code=\"401\" OR metric.label.response_code=\"403\")"
      denominator_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.05
      duration           = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Revisar WIF/audience/SA y client mapping. No relajar auth ni reactivar credenciales humanas."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_lease_fencing" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] stale worker lease fenced"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "fenced leases > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.reconciler_fenced_leases.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Verificar heartbeat/timeout y generación nueva; nunca ejecutar efectos desde el worker fenced."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_reconciler_health" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] ticket reconciler unhealthy"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "sin reconciler run durante 10m"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.reconciler_run.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
      duration = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  conditions {
    display_name = "reconciler errors > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.reconciler_errors.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Inspeccionar Cloud Run Job/Scheduler y locks. La CLI de requeue es break-glass auditado, no sustituto del reconciler."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_forusbots_reconciliation" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] ForusBots/manual reconciliation required"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "ForusBots timeout/failure"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.forusbots_failure.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  conditions {
    display_name = "manual reconciliation required"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.manual_reconciliation.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "No reenviar POST ambiguo. Conciliar por estado upstream y job_hash/trace_id sanitizados antes de cualquier requeue."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_pinecone_circuit" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] Pinecone circuit open"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "Pinecone circuit open > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.pinecone_circuit_open.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Mantener fail-fast acotado; no desactivar el circuit breaker durante el incidente."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_task_delivery_deadline" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] task delivery/deadline (DLQ equivalent)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "Cloud Tasks non-OK attempts sustained"
    condition_threshold {
      filter          = "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_count\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.project_id=\"${var.project_id}\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\" AND metric.label.response_code!=\"ok\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  conditions {
    display_name = "jobs terminalized by absolute deadline"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.deadline_terminalized.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Cloud Tasks no tiene DLQ nativa. Usar intentos no-OK + deadline como señal; no recrear task con el mismo nombre, usar generación/CLI auditada."
  }
  notification_channels = var.notification_channels
}

# Guardrail de uso/costo. No sustituye un presupuesto de Billing con owner;
# alerta sobre la métrica GA facturable del worker dentro de este entorno.
resource "google_monitoring_alert_policy" "ticket_billable_time_budget" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] worker billable-time guardrail"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "billable worker seconds per hour"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/billable_instance_time\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.env == "staging" ? 1800 : 7200
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Guardrail técnico; confirmar además el presupuesto de Billing y su owner en approvals.md."
  }
  notification_channels = var.notification_channels
}

# ---------------------------------------------------------------------------
# Dashboard único por módulo/entorno. Todos los datasets quedan filtrados al
# service/job/queue exactos y no agrupan por identificadores de negocio.
# ---------------------------------------------------------------------------

resource "google_monitoring_dashboard" "ticket_operations" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "[${var.env}] Ticket handler operations"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          xPos = 0, yPos = 0, width = 6, height = 4
          widget = {
            title = "Worker requests by response class"
            xyChart = {
              dataSets = [{
                plotType = "LINE"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_RATE"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.response_code_class"]
                  }
                } }
              }]
              yAxis = { label = "requests/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 0, width = 6, height = 4
          widget = {
            title = "Queue depth and dispatch delay"
            xyChart = {
              dataSets = [
                {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"cloudtasks.googleapis.com/queue/depth\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MAX" }
                  } }
                },
                {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_delays\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_99" }
                  } }
                }
              ]
              yAxis = { label = "tasks / delay-ms", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 0, yPos = 4, width = 6, height = 4
          widget = {
            title = "Terminal outcomes"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.terminal_total.name, google_logging_metric.terminal_incorrect.name] : {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE" }
                  } }
                }
              ]
              yAxis = { label = "terminal jobs/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 4, width = 6, height = 4
          widget = {
            title = "Poll 404 and 410"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.poll_not_found.name, google_logging_metric.poll_gone.name] : {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE" }
                  } }
                }
              ]
              yAxis = { label = "polls/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 0, yPos = 8, width = 6, height = 4
          widget = {
            title = "Lease fencing and reconciler errors"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.reconciler_fenced_leases.name, google_logging_metric.reconciler_errors.name] : {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM" }
                  } }
                }
              ]
              yAxis = { label = "events", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 8, width = 6, height = 4
          widget = {
            title = "ForusBots/manual reconciliation and Pinecone circuit"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.forusbots_failure.name, google_logging_metric.manual_reconciliation.name, google_logging_metric.pinecone_circuit_open.name] : {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE" }
                  } }
                }
              ]
              yAxis = { label = "events/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 0, yPos = 12, width = 6, height = 4
          widget = {
            title = "Task delivery failures and deadline terminalizations"
            xyChart = {
              dataSets = [
                {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_count\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\" AND metric.label.response_code!=\"ok\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM" }
                  } }
                },
                {
                  plotType = "LINE"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.deadline_terminalized.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM" }
                  } }
                }
              ]
              yAxis = { label = "events", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 12, width = 6, height = 4
          widget = {
            title = "Billable worker instance time"
            xyChart = {
              dataSets = [{
                plotType = "LINE"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter      = "metric.type=\"run.googleapis.com/container/billable_instance_time\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                  aggregation = { alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM" }
                } }
              }]
              yAxis = { label = "billable seconds/hour", scale = "LINEAR" }
            }
          }
        }
      ]
    }
  })
}
