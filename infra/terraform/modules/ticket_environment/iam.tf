# IAM least-privilege con rutas excluyentes (plan Tarea 10 Paso 5).
# Custom role queue-scoped: sólo create/get task + get queue; NUNCA
# list/delete/run/pause/purge/update ni roles/cloudtasks.viewer a nivel
# proyecto.

resource "google_project_iam_custom_role" "ticket_queue_enqueuer" {
  project     = var.project_id
  role_id     = "ticketQueueEnqueuer${title(var.env)}"
  title       = "Ticket Queue Enqueuer (${var.env})"
  description = "create/get task + get queue; sin operaciones admin."
  permissions = [
    "cloudtasks.tasks.create",
    "cloudtasks.tasks.get",
    "cloudtasks.queues.get",
  ]
}

# Cloud Tasks sí publica IAM sobre Queue en el provider 5.x. El custom role se
# enlaza al recurso exacto, no al proyecto ni mediante una condición heredada.
resource "google_cloud_tasks_queue_iam_member" "producer_queue" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.ticket.name
  role     = google_project_iam_custom_role.ticket_queue_enqueuer.id
  member   = "serviceAccount:${var.producer_sa_email}"
}

resource "google_service_account_iam_member" "producer_actas_signer" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.task_signer_sa_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.producer_sa_email}"
}

# Task signer: sin permisos de datos; run.invoker SÓLO sobre el worker de su
# entorno (Cloud Run IAM valida el OIDC de la task).
resource "google_cloud_run_v2_service_iam_member" "signer_invokes_worker" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.task_signer_sa_email}"
}

# Service agent oficial de Cloud Tasks: emite el ID token de la task signer.
resource "google_service_account_iam_member" "tasks_agent_signs_as_signer" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.task_signer_sa_email}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
}

# Reconciliador: database propia + mismo custom queue role + actAs sobre el
# signer; SIN LLM/Pinecone/ForusBots.
resource "google_cloud_tasks_queue_iam_member" "reconciler_queue" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.ticket.name
  role     = google_project_iam_custom_role.ticket_queue_enqueuer.id
  member   = "serviceAccount:${var.reconciler_sa_email}"
}

resource "google_service_account_iam_member" "reconciler_actas_signer" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.task_signer_sa_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.reconciler_sa_email}"
}

# Scheduler SA: ejecuta ÚNICAMENTE su Run Job reconciliador; sin acceso a
# Firestore/Tasks/secrets.
resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_reconciler" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.reconciler[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}

# Caller n8n: run.invoker SÓLO producer cuando IAM lo exige (servicio
# privado). La validación fail-closed del token WIF ocurre DENTRO de v2.
# Nunca worker/reconciliador.
resource "google_cloud_run_v2_service_iam_member" "n8n_invokes_producer" {
  count    = local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.producer[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.n8n_invoker_sa_email}"
}

# Preservar consumidores existentes del producer (no retirar en este rollout).
resource "google_cloud_run_v2_service_iam_member" "producer_preserved_invokers" {
  for_each = local.create_services ? toset(var.producer_invoker_members) : toset([])
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.producer[0].name
  role     = "roles/run.invoker"
  member   = each.value
}

# E2E sólo invoca el producer de staging. Nunca se crea un binding hacia el
# worker ni se acepta una identidad E2E en production.
resource "google_cloud_run_v2_service_iam_member" "e2e_invokes_producer" {
  count    = var.e2e_job.enabled && local.create_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.producer[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.e2e_job.service_account_email}"
}

# Firestore no expone un recurso IAM específico de database en el provider
# google/google-beta 5.x. Google documenta el aislamiento per-database como un
# binding project IAM no autoritativo con una condición sobre el nombre exacto
# de la database. Igualdad (no startsWith) evita acceso al otro entorno.
locals {
  firestore_database_resource = "projects/${var.project_id}/databases/${var.firestore_database}"
  manage_producer_firestore_iam = (
    var.manage_producer_firestore_iam != null
    ? var.manage_producer_firestore_iam
    : var.env != "production"
  )
}

resource "google_project_iam_member" "producer_firestore" {
  count   = local.manage_producer_firestore_iam ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${var.producer_sa_email}"

  condition {
    title       = "ticket_${var.env}_producer_database"
    description = "Producer limitado a ${var.firestore_database}."
    expression  = "resource.name == \"${local.firestore_database_resource}\""
  }
}

resource "google_project_iam_member" "worker_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${var.worker_sa_email}"

  condition {
    title       = "ticket_${var.env}_worker_database"
    description = "Worker limitado a ${var.firestore_database}."
    expression  = "resource.name == \"${local.firestore_database_resource}\""
  }
}

resource "google_project_iam_member" "reconciler_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${var.reconciler_sa_email}"

  condition {
    title       = "ticket_${var.env}_reconciler_database"
    description = "Reconciler limitado a ${var.firestore_database}."
    expression  = "resource.name == \"${local.firestore_database_resource}\""
  }
}

data "google_project" "this" {
  project_id = var.project_id
}
