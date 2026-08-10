output "ingest_url" {
  description = "Private Cloud Run origin delivered to the reconciler through the reviewed release manifest."
  value       = local.workload_enabled ? local.ingest_origin : null
}

output "ingest_audience" {
  description = "Exact custom OIDC audience for immutable evaluation delivery."
  value       = local.workload_enabled ? local.ingest_audience : null
}

output "console_url" {
  description = "Single IAP-protected reviewer URL."
  value       = local.workload_enabled ? local.console_url : null
}

output "console_iap_audience" {
  value = local.workload_enabled ? local.iap_audience : null
}

output "console_database" {
  value = google_firestore_database.console.name
}

output "console_service_account_email" {
  value = google_service_account.console.email
}

output "ingest_service_account_email" {
  value = google_service_account.ingest.email
}

output "evidence_broker_service_account_email" {
  value = google_service_account.evidence_broker.email
}

output "evidence_broker_url" {
  value = local.workload_enabled ? local.broker_origin : null
}
