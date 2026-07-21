output "runtime_service_accounts" {
  value = { for k, sa in google_service_account.runtime : k => sa.email }
}

output "evidence_bucket" {
  value = google_storage_bucket.evidence.name
}

output "artifact_image_paths" {
  description = "Paquetes separados dentro del repo existente; toda promoción añade @sha256."
  value = {
    runtime            = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/kb-rag-system"
    e2e                = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/kb-rag-e2e"
    release_controller = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/release-controller"
  }
}

output "release_controller_builder_service_account" {
  value = google_service_account.controller_builder.email
}

output "wif_provider" {
  value = var.enable_n8n_wif ? google_iam_workload_identity_pool_provider.n8n_aws[0].name : null
}

output "firestore_scope_phase" {
  description = "Fase G1C efectiva; consumers sólo pueden avanzar tras enforce."
  value       = var.firestore_scope_migration.enabled ? var.firestore_scope_migration.phase : "disabled"
}

output "firestore_scope_enforced" {
  value = var.firestore_scope_migration.enabled && var.firestore_scope_migration.phase == "enforce"
}

output "pipeline_service_accounts" {
  description = "Identidades privilegiadas por pipeline; vacío antes de G1B."
  value       = local.privileged_pipeline_sas
}

output "environment_handoff_phase" {
  description = "Fase bifásica efectiva por environment; el controller exige managed antes de rollout."
  value       = var.environment_handoff_phase
}

output "environment_container_phase" {
  description = "Ownership de containers activado sólo tras gate combinado G1B+G2/G6B."
  value       = var.environment_container_phase
}

output "environment_run_resources" {
  description = "Inventario atestado de services/jobs que ya reciben IAM directo."
  value       = var.environment_run_resources
}

output "environment_release_phase" {
  description = "Fase atestada que gobierna fail-closed el scheduler del reconciliador."
  value       = var.environment_release_phase
}

output "environment_secret_ids" {
  description = "Inventario exacto de containers Secret Manager por environment; nunca incluye versiones/payload."
  value       = var.environment_secret_ids
}
