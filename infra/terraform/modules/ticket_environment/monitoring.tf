# Observabilidad ejecutable por entorno (plan Tarea 11).
#
# Invariantes:
# - cada filtro queda acotado al service/job/queue del módulo;
# - las métricas basadas en logs no extraen labels: ningún identificador de
#   cliente, ticket, job o upstream entra en Monitoring;
# - infra_only puede planificar sin canales; cualquier entorno con servicios
#   exige dos canales on-call probados y crea siempre sus policies;
# - Cloud Tasks no tiene DLQ nativa: se alerta sobre intentos no-OK y sobre
#   terminalizaciones por deadline, que son las señales operativas reales.

locals {
  metric_prefix           = "ticket_${var.env}"
  monitoring_policy_count = local.create_services ? 1 : 0

  producer_log_filter   = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.producer_service_name}"
    labels.python_logger="ticket_metrics"
  EOT
  worker_log_filter     = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.worker_service_name}"
    labels.python_logger="ticket_metrics"
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
      !local.create_services ||
      (
        length(var.notification_channels) >= 2 &&
        length(distinct(var.notification_channels)) >= 2 &&
        alltrue([
          for channel in var.notification_channels :
          can(regex("^projects/${var.project_id}/notificationChannels/[0-9]+$", channel))
        ])
      )
    )
    error_message = "un entorno con servicios activos exige al menos dos IDs distintos y válidos de notificationChannels del proyecto."
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
    jsonPayload.message:"ticket_metric ticket_poll_not_found"
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
    jsonPayload.message:"ticket_metric ticket_poll_gone"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "accepted_total" {
  project     = var.project_id
  name        = "${local.metric_prefix}_accepted_total"
  description = "Nuevos ticket jobs aceptados por el producer; replays excluidos."
  filter      = <<-EOT
    ${local.producer_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_job_accepted\""
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
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_job_terminal\""
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
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_job_terminal\""
    (jsonPayload.message:"\"state\":\"partial\"" OR jsonPayload.message:"\"state\":\"failed\"" OR jsonPayload.message:"\"state\":\"timeout\"" OR jsonPayload.message:"\"state\":\"cancelled\"")
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "terminal_failed" {
  project     = var.project_id
  name        = "${local.metric_prefix}_terminal_failed"
  description = "Jobs terminalizados en failed; contador sin IDs ni payloads."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_job_terminal\""
    jsonPayload.message:"\"state\":\"failed\""
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "terminal_partial" {
  project     = var.project_id
  name        = "${local.metric_prefix}_terminal_partial"
  description = "Jobs terminalizados en partial; contador sin IDs ni payloads."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_job_terminal\""
    jsonPayload.message:"\"state\":\"partial\""
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "terminal_internal_error" {
  project     = var.project_id
  name        = "${local.metric_prefix}_terminal_internal_error"
  description = "Inquiries terminales INTERNAL_ERROR por ruta cerrada; sin IDs."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_inquiry_terminal\""
    jsonPayload.message:"\"code\":\"INTERNAL_ERROR\""
  EOT
  label_extractors = {
    route = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"route\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "route"
      value_type  = "STRING"
      description = "knowledge_question, generate_response o needs_more_info."
    }
  }
}

resource "google_logging_metric" "reconciler_run" {
  project     = var.project_id
  name        = "${local.metric_prefix}_reconciler_run"
  description = "Heartbeat de cada ejecución del reconciliador."
  filter      = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_reconciler_count\""
    textPayload:"\"reason\":\"scanned\""
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "reconciler_fenced_leases" {
  project     = var.project_id
  name        = "${local.metric_prefix}_reconciler_fenced_leases"
  description = "Leases vencidos fenceados y reencolados."
  filter      = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_reconciler_count\""
    textPayload:"\"reason\":\"fenced_leases\""
    textPayload=~"\"value\":[1-9][0-9]*"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "reconciler_errors" {
  project     = var.project_id
  name        = "${local.metric_prefix}_reconciler_errors"
  description = "Errores sanitizados reportados por el reconciliador."
  filter      = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_reconciler_count\""
    textPayload:"\"reason\":\"errors\""
    textPayload=~"\"value\":[1-9][0-9]*"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "deadline_terminalized" {
  project     = var.project_id
  name        = "${local.metric_prefix}_deadline_terminalized"
  description = "Jobs terminalizados por deadline absoluto."
  filter      = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_reconciler_count\""
    textPayload:"\"reason\":\"deadline_terminalized\""
    textPayload=~"\"value\":[1-9][0-9]*"
  EOT
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
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"ticket_manual_reconciliation_required"
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
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_forusbots_count\""
    (jsonPayload.message:"\"code\":\"ambiguous\"" OR jsonPayload.message:"\"code\":\"failure\"" OR jsonPayload.message:"\"code\":\"timeout\"")
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
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_pinecone_circuit_count\""
    jsonPayload.message:"\"state\":\"open\""
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# Señales estructuradas del runtime. Los únicos labels extraídos son enums
# cerrados y validados por api.metrics; job_hash/trace_id nunca se convierten
# en dimensiones de Monitoring.
resource "google_logging_metric" "queue_delay" {
  project         = var.project_id
  name            = "${local.metric_prefix}_queue_delay_seconds"
  description     = "Distribución de demora observada al evaluar admisión."
  filter          = <<-EOT
    ${local.producer_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_queue_delay_seconds\""
  EOT
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"
  label_extractors = {
    code = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"code\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
    labels {
      key         = "code"
      value_type  = "STRING"
      description = "observed, unavailable o rejected."
    }
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 18
      growth_factor      = 2
      scale              = 0.01
    }
  }
}

resource "google_logging_metric" "jobs_active" {
  project         = var.project_id
  name            = "${local.metric_prefix}_jobs_active"
  description     = "Distribución del número de jobs no terminales observado."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_jobs_active\""
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2
      scale              = 1
    }
  }
}

resource "google_logging_metric" "jobs_oldest_age" {
  project         = var.project_id
  name            = "${local.metric_prefix}_jobs_oldest_age_seconds"
  description     = "Distribución de antigüedad del job activo más antiguo."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_jobs_oldest_age_seconds\""
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2
      scale              = 1
    }
  }
}

resource "google_logging_metric" "reconciler_duration" {
  project         = var.project_id
  name            = "${local.metric_prefix}_reconciler_duration_seconds"
  description     = "Distribución del tiempo de aplicación del reconciliador; excluye aprovisionamiento."
  filter          = <<-EOT
    ${local.reconciler_log_filter}
    textPayload:"ticket_metric_event"
    textPayload:"\"metric\":\"ticket_reconciler_duration_seconds\""
  EOT
  value_extractor = "REGEXP_EXTRACT(textPayload, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 2
      scale              = 0.01
    }
  }
}

resource "google_logging_metric" "step_latency" {
  project         = var.project_id
  name            = "${local.metric_prefix}_step_latency_seconds"
  description     = "Latencia por step y outcome público acotado."
  filter          = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_step_latency_seconds\""
  EOT
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"
  label_extractors = {
    step = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"step\\\":\\\"([a-z_]+)\\\"\")"
    code = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"code\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
    labels {
      key         = "step"
      value_type  = "STRING"
      description = "Step cerrado del worker."
    }
    labels {
      key         = "code"
      value_type  = "STRING"
      description = "Outcome cerrado y sanitizado."
    }
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 18
      growth_factor      = 2
      scale              = 0.01
    }
  }
}

