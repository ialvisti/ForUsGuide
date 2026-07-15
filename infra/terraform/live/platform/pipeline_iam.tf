# Acceso de pipelines a state/evidence. Los mapas codifican el ownership por
# entorno; no existe una lista global que pueda otorgar acceso cross-state.
locals {
  state_pipelines = var.cicd_bootstrap.enabled ? {
    platform = {
      bucket      = "rag-kb-system-tfstate-platform-900340137010"
      plan_email  = google_service_account.plan_platform[0].email
      apply_email = google_service_account.apply_platform[0].email
    }
    staging = {
      bucket      = "rag-kb-system-tfstate-staging-900340137010"
      plan_email  = google_service_account.plan_staging[0].email
      apply_email = google_service_account.apply_staging[0].email
    }
    production = {
      bucket      = "rag-kb-system-tfstate-production-900340137010"
      plan_email  = google_service_account.plan_production[0].email
      apply_email = google_service_account.apply_production[0].email
    }
  } : {}

  privileged_pipeline_sas = var.cicd_bootstrap.enabled ? {
    platform-plan     = google_service_account.plan_platform[0].email
    platform-apply    = google_service_account.apply_platform[0].email
    staging-plan      = google_service_account.plan_staging[0].email
    staging-apply     = google_service_account.apply_staging[0].email
    production-plan   = google_service_account.plan_production[0].email
    production-apply  = google_service_account.apply_production[0].email
    staging-attest    = google_service_account.staging_attest[0].email
    evidence-manifest = google_service_account.evidence_manifest[0].email
    test-only         = google_service_account.test_only[0].email
    e2e-image         = google_service_account.e2e_image[0].email
  } : {}

  controller_repository = var.cicd_bootstrap.enabled ? split("/", var.cicd_bootstrap.release_controller_image_digest)[2] : ""
}

resource "google_project_iam_custom_role" "terraform_plan_lock" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTerraformPlanLock"
  title       = "Ticket Terraform plan lock"
  description = "Create/delete exclusivamente el lock del backend; sin state write."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
  ]
}

resource "google_storage_bucket_iam_member" "plan_state_viewer" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value.plan_email}"
}

resource "google_storage_bucket_iam_member" "plan_state_lock" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = google_project_iam_custom_role.terraform_plan_lock[0].id
  member   = "serviceAccount:${each.value.plan_email}"

  condition {
    title       = "${each.key}_default_workspace_lock"
    description = "Sólo state/default.tflock del backend ${each.key}."
    expression  = "resource.name == \"projects/_/buckets/${each.value.bucket}/objects/state/default.tflock\""
  }
}

resource "google_storage_bucket_iam_member" "apply_state_admin" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${each.value.apply_email}"
}

# Plan crea objetos nuevos; apply sólo lee plan/manifest aprobado.
resource "google_storage_bucket_iam_member" "plan_evidence_writer" {
  for_each = local.state_pipelines
  bucket   = google_storage_bucket.evidence.name
  role     = "roles/storage.objectCreator"
  member   = "serviceAccount:${each.value.plan_email}"
}

resource "google_storage_bucket_iam_member" "apply_evidence_reader" {
  for_each = local.state_pipelines
  bucket   = google_storage_bucket.evidence.name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value.apply_email}"
}

# Permisos de evidencia exclusivos de pipelines sin state/deploy.
resource "google_storage_bucket_iam_member" "ci_evidence_writer" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_storage_bucket_iam_member" "aux_evidence_writer" {
  for_each = var.cicd_bootstrap.enabled ? {
    staging-attest    = google_service_account.staging_attest[0].email
    evidence-manifest = google_service_account.evidence_manifest[0].email
    e2e-image         = google_service_account.e2e_image[0].email
  } : {}
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${each.value}"
}

resource "google_storage_bucket_iam_member" "aux_evidence_reader" {
  for_each = var.cicd_bootstrap.enabled ? {
    production-plan   = google_service_account.plan_production[0].email
    staging-attest    = google_service_account.staging_attest[0].email
    evidence-manifest = google_service_account.evidence_manifest[0].email
    test-only         = google_service_account.test_only[0].email
  } : {}
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value}"
}

# Pull del controller limitado al repositorio inferido del digest fijado.
resource "google_artifact_registry_repository_iam_member" "controller_reader" {
  for_each   = local.privileged_pipeline_sas
  project    = var.project_id
  location   = var.region
  repository = local.controller_repository
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"

  lifecycle {
    precondition {
      condition = startswith(
        var.cicd_bootstrap.release_controller_image_digest,
        "${var.region}-docker.pkg.dev/${var.project_id}/",
      )
      error_message = "el release-controller debe vivir en Artifact Registry del proyecto/región declarados."
    }
  }
}

# Custom build SAs necesitan emitir logs; ninguna recibe Compute default,
# editor ni permisos implícitos de deploy/state.
resource "google_project_iam_member" "pipeline_logs" {
  for_each = merge(
    { ci = google_service_account.ci.email },
    local.privileged_pipeline_sas,
  )
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${each.value}"
}
