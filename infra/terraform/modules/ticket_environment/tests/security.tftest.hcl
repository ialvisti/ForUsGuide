mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id = "rag-kb-system"
  region     = "us-central1"
  env        = "staging"

  image_digest        = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  release_phase       = "dark_100"
  shadow_sample_rate  = 0
  ticket_handler_mode = "disabled"
  enable_services     = true

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

  worker_url                      = "https://worker.example.run.app"
  worker_max_instances            = 1
  queue_max_concurrent_dispatches = 1
}

run "firestore_roles_are_conditioned_to_one_database" {
  command = plan

  assert {
    condition = alltrue([
      google_project_iam_member.producer_firestore[0].condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/ticket-staging\"",
      google_project_iam_member.worker_firestore.condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/ticket-staging\"",
      google_project_iam_member.reconciler_firestore.condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/ticket-staging\"",
    ])
    error_message = "Producer, worker y reconciler sólo pueden acceder a su database."
  }
}

run "queue_roles_are_bound_to_the_queue_resource" {
  command = apply

  assert {
    condition = (
      google_cloud_tasks_queue_iam_member.producer_queue.name == google_cloud_tasks_queue.ticket.name &&
      google_cloud_tasks_queue_iam_member.reconciler_queue.name == google_cloud_tasks_queue.ticket.name &&
      google_cloud_tasks_queue_iam_member.producer_queue.role == google_project_iam_custom_role.ticket_queue_enqueuer.id &&
      google_cloud_tasks_queue_iam_member.reconciler_queue.role == google_project_iam_custom_role.ticket_queue_enqueuer.id
    )
    error_message = "El custom role de enqueue debe enlazarse directamente a una sola cola."
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
        API_KEY              = ["producer", "e2e"]
        FORUSBOTS_AUTH_TOKEN = ["worker"]
        OPENAI_API_KEY       = ["worker"]
        PINECONE_API_KEY     = ["worker"]
      }
    }
    e2e_job = {
      enabled               = true
      image_digest          = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/e2e@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      service_account_email = "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
      producer_url          = "https://producer.example.run.app"
      secret_version_refs = {
        API_KEY = "projects/rag-kb-system/secrets/ticket-api-key-stg/versions/1"
      }
    }
  }

  assert {
    condition = (
      length(google_secret_manager_secret.runtime) == 4 &&
      length(google_secret_manager_secret_iam_member.runtime_accessor) == 5 &&
      length(google_cloud_run_v2_job.e2e) == 1 &&
      google_cloud_run_v2_job.e2e[0].name == "ticket-e2e-staging" &&
      google_cloud_run_v2_job.e2e[0].template[0].template[0].service_account == "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com" &&
      length(google_cloud_run_v2_service_iam_member.e2e_invokes_producer) == 1
    )
    error_message = "Los containers/accesos deben ser por rol y E2E debe ser un Job de staging."
  }
}

run "production_rejects_an_e2e_job" {
  command = plan

  variables {
    env             = "production"
    enable_services = false
    e2e_job = {
      enabled               = true
      image_digest          = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/e2e@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      service_account_email = "ticket-e2e-stg@rag-kb-system.iam.gserviceaccount.com"
      producer_url          = "https://producer.example.run.app"
      secret_version_refs   = {}
    }
  }

  expect_failures = [google_cloud_run_v2_job.e2e[0]]
}
