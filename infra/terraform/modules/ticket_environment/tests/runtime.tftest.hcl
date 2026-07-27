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

  producer_sa_email               = "ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com"
  worker_sa_email                 = "ticket-worker-prod@rag-kb-system.iam.gserviceaccount.com"
  reconciler_sa_email             = "ticket-reconciler-prod@rag-kb-system.iam.gserviceaccount.com"
  task_signer_sa_email            = "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com"
  scheduler_sa_email              = "ticket-scheduler-prod@rag-kb-system.iam.gserviceaccount.com"
  worker_max_instances            = 2
  queue_max_concurrent_dispatches = 2
  producer_baseline_revision      = "kb-rag-system-00048-bkc"
  producer_baseline_tag           = "baseline"
  notification_channels = [
    "projects/rag-kb-system/notificationChannels/111",
    "projects/rag-kb-system/notificationChannels/222",
  ]

  producer_core_env = {
    ENABLE_EXECUTION_LOGGING        = "true"
    FORUSBOTS_BASE_URL              = "https://forusbots.example.test"
    GCS_BUCKET                      = "rag-kb-system-kb-articles"
    INDEX_NAME                      = "kb-articles-production"
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
    API_KEY              = "projects/rag-kb-system/secrets/api-key/versions/1"
    FORUSBOTS_AUTH_TOKEN = "projects/rag-kb-system/secrets/forusbots-token/versions/1"
    OPENAI_API_KEY       = "projects/rag-kb-system/secrets/openai-key/versions/1"
    PINECONE_API_KEY     = "projects/rag-kb-system/secrets/pinecone-key/versions/1"
  }
  secret_containers = {
    enabled = true
    ids = {
      API_KEY              = "api-key"
      FORUSBOTS_AUTH_TOKEN = "forusbots-token"
      OPENAI_API_KEY       = "openai-key"
      PINECONE_API_KEY     = "pinecone-key"
    }
    accessor_roles = {
      API_KEY              = ["producer"]
      FORUSBOTS_AUTH_TOKEN = ["worker"]
      OPENAI_API_KEY       = ["producer", "worker"]
      PINECONE_API_KEY     = ["producer", "worker"]
    }
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
      google_cloud_run_v2_service.producer[0].traffic[1].percent == 0 &&
      google_cloud_run_v2_service.producer[0].template[0].service_account == "ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com"
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
      google_cloud_run_v2_service.producer[0].traffic[0].percent == 100 &&
      google_cloud_run_v2_service.producer[0].template[0].service_account == "ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com"
    )
    error_message = "dark_100 debe promover latest explícitamente al 100%."
  }
}

