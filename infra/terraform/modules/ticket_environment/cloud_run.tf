# Cloud Run: producer (API completa) + worker privado + reconciliador Job
# (plan Tarea 10 Paso 4). Misma imagen, APP_ROLE distinto. Sólo se crean con
# enable_services (fuera de infra_only). Secrets SIEMPRE por versión numérica.

locals {
  common_env = {
    APP_ENV            = var.env
    ENVIRONMENT        = var.env == "production" ? "production" : "staging"
    GCP_PROJECT        = var.project_id
    GCP_LOCATION       = var.region
    FIRESTORE_DATABASE = var.firestore_database
    TICKET_JOB_BACKEND = "firestore"
    TICKET_TASK_QUEUE  = "cloudtasks"
    CLOUD_TASKS_QUEUE  = var.queue_name
    CLOUD_TASKS_LOCATION = var.region
  }
}

# --- Producer: API COMPLETA existente (v1/v2/status + core no-ticket) -------
resource "google_cloud_run_v2_service" "producer" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.producer_service_name
  ingress  = "INGRESS_TRAFFIC_ALL" # AWS n8n/E2E; invoker policy preservada

  template {
    service_account = var.producer_sa_email
    scaling {
      max_instance_count = 4
    }
    containers {
      image = var.image_digest

      env {
        name  = "APP_ROLE"
        value = "producer"
      }
      env {
        name  = "TICKET_HANDLER_MODE"
        value = var.ticket_handler_mode
      }
      env {
        name  = "TICKET_SHADOW_SAMPLE_RATE"
        value = tostring(var.shadow_sample_rate / 100)
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secret_version_refs
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = split("/versions/", env.value)[0]
              version = split("/versions/", env.value)[1]
            }
          }
        }
      }
    }
  }

  # Terraform controla el tráfico; dark_no_traffic mantiene la revisión
  # segura anterior al 100% (el root de producción lo modela con traffic).
  lifecycle {
    ignore_changes = [client, client_version]
  }
}

# --- Worker PRIVADO: sólo health + ruta interna de Cloud Tasks -------------
resource "google_cloud_run_v2_service" "worker" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.worker_service_name
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = var.worker_sa_email
    max_instance_request_concurrency = 1
    timeout                          = "520s"
    scaling {
      max_instance_count = var.worker_max_instances
    }
    containers {
      image = var.image_digest
      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
      }
      env {
        name  = "APP_ROLE"
        value = "worker"
      }
      env {
        name  = "TICKET_HANDLER_MODE"
        value = var.ticket_handler_mode
      }
      env {
        name  = "TICKET_WORKER_REQUIRE_OIDC"
        value = "true"
      }
      env {
        name  = "TICKET_WORKER_SERVICE_ACCOUNT"
        value = var.task_signer_sa_email
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secret_version_refs
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = split("/versions/", env.value)[0]
              version = split("/versions/", env.value)[1]
            }
          }
        }
      }
    }
  }
}

# --- Reconciliador: Run Job batch (no sirve HTTP) --------------------------
resource "google_cloud_run_v2_job" "reconciler" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.reconciler_job_name

  template {
    template {
      service_account = var.reconciler_sa_email
      max_retries     = 1
      timeout         = "300s"
      containers {
        image   = var.image_digest
        command = ["python", "-m", "data_pipeline.ticket_reconciler"]
        args    = ["--once", "--batch-size=25"]
        env {
          name  = "APP_ROLE"
          value = "reconciler"
        }
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
}

# --- Scheduler → Run Job cada minuto ---------------------------------------
resource "google_cloud_scheduler_job" "reconciler_tick" {
  count     = local.create_services ? 1 : 0
  project   = var.project_id
  region    = var.region
  name      = "${var.reconciler_job_name}-tick"
  schedule  = "* * * * *"
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${var.reconciler_job_name}:run"
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

# Worker URL (audiencia OIDC) publicada por el worker; el productor la recibe
# como TICKET_WORKER_URL vía el root (que la lee del output).
