# Containers únicamente: Terraform no crea ni lee payloads/versiones.
resource "google_secret_manager_secret" "runtime" {
  for_each  = var.secret_containers.enabled ? var.secret_containers.ids : {}
  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  secret_role_members = {
    producer = var.producer_sa_email
    worker   = var.worker_sa_email
    e2e      = var.e2e_job.service_account_email
  }
  secret_accessor_grants = flatten([
    for secret_key, roles in var.secret_containers.accessor_roles : [
      for runtime_role in roles : {
        key          = "${secret_key}:${runtime_role}"
        secret_key   = secret_key
        runtime_role = runtime_role
        member       = lookup(local.secret_role_members, runtime_role, "")
      }
    ]
  ])
}

resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = var.secret_containers.enabled ? {
    for grant in local.secret_accessor_grants : grant.key => grant
  } : {}

  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime[each.value.secret_key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.member}"

  lifecycle {
    precondition {
      condition     = each.value.runtime_role != "e2e" || (var.env == "staging" && var.e2e_job.enabled)
      error_message = "el accessor e2e sólo existe con el Run Job staging habilitado."
    }
    precondition {
      condition     = trimspace(each.value.member) != ""
      error_message = "cada accessor exige una service account explícita."
    }
  }
}
