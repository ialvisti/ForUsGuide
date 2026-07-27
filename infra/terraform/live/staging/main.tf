# Staging: base nombrada ticket-staging aislada, cola, servicios/reconciler
# (plan Tarea 10 Paso 4). Las SAs llegan en el manifest firmado del plan; este
# root nunca lee el bucket/state de platform.

locals {
  sas = var.runtime_service_accounts
}

module "staging" {
  source = "../../modules/ticket_environment"

  project_id = var.project_id
  region     = var.region
  env        = "staging"

  image_digest               = var.image_digest
  release_phase              = var.release_phase
  producer_baseline_revision = var.producer_baseline_revision
  producer_baseline_tag      = var.producer_baseline_tag
  producer_candidate_tag     = var.producer_candidate_tag
  shadow_sample_rate         = var.shadow_sample_rate
  ticket_handler_mode        = contains(["dark_no_traffic", "dark_100", "infra_only"], var.release_phase) ? "disabled" : (var.release_phase == "knowledge_only" ? "knowledge_only" : (var.release_phase == "full" ? "full" : "shadow"))
  enable_services            = var.release_phase != "infra_only"

  firestore_database = "ticket-staging"

  producer_service_name = "kb-rag-system-staging"
  worker_service_name   = "kb-rag-ticket-worker-staging"
  reconciler_job_name   = "ticket-reconciler-staging"
  queue_name            = "ticket-jobs-staging"

  producer_sa_email               = local.sas["ticket-producer-stg"]
  worker_sa_email                 = local.sas["ticket-worker-stg"]
  reconciler_sa_email             = local.sas["ticket-reconciler-stg"]
  task_signer_sa_email            = local.sas["ticket-task-signer-stg"]
  scheduler_sa_email              = local.sas["ticket-scheduler-stg"]
  worker_max_instances            = 1
  queue_max_concurrent_dispatches = 1

  producer_core_env     = var.producer_core_env
  secret_version_refs   = var.secret_version_refs
  secret_containers     = var.secret_containers
  e2e_job               = var.e2e_job
  e2e_secret_containers = var.e2e_secret_containers
  notification_channels = var.notification_channels
}
