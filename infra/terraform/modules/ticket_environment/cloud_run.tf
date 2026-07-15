# Cloud Run: producer (API completa) + worker privado + reconciliador Job
# (plan Tarea 10 Paso 4). Misma imagen, APP_ROLE distinto. Sólo se crean con
# enable_services (fuera de infra_only). Secrets SIEMPRE por versión numérica.

locals {
  common_env = {
    APP_ENV              = var.env
    ENVIRONMENT          = var.env == "production" ? "production" : "staging"
    GCP_PROJECT          = var.project_id
    GCP_LOCATION         = var.region
    FIRESTORE_DATABASE   = var.firestore_database
    TICKET_JOB_BACKEND   = "firestore"
    TICKET_TASK_QUEUE    = "cloudtasks"
    CLOUD_TASKS_QUEUE    = var.queue_name
    CLOUD_TASKS_LOCATION = var.region
  }

  # Configuración no-ticket observada en el producer productivo. Los valores
  # viven en el root/manifest aprobado; aquí sólo se exige que ninguna clave se
  # pierda al importar el servicio existente.
  required_producer_core_env = toset([
    "ENABLE_EXECUTION_LOGGING",
    "FORUSBOTS_BASE_URL",
    "GCS_BUCKET",
    "INDEX_NAME",
    "LLM_ROUTE_CLASSIFY",
    "LLM_ROUTE_DECOMPOSE",
    "LLM_ROUTE_GR_OUTCOME",
    "LLM_ROUTE_GR_RESPONSE",
    "LLM_ROUTE_KNOWLEDGE",
    "LLM_ROUTE_REQUIRED_DATA",
    "LOG_LEVEL",
    "NAMESPACE",
    "OPENAI_MODEL",
    "OPENAI_REASONING_EFFORT",
    "USE_VERTEX_AI",
  ])
  required_producer_secret_env = toset([
    "API_KEY",
    "FORUSBOTS_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "PINECONE_API_KEY",
  ])
  producer_managed_env_names = setunion(toset(keys(local.common_env)), toset([
    "APP_ROLE",
    "TICKET_HANDLER_MODE",
    "TICKET_SHADOW_SAMPLE_RATE",
    "TICKET_WORKER_URL",
    "TICKET_WORKER_SERVICE_ACCOUNT",
    "TICKET_WIF_AUDIENCE",
    "TICKET_WIF_EXPECTED_EMAIL",
  ]))
}

