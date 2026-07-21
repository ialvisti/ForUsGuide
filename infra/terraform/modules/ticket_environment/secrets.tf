# Containers se crean/importan en platform. Este root nunca crea, borra ni lee
# payloads/versiones; sólo declara accessor IAM sobre IDs existentes aprobados.

locals {
  secret_role_members = {
    producer = var.producer_sa_email
    worker   = var.worker_sa_email
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
  secret_id = var.secret_containers.ids[each.value.secret_key]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.member}"

  lifecycle {
    precondition {
      condition     = contains(["producer", "worker"], each.value.runtime_role)
      error_message = "los secrets runtime sólo admiten accessors producer/worker."
    }
    precondition {
      condition     = trimspace(each.value.member) != ""
      error_message = "cada accessor exige una service account explícita."
    }
  }
}

# Contratos/probes E2E son secretos externos y no forman parte del inventario
# runtime. No se crean ni se leen aquí: sólo se concede access a sus once IDs
# exactos a la SA E2E de staging.
resource "google_secret_manager_secret_iam_member" "e2e_runtime_accessor" {
  for_each = var.e2e_job.enabled && var.env == "staging" ? var.e2e_secret_containers : {}

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.e2e_job.service_account_email}"

  lifecycle {
    precondition {
      condition     = var.env == "staging" && local.e2e_secret_inventory_exact
      error_message = "los accessors E2E sólo pueden existir para los once secretos staging exactos."
    }
  }
}