run "active_production_rejects_the_legacy_producer_identity" {
  command = plan

  variables {
    release_phase       = "shadow"
    shadow_sample_rate  = 100
    ticket_handler_mode = "shadow"
    producer_sa_email   = "kb-rag-runner@rag-kb-system.iam.gserviceaccount.com"
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "dark_no_traffic_rejects_the_legacy_producer_identity" {
  command = plan

  variables {
    producer_sa_email = "kb-rag-runner@rag-kb-system.iam.gserviceaccount.com"
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "dark_100_rejects_the_legacy_producer_identity" {
  command = plan

  variables {
    release_phase     = "dark_100"
    producer_sa_email = "kb-rag-runner@rag-kb-system.iam.gserviceaccount.com"
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "production_rejects_an_incomplete_core_inventory" {
  command = plan

  variables {
    producer_core_env = {}
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_pricing_with_missing_rate_field" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":5.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_pricing_with_extra_rate_field" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":5.0,\"output_usd_per_million\":30.0,\"currency\":\"USD\"},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_pricing_with_string_rate" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":\"5.0\",\"output_usd_per_million\":30.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_pricing_with_negative_rate" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":-0.01,\"output_usd_per_million\":30.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_pricing_above_reviewed_bound" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":5.0,\"output_usd_per_million\":501.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_non_json_nan_pricing" {
  command = plan

  variables {
    producer_core_env = merge(var.producer_core_env, {
      TICKET_LLM_PRICING_JSON = "{\"pricing_as_of\":\"2026-07-21\",\"source\":\"openai-google-official-public-pricing\",\"models\":{\"openai:gpt-5.5\":{\"input_usd_per_million\":NaN,\"output_usd_per_million\":30.0},\"gemini:gemini-2.5-pro\":{\"input_usd_per_million\":1.25,\"output_usd_per_million\":10.0}}}"
    })
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_an_unreviewed_core_env_key" {
  command = plan

  variables {
    producer_core_env = {
      ENABLE_EXECUTION_LOGGING        = "true"
      FORUSBOTS_BASE_URL              = "https://forusbots.example.test"
      GCS_BUCKET                      = "rag-kb-system-kb-articles"
      GOOGLE_APPLICATION_CREDENTIALS  = "/tmp/attacker.json"
      INDEX_NAME                      = "kb-articles-production"
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
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_swapped_secret_version_refs" {
  command = plan

  variables {
    secret_version_refs = {
      API_KEY              = "projects/rag-kb-system/secrets/openai-key/versions/1"
      FORUSBOTS_AUTH_TOKEN = "projects/rag-kb-system/secrets/forusbots-token/versions/1"
      OPENAI_API_KEY       = "projects/rag-kb-system/secrets/api-key/versions/1"
      PINECONE_API_KEY     = "projects/rag-kb-system/secrets/pinecone-api-key/versions/1"
    }
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_cross_project_secret_version_refs" {
  command = plan

  variables {
    secret_version_refs = {
      API_KEY              = "projects/attacker-project/secrets/api-key/versions/1"
      FORUSBOTS_AUTH_TOKEN = "projects/rag-kb-system/secrets/forusbots-token/versions/1"
      OPENAI_API_KEY       = "projects/rag-kb-system/secrets/openai-key/versions/1"
      PINECONE_API_KEY     = "projects/rag-kb-system/secrets/pinecone-key/versions/1"
    }
  }

  expect_failures = [google_cloud_run_v2_service.producer]
}

run "runtime_rejects_noncanonical_forusbots_origin" {
  command = plan

  variables {
    producer_core_env = {
      ENABLE_EXECUTION_LOGGING        = "true"
      FORUSBOTS_BASE_URL              = "https://forusbots.example.test/redirect"
      GCS_BUCKET                      = "rag-kb-system-kb-articles"
      INDEX_NAME                      = "kb-articles-production"
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

run "active_environment_rejects_missing_notification_channels" {
  command = plan

  variables {
    notification_channels = []
  }

  expect_failures = [check.ticket_monitoring_notification_channels]
}

run "active_environment_rejects_duplicate_notification_channels" {
  command = plan

  variables {
    notification_channels = [
      "projects/rag-kb-system/notificationChannels/111",
      "projects/rag-kb-system/notificationChannels/111",
    ]
  }

  expect_failures = [check.ticket_monitoring_notification_channels]
}

run "active_environment_rejects_malformed_notification_channels" {
  command = plan

  variables {
    notification_channels = ["primary", "secondary"]
  }

  expect_failures = [check.ticket_monitoring_notification_channels]
}

run "production_secrets_grant_worker_without_project_wide_or_reconciler_access" {
  command = plan

  assert {
    condition = (
      toset(keys(google_secret_manager_secret_iam_member.runtime_accessor)) == toset([
        "API_KEY:producer",
        "FORUSBOTS_AUTH_TOKEN:worker",
        "OPENAI_API_KEY:producer",
        "OPENAI_API_KEY:worker",
        "PINECONE_API_KEY:producer",
        "PINECONE_API_KEY:worker",
      ]) &&
      google_secret_manager_secret_iam_member.runtime_accessor["FORUSBOTS_AUTH_TOKEN:worker"].member == "serviceAccount:ticket-worker-prod@rag-kb-system.iam.gserviceaccount.com" &&
      google_secret_manager_secret_iam_member.runtime_accessor["OPENAI_API_KEY:worker"].role == "roles/secretmanager.secretAccessor" &&
      google_secret_manager_secret_iam_member.runtime_accessor["PINECONE_API_KEY:worker"].role == "roles/secretmanager.secretAccessor" &&
      alltrue([
        for env in google_cloud_run_v2_service.worker[0].template[0].containers[0].env :
        env.name != "API_KEY"
      ]) &&
      alltrue([
        for env in google_cloud_run_v2_service.producer[0].template[0].containers[0].env :
        env.name != "FORUSBOTS_AUTH_TOKEN"
      ]) &&
      alltrue([
        for key in keys(google_secret_manager_secret_iam_member.runtime_accessor) :
        !endswith(key, ":reconciler")
      ])
    )
    error_message = "Worker debe leer sólo sus secretos por IAM de cada secret; reconciler nunca recibe accessor."
  }
}
