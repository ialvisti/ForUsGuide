# Project-level runtime grants live in platform, the only root whose trusted
# controller may broker project IAM. Environment apply identities never receive
# resourcemanager.projects.setIamPolicy.

locals {
  runtime_firestore_grant_inventory = {
    staging-producer = {
      environment = "staging"
      email       = google_service_account.runtime["ticket-producer-stg"].email
      database    = "ticket-staging"
    }
    staging-worker = {
      environment = "staging"
      email       = google_service_account.runtime["ticket-worker-stg"].email
      database    = "ticket-staging"
    }
    staging-reconciler = {
      environment = "staging"
      email       = google_service_account.runtime["ticket-reconciler-stg"].email
      database    = "ticket-staging"
    }
    production-producer = {
      environment = "production"
      email       = google_service_account.runtime["ticket-producer-prod"].email
      database    = "(default)"
    }
    production-worker = {
      environment = "production"
      email       = google_service_account.runtime["ticket-worker-prod"].email
      database    = "(default)"
    }
    production-reconciler = {
      environment = "production"
      email       = google_service_account.runtime["ticket-reconciler-prod"].email
      database    = "(default)"
    }
  }
  runtime_firestore_grants = {
    for key, grant in local.runtime_firestore_grant_inventory : key => grant
    if var.environment_container_phase[grant.environment] == "managed"
  }

  runtime_vertex_grant_inventory = {
    staging-producer = {
      environment = "staging"
      email       = google_service_account.runtime["ticket-producer-stg"].email
    }
    staging-worker = {
      environment = "staging"
      email       = google_service_account.runtime["ticket-worker-stg"].email
    }
    production-worker = {
      environment = "production"
      email       = google_service_account.runtime["ticket-worker-prod"].email
    }
    production-producer = {
      environment = "production"
      email       = google_service_account.runtime["ticket-producer-prod"].email
    }
  }
  runtime_vertex_grants = {
    for key, grant in local.runtime_vertex_grant_inventory : key => grant
    if var.environment_container_phase[grant.environment] == "managed"
  }

  environment_runtime_iam_inventory = {
    staging = {
      producer        = google_service_account.runtime["ticket-producer-stg"].email
      worker          = google_service_account.runtime["ticket-worker-stg"].email
      reconciler      = google_service_account.runtime["ticket-reconciler-stg"].email
      task_signer     = google_service_account.runtime["ticket-task-signer-stg"].email
      scheduler       = google_service_account.runtime["ticket-scheduler-stg"].email
      n8n             = google_service_account.runtime["n8n-ticket-invoker-stg"].email
      e2e             = google_service_account.runtime["ticket-e2e-stg"].email
      producer_name   = "kb-rag-system-staging"
      worker_name     = "kb-rag-ticket-worker-staging"
      reconciler_name = "ticket-reconciler-staging"
      queue_name      = "ticket-jobs-staging"
    }
    production = {
      producer        = google_service_account.runtime["ticket-producer-prod"].email
      worker          = google_service_account.runtime["ticket-worker-prod"].email
      reconciler      = google_service_account.runtime["ticket-reconciler-prod"].email
      task_signer     = google_service_account.runtime["ticket-task-signer-prod"].email
      scheduler       = google_service_account.runtime["ticket-scheduler-prod"].email
      n8n             = google_service_account.runtime["n8n-ticket-invoker-prod"].email
      e2e             = null
      producer_name   = "kb-rag-system"
      worker_name     = "kb-rag-ticket-worker"
      reconciler_name = "ticket-reconciler-prod"
      queue_name      = "ticket-jobs-prod"
    }
  }
  environment_runtime_iam = {
    for environment, config in local.environment_runtime_iam_inventory :
    environment => config
    if var.environment_container_phase[environment] == "managed"
  }

  runtime_telemetry_grant_inventory = {
    production-producer-logging = {
      environment = "production"
      email       = google_service_account.runtime["ticket-producer-prod"].email
      role        = "roles/logging.logWriter"
    }
    production-producer-monitoring = {
      environment = "production"
      email       = google_service_account.runtime["ticket-producer-prod"].email
      role        = "roles/monitoring.metricWriter"
    }
  }
  runtime_telemetry_grants = {
    for key, grant in local.runtime_telemetry_grant_inventory : key => grant
    if var.environment_container_phase[grant.environment] == "managed"
  }

  environment_apply_run_services = var.cicd_bootstrap.enabled ? merge([
    for environment, boundary in local.environment_apply_boundaries : {
      for suffix in var.environment_run_resources[environment] :
      "${environment}-${trimprefix(suffix, "services/")}" => {
        environment = environment
        email       = boundary.email
        name        = trimprefix(suffix, "services/")
      } if startswith(suffix, "services/")
    }
  ]...) : {}
  environment_apply_run_jobs = var.cicd_bootstrap.enabled ? merge([
    for environment, boundary in local.environment_apply_boundaries : {
      for suffix in var.environment_run_resources[environment] :
      "${environment}-${trimprefix(suffix, "jobs/")}" => {
        environment = environment
        email       = boundary.email
        name        = trimprefix(suffix, "jobs/")
      } if startswith(suffix, "jobs/")
    }
  ]...) : {}
}

