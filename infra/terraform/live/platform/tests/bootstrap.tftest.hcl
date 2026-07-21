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
  mock_resource "google_service_account" {
    defaults = {
      email = "mock-build@rag-kb-system.iam.gserviceaccount.com"
      name  = "projects/rag-kb-system/serviceAccounts/mock-build@rag-kb-system.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}

# El repo existe y se importa en G1B; bajo mock no hay API remota de import.
override_resource {
  target = google_artifact_registry_repository.images
}

override_resource {
  target = google_service_account.runtime["ticket-producer-prod"]
  values = {
    email = "ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com"
    name  = "projects/rag-kb-system/serviceAccounts/ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com"
  }
}

override_resource {
  target = google_firestore_database.environment["production"]
}

variables {
  cicd_bootstrap = {
    enabled                         = true
    release_controller_image_digest = "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/release-controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  production_release_group_email = "ticket-release@example.com"
  gate_approver_accounts = {
    g1b-gcp-owner             = ["gcp-owner@example.com"]
    g1b-release-owner         = ["release-owner@example.com"]
    g2-gcp-owner              = ["gcp-owner@example.com"]
    g6b-gcp-owner             = ["gcp-owner@example.com"]
    g6b-release-owner         = ["release-owner@example.com"]
    g6b-forusbots-owner       = ["forusbots-owner@example.com"]
    g1c-prepare-gcp-owner     = ["gcp-owner@example.com"]
    g1c-prepare-api-owner     = ["api-owner@example.com"]
    g1c-prepare-operations    = ["operations@example.com"]
    g1c-enforce-gcp-owner     = ["gcp-owner@example.com"]
    g1c-enforce-api-owner     = ["api-owner@example.com"]
    g1c-enforce-operations    = ["operations@example.com"]
    g4-requester              = ["g4-requester@example.com"]
    g4-n8n-owner              = ["g4-n8n-owner@example.com"]
    g4-participant-plan-owner = ["g4-participant-plan@example.com"]
    g4-forusbots-owner        = ["g4-forusbots-owner@example.com"]
    g4-delivery-owner         = ["g4-delivery-owner@example.com"]
    g5-maintainer             = ["g5-maintainer@example.com"]
    g5-requester              = ["g5-requester@example.com"]
    g5v-security-owner        = ["g5v-security@example.com"]
    g5v-release-owner         = ["g5v-release@example.com"]
    g5v-requester             = ["g5v-requester@example.com"]
  }
}

run "privileged_and_gate_triggers_use_distinct_sas_and_a_pinned_controller" {
  command = plan

  assert {
    condition = length(toset([
      google_service_account.plan_platform[0].account_id,
      google_service_account.apply_platform[0].account_id,
      google_service_account.plan_staging[0].account_id,
      google_service_account.apply_staging[0].account_id,
      google_service_account.plan_production[0].account_id,
      google_service_account.apply_production[0].account_id,
      google_service_account.staging_attest[0].account_id,
      google_service_account.evidence_manifest[0].account_id,
      google_service_account.test_only[0].account_id,
      google_service_account.e2e_image[0].account_id,
      google_service_account.runtime_attest[0].account_id,
      google_service_account.staging_observer[0].account_id,
    ])) == 12
    error_message = "Cada propósito privilegiado debe usar una SA exclusiva; sólo las dos observaciones comparten observer."
  }

  assert {
    condition = alltrue([
      for image in concat([
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
        google_cloudbuild_trigger.runtime_attest[0].build[0].step[0].name,
        google_cloudbuild_trigger.staging_observe[0].build[0].step[0].name,
        google_cloudbuild_trigger.rollback_observe[0].build[0].step[0].name,
        ], [
        for trigger in values(google_cloudbuild_trigger.gate_receipt) :
        trigger.build[0].step[0].name
      ]) : image == var.cicd_bootstrap.release_controller_image_digest
    ])
    error_message = "Los triggers privilegiados sólo pueden ejecutar el controller fijado por digest."
  }

  assert {
    condition = (
      google_cloudbuild_trigger.platform_plan[0].substitutions["_CICD_BOOTSTRAP_CONTROLLER_DIGEST"] == var.cicd_bootstrap.release_controller_image_digest &&
      google_cloudbuild_trigger.platform_plan[0].substitutions["_GATE_APPROVER_ACCOUNTS_JSON"] == jsonencode(var.gate_approver_accounts) &&
      google_cloudbuild_trigger.platform_plan[0].substitutions["_PRODUCTION_RELEASE_GROUP_EMAIL"] == var.production_release_group_email &&
      google_cloudbuild_trigger.platform_plan[0].substitutions["_ENABLE_LEGACY_TRIGGER_NEUTRALIZATION"] == "false" &&
      alltrue([
        for flag in [
          "--cicd-bootstrap-controller-digest",
          "--gate-approver-accounts-json",
          "--production-release-group-email",
          "--enable-legacy-trigger-neutralization",
        ] : contains(google_cloudbuild_trigger.platform_plan[0].build[0].step[0].args, flag)
      ])
    )
    error_message = "Platform plan debe conservar todos los inputs bootstrap en substitutions y argumentos explícitos."
  }

  assert {
    condition = alltrue(concat([
      google_cloudbuild_trigger.platform_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.staging_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.production_apply[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.evidence_manifest[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.runtime_attest[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.staging_observe[0].approval_config[0].approval_required,
      google_cloudbuild_trigger.rollback_observe[0].approval_config[0].approval_required,
      ], [
      for trigger in values(google_cloudbuild_trigger.gate_receipt) :
      trigger.approval_config[0].approval_required
    ]))
    error_message = "Apply y el manifest de promoción deben requerir aprobación manual."
  }

  assert {
    condition = (
      length(google_service_account.gate_receipt) == 22 &&
      length(toset([
        for account in values(google_service_account.gate_receipt) :
        account.account_id
      ])) == 22 &&
      alltrue([
        for account in values(google_service_account.gate_receipt) :
        length(account.account_id) >= 6 && length(account.account_id) <= 30
      ]) &&
      alltrue([
        for key, trigger in google_cloudbuild_trigger.gate_receipt :
        trigger.build[0].step[0].args[0] == "gate-receipt" &&
        trigger.build[0].step[0].args[4] == local.gate_receipt_specs[key].approver_role &&
        trigger.build[0].step[0].args[6] == local.gate_receipt_specs[key].approver_accounts
      ])
    )
    error_message = "Cada receipt gate/rol requiere SA corta exclusiva y allowlist literal trusted."
  }
}

run "platform_apply_actas_only_exact_managed_service_accounts" {
  command = apply

  assert {
    condition = (
      toset(keys(google_service_account_iam_member.platform_apply_actas_scheduler)) ==
      toset(concat(
        ["scheduler-staging", "scheduler-production"],
        [for key in keys(local.controller_runtime_sas) : "build-${key}"],
      )) &&
      alltrue([
        for key, grant in google_service_account_iam_member.platform_apply_actas_scheduler :
        grant.service_account_id == local.platform_apply_actas_sas[key] &&
        grant.role == "roles/iam.serviceAccountUser" &&
        grant.member == "serviceAccount:${google_service_account.apply_platform[0].email}"
      ])
    )
    error_message = "Platform apply sólo puede actAs las SAs exactas de triggers/controller y schedulers administradas por platform."
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

run "artifact_builders_use_imported_repo_and_are_scanners_only" {
  command = plan

  assert {
    condition = (
      google_artifact_registry_repository.images.repository_id == "kb-rag" &&
      google_artifact_registry_repository_iam_member.image_writer["runtime"].repository == "kb-rag" &&
      google_artifact_registry_repository_iam_member.image_writer["e2e"].repository == "kb-rag" &&
      google_artifact_registry_repository_iam_member.image_writer["release_controller"].repository == "kb-rag"
    )
    error_message = "Los builders explícitos deben usar el repo existente importado."
  }

  assert {
    condition = alltrue([
      for binding in values(google_project_iam_member.image_scanner) :
      binding.role == "roles/ondemandscanning.admin"
    ])
    error_message = "Los tres builders necesitan On-Demand Scanning sin permisos deploy/state."
  }
}

run "plan_is_read_only_apply_is_root_functional_and_release_owned" {
  command = plan

  assert {
    condition = (
      !contains(google_project_iam_custom_role.environment_plan_reader[0].permissions, "datastore.entities.get") &&
      contains(google_project_iam_custom_role.environment_plan_reader[0].permissions, "datastore.databases.getMetadata") &&
      google_project_iam_member.apply_functional["platform-roles-serviceusage.serviceUsageAdmin"].role == "roles/serviceusage.serviceUsageAdmin" &&
      length(google_project_iam_member.environment_run_creator) == 0 &&
      length(google_project_iam_member.platform_apply_iam_broker) == 1 &&
      length(google_project_iam_custom_role.platform_project_iam_broker) == 1
    )
    error_message = "Plan debe ser metadata-only y cada apply debe usar la SA de su root."
  }

  assert {
    condition = (
      google_artifact_registry_repository_iam_member.environment_apply_runtime_reader["staging"].repository == "kb-rag" &&
      google_artifact_registry_repository_iam_member.environment_apply_runtime_reader["production"].repository == "kb-rag" &&
      google_artifact_registry_repository_iam_member.environment_apply_runtime_reader["production"].role == "roles/artifactregistry.reader"
    )
    error_message = "Apply sólo recibe reader del repo importado; nunca puede publicar o retaggear."
  }

  assert {
    condition = (
      google_service_account_iam_member.production_release_group[0].member == "group:ticket-release@example.com" &&
      google_project_iam_member.production_release_approver[0].role == "roles/cloudbuild.builds.approver" &&
      google_storage_bucket_iam_member.apply_state_admin["production"].bucket == "rag-kb-system-tfstate-production-900340137010"
    )
    error_message = "Production apply exige release group y sólo state production."
  }
}

run "g1b_alone_creates_zero_environment_resources" {
  command = plan

  assert {
    condition = alltrue([
      length(google_firestore_database.environment) == 0,
      length(google_secret_manager_secret.environment) == 0,
      length(google_cloud_tasks_queue.environment) == 0,
      length(google_cloud_scheduler_job.environment) == 0,
      length(google_project_iam_custom_role.ticket_queue_enqueuer) == 0,
      length(google_project_iam_member.runtime_firestore) == 0,
      length(google_project_iam_member.runtime_vertex) == 0,
      length(google_project_iam_member.runtime_telemetry) == 0,
      length(google_cloud_tasks_queue_iam_member.runtime_producer_queue) == 0,
      length(google_cloud_tasks_queue_iam_member.runtime_reconciler_queue) == 0,
      length(google_service_account_iam_member.runtime_producer_actas_signer) == 0,
      length(google_service_account_iam_member.runtime_reconciler_actas_signer) == 0,
      length(google_service_account_iam_member.tasks_agent_signs_as_runtime_signer) == 0,
      length(google_project_iam_member.environment_run_creator) == 0,
      length(google_cloud_run_v2_service_iam_member.environment_apply_developer) == 0,
      length(google_cloud_run_v2_job_iam_member.environment_apply_developer) == 0,
    ])
    error_message = "G1B/default no puede crear ni conceder runtime IAM de staging/production."
  }
}

run "g1b_plus_g2_creates_only_staging_containers" {
  command = plan

  variables {
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_secret_ids = {
      staging    = ["ticket-staging-api-key"]
      production = []
    }
  }

  assert {
    condition = (
      keys(google_firestore_database.environment) == ["staging"] &&
      keys(google_cloud_tasks_queue.environment) == ["staging"] &&
      keys(google_cloud_scheduler_job.environment) == ["staging"] &&
      keys(google_project_iam_custom_role.ticket_queue_enqueuer) == ["staging"] &&
      keys(google_secret_manager_secret.environment) == ["staging-ticket-staging-api-key"] &&
      keys(google_cloud_tasks_queue_iam_member.platform_apply_queue_task_inspector) == ["staging"] &&
      google_cloud_tasks_queue_iam_member.platform_apply_queue_task_inspector["staging"].name == "ticket-jobs-staging" &&
      google_cloud_tasks_queue_iam_member.platform_apply_queue_task_inspector["staging"].role == google_project_iam_custom_role.platform_queue_task_inspector[0].id &&
      alltrue([
        for grant in values(google_project_iam_member.runtime_firestore) :
        grant.condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/ticket-staging\""
      ])
    )
    error_message = "G1B+G2 debe materializar exclusivamente containers/IAM de staging."
  }
}

run "g1b_plus_g6b_creates_only_production_containers" {
  command = plan

  variables {
    environment_container_phase = {
      staging    = "disabled"
      production = "managed"
    }
    environment_secret_ids = {
      staging    = []
      production = ["ticket-production-api-key"]
    }
    existing_environment_secret_ids = {
      staging    = []
      production = ["ticket-production-api-key"]
    }
  }

  override_resource {
    target = google_secret_manager_secret.environment["production-ticket-production-api-key"]
  }

  assert {
    condition = (
      keys(google_firestore_database.environment) == ["production"] &&
      keys(google_cloud_tasks_queue.environment) == ["production"] &&
      keys(google_cloud_scheduler_job.environment) == ["production"] &&
      keys(google_project_iam_custom_role.ticket_queue_enqueuer) == ["production"] &&
      keys(google_secret_manager_secret.environment) == ["production-ticket-production-api-key"] &&
      google_service_account.runtime["ticket-producer-prod"].account_id == "ticket-producer-prod" &&
      google_project_iam_member.runtime_firestore["production-producer"].member == "serviceAccount:ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com" &&
      google_project_iam_member.runtime_vertex["production-producer"].member == "serviceAccount:ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com" &&
      toset(keys(google_project_iam_member.runtime_telemetry)) == toset([
        "production-producer-logging",
        "production-producer-monitoring",
      ]) &&
      alltrue([
        for grant in values(google_project_iam_member.runtime_firestore) :
        grant.condition[0].expression == "resource.name == \"projects/rag-kb-system/databases/(default)\""
      ])
    )
    error_message = "G1B+G6B debe importar/materializar exclusivamente containers/IAM de production."
  }
}

run "container_phase_rejects_managed_before_g1b" {
  command = plan

  variables {
    cicd_bootstrap = {
      enabled                         = false
      release_controller_image_digest = ""
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
  }

  expect_failures = [var.environment_container_phase]
}

run "run_handoff_rejects_missing_container_gate" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "bootstrap"
      production = "disabled"
    }
  }

  expect_failures = [var.environment_handoff_phase]
}

run "disabled_containers_reject_secret_inventory" {
  command = plan

  variables {
    environment_secret_ids = {
      staging    = ["forbidden-before-g2"]
      production = []
    }
  }

  expect_failures = [var.environment_secret_ids]
}

run "bootstrap_rejects_missing_release_group" {
  command = plan

  variables {
    production_release_group_email = ""
  }

  expect_failures = [var.production_release_group_email]
}

run "bootstrap_rejects_one_principal_claiming_two_gate_roles" {
  command = plan

  variables {
    gate_approver_accounts = {
      g1b-gcp-owner             = ["same-owner@example.com"]
      g1b-release-owner         = ["same-owner@example.com"]
      g2-gcp-owner              = ["gcp-owner@example.com"]
      g6b-gcp-owner             = ["gcp-owner@example.com"]
      g6b-release-owner         = ["release-owner@example.com"]
      g6b-forusbots-owner       = ["forusbots-owner@example.com"]
      g1c-prepare-gcp-owner     = ["gcp-owner@example.com"]
      g1c-prepare-api-owner     = ["api-owner@example.com"]
      g1c-prepare-operations    = ["operations@example.com"]
      g1c-enforce-gcp-owner     = ["gcp-owner@example.com"]
      g1c-enforce-api-owner     = ["api-owner@example.com"]
      g1c-enforce-operations    = ["operations@example.com"]
      g4-requester              = ["g4-requester@example.com"]
      g4-n8n-owner              = ["g4-n8n-owner@example.com"]
      g4-participant-plan-owner = ["g4-participant-plan@example.com"]
      g4-forusbots-owner        = ["g4-forusbots-owner@example.com"]
      g4-delivery-owner         = ["g4-delivery-owner@example.com"]
      g5-maintainer             = ["g5-maintainer@example.com"]
      g5-requester              = ["g5-requester@example.com"]
      g5v-security-owner        = ["g5v-security@example.com"]
      g5v-release-owner         = ["g5v-release@example.com"]
      g5v-requester             = ["g5v-requester@example.com"]
    }
  }

  expect_failures = [var.gate_approver_accounts]
}

run "run_handoff_bootstrap_grants_only_temporary_create" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "bootstrap"
      production = "disabled"
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
  }

  assert {
    condition = (
      length(google_project_iam_member.environment_run_creator) == 1 &&
      contains(keys(google_project_iam_member.environment_run_creator), "staging") &&
      length(google_cloud_run_v2_service_iam_member.environment_apply_developer) == 0 &&
      length(google_cloud_run_v2_job_iam_member.environment_apply_developer) == 0 &&
      google_project_iam_custom_role.environment_run_creator[0].permissions == toset([
        "run.jobs.create",
        "run.services.create",
      ])
    )
    error_message = "Bootstrap sólo puede otorgar create temporal y todavía no debe inventar IAM child."
  }
}

run "run_handoff_bootstrap_addition_keeps_existing_child_iam" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "bootstrap"
      production = "disabled"
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_run_resources = {
      staging = [
        "services/kb-rag-system-staging",
        "services/kb-rag-ticket-worker-staging",
        "jobs/ticket-reconciler-staging",
      ]
      production = []
    }
  }

  assert {
    condition = (
      length(google_project_iam_member.environment_run_creator) == 1 &&
      length(google_cloud_run_v2_service_iam_member.environment_apply_developer) == 2 &&
      length(google_cloud_run_v2_job_iam_member.environment_apply_developer) == 1 &&
      google_cloud_scheduler_job.environment["staging"].paused == true
    )
    error_message = "Añadir E2E conserva IAM directo, pero el scheduler sigue pausado sin fase activa atestada."
  }
}

run "scheduler_stays_paused_in_dark_and_only_unpauses_for_active_phase" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_run_resources = {
      staging = [
        "services/kb-rag-system-staging",
        "services/kb-rag-ticket-worker-staging",
        "jobs/ticket-reconciler-staging",
      ]
      production = []
    }
    environment_release_phase = {
      staging    = "shadow"
      production = "disabled"
    }
  }

  assert {
    condition     = google_cloud_scheduler_job.environment["staging"].paused == false
    error_message = "Sólo una fase activa atestada puede habilitar el scheduler staging."
  }
}

