mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id = "rag-kb-system"
  region     = "us-central1"
  env        = "staging"

  image_digest               = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  release_phase              = "dark_100"
  producer_baseline_revision = "kb-rag-system-staging-00001-abc"
  shadow_sample_rate         = 0
  ticket_handler_mode        = "disabled"
  enable_services            = true

  firestore_database    = "ticket-staging"
  producer_service_name = "kb-rag-system-staging"
  worker_service_name   = "kb-rag-ticket-worker-staging"
  reconciler_job_name   = "ticket-reconciler-staging"
  queue_name            = "ticket-jobs-staging"

  producer_sa_email    = "ticket-producer-stg@rag-kb-system.iam.gserviceaccount.com"
  worker_sa_email      = "ticket-worker-stg@rag-kb-system.iam.gserviceaccount.com"
  reconciler_sa_email  = "ticket-reconciler-stg@rag-kb-system.iam.gserviceaccount.com"
  task_signer_sa_email = "ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com"
  scheduler_sa_email   = "ticket-scheduler-stg@rag-kb-system.iam.gserviceaccount.com"
  n8n_invoker_sa_email = "n8n-ticket-invoker-stg@rag-kb-system.iam.gserviceaccount.com"

  worker_max_instances            = 1
  queue_max_concurrent_dispatches = 1
  notification_channels = [
    "projects/rag-kb-system/notificationChannels/111",
    "projects/rag-kb-system/notificationChannels/222",
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
    API_KEY              = "projects/rag-kb-system/secrets/ticket-api-key-stg/versions/1"
    FORUSBOTS_AUTH_TOKEN = "projects/rag-kb-system/secrets/ticket-forusbots-token-stg/versions/1"
    OPENAI_API_KEY       = "projects/rag-kb-system/secrets/ticket-openai-key-stg/versions/1"
    PINECONE_API_KEY     = "projects/rag-kb-system/secrets/ticket-pinecone-key-stg/versions/1"
  }
  secret_containers = {
    enabled = true
    ids = {
      API_KEY              = "ticket-api-key-stg"
      FORUSBOTS_AUTH_TOKEN = "ticket-forusbots-token-stg"
      OPENAI_API_KEY       = "ticket-openai-key-stg"
      PINECONE_API_KEY     = "ticket-pinecone-key-stg"
    }
    accessor_roles = {
      API_KEY              = ["producer"]
      FORUSBOTS_AUTH_TOKEN = ["worker"]
      OPENAI_API_KEY       = ["producer", "worker"]
      PINECONE_API_KEY     = ["producer", "worker"]
    }
  }
}

run "worker_target_and_oidc_audience_are_independent" {
  command = apply

  variables {
    e2e_secret_containers = {
      for key in [
        "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
        "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
        "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
        "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
        "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
      ] : key => lower(replace(key, "_", "-"))
    }
    e2e_job = {
      enabled               = true
      image_digest          = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/e2e@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      service_account_email = "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
      nonsecret_env = {
        for key in [
          "E2E_PRINCIPAL_ID", "E2E_TENANT_ID", "E2E_PARTICIPANT_ID", "E2E_PLAN_ID",
          "E2E_MISMATCHED_PARTICIPANT_ID", "E2E_MISMATCHED_PLAN_ID", "E2E_COMPANY_NAME",
          "E2E_RECORD_KEEPER", "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION",
          "E2E_N8N_CONTRACT_URL", "E2E_N8N_CONTRACT_VERSION",
          "E2E_FORUSBOTS_CONTRACT_VERSION", "E2E_FORUSBOTS_LOOKUP_URL",
          "E2E_DELIVERY_CONTRACT_VERSION", "E2E_DELIVERY_LOOKUP_URL",
          "E2E_GCP_AUDIT_CONTRACT_URL", "E2E_GCP_AUDIT_CONTRACT_VERSION",
          "E2E_TTL_SENTINEL_REFERENCE", "E2E_PRODUCTION_NEGATIVE_ATTESTATION",
          "E2E_PINECONE_INDEX", "E2E_PINECONE_NAMESPACE", "E2E_PINECONE_DIMENSION",
          "E2E_DIFFERENTIAL_LEGACY_URL", "E2E_DIFFERENTIAL_LEGACY_AUDIENCE",
          "E2E_DIFFERENTIAL_EVIDENCE_URI", "E2E_MAIN_SHA", "E2E_EVIDENCE_URI",
        ] : key => "synthetic-contract-value"
      }
      secret_version_refs = {
        for key in [
          "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
          "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
          "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
          "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
          "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
        ] : key => "projects/rag-kb-system/secrets/${lower(replace(key, "_", "-"))}/versions/1"
      }
    }
  }

  override_resource {
    target = google_cloud_run_v2_service.worker[0]
    values = {
      uri = "https://worker-generated.run.app"
    }
  }

  override_resource {
    target = google_cloud_run_v2_service.producer[0]
    values = {
      uri = "https://producer-generated.run.app"
    }
  }

  assert {
    condition = (
      google_cloud_run_v2_service.worker[0].custom_audiences[0] == "https://kb-rag-ticket-worker-staging.rag-kb-system.ticket.internal" &&
      google_cloud_run_v2_service.producer[0].template[0].containers[0].env[3].value == "https://worker-generated.run.app" &&
      google_cloud_run_v2_service.producer[0].template[0].containers[0].env[4].value == "https://kb-rag-ticket-worker-staging.rag-kb-system.ticket.internal" &&
      google_cloud_run_v2_service.worker[0].template[0].containers[0].env[4].value == "https://kb-rag-ticket-worker-staging.rag-kb-system.ticket.internal" &&
      google_cloud_run_v2_job.reconciler[0].template[0].template[0].containers[0].env[2].value == "https://worker-generated.run.app" &&
      google_cloud_run_v2_job.reconciler[0].template[0].template[0].containers[0].env[3].value == "https://kb-rag-ticket-worker-staging.rag-kb-system.ticket.internal" &&
      google_cloud_run_v2_job.e2e[0].template[0].template[0].containers[0].env[1].value == "https://producer-generated.run.app" &&
      google_cloud_run_v2_job.e2e[0].template[0].template[0].containers[0].env[2].value == "https://producer-generated.run.app"
    )
    error_message = "Target HTTP computado y custom audience OIDC estable deben permanecer separados."
  }
}

