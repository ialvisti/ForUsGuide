output "runtime_service_accounts" {
  value = { for k, sa in google_service_account.runtime : k => sa.email }
}

output "evidence_bucket" {
  value = google_storage_bucket.evidence.name
}

output "wif_provider" {
  value = var.enable_n8n_wif ? google_iam_workload_identity_pool_provider.n8n_aws[0].name : null
}