run "scheduler_dark_phase_is_always_paused" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_run_resources = {
      staging    = ["jobs/ticket-reconciler-staging"]
      production = []
    }
    environment_release_phase = {
      staging    = "dark_100"
      production = "disabled"
    }
  }

  assert {
    condition     = google_cloud_scheduler_job.environment["staging"].paused == true
    error_message = "Las fases infra/dark nunca pueden ejecutar el reconciliador."
  }
}

run "run_handoff_managed_revokes_creator_and_uses_child_iam" {
  command = plan

  variables {
    environment_handoff_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_container_phase = {
      staging    = "managed"
      production = "disabled"
    }
    environment_run_resources = {
      staging = [
        "services/kb-rag-system-staging",
        "services/kb-rag-ticket-worker-staging",
        "jobs/ticket-reconciler-staging",
        "jobs/ticket-e2e-staging",
      ]
      production = []
    }
  }

  assert {
    condition = (
      length(google_project_iam_member.environment_run_creator) == 0 &&
      length(google_cloud_run_v2_service_iam_member.environment_apply_developer) == 2 &&
      length(google_cloud_run_v2_job_iam_member.environment_apply_developer) == 2 &&
      alltrue([
        for binding in values(google_cloud_run_v2_service_iam_member.environment_apply_developer) :
        binding.role == "roles/run.developer"
      ]) &&
      alltrue([
        for binding in values(google_cloud_run_v2_job_iam_member.environment_apply_developer) :
        binding.role == "roles/run.developer"
      ])
    )
    error_message = "Managed debe demostrar revocación del creator y IAM directo de todos los recursos inventariados."
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
  command = plan

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
      strcontains(google_iam_workload_identity_pool_provider.n8n_aws[0].attribute_condition, "n8n-ticket-staging") &&
      strcontains(google_iam_workload_identity_pool_provider.n8n_aws[0].attribute_condition, "n8n-ticket-production") &&
      length(google_service_account_iam_member.n8n_wif_stg) == 1 &&
      length(google_service_account_iam_member.n8n_wif_prod) == 1
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