resource "google_logging_metric" "result_count" {
  project     = var.project_id
  name        = "${local.metric_prefix}_result_count"
  description = "Resultados partial, truncated o unprocessed."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_result_count\""
  EOT
  label_extractors = {
    reason = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"reason\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "reason"
      value_type  = "STRING"
      description = "partial, truncated o unprocessed."
    }
  }
}

resource "google_logging_metric" "forusbots_count" {
  project     = var.project_id
  name        = "${local.metric_prefix}_forusbots_count"
  description = "Submit/poll/outcome de ForUsBots sin job ID upstream."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_forusbots_count\""
  EOT
  label_extractors = {
    step = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"step\\\":\\\"([a-z_]+)\\\"\")"
    code = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"code\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "step"
      value_type  = "STRING"
      description = "participant o plan."
    }
    labels {
      key         = "code"
      value_type  = "STRING"
      description = "submit_success, poll_success, ambiguous, failure o timeout."
    }
  }
}

resource "google_logging_metric" "forusbots_circuit" {
  project     = var.project_id
  name        = "${local.metric_prefix}_forusbots_circuit_count"
  description = "Transiciones del circuit breaker de ForUsBots."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_forusbots_circuit_count\""
  EOT
  label_extractors = {
    state = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"state\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "state"
      value_type  = "STRING"
      description = "open, half_open o closed."
    }
  }
}

