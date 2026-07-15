# AWS Workload Identity Federation para n8n (plan Tarea 10 Paso 5).
# enable_n8n_wif=false por defecto: sin el ARN de n8n (Tarea 1 Paso 3) NO se
# crea el pool/provider ni el binding — nunca se inventa/wildcardea. El
# provider restringe cuenta AWS + dos roles de entorno distintos. La sesión
# STS cambia en cada ejecución, por eso se extrae un atributo aws_role estable
# y los bindings apuntan a principalSet por role, nunca al ARN de sesión.

locals {
  n8n_aws_role_names = {
    staging    = basename(var.n8n_aws_role_arns.staging)
    production = basename(var.n8n_aws_role_arns.production)
  }
}

resource "google_iam_workload_identity_pool" "n8n" {
  count                     = var.enable_n8n_wif ? 1 : 0
  project                   = var.project_id
  workload_identity_pool_id = "n8n-aws-pool"
  display_name              = "n8n AWS WIF pool"

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9]{12}$", var.n8n_aws_account_id))
      error_message = "WIF habilitado exige el account ID AWS de 12 dígitos."
    }
    precondition {
      condition = (
        startswith(var.n8n_aws_role_arns.staging, "arn:aws:iam::${var.n8n_aws_account_id}:role/") &&
        startswith(var.n8n_aws_role_arns.production, "arn:aws:iam::${var.n8n_aws_account_id}:role/") &&
        local.n8n_aws_role_names.staging != local.n8n_aws_role_names.production
      )
      error_message = "WIF exige roles AWS exactos y distintos para staging y production en la cuenta declarada."
    }
  }
}

resource "google_iam_workload_identity_pool_provider" "n8n_aws" {
  count                              = var.enable_n8n_wif ? 1 : 0
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.n8n[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "n8n-aws"
  display_name                       = "n8n AWS provider"

  aws {
    account_id = var.n8n_aws_account_id
  }

  attribute_condition = "attribute.aws_account == \"${var.n8n_aws_account_id}\" && (attribute.aws_role == \"${local.n8n_aws_role_names.staging}\" || attribute.aws_role == \"${local.n8n_aws_role_names.production}\")"
  attribute_mapping = {
    "google.subject"        = "assertion.arn"
    "attribute.aws_account" = "assertion.account"
    "attribute.aws_role"    = "assertion.arn.extract('assumed-role/{role_name}/')"
  }
}

# workloadIdentityUser SÓLO sobre las SA invoker de cada entorno.
resource "google_service_account_iam_member" "n8n_wif_stg" {
  count              = var.enable_n8n_wif ? 1 : 0
  service_account_id = "projects/${var.project_id}/serviceAccounts/n8n-ticket-invoker-stg@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.n8n[0].name}/attribute.aws_role/${local.n8n_aws_role_names.staging}"
}

resource "google_service_account_iam_member" "n8n_wif_prod" {
  count              = var.enable_n8n_wif ? 1 : 0
  service_account_id = "projects/${var.project_id}/serviceAccounts/n8n-ticket-invoker-prod@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.n8n[0].name}/attribute.aws_role/${local.n8n_aws_role_names.production}"
}