run "runtime_provider_iam_excludes_reconciler_and_project_storage" {
  command = apply

  assert {
    condition = (
      google_storage_bucket_iam_member.producer_core_objects[0].bucket == "rag-kb-system-kb-articles" &&
      google_storage_bucket_iam_member.producer_core_objects[0].role == "roles/storage.objectViewer" &&
      google_storage_bucket_iam_member.producer_core_objects[0].member == "serviceAccount:ticket-producer-stg@rag-kb-system.iam.gserviceaccount.com"
    )
    error_message = "Vertex sólo producer/worker y GCS sólo producer a nivel bucket."
  }
}

run "secret_access_is_role_specific_and_e2e_exists_only_in_staging" {
  command = plan

  variables {
    secret_containers = {
      enabled = true
      ids = {
        API_KEY              = "ticket-api-key-stg"
        FORUSBOTS_AUTH_TOKEN = "ticket-forusbots-token-stg"
        OPENAI_API_KEY       = "ticket-openai-key-stg"
        PINECONE_API_KEY     = "ticket-pinecone-key-stg"
      }
      accessor_roles = {
        API_KEY              = ["producer"]
        FORUSBOTS_AUTH_TOKEN = ["worker"]
        OPENAI_API_KEY       = ["producer", "worker"]
        PINECONE_API_KEY     = ["producer", "worker"]
      }
    }
    e2e_secret_containers = {
      for key in [
        "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
        "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
        "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
        "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
        "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
      ] : key => lower(replace(key, "_", "-"))
    }
    e2e_job = {
      enabled               = true
      image_digest          = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/e2e@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      service_account_email = "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
      nonsecret_env = {
        for key in [
          "E2E_PRINCIPAL_ID", "E2E_TENANT_ID", "E2E_PARTICIPANT_ID", "E2E_PLAN_ID",
          "E2E_MISMATCHED_PARTICIPANT_ID", "E2E_MISMATCHED_PLAN_ID", "E2E_COMPANY_NAME",
          "E2E_RECORD_KEEPER", "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION",
          "E2E_N8N_CONTRACT_URL", "E2E_N8N_CONTRACT_VERSION",
          "E2E_FORUSBOTS_CONTRACT_VERSION", "E2E_FORUSBOTS_LOOKUP_URL",
          "E2E_DELIVERY_CONTRACT_VERSION", "E2E_DELIVERY_LOOKUP_URL",
          "E2E_GCP_AUDIT_CONTRACT_URL", "E2E_GCP_AUDIT_CONTRACT_VERSION",
          "E2E_TTL_SENTINEL_REFERENCE", "E2E_PRODUCTION_NEGATIVE_ATTESTATION",
          "E2E_PINECONE_INDEX", "E2E_PINECONE_NAMESPACE", "E2E_PINECONE_DIMENSION",
          "E2E_DIFFERENTIAL_LEGACY_URL", "E2E_DIFFERENTIAL_LEGACY_AUDIENCE",
          "E2E_DIFFERENTIAL_EVIDENCE_URI", "E2E_MAIN_SHA", "E2E_EVIDENCE_URI",
        ] : key => "synthetic-contract-value"
      }
      secret_version_refs = {
        for key in [
          "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
          "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
          "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
          "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
          "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
        ] : key => "projects/rag-kb-system/secrets/${lower(replace(key, "_", "-"))}/versions/1"
      }
    }
  }

  assert {
    condition = (
      length(var.secret_containers.ids) == 4 &&
      length(google_secret_manager_secret_iam_member.runtime_accessor) == 6 &&
      toset(keys(google_secret_manager_secret_iam_member.runtime_accessor)) == toset([
        "API_KEY:producer",
        "FORUSBOTS_AUTH_TOKEN:worker",
        "OPENAI_API_KEY:producer",
        "OPENAI_API_KEY:worker",
        "PINECONE_API_KEY:producer",
        "PINECONE_API_KEY:worker",
      ]) &&
      toset([
        for env in google_cloud_run_v2_service.producer[0].template[0].containers[0].env :
        env.name if contains(keys(var.secret_version_refs), env.name)
      ]) == toset(["API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY"]) &&
      toset([
        for env in google_cloud_run_v2_service.worker[0].template[0].containers[0].env :
        env.name if contains(keys(var.secret_version_refs), env.name)
      ]) == toset(["FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY"]) &&
      length(google_secret_manager_secret_iam_member.e2e_runtime_accessor) == 11 &&
      length(google_cloud_run_v2_job.e2e) == 1 &&
      google_cloud_run_v2_job.e2e[0].name == "ticket-e2e-staging" &&
      google_cloud_run_v2_job.e2e[0].template[0].template[0].service_account == "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
    )
    error_message = "Los containers/accesos deben ser por rol y E2E debe ser un Job de staging."
  }
}

