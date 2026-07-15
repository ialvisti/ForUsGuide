mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id = "rag-kb-system"
  region     = "us-central1"
  env        = "production"

  image_digest        = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  release_phase       = "dark_no_traffic"
  shadow_sample_rate  = 0
  ticket_handler_mode = "disabled"
  enable_services     = true

  firestore_database    = "(default)"
  producer_service_name = "kb-rag-system"
  worker_service_name   = "kb-rag-ticket-worker"
  reconciler_job_name   = "ticket-reconciler-prod"
  queue_name            = "ticket-jobs-prod"

  producer_sa_email    = "kb-rag-runner@rag-kb-system.iam.gserviceaccount.com"
  worker_sa_email      = "ticket-worker-prod@rag-kb-system.iam.gserviceaccount.com"
  reconciler_sa_email  = "ticket-reconciler-prod@rag-kb-system.iam.gserviceaccount.com"
  task_signer_sa_email = "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com"
  scheduler_sa_email   = "ticket-scheduler-prod@rag-kb-system.iam.gserviceaccount.com"
  n8n_invoker_sa_email = "n8n-ticket-invoker-prod@rag-kb-system.iam.gserviceaccount.com"

  worker_max_instances            = 2
  queue_max_concurrent_dispatches = 2
  worker_url                      = "https://worker.example.run.app"
  producer_baseline_revision      = "kb-rag-system-00048-bkc"

  producer_core_env = {
    ENABLE_EXECUTION_LOGGING = "true"
    FORUSBOTS_BASE_URL       = "https://forusbots.example.test"
    GCS_BUCKET               = "rag-kb-system-kb-articles"
    INDEX_NAME               = "kb-articles-production"
    LLM_ROUTE_CLASSIFY       = "model"
    LLM_ROUTE_DECOMPOSE      = "model"
    LLM_ROUTE_GR_OUTCOME     = "model"
    LLM_ROUTE_GR_RESPONSE    = "model"
    LLM_ROUTE_KNOWLEDGE      = "model"
    LLM_ROUTE_REQUIRED_DATA  = "model"
    LOG_LEVEL                = "INFO"
    NAMESPACE                = "kb_articles"
    OPENAI_MODEL             = "model"
    OPENAI_REASONING_EFFORT  = "medium"
    USE_VERTEX_AI            = "true"
  }
  secret_version_refs = {
    API_KEY              = "projects/rag-kb-system/secrets/api-key/versions/1"
    FORUSBOTS_AUTH_TOKEN = "projects/rag-kb-system/secrets/forusbots-token/versions/1"
    OPENAI_API_KEY       = "projects/rag-kb-system/secrets/openai-key/versions/1"
    PINECONE_API_KEY     = "projects/rag-kb-system/secrets/pinecone-key/versions/1"
  }
}

run "dark_no_traffic_keeps_baseline_at_one_hundred" {
  command = plan

  assert {
    condition = (
      length(google_cloud_run_v2_service.producer[0].traffic) == 2 &&
      google_cloud_run_v2_service.producer[0].traffic[0].type == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION" &&
      google_cloud_run_v2_service.producer[0].traffic[0].revision == "kb-rag-system-00048-bkc" &&
      google_cloud_run_v2_service.producer[0].traffic[0].percent == 100 &&
      google_cloud_run_v2_service.producer[0].traffic[1].type == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST" &&
      google_cloud_run_v2_service.producer[0].traffic[1].percent == 0
    )
    error_message = "dark_no_traffic debe conservar baseline=100 y latest=0."
  }
}

run "dark_100_promotes_latest_explicitly" {
  command = plan

  variables {
    release_phase = "dark_100"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_service.producer[0].traffic) == 1 &&
      google_cloud_run_v2_service.producer[0].traffic[0].type == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST" &&
      google_cloud_run_v2_service.producer[0].traffic[0].percent == 100
    )
    error_message = "dark_100 debe promover latest explícitamente al 100%."
  }
}

run "production_rejects_an_incomplete_core_inventory" {
  command = plan

  variables {
    producer_core_env = {}
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "infra_only_does_not_require_an_image" {
  command = plan

  variables {
    image_digest    = ""
    release_phase   = "infra_only"
    enable_services = false
  }

  assert {
    condition     = length(google_cloud_run_v2_service.producer) == 0
    error_message = "infra_only no debe crear Cloud Run ni exigir un digest ficticio."
  }
}
