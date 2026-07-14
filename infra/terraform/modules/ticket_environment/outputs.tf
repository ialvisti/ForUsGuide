output "worker_url" {
  description = "URL del worker privado (audiencia OIDC de las tasks)."
  value       = local.create_services ? google_cloud_run_v2_service.worker[0].uri : null
}

output "producer_url" {
  value = local.create_services ? google_cloud_run_v2_service.producer[0].uri : null
}

output "queue_id" {
  value = google_cloud_tasks_queue.ticket.id
}

output "firestore_database" {
  value = var.firestore_database
}

output "custom_queue_role_id" {
  value = google_project_iam_custom_role.ticket_queue_enqueuer.id
}