resource "google_logging_metric" "pinecone_retry" {
  project     = var.project_id
  name        = "${local.metric_prefix}_pinecone_retry_count"
  description = "Retries acotados de Pinecone por clase sanitizada."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_pinecone_retry_count\""
  EOT
  label_extractors = {
    reason = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"reason\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "reason"
      value_type  = "STRING"
      description = "rate_limit, timeout, unavailable u other."
    }
  }
}

resource "google_logging_metric" "pinecone_circuit" {
  project     = var.project_id
  name        = "${local.metric_prefix}_pinecone_circuit_count"
  description = "Transiciones del circuit breaker de Pinecone."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_pinecone_circuit_count\""
  EOT
  label_extractors = {
    state = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"state\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "state"
      value_type  = "STRING"
      description = "open, half_open o closed."
    }
  }
}

resource "google_logging_metric" "llm_parse" {
  project     = var.project_id
  name        = "${local.metric_prefix}_llm_parse_count"
  description = "Parse success/failed del LLM sin contenido de respuesta."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_llm_parse_count\""
  EOT
  label_extractors = {
    code = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"code\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "code"
      value_type  = "STRING"
      description = "success o failed."
    }
  }
}

resource "google_logging_metric" "llm_fallback" {
  project     = var.project_id
  name        = "${local.metric_prefix}_llm_fallback_count"
  description = "Uso del fallback del LLM."
  filter      = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_llm_fallback_count\""
  EOT
  label_extractors = {
    code = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"code\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "code"
      value_type  = "STRING"
      description = "used o not_used."
    }
  }
}

resource "google_logging_metric" "llm_tokens" {
  project         = var.project_id
  name            = "${local.metric_prefix}_llm_tokens"
  description     = "Tokens input/output agregables, sin prompt ni respuesta."
  filter          = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_llm_tokens\""
  EOT
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"
  label_extractors = {
    reason = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"reason\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
    labels {
      key         = "reason"
      value_type  = "STRING"
      description = "input u output."
    }
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 31
      growth_factor      = 2
      scale              = 1
    }
  }
}

resource "google_logging_metric" "llm_cost" {
  project         = var.project_id
  name            = "${local.metric_prefix}_llm_cost_usd"
  description     = "Costo LLM estimado en USD por evento."
  filter          = <<-EOT
    ${local.worker_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_llm_cost_usd\""
  EOT
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"value\\\":([0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "{USD}"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 40
      growth_factor      = 2
      scale              = 0.000001
    }
  }
}

resource "google_logging_metric" "n8n_poll" {
  project     = var.project_id
  name        = "${local.metric_prefix}_n8n_poll_count"
  description = "Estado observado por el endpoint de poll consumido por n8n."
  filter      = <<-EOT
    ${local.producer_log_filter}
    jsonPayload.message:"ticket_metric_event"
    jsonPayload.message:"\"metric\":\"ticket_n8n_poll_count\""
  EOT
  label_extractors = {
    state = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"state\\\":\\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "state"
      value_type  = "STRING"
      description = "queued, running, succeeded, partial, failed, timeout o cancelled."
    }
  }
}

# ---------------------------------------------------------------------------
# Alert policies. Se crean siempre que el entorno tiene servicios; el check de
# arriba exige redundancia antes de activarlos.
# ---------------------------------------------------------------------------