# --- Producer: API COMPLETA existente (v1/v2/status + core no-ticket) -------
resource "google_cloud_run_v2_service" "producer" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.producer_service_name
  ingress  = var.producer_ingress

  template {
    service_account                  = var.producer_sa_email
    max_instance_request_concurrency = var.producer_concurrency
    timeout                          = var.producer_timeout
    scaling {
      min_instance_count = var.producer_min_instances
      max_instance_count = var.producer_max_instances
    }
    containers {
      image = var.image_digest

      ports {
        name           = "http1"
        container_port = var.producer_port
      }

      resources {
        limits = {
          cpu    = var.producer_cpu
          memory = var.producer_memory
        }
        cpu_idle          = var.producer_cpu_idle
        startup_cpu_boost = var.producer_startup_cpu_boost
      }

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
      env {
        name  = "TICKET_WORKER_URL"
        value = var.worker_url
      }
      env {
        name  = "TICKET_WORKER_SERVICE_ACCOUNT"
        value = var.task_signer_sa_email
      }
      env {
        name  = "TICKET_WIF_AUDIENCE"
        value = var.ticket_wif_audience
      }
      env {
        name  = "TICKET_WIF_EXPECTED_EMAIL"
        value = var.ticket_wif_expected_email
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.producer_core_env
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

      dynamic "startup_probe" {
        for_each = var.producer_startup_probe == null ? [] : [var.producer_startup_probe]
        content {
          initial_delay_seconds = startup_probe.value.initial_delay_seconds
          timeout_seconds       = startup_probe.value.timeout_seconds
          period_seconds        = startup_probe.value.period_seconds
          failure_threshold     = startup_probe.value.failure_threshold
          tcp_socket {
            port = startup_probe.value.tcp_socket_port
          }
        }
      }

      dynamic "liveness_probe" {
        for_each = var.producer_liveness_probe == null ? [] : [var.producer_liveness_probe]
        content {
          initial_delay_seconds = liveness_probe.value.initial_delay_seconds
          timeout_seconds       = liveness_probe.value.timeout_seconds
          period_seconds        = liveness_probe.value.period_seconds
          failure_threshold     = liveness_probe.value.failure_threshold
          tcp_socket {
            port = liveness_probe.value.tcp_socket_port
          }
        }
      }
    }
  }

  # dark_no_traffic conserva la revisión segura al 100% y crea/taggea latest
  # con 0%. Cualquier fase posterior promueve latest explícitamente al 100%; el
  # tráfico nunca depende del default implícito de Cloud Run.
  dynamic "traffic" {
    for_each = var.release_phase == "dark_no_traffic" ? [var.producer_baseline_revision] : []
    content {
      type     = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
      revision = var.producer_baseline_revision
      percent  = 100
    }
  }

  dynamic "traffic" {
    for_each = var.release_phase == "dark_no_traffic" ? [1] : []
    content {
      type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
      percent = 0
      tag     = var.producer_candidate_tag
    }
  }

  dynamic "traffic" {
    for_each = var.release_phase != "dark_no_traffic" ? [1] : []
    content {
      type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
      percent = 100
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]

    precondition {
      condition     = local.image_is_immutable
      error_message = "un digest @sha256 es obligatorio al crear servicios."
    }
    precondition {
      condition     = local.shadow_rate_ok
      error_message = "shadow_sample_rate=100 sólo es válido con release_phase=shadow."
    }
    precondition {
      condition     = local.mode_ok
      error_message = "las fases dark_* exigen ticket_handler_mode=disabled."
    }
    precondition {
      condition = (
        var.release_phase != "dark_no_traffic" ||
        trimspace(var.producer_baseline_revision) != ""
      )
      error_message = "dark_no_traffic exige producer_baseline_revision inmutable."
    }
    precondition {
      condition = (
        var.env != "production" ||
        alltrue([
          for key in local.required_producer_core_env :
          trimspace(lookup(var.producer_core_env, key, "")) != ""
        ])
      )
      error_message = "producción exige el mapa completo de variables core observadas del producer."
    }
    precondition {
      condition = (
        var.env != "production" ||
        alltrue([
          for key in local.required_producer_secret_env :
          trimspace(lookup(var.secret_version_refs, key, "")) != ""
        ])
      )
      error_message = "producción exige versiones numéricas de todos los secretos observados."
    }
    precondition {
      condition = (
        length(setintersection(
          toset(keys(var.producer_core_env)),
          local.producer_managed_env_names,
        )) == 0 &&
        length(setintersection(
          toset(keys(var.producer_core_env)),
          toset(keys(var.secret_version_refs)),
        )) == 0
      )
      error_message = "producer_core_env no puede sobrescribir env gestionado ni secretos."
    }
    precondition {
      condition     = can(regex("^https://", var.worker_url))
      error_message = "worker_url https es obligatorio al crear servicios."
    }
    precondition {
      condition = (
        var.env != "production" ||
        var.ticket_handler_mode == "disabled" ||
        (
          trimspace(var.ticket_wif_audience) != "" &&
          trimspace(var.ticket_wif_expected_email) != ""
        )
      )
      error_message = "un producer activo en producción exige audiencia y SA esperada WIF."
    }
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
    service_account                  = var.worker_sa_email
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
      env {
        name  = "TICKET_WORKER_URL"
        value = var.worker_url
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      # El worker inicializa el mismo RAG/LLM/ForusBots que el producer. Sin
      # este mapa heredaría defaults distintos (incluido un endpoint HTTP de
      # ForusBots) y la imagen validada no podría arrancar en producción.
      dynamic "env" {
        for_each = var.producer_core_env
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

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    precondition {
      condition     = local.image_is_immutable
      error_message = "un digest @sha256 es obligatorio al crear servicios."
    }
    precondition {
      condition     = can(regex("^https://", var.worker_url))
      error_message = "worker_url https es obligatorio al crear el worker."
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
        env {
          name  = "TICKET_HANDLER_MODE"
          value = var.ticket_handler_mode
        }
        env {
          name  = "TICKET_WORKER_URL"
          value = var.worker_url
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
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.image_is_immutable
      error_message = "un digest @sha256 es obligatorio al crear servicios."
    }
    precondition {
      condition     = can(regex("^https://", var.worker_url))
      error_message = "worker_url https es obligatorio al crear el reconciliador."
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

# worker_url es una entrada gateada porque usar el output del propio servicio
# como env produciría una dependencia circular. Debe ser la URL/audiencia
# estable que Cloud Tasks y el verificador OIDC comparten.
