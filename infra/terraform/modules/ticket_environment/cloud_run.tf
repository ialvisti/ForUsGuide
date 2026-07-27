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
    "LLM_ROUTE_EXTRACT_INQUIRIES",
    "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
    "LLM_ROUTE_FORUSBOTS_FIELD_MAP",
    "LLM_ROUTE_GR_BODY_BUILD",
    "LLM_ROUTE_TICKET_FIELD_EXTRACT",
    "LOG_LEVEL",
    "NAMESPACE",
    "OPENAI_MODEL",
    "OPENAI_REASONING_EFFORT",
    "TICKET_LLM_PRICING_JSON",
    "USE_VERTEX_AI",
  ])
  required_producer_secret_env = toset([
    "API_KEY",
    "FORUSBOTS_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "PINECONE_API_KEY",
  ])
  active_producer_secret_env = setunion(toset([
    "API_KEY",
    "FORUSBOTS_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "PINECONE_API_KEY",
    ]), var.env == "staging" ? toset([
    "TICKET_FAULT_SIGNING_SECRET",
  ]) : toset([]))
  worker_runtime_secret_env = setunion(toset([
    "FORUSBOTS_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "PINECONE_API_KEY",
    ]), var.env == "staging" && var.ticket_handler_mode != "disabled" ? toset([
    "TICKET_FAULT_SIGNING_SECRET",
  ]) : toset([]))

  # Todo servicio creado debe poder arrancar, incluso en dark: el producer
  # sigue sirviendo core/polling y el worker puede terminar jobs ya admitidos.
  # Production conserva desde el primer import sus siete secrets completos;
  # staging dark usa el mínimo de arranque y una fase activa añade v2/fault.
  expected_runtime_secret_env = (
    var.env == "production" || var.ticket_handler_mode != "disabled" ?
    local.active_producer_secret_env : local.required_producer_secret_env
  )
  expected_secret_accessor_roles_all = {
    API_KEY                     = toset(["producer"])
    FORUSBOTS_AUTH_TOKEN        = toset(["worker"])
    OPENAI_API_KEY              = toset(["producer", "worker"])
    PINECONE_API_KEY            = toset(["producer", "worker"])
    TICKET_FAULT_SIGNING_SECRET = toset(["producer", "worker"])
  }
  expected_secret_accessor_roles = {
    for key in local.expected_runtime_secret_env :
    key => local.expected_secret_accessor_roles_all[key]
  }
  runtime_secret_ref_prefixes = {
    for key, secret_id in var.secret_containers.ids :
    key => "projects/${var.project_id}/secrets/${secret_id}/versions/"
  }
  runtime_secret_refs_exact = (
    var.secret_containers.enabled &&
    toset(keys(var.secret_version_refs)) == local.expected_runtime_secret_env &&
    toset(keys(var.secret_containers.ids)) == local.expected_runtime_secret_env &&
    alltrue([
      for key in local.expected_runtime_secret_env :
      startswith(
        lookup(var.secret_version_refs, key, ""),
        lookup(local.runtime_secret_ref_prefixes, key, "__invalid__"),
        ) && can(regex(
          "^[0-9]+$",
          trimprefix(
            lookup(var.secret_version_refs, key, ""),
            lookup(local.runtime_secret_ref_prefixes, key, "__invalid__"),
          ),
      ))
    ])
  )
  runtime_secret_accessors_exact = (
    var.secret_containers.enabled &&
    toset(keys(var.secret_containers.accessor_roles)) == local.expected_runtime_secret_env &&
    alltrue([
      for key in local.expected_runtime_secret_env :
      lookup(var.secret_containers.accessor_roles, key, toset([])) ==
      local.expected_secret_accessor_roles[key]
    ])
  )
  pricing_manifest = try(
    jsondecode(lookup(var.producer_core_env, "TICKET_LLM_PRICING_JSON", "")),
    {},
  )
  pricing_rate_fields = toset([
    "input_usd_per_million",
    "output_usd_per_million",
  ])
  pricing_manifest_is_reviewed = try(
    toset(keys(local.pricing_manifest)) == toset([
      "pricing_as_of", "source", "models",
    ]) &&
    local.pricing_manifest.pricing_as_of == "2026-07-21" &&
    local.pricing_manifest.source == "openai-google-official-public-pricing" &&
    toset(keys(local.pricing_manifest.models)) == local.expected_pricing_model_keys &&
    alltrue([
      for model in values(local.pricing_manifest.models) :
      toset(keys(model)) == local.pricing_rate_fields && alltrue([
        for field in local.pricing_rate_fields :
        # jsonencode distingue un JSON number de un string numérico aunque
        # tonumber acepte ambos. El bound además rechaza magnitudes no
        # revisadas/no finitas antes de que arranque el runtime.
        jsonencode(model[field]) == jsonencode(tonumber(model[field])) &&
        tonumber(model[field]) >= 0 && tonumber(model[field]) <= 500
      ])
    ]),
    false,
  )
  runtime_core_env_complete = (
    toset(keys(var.producer_core_env)) == local.required_producer_core_env &&
    alltrue([
      for key in local.required_producer_core_env :
      trimspace(lookup(var.producer_core_env, key, "")) != ""
    ])
  )
  reviewed_route_env_names = toset([
    "LLM_ROUTE_CLASSIFY", "LLM_ROUTE_DECOMPOSE", "LLM_ROUTE_GR_OUTCOME",
    "LLM_ROUTE_GR_RESPONSE", "LLM_ROUTE_KNOWLEDGE", "LLM_ROUTE_REQUIRED_DATA",
    "LLM_ROUTE_EXTRACT_INQUIRIES", "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
    "LLM_ROUTE_FORUSBOTS_FIELD_MAP", "LLM_ROUTE_GR_BODY_BUILD",
    "LLM_ROUTE_TICKET_FIELD_EXTRACT",
  ])
  runtime_route_models_valid = alltrue([
    for key in local.reviewed_route_env_names : can(regex(
      "^(gpt-|gemini-)",
      lower(trimspace(lookup(var.producer_core_env, key, ""))),
    ))
  ])
  configured_route_pricing_keys = toset([
    for key in local.reviewed_route_env_names :
    "${startswith(lower(lookup(var.producer_core_env, key, "")), "gpt-") ? "openai" : "gemini"}:${lower(lookup(var.producer_core_env, key, ""))}"
  ])
  expected_pricing_model_keys = setunion(
    local.configured_route_pricing_keys,
    length([
      for key in local.configured_route_pricing_keys : key
      if startswith(key, "openai:")
    ]) > 0 ? toset(["gemini:gemini-2.5-pro"]) : toset([]),
    length([
      for key in local.configured_route_pricing_keys : key
      if startswith(key, "gemini:")
    ]) > 0 ? toset(["openai:gpt-5.5"]) : toset([]),
  )
  forusbots_origin_is_canonical = (
    can(regex(
      "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$",
      trimspace(lookup(var.producer_core_env, "FORUSBOTS_BASE_URL", "")),
    )) ||
    can(regex(
      "^http://35\\.224\\.156\\.104:10000/?$",
      trimspace(lookup(var.producer_core_env, "FORUSBOTS_BASE_URL", "")),
    ))
  )
  producer_managed_env_names = setunion(toset(keys(local.common_env)), toset([
    "APP_ROLE",
    "TICKET_HANDLER_MODE",
    "TICKET_SHADOW_SAMPLE_RATE",
    "TICKET_WORKER_URL",
    "TICKET_WORKER_AUDIENCE",
    "TICKET_WORKER_SERVICE_ACCOUNT",
  ]))
  # El worker no sirve APIs de cliente y no necesita API_KEY. No inyectar una
  # referencia que su SA no puede resolver; el resto sí es runtime RAG/LLM y
  # ForusBots requerido por ese rol.
  worker_secret_version_refs = {
    for key, ref in var.secret_version_refs : key => ref
    if contains(local.worker_runtime_secret_env, key)
  }
  producer_runtime_secret_env = setsubtract(
    local.expected_runtime_secret_env,
    toset(["FORUSBOTS_AUTH_TOKEN"]),
  )
  producer_secret_version_refs = {
    for key, ref in var.secret_version_refs : key => ref
    if contains(local.producer_runtime_secret_env, key)
  }
  production_producer_identity_is_dedicated = (
    var.env != "production" ||
    var.producer_sa_email == "ticket-producer-prod@${var.project_id}.iam.gserviceaccount.com"
  )
}