# Policy preexistente inventariada en producción. El import del root adopta el
# ID real antes de cambiarla, evitando crear otra policy con el mismo nombre.
# Queda deshabilitada porque su umbral era una tasa absoluta (>5), aunque el
# nombre/documentación históricos afirmaban erróneamente que era porcentaje.
# `worker_5xx_ratio` la sustituye con un numerador/denominador real.
resource "google_monitoring_alert_policy" "legacy_high_error_rate" {
  count        = var.env == "production" ? 1 : 0
  project      = var.project_id
  display_name = "KB RAG High Error Rate (neutralized)"
  combiner     = "OR"
  enabled      = false
  user_labels  = merge(local.alert_labels, { lifecycle = "legacy-neutralized" })

  conditions {
    display_name = "legacy absolute 5xx rate > 5 (disabled)"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\" AND metric.label.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "NEUTRALIZADA: umbral absoluto mal rotulado. Sustituida por worker_5xx_ratio, que calcula 5xx/requests."
  }

  lifecycle {
    prevent_destroy = true
  }
}

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

resource "google_monitoring_alert_policy" "ticket_accepted_terminal_ratio" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] accepted-to-terminal ratio < 99% (15m)"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "worker terminales sobre nuevos jobs aceptados"
    condition_threshold {
      filter             = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_total.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      denominator_filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.accepted_total.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
      comparison         = "COMPARISON_LT"
      threshold_value    = 0.99
      duration           = "900s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Una brecha sostenida indica jobs aceptados sin terminalización del worker; correlacionar sólo job_hash/trace_id sanitizados y revisar reconciler/deadlines."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_terminal_failed" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] terminal failed > 0"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "failed terminal > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_failed.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Investigar code/fase sanitizados; no consultar ni incluir payloads o identificadores en la alerta."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_terminal_partial" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] terminal partial > 0"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "partial terminal > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_partial.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Revisar sólo estado, código y fase sanitizados para distinguir degradación publicable de fallo técnico."
  }
  notification_channels = var.notification_channels
}

resource "google_monitoring_alert_policy" "ticket_terminal_internal_error" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] INTERNAL_ERROR > 0"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "INTERNAL_ERROR terminal > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.terminal_internal_error.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.route"]
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Incidente técnico inmediato: correlacionar únicamente trace_id/job_hash sanitizados y fase; nunca payloads."
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
    content   = "Revisar el ID token IAM de kb-rag-client y X-API-Key. No relajar auth ni cambiar el workflow durante el incidente."
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

resource "google_monitoring_alert_policy" "ticket_oldest_active_job" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] active job exceeded 2400s deadline"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "oldest active job > 2400s absolute job SLA"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.jobs_oldest_age.name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2400
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "2400s coincide con TICKET_JOB_DEADLINE_S. Verificar heartbeat/fencing y no reenviar efectos externos a ciegas."
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
    display_name = "ForUsBots circuit open"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.forusbots_circuit.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\" AND metric.label.state=\"open\""
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

resource "google_monitoring_alert_policy" "ticket_llm_cost_budget" {
  count        = local.monitoring_policy_count
  project      = var.project_id
  display_name = "[${var.env}] LLM per-call cost guardrail"
  combiner     = "OR"
  user_labels  = local.alert_labels

  conditions {
    display_name = "p99 estimated LLM USD per call"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.llm_cost.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.env == "staging" ? 5 : 50
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Costo estimado p99 por llamada; contener cohort/admisión y confirmar el presupuesto agregado de Billing con su owner."
  }
  notification_channels = var.notification_channels
}

# ---------------------------------------------------------------------------
# Dashboard único por módulo/entorno. Todos los datasets quedan filtrados al
# service/job/queue exactos y no agrupan por identificadores de negocio.
# ---------------------------------------------------------------------------

