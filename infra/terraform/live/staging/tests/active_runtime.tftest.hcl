mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  runtime_service_accounts = {
    ticket-producer-stg    = "ticket-producer-stg@rag-kb-system.iam.gserviceaccount.com"
    ticket-worker-stg      = "ticket-worker-stg@rag-kb-system.iam.gserviceaccount.com"
    ticket-reconciler-stg  = "ticket-reconciler-stg@rag-kb-system.iam.gserviceaccount.com"
    ticket-task-signer-stg = "ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com"
    ticket-scheduler-stg   = "ticket-scheduler-stg@rag-kb-system.iam.gserviceaccount.com"
    n8n-ticket-invoker-stg = "n8n-ticket-invoker-stg@rag-kb-system.iam.gserviceaccount.com"
  }

  image_digest               = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  release_phase              = "shadow"
  producer_baseline_revision = "kb-rag-system-staging-00001-abc"
  shadow_sample_rate         = 100
  ticket_wif_audience        = "https://producer.example.run.app"
  ticket_wif_allowed_emails = [
    "n8n-ticket-invoker-stg@rag-kb-system.iam.gserviceaccount.com",
    "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com",
  ]
  producer_core_env = {
    ENABLE_EXECUTION_LOGGING        = "true"
    FORUSBOTS_BASE_URL              = "https://forusbots.example.test"
    GCS_BUCKET                      = "rag-kb-system-kb-articles"
    INDEX_NAME                      = "kb-articles-staging"
    LLM_ROUTE_CLASSIFY              = "gpt-5.5"
    LLM_ROUTE_DECOMPOSE             = "gpt-5.5"
    LLM_ROUTE_GR_OUTCOME            = "gpt-5.5"
    LLM_ROUTE_GR_RESPONSE           = "gpt-5.5"
    LLM_ROUTE_KNOWLEDGE             = "gpt-5.5"
    LLM_ROUTE_REQUIRED_DATA         = "gpt-5.5"
    LLM_ROUTE_EXTRACT_INQUIRIES     = "gpt-5.5"
    LLM_ROUTE_KB_QUESTION_SYNTHESIS = "gpt-5.5"
    LLM_ROUTE_FORUSBOTS_FIELD_MAP   = "gpt-5.5"
    LLM_ROUTE_GR_BODY_BUILD         = "gpt-5.5"
    LLM_ROUTE_TICKET_FIELD_EXTRACT  = "gpt-5.5"
    LOG_LEVEL                       = "INFO"
    NAMESPACE                       = "kb_articles"
    OPENAI_MODEL                    = "gpt-5.5"
    OPENAI_REASONING_EFFORT         = "medium"
    TICKET_LLM_PRICING_JSON         = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":5.0,\"output_usd_per_million\":30.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    USE_VERTEX_AI                   = "true"
  }
  secret_version_refs = {
    API_KEY                     = "projects/rag-kb-system/secrets/api-key/versions/1"
    API_CLIENT_KEYS             = "projects/rag-kb-system/secrets/api-client-keys/versions/1"
    API_CLIENT_TENANTS          = "projects/rag-kb-system/secrets/api-client-tenants/versions/1"
    PARTICIPANT_PLAN_SOURCE     = "projects/rag-kb-system/secrets/participant-plan-source/versions/1"
    FORUSBOTS_AUTH_TOKEN        = "projects/rag-kb-system/secrets/forusbots-token/versions/1"
    OPENAI_API_KEY              = "projects/rag-kb-system/secrets/openai-key/versions/1"
    PINECONE_API_KEY            = "projects/rag-kb-system/secrets/pinecone-key/versions/1"
    TICKET_FAULT_SIGNING_SECRET = "projects/rag-kb-system/secrets/fault-signing/versions/1"
  }
  secret_containers = {
    enabled = true
    ids = {
      API_KEY                     = "api-key"
      API_CLIENT_KEYS             = "api-client-keys"
      API_CLIENT_TENANTS          = "api-client-tenants"
      PARTICIPANT_PLAN_SOURCE     = "participant-plan-source"
      FORUSBOTS_AUTH_TOKEN        = "forusbots-token"
      OPENAI_API_KEY              = "openai-key"
      PINECONE_API_KEY            = "pinecone-key"
      TICKET_FAULT_SIGNING_SECRET = "fault-signing"
    }
    accessor_roles = {
      API_KEY                     = ["producer"]
      API_CLIENT_KEYS             = ["producer"]
      API_CLIENT_TENANTS          = ["producer"]
      PARTICIPANT_PLAN_SOURCE     = ["producer"]
      FORUSBOTS_AUTH_TOKEN        = ["worker"]
      OPENAI_API_KEY              = ["producer", "worker"]
      PINECONE_API_KEY            = ["producer", "worker"]
      TICKET_FAULT_SIGNING_SECRET = ["producer", "worker"]
    }
  }
  notification_channels = [
    "projects/rag-kb-system/notificationChannels/111",
    "projects/rag-kb-system/notificationChannels/222",
  ]
}

run "active_staging_plans_without_external_worker_url_or_platform_state" {
  command = apply

  assert {
    condition = (
      output.worker_url != null &&
      output.producer_url != null &&
      output.firestore_database == "ticket-staging"
    )
    error_message = "Staging activo debe resolver worker/producer usando inputs firmados, sin worker_url ni remote state."
  }
}