run "production_rejects_an_e2e_job" {
  command = plan

  variables {
    env             = "production"
    enable_services = false
    e2e_secret_containers = {
      for key in [
        "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
        "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
        "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
        "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
        "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
      ] : key => lower(replace(key, "_", "-"))
    }
    e2e_job = {
      enabled               = true
      image_digest          = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/e2e@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      service_account_email = "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
      nonsecret_env = {
        for key in [
          "E2E_PRINCIPAL_ID", "E2E_TENANT_ID", "E2E_PARTICIPANT_ID", "E2E_PLAN_ID",
          "E2E_MISMATCHED_PARTICIPANT_ID", "E2E_MISMATCHED_PLAN_ID", "E2E_COMPANY_NAME",
          "E2E_RECORD_KEEPER", "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION",
          "E2E_N8N_CONTRACT_URL", "E2E_N8N_CONTRACT_VERSION",
          "E2E_FORUSBOTS_CONTRACT_VERSION", "E2E_FORUSBOTS_LOOKUP_URL",
          "E2E_DELIVERY_CONTRACT_VERSION", "E2E_DELIVERY_LOOKUP_URL",
          "E2E_GCP_AUDIT_CONTRACT_URL", "E2E_GCP_AUDIT_CONTRACT_VERSION",
          "E2E_TTL_SENTINEL_REFERENCE", "E2E_PRODUCTION_NEGATIVE_ATTESTATION",
          "E2E_PINECONE_INDEX", "E2E_PINECONE_NAMESPACE", "E2E_PINECONE_DIMENSION",
          "E2E_DIFFERENTIAL_LEGACY_URL", "E2E_DIFFERENTIAL_LEGACY_AUDIENCE",
          "E2E_DIFFERENTIAL_EVIDENCE_URI", "E2E_MAIN_SHA", "E2E_EVIDENCE_URI",
        ] : key => "synthetic-contract-value"
      }
      secret_version_refs = {
        for key in [
          "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
          "E2E_WRONG_PRINCIPAL_API_KEY", "E2E_WRONG_TENANT_API_KEY",
          "E2E_RATE_LIMIT_API_KEY", "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
          "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
          "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
        ] : key => "projects/rag-kb-system/secrets/${lower(replace(key, "_", "-"))}/versions/1"
      }
    }
  }

  expect_failures = [google_cloud_run_v2_job.e2e[0]]
}
