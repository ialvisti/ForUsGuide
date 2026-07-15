output "runtime_service_accounts" {
  value = { for k, sa in google_service_account.runtime : k => sa.email }
}

output "evidence_bucket" {
  value = google_storage_bucket.evidence.name
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
