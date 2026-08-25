locals {
  named_database_writers = local.workload_enabled ? {
    console = google_service_account.console.email
    ingest  = google_service_account.ingest.email
  } : {}
}

# The console and ingest service can read/write only the named evaluation
# database.  They receive no role on the RAG handler's `(default)` database.
resource "google_project_iam_member" "named_database_user" {
  for_each = local.named_database_writers
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${each.value}"

  condition {
    title       = "tickets_${var.environment}_${each.key}_named_database"
    description = "Runtime restricted to the ticket evaluation database."
    expression  = "resource.name == \"projects/${var.project_id}/databases/${local.console_database}\""
  }

  depends_on = [google_firestore_database.console]
}

# The evidence broker is intentionally read-only and is the only new identity
# with access to `(default)`.  Its HTTP contract further bounds the collection
# and response shape because Firestore IAM cannot scope a role by collection.
resource "google_project_iam_member" "broker_default_database_viewer" {
  count   = local.workload_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.viewer"
  member  = "serviceAccount:${google_service_account.evidence_broker.email}"

  condition {
    title       = "tickets_${var.environment}_broker_default_database"
    description = "Read-only evidence broker access to the RAG evidence database."
    expression  = "resource.name == \"projects/${var.project_id}/databases/(default)\""
  }
}

resource "google_secret_manager_secret_iam_member" "console" {
  for_each = local.workload_enabled ? toset([
    "CSRF_SIGNING_SECRET",
    "CURSOR_AEAD_KEY",
    "DEVREV_TOKEN",
    "ROLE_BINDINGS",
  ]) : toset([])

  project   = var.project_id
  secret_id = local.secret_parts[each.value].secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.console.email}"
}

resource "google_secret_manager_secret_iam_member" "ingest" {
  for_each = local.workload_enabled ? toset([
    "CURSOR_AEAD_KEY",
    "DEVREV_TOKEN",
  ]) : toset([])

  project   = var.project_id
  secret_id = local.secret_parts[each.value].secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_secret_manager_secret_iam_member" "broker" {
  for_each = local.workload_enabled ? toset(["CORRELATION_KEYRING"]) : toset([])

  project   = var.project_id
  secret_id = local.secret_parts[each.value].secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.evidence_broker.email}"
}

# Producer and worker provide the immediate path; reconciler is the repair
# path. Runtime OIDC verifies the same closed identity set and custom audience,
# so Cloud Run IAM is not the only authorization boundary.
resource "google_cloud_run_v2_service_iam_member" "publisher_invokes_ingest" {
  for_each = local.workload_enabled ? local.expected_publisher_service_accounts_by_role : {}
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ingest[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

# Preserve the existing reconciler member while moving from the original
# single-instance resource to the keyed, three-publisher contract.
moved {
  from = google_cloud_run_v2_service_iam_member.publisher_invokes_ingest[0]
  to   = google_cloud_run_v2_service_iam_member.publisher_invokes_ingest["reconciler"]
}

# Only the console runtime can call the evidence broker.
resource "google_cloud_run_v2_service_iam_member" "console_invokes_broker" {
  count    = local.workload_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.evidence_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.console.email}"
}

# Direct Cloud Run IAP requires the IAP service agent to invoke the protected
# service.  Reviewers receive IAP access, never Cloud Run invoker directly.
resource "google_cloud_run_v2_service_iam_member" "iap_service_agent_invokes_console" {
  count    = local.workload_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.console[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_binding" "reviewers" {
  count                  = local.workload_enabled ? 1 : 0
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.console[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  members                = sort(tolist(var.reviewer_iap_members))
}
