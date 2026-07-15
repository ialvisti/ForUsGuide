# Runner E2E aislado. Su imagen contiene tests y por contrato nunca puede ser
# el digest del runtime productivo.
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
      timeout         = "1800s"

      containers {
        image = var.e2e_job.image_digest
        args  = ["-q", "-m", "staging_e2e", "tests/e2e/test_ticket_staging.py"]

        env {
          name  = "APP_ROLE"
          value = "e2e"
        }
        env {
          name  = "API_BASE_URL"
          value = var.e2e_job.producer_url
        }
        env {
          name  = "TICKET_PRODUCER_URL"
          value = var.e2e_job.producer_url
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
  }
}
