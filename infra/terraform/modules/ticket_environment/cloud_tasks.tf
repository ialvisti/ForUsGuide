# Cloud Tasks: cola con concurrencia/tasa acotadas y retry (plan Tarea 10
# Paso 4). max_attempts=5 NO es cap duro junto con max_retry_duration; la
# garantía real la da job_deadline_at en la app.

resource "google_cloud_tasks_queue" "ticket" {
  project  = var.project_id
  location = var.region
  name     = var.queue_name

  rate_limits {
    max_concurrent_dispatches = var.queue_max_concurrent_dispatches
    max_dispatches_per_second = var.queue_max_dispatches_per_second
  }

  retry_config {
    max_attempts       = 5
    max_retry_duration = "1800s"
    min_backoff        = "30s"
    max_backoff        = "120s"
    max_doublings      = 2
  }
}