# --- Producer: API COMPLETA existente (v1/v2/status + core no-ticket) -------
resource "google_cloud_run_v2_service" "producer" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.producer_service_name
  ingress  = var.producer_ingress
  depends_on = [
    google_secret_manager_secret_iam_member.runtime_accessor,
    google_storage_bucket_iam_member.producer_core_objects,
  ]

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
        value = local.worker_target_url
      }
      env {
        name  = "TICKET_WORKER_AUDIENCE"
        value = local.worker_oidc_audience
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
        for_each = var.producer_core_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.producer_secret_version_refs
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
      tag      = var.producer_baseline_tag
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
    for_each = var.release_phase != "dark_no_traffic" && var.e2e_job.enabled ? [var.producer_baseline_revision] : []
    content {
      type     = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
      revision = var.producer_baseline_revision
      percent  = 0
      tag      = var.producer_baseline_tag
    }
  }

  dynamic "traffic" {
    for_each = var.release_phase != "dark_no_traffic" ? [1] : []
    content {
      type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
      percent = 100
      tag     = var.producer_candidate_tag
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
      condition     = local.production_producer_identity_is_dedicated
      error_message = "todo producer production exige la SA dedicada ticket-producer-prod, incluso en dark."
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
        !var.e2e_job.enabled ||
        (trimspace(var.producer_baseline_revision) != "" &&
        var.producer_baseline_tag != var.producer_candidate_tag)
      )
      error_message = "E2E exige revisión baseline inmutable y tags baseline/candidate distintos."
    }
    precondition {
      condition = (
        local.runtime_core_env_complete &&
        local.runtime_route_models_valid &&
        local.pricing_manifest_is_reviewed
      )
      error_message = "todo producer/worker desplegado exige core env exacto, pricing revisado y rutas LLM con proveedor válido."
    }
    precondition {
      condition     = local.forusbots_origin_is_canonical
      error_message = "FORUSBOTS_BASE_URL debe ser un origen canónico revisado sin credenciales, path, query ni fragment."
    }
    precondition {
      condition     = local.runtime_secret_refs_exact
      error_message = "cada secret_version_ref debe coincidir con project/key/container y versión numérica exactos."
    }
    precondition {
      condition     = local.runtime_secret_accessors_exact
      error_message = "los accessors de secrets deben coincidir exactamente con el rol runtime que los consume."
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
      condition = (
        var.ticket_handler_mode == "disabled" ||
        toset(keys(var.secret_version_refs)) == local.active_producer_secret_env
      )
      error_message = "un producer activo exige el set exacto de secretos del runtime; fault sólo staging."
    }
  }
}

# --- Worker PRIVADO: sólo health + ruta interna de Cloud Tasks -------------
resource "google_cloud_run_v2_service" "worker" {
  count            = local.create_services ? 1 : 0
  project          = var.project_id
  location         = var.region
  name             = var.worker_service_name
  ingress          = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences = [local.worker_oidc_audience]
  depends_on       = [google_secret_manager_secret_iam_member.runtime_accessor]

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
        name  = "TICKET_WORKER_AUDIENCE"
        value = local.worker_oidc_audience
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
        for_each = local.worker_secret_version_refs
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
          value = local.worker_target_url
        }
        env {
          name  = "TICKET_WORKER_AUDIENCE"
          value = local.worker_oidc_audience
        }
        env {
          name  = "TICKET_WORKER_SERVICE_ACCOUNT"
          value = var.task_signer_sa_email
        }
        env {
          name  = "TICKET_LLM_PRICING_JSON"
          value = lookup(var.producer_core_env, "TICKET_LLM_PRICING_JSON", "")
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
  }
}

locals {
  worker_target_url = local.create_services ? google_cloud_run_v2_service.worker[0].uri : ""
}
