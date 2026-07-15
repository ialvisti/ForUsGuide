# Producción: recursos equivalentes sobre la base (default) (plan Tarea 10
# Paso 4). El producer prod (kb-rag-system) y su SA kb-rag-runner se importan
# in-place preservando la baseline disabled (ver imports.tf). El primer plan
# importa y NO cambia tráfico ni modo.

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

module "production" {
  source = "../../modules/ticket_environment"

  project_id = var.project_id
  region     = var.region
  env        = "production"

  image_digest        = var.image_digest
  release_phase       = var.release_phase
  shadow_sample_rate  = var.shadow_sample_rate
  ticket_handler_mode = contains(["dark_no_traffic", "dark_100", "infra_only"], var.release_phase) ? "disabled" : (var.release_phase == "knowledge_only" ? "knowledge_only" : (var.release_phase == "full" ? "full" : "shadow"))
  enable_services     = var.release_phase != "infra_only"

  producer_core_env          = var.producer_core_env
  producer_baseline_revision = var.producer_baseline_revision
  producer_candidate_tag     = var.producer_candidate_tag
  producer_ingress           = var.producer_ingress
  producer_max_instances     = var.producer_max_instances
  producer_min_instances     = var.producer_min_instances
  producer_concurrency       = var.producer_concurrency
  producer_timeout           = var.producer_timeout
  producer_cpu               = var.producer_cpu
  producer_memory            = var.producer_memory
  producer_cpu_idle          = var.producer_cpu_idle
  producer_startup_cpu_boost = var.producer_startup_cpu_boost
  producer_port              = var.producer_port
  producer_startup_probe     = var.producer_startup_probe
  producer_liveness_probe    = var.producer_liveness_probe
  worker_url                 = var.worker_url
  ticket_wif_audience        = var.ticket_wif_audience
  ticket_wif_expected_email  = var.ticket_wif_expected_email

  firestore_database = "(default)"

  producer_service_name = "kb-rag-system"
  worker_service_name   = "kb-rag-ticket-worker"
  reconciler_job_name   = "ticket-reconciler-prod"
  queue_name            = "ticket-jobs-prod"

  # kb-rag-runner (existente) se preserva inicialmente como SA del producer.
  producer_sa_email    = "kb-rag-runner@${var.project_id}.iam.gserviceaccount.com"
  worker_sa_email      = local.sas["ticket-worker-prod"]
  reconciler_sa_email  = local.sas["ticket-reconciler-prod"]
  task_signer_sa_email = local.sas["ticket-task-signer-prod"]
  scheduler_sa_email   = local.sas["ticket-scheduler-prod"]
  n8n_invoker_sa_email = local.sas["n8n-ticket-invoker-prod"]

  producer_invoker_members = var.producer_invoker_members

  worker_max_instances            = 2
  queue_max_concurrent_dispatches = 2

  secret_version_refs   = var.secret_version_refs
  notification_channels = var.notification_channels
}