locals {
  ticket_operations_dashboard_json = jsonencode({
    displayName = "[${var.env}] Ticket handler operations"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width = 6, height = 4
          widget = {
            title = "Worker requests by response class"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
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
          xPos = 6, width = 6, height = 4
          widget = {
            title = "Queue depth and dispatch delay"
            xyChart = {
              dataSets = [
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"cloudtasks.googleapis.com/queue/depth\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MAX" }
                  } }
                },
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
          yPos = 4, width = 6, height = 4
          widget = {
            title = "Terminal outcomes"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.terminal_total.name, google_logging_metric.terminal_incorrect.name] : {
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
          yPos = 8, width = 6, height = 4
          widget = {
            title = "Lease fencing and reconciler errors"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.reconciler_fenced_leases.name, google_logging_metric.reconciler_errors.name] : {
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
          yPos = 12, width = 6, height = 4
          widget = {
            title = "Task delivery failures and deadline terminalizations"
            xyChart = {
              dataSets = [
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_count\" AND resource.type=\"cloud_tasks_queue\" AND resource.label.location=\"${var.region}\" AND resource.label.queue_id=\"${var.queue_name}\" AND metric.label.response_code!=\"ok\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM" }
                  } }
                },
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
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
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter      = "metric.type=\"run.googleapis.com/container/billable_instance_time\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                  aggregation = { alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM" }
                } }
              }]
              yAxis = { label = "billable seconds/hour", scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 16, width = 6, height = 4
          widget = {
            title = "Active jobs and oldest age"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.jobs_active.name, google_logging_metric.jobs_oldest_age.name, google_logging_metric.reconciler_duration.name] : {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${var.reconciler_job_name}\""
                    aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_99" }
                  } }
                }
              ]
              yAxis = { label = "jobs / seconds", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 16, width = 6, height = 4
          widget = {
            title = "Application queue delay"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.queue_delay.name}\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_PERCENTILE_95"
                    crossSeriesReducer = "REDUCE_MAX"
                    groupByFields      = ["metric.label.code"]
                  }
                } }
              }]
              yAxis = { label = "p95 seconds", scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 20, width = 6, height = 4
          widget = {
            title = "Step latency by step and code"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.step_latency.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_PERCENTILE_95"
                    crossSeriesReducer = "REDUCE_MAX"
                    groupByFields      = ["metric.label.step", "metric.label.code"]
                  }
                } }
              }]
              yAxis = { label = "p95 seconds", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 20, width = 6, height = 4
          widget = {
            title = "Partial, truncated and unprocessed results"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.result_count.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_RATE"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.reason"]
                  }
                } }
              }]
              yAxis = { label = "results/s", scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 24, width = 6, height = 4
          widget = {
            title = "ForUsBots submit/poll/ambiguous and circuit"
            xyChart = {
              dataSets = [
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.forusbots_count.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.step", "metric.label.code"]
                    }
                  } }
                },
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.forusbots_circuit.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.state"]
                    }
                  } }
                }
              ]
              yAxis = { label = "events/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 24, width = 6, height = 4
          widget = {
            title = "Pinecone retry and circuit state"
            xyChart = {
              dataSets = [
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.pinecone_retry.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.reason"]
                    }
                  } }
                },
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.pinecone_circuit.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.state"]
                    }
                  } }
                }
              ]
              yAxis = { label = "events/s", scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 28, width = 6, height = 4
          widget = {
            title = "LLM parse and fallback"
            xyChart = {
              dataSets = [
                for metric_name in [google_logging_metric.llm_parse.name, google_logging_metric.llm_fallback.name] : {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.code"]
                    }
                  } }
                }
              ]
              yAxis = { label = "events/s", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 28, width = 6, height = 4
          widget = {
            title = "LLM tokens and estimated cost"
            xyChart = {
              dataSets = [
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.llm_tokens.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_99"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.reason"]
                    }
                  } }
                },
                {
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = { timeSeriesFilter = {
                    filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.llm_cost.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.worker_service_name}\""
                    aggregation = { alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_PERCENTILE_99", crossSeriesReducer = "REDUCE_SUM" }
                  } }
                }
              ]
              yAxis = { label = "p99 tokens / USD per call", scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 32, width = 12, height = 4
          widget = {
            title = "n8n poll state"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.n8n_poll.name}\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.producer_service_name}\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_RATE"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.state"]
                  }
                } }
              }]
              yAxis = { label = "polls/s", scale = "LINEAR" }
            }
          }
        }
      ]
    }
  })
}

resource "google_monitoring_dashboard" "ticket_operations" {
  project        = var.project_id
  dashboard_json = local.ticket_operations_dashboard_json
}
