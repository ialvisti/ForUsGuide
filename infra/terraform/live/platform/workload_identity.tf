# AWS Workload Identity Federation para n8n (plan Tarea 10 Paso 5).
# enable_n8n_wif=false por defecto: sin el ARN de n8n (Tarea 1 Paso 3) NO se
# crea el pool/provider ni el binding — nunca se inventa/wildcardea. El
# provider restringe cuenta AWS + ARN exacto por attribute condition; sólo ese
# principal obtiene workloadIdentityUser sobre la SA n8n de su entorno.

resource "google_iam_workload_identity_pool" "n8n" {
  count                     = var.enable_n8n_wif ? 1 : 0
  project                   = var.project_id
  workload_identity_pool_id = "n8n-aws-pool"
  display_name              = "n8n AWS WIF pool"
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

  # Sólo el execution role EXACTO de n8n (nunca usuario genérico ni wildcard).
  attribute_condition = "assertion.arn == \"${var.n8n_aws_role_arn}\""
  attribute_mapping = {
    "google.subject" = "assertion.arn"
  }
}

# workloadIdentityUser SÓLO sobre las SA invoker de cada entorno.
resource "google_service_account_iam_member" "n8n_wif_stg" {
  count              = var.enable_n8n_wif ? 1 : 0
  service_account_id = google_service_account.runtime["n8n-ticket-invoker-stg"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.n8n[0].name}/subject/${var.n8n_aws_role_arn}"
}

resource "google_service_account_iam_member" "n8n_wif_prod" {
  count              = var.enable_n8n_wif ? 1 : 0
  service_account_id = google_service_account.runtime["n8n-ticket-invoker-prod"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.n8n[0].name}/subject/${var.n8n_aws_role_arn}"
}
