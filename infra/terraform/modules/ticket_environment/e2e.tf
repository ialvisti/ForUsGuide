# Runner E2E aislado. Su imagen contiene tests y por contrato nunca puede ser
# el digest del runtime productivo.
locals {
  e2e_secret_keys = toset([
    "E2E_API_KEY",
    "E2E_DIFFERENTIAL_LEGACY_API_KEY",
    "E2E_WRONG_PRINCIPAL_API_KEY",
    "E2E_WRONG_TENANT_API_KEY",
    "E2E_RATE_LIMIT_API_KEY",
    "E2E_FAULT_SIGNING_SECRET",
    "E2E_N8N_CONTRACT_TOKEN",
    "E2E_FORUSBOTS_LOOKUP_TOKEN",
    "E2E_DELIVERY_LOOKUP_TOKEN",
    "E2E_GCP_AUDIT_TOKEN",
    "PINECONE_API_KEY",
  ])
  e2e_secret_inventory_exact = (
    toset(keys(var.e2e_secret_containers)) == local.e2e_secret_keys &&
    length(toset(values(var.e2e_secret_containers))) == length(local.e2e_secret_keys) &&
    toset(keys(var.e2e_job.secret_version_refs)) == local.e2e_secret_keys &&
    alltrue([
      for key, secret_id in var.e2e_secret_containers :
      startswith(
        var.e2e_job.secret_version_refs[key],
        "projects/${var.project_id}/secrets/${secret_id}/versions/",
      )
    ])
  )
}

resource "google_cloud_run_v2_job" "e2e" {
  count    = var.e2e_job.enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = "ticket-e2e-staging"

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = var.e2e_job.service_account_email
      max_retries     = 0
      # 20 casos E2E + 3 diferenciales son secuenciales y cada uno puede agotar
      # 2700 s de poll. 72000 s incluye audit/teardown; pytest corta al primer
      # fallo, por lo que un sistema roto no consume todos los timeouts.
      timeout = "72000s"

      containers {
        image = var.e2e_job.image_digest
        args  = ["all"]

        env {
          name  = "APP_ROLE"
          value = "e2e"
        }
        env {
          name  = "API_BASE_URL"
          value = google_cloud_run_v2_service.producer[0].uri
        }
        env {
          name  = "TICKET_PRODUCER_URL"
          value = google_cloud_run_v2_service.producer[0].uri
        }
        env {
          name  = "E2E_ENVIRONMENT"
          value = var.env
        }
        env {
          name  = "E2E_GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "E2E_GCP_REGION"
          value = var.region
        }
        env {
          name  = "E2E_PRODUCER_URL"
          value = google_cloud_run_v2_service.producer[0].uri
        }
        env {
          name  = "E2E_SECONDARY_PRODUCER_URL"
          value = local.producer_baseline_url
        }
        env {
          name  = "E2E_PRODUCER_SERVICE"
          value = var.producer_service_name
        }
        env {
          name  = "E2E_WORKER_SERVICE"
          value = var.worker_service_name
        }
        env {
          name  = "E2E_FIRESTORE_DATABASE"
          value = var.firestore_database
        }
        env {
          name  = "E2E_QUEUE"
          value = var.queue_name
        }
        env {
          name  = "E2E_RECONCILER_JOB"
          value = var.reconciler_job_name
        }
        env {
          name  = "E2E_RUNTIME_DIGEST"
          value = var.image_digest
        }
        env {
          name  = "E2E_RUNNER_DIGEST"
          value = var.e2e_job.image_digest
        }
        env {
          name  = "E2E_RUNNER_SERVICE_ACCOUNT"
          value = var.e2e_job.service_account_email
        }
        env {
          name  = "E2E_EVIDENCE_PATH"
          value = "/app/evidence/14-staging-e2e.json"
        }
        dynamic "env" {
          for_each = var.e2e_job.nonsecret_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = var.e2e_job.secret_version_refs
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

  lifecycle {
    precondition {
      condition     = var.env == "staging"
      error_message = "el Run Job E2E está prohibido fuera de staging."
    }
    precondition {
      condition     = local.create_services
      error_message = "E2E exige un producer staging desplegado."
    }
    precondition {
      condition     = var.e2e_job.image_digest != var.image_digest
      error_message = "el digest E2E nunca puede usarse como imagen runtime."
    }
    precondition {
      condition     = local.e2e_secret_inventory_exact
      error_message = "E2E exige once secret containers exactos y refs numéricas que apunten a esos IDs."
    }
  }
}
