# Creación/import de los dos límites de datos ocurre en platform/bootstrap.
# Las SAs apply de staging/production sólo administran recursos hijos dentro
# de containers ya existentes, por lo que sus IAM Conditions son efectivas.

locals {
  environment_database_inventory = {
    staging    = "ticket-staging"
    production = "(default)"
  }
  environment_databases = {
    for environment, database in local.environment_database_inventory :
    environment => database
    if var.environment_container_phase[environment] == "managed"
  }
  environment_queue_inventory = {
    staging = {
      name                = "ticket-jobs-staging"
      max_concurrent      = 1
      max_dispatches_rate = 1
    }
    production = {
      name                = "ticket-jobs-prod"
      max_concurrent      = 2
      max_dispatches_rate = 2
    }
  }
  environment_queues = {
    for environment, queue in local.environment_queue_inventory :
    environment => queue
    if var.environment_container_phase[environment] == "managed"
  }
  environment_scheduler_inventory = {
    staging = {
      name         = "ticket-reconciler-staging-tick"
      reconciler   = "ticket-reconciler-staging"
      scheduler_sa = google_service_account.runtime["ticket-scheduler-stg"].email
    }
    production = {
      name         = "ticket-reconciler-prod-tick"
      reconciler   = "ticket-reconciler-prod"
      scheduler_sa = google_service_account.runtime["ticket-scheduler-prod"].email
    }
  }
  environment_schedulers = {
    for environment, scheduler in local.environment_scheduler_inventory :
    environment => scheduler
    if var.environment_container_phase[environment] == "managed"
  }
  environment_secret_containers = merge([
    for environment, secret_ids in var.environment_secret_ids : {
      for secret_id in secret_ids : "${environment}-${secret_id}" => {
        environment = environment
        secret_id   = secret_id
      } if var.environment_container_phase[environment] == "managed"
    }
  ]...)
  existing_environment_secret_containers = merge([
    for environment, secret_ids in var.existing_environment_secret_ids : {
      for secret_id in secret_ids : "${environment}-${secret_id}" => secret_id
    }
  ]...)
}

resource "google_firestore_database" "environment" {
  for_each    = local.environment_databases
  project     = var.project_id
  name        = each.value
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"

  depends_on = [google_project_service.enabled["firestore.googleapis.com"]]
}

resource "google_secret_manager_secret" "environment" {
  for_each  = local.environment_secret_containers
  project   = var.project_id
  secret_id = each.value.secret_id

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled["secretmanager.googleapis.com"]]
}

resource "google_cloud_tasks_queue" "environment" {
  for_each = local.environment_queues
  project  = var.project_id
  location = var.region
  name     = each.value.name

  rate_limits {
    max_concurrent_dispatches = each.value.max_concurrent
    max_dispatches_per_second = each.value.max_dispatches_rate
  }

  retry_config {
    max_attempts       = 5
    max_retry_duration = "1800s"
    min_backoff        = "30s"
    max_backoff        = "120s"
    max_doublings      = 2
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled["cloudtasks.googleapis.com"]]
}

resource "google_project_iam_custom_role" "ticket_queue_enqueuer" {
  for_each    = local.environment_queues
  project     = var.project_id
  role_id     = "ticketQueueEnqueuer${title(each.key)}"
  title       = "Ticket Queue Enqueuer (${each.key})"
  description = "create/get task + get queue; sin operaciones admin."
  permissions = [
    "cloudtasks.tasks.create",
    "cloudtasks.tasks.get",
    "cloudtasks.queues.get",
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_scheduler_job" "environment" {
  for_each  = local.environment_schedulers
  project   = var.project_id
  region    = var.region
  name      = each.value.name
  schedule  = "* * * * *"
  time_zone = "Etc/UTC"
  paused = !(
    contains(
      ["shadow", "knowledge_only", "full"],
      var.environment_release_phase[each.key],
    ) &&
    contains(
      var.environment_run_resources[each.key],
      "jobs/${each.value.reconciler}",
    )
  )

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${each.value.reconciler}:run"
    oauth_token {
      service_account_email = each.value.scheduler_sa
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled["cloudscheduler.googleapis.com"]]
}

import {
  for_each = var.environment_container_phase.production == "managed" ? {
    production = "projects/rag-kb-system/databases/(default)"
  } : {}
  to = google_firestore_database.environment[each.key]
  id = each.value
}

import {
  for_each = local.existing_environment_secret_containers
  to       = google_secret_manager_secret.environment[each.key]
  id       = "projects/${var.project_id}/secrets/${each.value}"
}
