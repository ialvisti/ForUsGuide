output "worker_url" {
  description = "URL del worker privado (audiencia OIDC de las tasks)."
  value       = local.create_services ? google_cloud_run_v2_service.worker[0].uri : null
}

output "producer_url" {
  value = local.create_services ? google_cloud_run_v2_service.producer[0].uri : null
}

output "e2e_secondary_producer_url" {
  description = "URL taggeada de la revisión baseline; sólo existe para el gate E2E staging."
  value       = var.e2e_job.enabled ? local.producer_baseline_url : null
}

output "queue_id" {
  value = "projects/${var.project_id}/locations/${var.region}/queues/${var.queue_name}"
}

output "firestore_database" {
  value = var.firestore_database
}

output "custom_queue_role_id" {
  value = "projects/${var.project_id}/roles/ticketQueueEnqueuer${title(var.env)}"
}

output "managed_secret_containers" {
  description = "IDs de containers platform referenciados; nunca incluye versiones ni payloads."
  value       = var.secret_containers.enabled ? var.secret_containers.ids : {}
}

output "e2e_job_name" {
  value = var.e2e_job.enabled ? google_cloud_run_v2_job.e2e[0].name : null
}
