# Staging: base nombrada ticket-staging aislada, cola, servicios/reconciler
# (plan Tarea 10 Paso 4). Lee las SAs del state de platform (referenciando
# nombres/emails, nunca payloads).

data "terraform_remote_state" "platform" {
  backend = "gcs"
  config = {
    bucket = "rag-kb-system-tfstate-platform-900340137010"
    prefix = "state"
  }
}

locals {
  sas = data.terraform_remote_state.platform.outputs.runtime_service_accounts
}

module "staging" {
  source = "../../modules/ticket_environment"

  project_id = var.project_id
  region     = var.region
  env        = "staging"

  image_digest        = var.image_digest
  release_phase       = var.release_phase
  shadow_sample_rate  = var.shadow_sample_rate
  ticket_handler_mode = contains(["dark_no_traffic", "dark_100", "infra_only"], var.release_phase) ? "disabled" : (var.release_phase == "knowledge_only" ? "knowledge_only" : (var.release_phase == "full" ? "full" : "shadow"))
  enable_services     = var.release_phase != "infra_only"

  firestore_database = "ticket-staging"

  producer_service_name = "kb-rag-system-staging"
  worker_service_name   = "kb-rag-ticket-worker-staging"
  reconciler_job_name   = "ticket-reconciler-staging"
  queue_name            = "ticket-jobs-staging"

  producer_sa_email    = local.sas["ticket-producer-stg"]
  worker_sa_email      = local.sas["ticket-worker-stg"]
  reconciler_sa_email  = local.sas["ticket-reconciler-stg"]
  task_signer_sa_email = local.sas["ticket-task-signer-stg"]
  scheduler_sa_email   = local.sas["ticket-scheduler-stg"]
  n8n_invoker_sa_email = local.sas["n8n-ticket-invoker-stg"]

  worker_max_instances            = 1
  queue_max_concurrent_dispatches = 1

  secret_version_refs   = var.secret_version_refs
  notification_channels = var.notification_channels
}
