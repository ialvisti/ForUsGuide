mock_provider "google" {
  mock_resource "google_cloudbuild_trigger" {
    defaults = {
      create_time = "2026-07-15T00:00:00Z"
      id          = "projects/rag-kb-system/locations/global/triggers/mock"
      name        = "mock-trigger"
      project     = "rag-kb-system"
      trigger_id  = "00000000-0000-0000-0000-000000000000"
    }
  }
  mock_resource "google_project_iam_member" {
    defaults = {
      etag = "mock-etag"
      id   = "mock-iam-member"
    }
  }
}
mock_provider "google-beta" {}

variables {
  cicd_bootstrap = {
    enabled                         = true
    release_controller_image_digest = "us-central1-docker.pkg.dev/rag-kb-system/release/controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}

run "privileged_triggers_use_ten_distinct_sas_and_a_pinned_controller" {
  command = apply

  assert {
    condition = length(toset([
      google_cloudbuild_trigger.platform_plan[0].service_account,
      google_cloudbuild_trigger.platform_apply[0].service_account,
      google_cloudbuild_trigger.staging_plan[0].service_account,
      google_cloudbuild_trigger.staging_apply[0].service_account,
      google_cloudbuild_trigger.production_plan[0].service_account,
      google_cloudbuild_trigger.production_apply[0].service_account,
      google_cloudbuild_trigger.staging_attest[0].service_account,
      google_cloudbuild_trigger.evidence_manifest[0].service_account,
      google_cloudbuild_trigger.test_only[0].service_account,
      google_cloudbuild_trigger.e2e_image[0].service_account,
    ])) == 10
    error_message = "Cada trigger privilegiado debe usar una SA exclusiva."
  }

  assert {
    condition = alltrue([
      for image in [
        google_cloudbuild_trigger.platform_plan[0].build[0].step[0].name,
        google_cloudbuild_trigger.platform_apply[0].build[0].step[0].name,
        google_cloudbuild_trigger.staging_plan[0].build[0].step[0].name,
        google_cloudbuild_trigger.staging_apply[0].build[0].step[0].name,
        google_cloudbuild_trigger.production_plan[0].build[0].step[0].name,
        google_cloudbuild_trigger.production_apply[0].build[0].step[0].name,
        google_cloudbuild_trigger.staging_attest[0].build[0].step[0].name,
        google_cloudbuild_trigger.evidence_manifest[0].build[0].step[0].name,
        google_cloudbuild_trigger.test_only[0].build[0].step[0].name,
        google_cloudbuild_trigger.e2e_image[0].build[0].step[0].name,
      ] : image == var.cicd_bootstrap.release_controller_image_digest
    ])
    error_message = "Los triggers privilegiados sólo pueden ejecutar el controller fijado por digest."
  }

  assert {
    condition = alltrue([
      google_cloudbuild_trigger.platform_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.staging_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.production_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.evidence_manifest[0].approval_config[0].approval_required,
    ])
    error_message = "Apply y el manifest de promoción deben requerir aprobación manual."
  }
}

run "state_access_is_environment_local_and_evidence_access_is_minimal" {
  command = plan

  assert {
    condition = alltrue([
      google_storage_bucket_iam_member.plan_state_viewer["platform"].bucket == "rag-kb-system-tfstate-platform-900340137010",
      google_storage_bucket_iam_member.plan_state_viewer["staging"].bucket == "rag-kb-system-tfstate-staging-900340137010",
      google_storage_bucket_iam_member.plan_state_viewer["production"].bucket == "rag-kb-system-tfstate-production-900340137010",
      google_storage_bucket_iam_member.apply_state_admin["platform"].bucket == "rag-kb-system-tfstate-platform-900340137010",
      google_storage_bucket_iam_member.apply_state_admin["staging"].bucket == "rag-kb-system-tfstate-staging-900340137010",
      google_storage_bucket_iam_member.apply_state_admin["production"].bucket == "rag-kb-system-tfstate-production-900340137010",
    ])
    error_message = "Ninguna SA plan/apply puede cruzar el bucket de state de su entorno."
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plan_evidence_writer["platform"].role == "roles/storage.objectCreator" &&
      google_storage_bucket_iam_member.apply_evidence_reader["platform"].role == "roles/storage.objectViewer"
    )
    error_message = "Plan crea evidencia write-once y apply sólo la lee."
  }
}

run "g1c_prepare_keeps_legacy_and_adds_exact_database_scope" {
  command = plan

  override_resource {
    target = google_project_iam_member.kb_rag_runner_firestore_legacy["legacy"]
  }

  variables {
    firestore_scope_migration = {
      enabled       = true
      phase         = "prepare"
      import_legacy = true
    }
  }

  assert {
    condition = (
      length(google_project_iam_member.kb_rag_runner_firestore_legacy) == 1 &&
      length(google_project_iam_member.kb_rag_runner_firestore_scoped) == 1 &&
      google_project_iam_member.kb_rag_runner_firestore_scoped["scoped"].condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/(default)\""
    )
    error_message = "G1C prepare debe conservar el grant amplio y añadir el scope exacto."
  }
}

run "g1c_enforce_removes_only_the_legacy_grant" {
  command = plan

  variables {
    firestore_scope_migration = {
      enabled       = true
      phase         = "enforce"
      import_legacy = false
    }
  }

  assert {
    condition = (
      length(google_project_iam_member.kb_rag_runner_firestore_legacy) == 0 &&
      length(google_project_iam_member.kb_rag_runner_firestore_scoped) == 1
    )
    error_message = "G1C enforce debe retirar el grant project-wide y conservar el scoped."
  }
}

run "aws_wif_uses_stable_role_attributes_and_separate_environment_roles" {
  command = apply

  variables {
    enable_n8n_wif     = true
    n8n_aws_account_id = "123456789012"
    n8n_aws_role_arns = {
      staging    = "arn:aws:iam::123456789012:role/n8n-ticket-staging"
      production = "arn:aws:iam::123456789012:role/n8n-ticket-production"
    }
  }

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.n8n_aws[0].attribute_mapping["attribute.aws_role"] == "assertion.arn.extract('assumed-role/{role_name}/')" &&
      strcontains(google_iam_workload_identity_pool_provider.n8n_aws[0].attribute_condition, "attribute.aws_role") &&
      strcontains(google_service_account_iam_member.n8n_wif_stg[0].member, "/attribute.aws_role/n8n-ticket-staging") &&
      strcontains(google_service_account_iam_member.n8n_wif_prod[0].member, "/attribute.aws_role/n8n-ticket-production")
    )
    error_message = "WIF debe normalizar el role de sesión y separar los principals por entorno."
  }
}

run "aws_wif_rejects_one_role_shared_by_both_environments" {
  command = plan

  variables {
    enable_n8n_wif     = true
    n8n_aws_account_id = "123456789012"
    n8n_aws_role_arns = {
      staging    = "arn:aws:iam::123456789012:role/n8n-ticket"
      production = "arn:aws:iam::123456789012:role/n8n-ticket"
    }
  }

  expect_failures = [google_iam_workload_identity_pool.n8n[0]]
}
