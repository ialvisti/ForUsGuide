# Three independent processes share an immutable image but not credentials:
# private event ingestion, human-facing IAP console, and read-only evidence
# broker.  No service accepts an unauthenticated Cloud Run invoker.

resource "google_cloud_run_v2_service" "ingest" {
  count    = local.workload_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = local.ingest_service_name
  # RAG publishers are Cloud Run services/jobs with no VPC egress. Cloud Run
  # does not treat that direct service-to-service path as internal; exact
  # invoker IAM plus the app's audience/email checks are the private boundary.
  ingress              = "INGRESS_TRAFFIC_ALL"
  custom_audiences     = [local.ingest_audience]
  invoker_iam_disabled = false
  deletion_protection  = true

  template {
    service_account                  = google_service_account.ingest.email
    max_instance_request_concurrency = 20
    timeout                          = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = var.ingest_max_instances
    }

    containers {
      image = var.image_digest
      ports {
        name           = "http1"
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "APP_ROLE"
        value = "ticket-evaluation-ingest"
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_HYDRATION_TIMEOUT_S"
        value = tostring(var.ingest_hydration_timeout_s)
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_FIRESTORE_DATABASE"
        value = local.console_database
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_OIDC_AUDIENCE"
        value = local.ingest_audience
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_ALLOWED_SERVICE_ACCOUNTS"
        value = jsonencode(sort(tolist(local.expected_publisher_service_accounts)))
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_DEVREV_API_BASE"
        value = "https://api.devrev.ai"
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_DEVREV_VERSION"
        value = "2022-10-20"
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_DEVREV_ALLOWED_PART_DONS"
        value = jsonencode(sort(tolist(var.devrev_allowed_part_dons)))
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_DEVREV_ALLOWED_TICKET_VISIBILITY_IDS"
        value = jsonencode(tolist(var.devrev_allowed_ticket_visibility_ids))
      }
      env {
        name  = "TICKET_EVALUATION_INGEST_DEVREV_ALLOWED_TIMELINE_VISIBILITIES"
        value = jsonencode(sort(tolist(var.devrev_allowed_timeline_visibilities)))
      }
      env {
        name = "TICKET_EVALUATION_INGEST_CURSOR_AEAD_KEY"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.CURSOR_AEAD_KEY.secret
            version = local.secret_parts.CURSOR_AEAD_KEY.version
          }
        }
      }
      env {
        name = "TICKET_EVALUATION_INGEST_DEVREV_TOKEN"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.DEVREV_TOKEN.secret
            version = local.secret_parts.DEVREV_TOKEN.version
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12
        http_get {
          path = "/readyz"
          port = 8080
        }
      }
      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 30
        failure_threshold     = 3
        http_get {
          path = "/livez"
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    precondition {
      condition     = local.workload_configuration_complete
      error_message = "ingest sólo se crea con el contrato workload completo."
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.ingest]
}