resource "google_project_iam_member" "runtime_firestore" {
  for_each = local.runtime_firestore_grants
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${each.value.email}"

  condition {
    title       = "ticket_${replace(each.key, "-", "_")}_database"
    description = "Runtime limitado a la database ${each.value.database}."
    expression  = "resource.name == \"projects/${var.project_id}/databases/${each.value.database}\""
  }
}

resource "google_project_iam_member" "runtime_vertex" {
  for_each = local.runtime_vertex_grants
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "runtime_telemetry" {
  for_each = local.runtime_telemetry_grants
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${each.value.email}"
}

# Queue IAM es propiedad de platform y se enlaza directamente a cada queue.
resource "google_cloud_tasks_queue_iam_member" "runtime_producer_queue" {
  for_each = local.environment_runtime_iam
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.environment[each.key].name
  role     = google_project_iam_custom_role.ticket_queue_enqueuer[each.key].id
  member   = "serviceAccount:${each.value.producer}"
}

resource "google_cloud_tasks_queue_iam_member" "runtime_reconciler_queue" {
  for_each = local.environment_runtime_iam
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.environment[each.key].name
  role     = google_project_iam_custom_role.ticket_queue_enqueuer[each.key].id
  member   = "serviceAccount:${each.value.reconciler}"
}

# Task signer policy se administra sobre la SA concreta, nunca con project IAM.
resource "google_service_account_iam_member" "runtime_producer_actas_signer" {
  for_each           = local.environment_runtime_iam
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value.task_signer}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${each.value.producer}"
}

resource "google_service_account_iam_member" "runtime_reconciler_actas_signer" {
  for_each           = local.environment_runtime_iam
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value.task_signer}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${each.value.reconciler}"
}

resource "google_service_account_iam_member" "tasks_agent_signs_as_runtime_signer" {
  for_each           = local.environment_runtime_iam
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value.task_signer}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
}

# Handoff final: Developer se liga directamente a cada service/job existente.
# El creator project-wide (pipeline_iam.tf) ya no existe en fase managed.
resource "google_cloud_run_v2_service_iam_member" "environment_apply_developer" {
  for_each = local.environment_apply_run_services
  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${each.value.email}"
}

resource "google_cloud_run_v2_job_iam_member" "environment_apply_developer" {
  for_each = local.environment_apply_run_jobs
  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${each.value.email}"
}

# Runtime invokers también se administran desde platform una vez que el
# inventario atestado confirma que el target existe.
resource "google_cloud_run_v2_service_iam_member" "task_signer_invokes_worker" {
  for_each = {
    for environment, config in local.environment_runtime_iam : environment => config
    if contains(var.environment_run_resources[environment], "services/${config.worker_name}")
  }
  project  = var.project_id
  location = var.region
  name     = each.value.worker_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.task_signer}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_reconciler" {
  for_each = {
    for environment, config in local.environment_runtime_iam : environment => config
    if contains(var.environment_run_resources[environment], "jobs/${config.reconciler_name}")
  }
  project  = var.project_id
  location = var.region
  name     = each.value.reconciler_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.scheduler}"
}

resource "google_cloud_run_v2_service_iam_member" "n8n_invokes_producer" {
  for_each = {
    for environment, config in local.environment_runtime_iam : environment => config
    if contains(var.environment_run_resources[environment], "services/${config.producer_name}")
  }
  project  = var.project_id
  location = var.region
  name     = each.value.producer_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.n8n}"
}

resource "google_cloud_run_v2_service_iam_member" "e2e_invokes_staging_producer" {
  count = contains(
    var.environment_run_resources.staging,
    "services/kb-rag-system-staging",
  ) ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = "kb-rag-system-staging"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime["ticket-e2e-stg"].email}"
}

resource "google_cloud_run_v2_service_iam_member" "production_preserved_invoker" {
  count = contains(
    var.environment_run_resources.production,
    "services/kb-rag-system",
  ) ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = "kb-rag-system"
  role     = "roles/run.invoker"
  member   = "serviceAccount:kb-rag-client@${var.project_id}.iam.gserviceaccount.com"
}
