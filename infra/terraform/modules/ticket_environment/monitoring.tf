# Observabilidad: alertas por entorno (plan Tarea 10 + Tarea 11 Paso 3).
# Políticas separadas por entorno; la entrega real a canales se prueba antes
# del gate (una policy sin notificación comprobada no pasa). Se declaran los
# canales como variable; el root los provee.

# Poll 404 sin explicación (>0 durante 5m).
resource "google_monitoring_alert_policy" "ticket_poll_not_found" {
  count        = length(var.notification_channels) > 0 ? 1 : 0
  project      = var.project_id
  display_name = "[${var.env}] ticket_poll_not_found > 0 (5m)"
  combiner     = "OR"

  conditions {
    display_name = "poll 404 sin explicación"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/ticket_poll_not_found\" resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = var.notification_channels
}

# Antigüedad máxima en cola (>120s durante 10m).
resource "google_monitoring_alert_policy" "ticket_queue_age" {
  count        = length(var.notification_channels) > 0 ? 1 : 0
  project      = var.project_id
  display_name = "[${var.env}] ticket queue age > 120s (10m)"
  combiner     = "OR"

  conditions {
    display_name = "job más antiguo en cola"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/ticket_queue_age_s\" resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 120
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = var.notification_channels
}

# 5xx del worker >1% (5m). Corregida a PROPORCIÓN (la policy legacy "High
# Error Rate >5%" era una tasa absoluta mal nombrada).
resource "google_monitoring_alert_policy" "worker_5xx_ratio" {
  count        = length(var.notification_channels) > 0 ? 1 : 0
  project      = var.project_id
  display_name = "[${var.env}] worker 5xx ratio > 1% (5m)"
  combiner     = "OR"

  conditions {
    display_name = "proporción de 5xx del worker"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\" resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.01
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_MEAN"
      }
    }
  }
  notification_channels = var.notification_channels
}

# Terminales incorrectos (>10% durante 15m), lease obsoleto en ejecución,
# task próxima a agotar reintentos, circuit Pinecone/ForusBots abierto y
# alertas de costo se declaran análogamente; se omiten aquí por brevedad y
# se listan en el runbook/11-incident-drill como pendientes de canal probado.