resource "google_cloud_run_v2_service" "evidence_broker" {
  count    = local.workload_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = local.broker_service_name
  # Console is also Cloud Run without VPC egress. The broker remains private
  # through console-only invoker IAM and its own exact token verification.
  ingress              = "INGRESS_TRAFFIC_ALL"
  custom_audiences     = [local.broker_audience]
  invoker_iam_disabled = false
  deletion_protection  = true

  template {
    service_account                  = google_service_account.evidence_broker.email
    max_instance_request_concurrency = 20
    timeout                          = "30s"
    scaling {
      min_instance_count = 0
      max_instance_count = var.broker_max_instances
    }

    containers {
      image = var.image_digest
      ports {
        name           = "http1"
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "APP_ROLE"
        value = "tickets-evidence-broker"
      }
      env {
        name  = "TICKETS_BROKER_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "TICKETS_BROKER_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "TICKETS_BROKER_FIRESTORE_DATABASE"
        value = "(default)"
      }
      env {
        name  = "TICKETS_BROKER_CONSOLE_SERVICE_ACCOUNT"
        value = google_service_account.console.email
      }
      env {
        name  = "TICKETS_BROKER_AUDIENCE"
        value = local.broker_audience
      }
      env {
        name  = "TICKETS_CORRELATION_ALLOWED_KEY_VERSIONS"
        value = jsonencode(tolist(var.correlation_lookup_allowed_key_versions))
      }
      env {
        name = "TICKETS_CORRELATION_LOOKUP_KEYRING_JSON"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.CORRELATION_KEYRING.secret
            version = local.secret_parts.CORRELATION_KEYRING.version
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12
        http_get {
          path = "/readyz"
          port = 8080
        }
      }
      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 30
        failure_threshold     = 3
        http_get {
          path = "/livez"
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    precondition {
      condition     = local.workload_configuration_complete
      error_message = "broker sólo se crea con el contrato workload completo."
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.broker]
}

resource "google_cloud_run_v2_service" "console" {
  count                = local.workload_enabled ? 1 : 0
  project              = var.project_id
  location             = var.region
  name                 = local.console_service_name
  ingress              = "INGRESS_TRAFFIC_ALL"
  iap_enabled          = true
  invoker_iam_disabled = false
  deletion_protection  = true

  template {
    service_account                  = google_service_account.console.email
    max_instance_request_concurrency = 40
    timeout                          = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = var.console_max_instances
    }

    containers {
      image = var.image_digest
      ports {
        name           = "http1"
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "APP_ROLE"
        value = "tickets-console"
      }
      env {
        name  = "TICKETS_ENABLED"
        value = "true"
      }
      env {
        name  = "TICKETS_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "TICKETS_AUTH_MODE"
        value = "iap"
      }
      env {
        name  = "TICKETS_ALLOW_LOCAL_AUTH"
        value = "false"
      }
      env {
        name  = "TICKETS_ENABLE_SYNTHETIC_VERIFICATION"
        value = "false"
      }
      env {
        name  = "TICKETS_ALLOW_UNBOUND_VIEWERS"
        value = "false"
      }
      env {
        name  = "TICKETS_IAP_AUDIENCE"
        value = local.iap_audience
      }
      env {
        name  = "TICKETS_ALLOWED_EMAIL_DOMAINS"
        value = jsonencode(sort(tolist(var.allowed_email_domains)))
      }
      env {
        name  = "TICKETS_CONSOLE_ORIGIN"
        value = local.console_origin
      }
      env {
        name  = "TICKETS_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "TICKETS_GCP_REGION"
        value = var.region
      }
      env {
        name  = "TICKETS_FIRESTORE_DATABASE"
        value = local.console_database
      }
      env {
        name  = "TICKETS_EVIDENCE_BROKER_URL"
        value = local.broker_origin
      }
      env {
        name  = "TICKETS_EVIDENCE_BROKER_AUDIENCE"
        value = local.broker_audience
      }
      env {
        name  = "TICKETS_DEVREV_API_BASE"
        value = "https://api.devrev.ai"
      }
      env {
        name  = "TICKETS_ALLOW_NON_OFFICIAL_DEVREV_BASE"
        value = "false"
      }
      env {
        name  = "TICKETS_DEVREV_VERSION"
        value = "2022-10-20"
      }
      env {
        name  = "TICKETS_DEVREV_ALLOWED_PART_DONS"
        value = jsonencode(sort(tolist(var.devrev_allowed_part_dons)))
      }
      env {
        name  = "TICKETS_DEVREV_ALLOWED_TICKET_VISIBILITY_IDS"
        value = jsonencode(tolist(var.devrev_allowed_ticket_visibility_ids))
      }
      env {
        name  = "TICKETS_DEVREV_ALLOWED_TIMELINE_VISIBILITIES"
        value = jsonencode(sort(tolist(var.devrev_allowed_timeline_visibilities)))
      }
      env {
        name  = "TICKETS_DEVREV_AI_AUTHOR_IDS"
        value = jsonencode(sort(tolist(var.devrev_ai_author_ids)))
      }
      env {
        name  = "TICKETS_DEVREV_SYSTEM_AUTHOR_IDS"
        value = jsonencode(sort(tolist(var.devrev_system_author_ids)))
      }
      env {
        name  = "TICKETS_DEVREV_HUMAN_AUTHOR_IDS"
        value = jsonencode(sort(tolist(var.devrev_human_author_ids)))
      }
      env {
        name  = "TICKETS_RETENTION_JOB_ENABLED"
        value = "false"
      }
      env {
        name = "TICKETS_ROLE_BINDINGS_JSON"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.ROLE_BINDINGS.secret
            version = local.secret_parts.ROLE_BINDINGS.version
          }
        }
      }
      env {
        name = "TICKETS_CSRF_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.CSRF_SIGNING_SECRET.secret
            version = local.secret_parts.CSRF_SIGNING_SECRET.version
          }
        }
      }
      env {
        name = "TICKETS_CURSOR_AEAD_KEY"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.CURSOR_AEAD_KEY.secret
            version = local.secret_parts.CURSOR_AEAD_KEY.version
          }
        }
      }
      env {
        name = "TICKETS_DEVREV_TOKEN"
        value_source {
          secret_key_ref {
            secret  = local.secret_parts.DEVREV_TOKEN.secret
            version = local.secret_parts.DEVREV_TOKEN.version
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12
        http_get {
          path = "/readyz"
          port = 8080
        }
      }
      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 30
        failure_threshold     = 3
        http_get {
          path = "/livez"
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    precondition {
      condition     = local.workload_configuration_complete
      error_message = "console sólo se crea con el contrato workload completo."
    }
  }

  depends_on = [
    google_cloud_run_v2_service.evidence_broker,
    google_firestore_index.ticket_evaluations_hydrated_updated_at,
    google_secret_manager_secret_iam_member.console,
  ]
}
