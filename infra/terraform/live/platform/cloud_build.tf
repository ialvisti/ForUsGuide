# Cloud Build bootstrap declarativo. El trigger histórico conserva su nombre e
# ID pero cambia in-place a CI sin deploy; los triggers privilegiados y sus
# receipts sólo existen cuando G1B fija un release-controller por digest.

locals {
  # Un build de Cloud Build registra un solo approver. Estos receipts separados
  # materializan el quorum multiparte sin permitir que un caller sustituya su
  # rol o allowlist. account_id usa aliases explícitos para respetar <=30 chars.
  gate_receipt_specs = var.cicd_bootstrap.enabled ? {
    g1b-gcp-owner = {
      account_id        = "ticket-g1b-gcp"
      gate              = "G1B"
      approver_role     = "gcp-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1b-gcp-owner"])))
    }
    g1b-release-owner = {
      account_id        = "ticket-g1b-release"
      gate              = "G1B"
      approver_role     = "release-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1b-release-owner"])))
    }
    g2-gcp-owner = {
      account_id        = "ticket-g2-gcp"
      gate              = "G2"
      approver_role     = "gcp-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g2-gcp-owner"])))
    }
    g6b-gcp-owner = {
      account_id        = "ticket-g6b-gcp"
      gate              = "G6B"
      approver_role     = "gcp-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g6b-gcp-owner"])))
    }
    g6b-release-owner = {
      account_id        = "ticket-g6b-release"
      gate              = "G6B"
      approver_role     = "release-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g6b-release-owner"])))
    }
    g6b-forusbots-owner = {
      account_id        = "ticket-g6b-forusbots"
      gate              = "G6B"
      approver_role     = "forusbots-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g6b-forusbots-owner"])))
    }
    g1c-prepare-gcp-owner = {
      account_id        = "ticket-g1cp-gcp"
      gate              = "G1C_PREPARE"
      approver_role     = "gcp-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-prepare-gcp-owner"])))
    }
    g1c-prepare-api-owner = {
      account_id        = "ticket-g1cp-api"
      gate              = "G1C_PREPARE"
      approver_role     = "api-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-prepare-api-owner"])))
    }
    g1c-prepare-operations = {
      account_id        = "ticket-g1cp-ops"
      gate              = "G1C_PREPARE"
      approver_role     = "operations"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-prepare-operations"])))
    }
    g1c-enforce-gcp-owner = {
      account_id        = "ticket-g1ce-gcp"
      gate              = "G1C_ENFORCE"
      approver_role     = "gcp-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-enforce-gcp-owner"])))
    }
    g1c-enforce-api-owner = {
      account_id        = "ticket-g1ce-api"
      gate              = "G1C_ENFORCE"
      approver_role     = "api-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-enforce-api-owner"])))
    }
    g1c-enforce-operations = {
      account_id        = "ticket-g1ce-ops"
      gate              = "G1C_ENFORCE"
      approver_role     = "operations"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g1c-enforce-operations"])))
    }
    g4-requester = {
      account_id        = "ticket-g4-requester"
      gate              = "G4"
      approver_role     = "requester"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g4-requester"])))
    }
    g4-n8n-owner = {
      account_id        = "ticket-g4-n8n"
      gate              = "G4"
      approver_role     = "n8n-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g4-n8n-owner"])))
    }
    g4-participant-plan-owner = {
      account_id        = "ticket-g4-participant"
      gate              = "G4"
      approver_role     = "participant-plan-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g4-participant-plan-owner"])))
    }
    g4-forusbots-owner = {
      account_id        = "ticket-g4-forusbots"
      gate              = "G4"
      approver_role     = "forusbots-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g4-forusbots-owner"])))
    }
    g4-delivery-owner = {
      account_id        = "ticket-g4-delivery"
      gate              = "G4"
      approver_role     = "delivery-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g4-delivery-owner"])))
    }
    g5-maintainer = {
      account_id        = "ticket-g5-maintainer"
      gate              = "G5"
      approver_role     = "maintainer"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g5-maintainer"])))
    }
    g5-requester = {
      account_id        = "ticket-g5-requester"
      gate              = "G5"
      approver_role     = "requester"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g5-requester"])))
    }
    g5v-security-owner = {
      account_id        = "ticket-g5v-security"
      gate              = "G5V"
      approver_role     = "security-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g5v-security-owner"])))
    }
    g5v-release-owner = {
      account_id        = "ticket-g5v-release"
      gate              = "G5V"
      approver_role     = "release-owner"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g5v-release-owner"])))
    }
    g5v-requester = {
      account_id        = "ticket-g5v-requester"
      gate              = "G5V"
      approver_role     = "requester"
      approver_accounts = join(",", sort(tolist(var.gate_approver_accounts["g5v-requester"])))
    }
  } : {}

  # Cada gate firma sólo el scope mínimo que su consumidor vuelve a validar.
  # Maps separados evitan que un approver autorice campos irrelevantes o que
  # un receipt de un flujo pueda reutilizarse en otro.
  gate_receipt_scope_substitutions = {
    G1B = tomap({
      _CANDIDATE_SHA                          = ""
      _CONTROLLER_DIGEST                      = ""
      _PLAN_URI                               = ""
      _PLAN_SHA256                            = ""
      _ROOT                                   = ""
      _RELEASE_PHASE                          = ""
      _IMAGE_DIGEST                           = ""
      _PLATFORM_CONTAINER_PHASES_SHA256       = ""
      _PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256 = ""
      _PLATFORM_RELEASE_PHASES_SHA256         = ""
    })
    G2 = tomap({
      _CANDIDATE_SHA                          = ""
      _CONTROLLER_DIGEST                      = ""
      _PLAN_URI                               = ""
      _PLAN_SHA256                            = ""
      _ROOT                                   = ""
      _RELEASE_PHASE                          = ""
      _IMAGE_DIGEST                           = ""
      _PLATFORM_CONTAINER_PHASES_SHA256       = ""
      _PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256 = ""
      _PLATFORM_RELEASE_PHASES_SHA256         = ""
    })
    G6B = tomap({
      _CANDIDATE_SHA                          = ""
      _CONTROLLER_DIGEST                      = ""
      _PLAN_URI                               = ""
      _PLAN_SHA256                            = ""
      _ROOT                                   = ""
      _RELEASE_PHASE                          = ""
      _IMAGE_DIGEST                           = ""
      _PLATFORM_CONTAINER_PHASES_SHA256       = ""
      _PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256 = ""
      _PLATFORM_RELEASE_PHASES_SHA256         = ""
    })
    G1C_PREPARE = tomap({
      _CANDIDATE_SHA                          = ""
      _CONTROLLER_DIGEST                      = ""
      _PLAN_URI                               = ""
      _PLAN_SHA256                            = ""
      _ROOT                                   = ""
      _RELEASE_PHASE                          = ""
      _IMAGE_DIGEST                           = ""
      _PLATFORM_CONTAINER_PHASES_SHA256       = ""
      _PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256 = ""
      _PLATFORM_RELEASE_PHASES_SHA256         = ""
    })
    G1C_ENFORCE = tomap({
      _CANDIDATE_SHA                          = ""
      _CONTROLLER_DIGEST                      = ""
      _PLAN_URI                               = ""
      _PLAN_SHA256                            = ""
      _ROOT                                   = ""
      _RELEASE_PHASE                          = ""
      _IMAGE_DIGEST                           = ""
      _PLATFORM_CONTAINER_PHASES_SHA256       = ""
      _PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256 = ""
      _PLATFORM_RELEASE_PHASES_SHA256         = ""
      _PREPARE_SMOKE_URI                      = ""
    })
    G4 = tomap({
      _CANDIDATE_SHA          = ""
      _CONTROLLER_DIGEST      = ""
      _IMAGE_DIGEST           = ""
      _EVIDENCE_INPUTS_SHA256 = ""
    })
    G5 = tomap({
      _CANDIDATE_SHA            = ""
      _CONTROLLER_DIGEST        = ""
      _IMAGE_DIGEST             = ""
      _EVIDENCE_INPUTS_SHA256   = ""
      _EVIDENCE_MANIFEST_URI    = ""
      _EVIDENCE_MANIFEST_SHA256 = ""
    })
    G5V = tomap({
      _CANDIDATE_SHA      = ""
      _CONTROLLER_DIGEST  = ""
      _IMAGE_DIGEST       = ""
      _VULNERABILITY_ID   = ""
      _SCAN_REPORT_SHA256 = ""
    })
  }
}

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "ticket-ci"
  display_name = "Ticket CI (sin Run/IAM Admin ni state prod)"
}

# Intended identity for candidate-controlled controller checks. It is absent
# from every artifact, scan, evidence, state and runtime grant in
# pipeline_iam.tf. The serviceAccount field in a submitted YAML only declares
# this choice; IAM and the caller's actAs grants determine the effective SA.
resource "google_service_account" "controller_verifier" {
  project      = var.project_id
  account_id   = "ticket-controller-verify"
  display_name = "Ticket controller candidate verifier (logging only)"
}

# Capacidad de publicación modelada pero deliberadamente dormida. Ningún YAML,
# trigger ni platform-apply actAs puede seleccionar esta identidad. El árbol
# aún no contiene un publisher pre-G1B confiable/source-less, por lo que su
# mera declaración no desbloquea la publicación ni autoriza G1B.
resource "google_service_account" "controller_builder" {
  project      = var.project_id
  account_id   = "ticket-controller-build"
  display_name = "Ticket release-controller builder (sin deploy/state)"
}

resource "google_service_account" "plan_platform" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-plan-platform"
  display_name = "Ticket platform plan"
}

resource "google_service_account" "apply_platform" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-apply-platform"
  display_name = "Ticket platform apply"
}

resource "google_service_account" "plan_staging" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-plan-staging"
  display_name = "Ticket staging plan"
}

resource "google_service_account" "apply_staging" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-apply-staging"
  display_name = "Ticket staging apply"
}

resource "google_service_account" "plan_production" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-plan-production"
  display_name = "Ticket production plan"
}

resource "google_service_account" "apply_production" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-apply-production"
  display_name = "Ticket production apply (sin state staging)"
}

resource "google_service_account" "staging_attest" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-stg-attest"
  display_name = "Ticket staging attestation"
}

resource "google_service_account" "evidence_manifest" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-evidence"
  display_name = "Ticket evidence manifest"
}

resource "google_service_account" "test_only" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-test-only"
  display_name = "Ticket test-only"
}

resource "google_service_account" "e2e_image" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-e2e-image"
  display_name = "Ticket E2E image builder"
}

resource "google_service_account" "runtime_attest" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-runtime-attest"
  display_name = "Ticket finalized runtime provenance attester"
}

resource "google_service_account" "staging_observer" {
  count        = var.cicd_bootstrap.enabled ? 1 : 0
  project      = var.project_id
  account_id   = "ticket-staging-observer"
  display_name = "Ticket staging and rollback evidence observer"
}

resource "google_service_account" "gate_receipt" {
  for_each     = local.gate_receipt_specs
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = "Ticket authenticated receipt ${each.key}"
}

# Los campos de scope son substitutions revisables por el approver y se
# contrastan después con el manifest inmutable del plan. La allowlist humana
# es deliberadamente un argumento literal de la configuración del trigger.
resource "google_cloudbuild_trigger" "gate_receipt" {
  for_each        = local.gate_receipt_specs
  project         = var.project_id
  name            = "handle-ticket-gate-${each.key}"
  location        = "global"
  service_account = google_service_account.gate_receipt[each.key].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = local.gate_receipt_scope_substitutions[each.value.gate]
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = [
        "gate-receipt",
        "--gate", each.value.gate,
        "--approver-role", each.value.approver_role,
        "--approver-accounts", each.value.approver_accounts,
      ]
      env = [
        "BUILD_ID=$BUILD_ID",
        "PROJECT_ID=$PROJECT_ID",
        "COMMIT_SHA=$COMMIT_SHA",
      ]
    }
    timeout = "900s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

# CI de rama: fuente del repo permitida porque no posee deploy/state.
resource "google_cloudbuild_trigger" "ci" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-ci"
  location        = "global"
  service_account = google_service_account.ci.id
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["runtime-image", "--candidate-sha", "$COMMIT_SHA"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    images  = ["${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/kb-rag-system:$COMMIT_SHA-$BUILD_ID"]
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }

  github {
    owner = "ialvisti"
    name  = "ForUsGuide"
    push {
      branch = "^handle-ticket-production-finalization$"
    }
  }
}

# Importa c2126528-7cd3-4063-9214-5eb82e9f76a6: mismo trigger/nombre, pero
# ahora ejecuta únicamente el CI canónico sin comandos de deploy.
resource "google_cloudbuild_trigger" "main_canonical" {
  for_each        = var.enable_legacy_trigger_neutralization && var.cicd_bootstrap.enabled ? { legacy = true } : {}
  project         = var.project_id
  name            = "deploy-kb-rag-system"
  location        = "global"
  service_account = google_service_account.ci.id
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["runtime-image", "--candidate-sha", "$COMMIT_SHA"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    images  = ["${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/kb-rag-system:$COMMIT_SHA-$BUILD_ID"]
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }

  ignored_files = [
    "docs/verification/**",
    "kb-rag-system/Development Docs/**",
    "**/README.md",
  ]

  github {
    owner = "ialvisti"
    name  = "ForUsGuide"
    push {
      branch = "^main$"
    }
  }
}

# A partir de aquí toda configuración es inline. Candidate SHA/digest/plan son
# argumentos del controller; jamás pueden sustituir el nombre de su imagen.
resource "google_cloudbuild_trigger" "platform_plan" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-platform-plan"
  location        = "global"
  service_account = google_service_account.plan_platform[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = {
    _CICD_BOOTSTRAP_CONTROLLER_DIGEST = (
      var.cicd_bootstrap.enabled
      ? var.cicd_bootstrap.release_controller_image_digest
      : ""
    )
    _GATE_APPROVER_ACCOUNTS_JSON          = jsonencode(var.gate_approver_accounts)
    _PRODUCTION_RELEASE_GROUP_EMAIL       = var.production_release_group_email
    _ENABLE_LEGACY_TRIGGER_NEUTRALIZATION = tostring(var.enable_legacy_trigger_neutralization)
    _STAGING_RELEASE_PHASE                = var.environment_release_phase.staging
    _PRODUCTION_RELEASE_PHASE             = var.environment_release_phase.production
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = [
        "plan", "platform",
        "--candidate-sha", "$_CANDIDATE_SHA",
        "--firestore-scope-phase", "$_FIRESTORE_SCOPE_PHASE",
        "--staging-container-phase", "$_STAGING_CONTAINER_PHASE",
        "--production-container-phase", "$_PRODUCTION_CONTAINER_PHASE",
        "--staging-release-phase", "$_STAGING_RELEASE_PHASE",
        "--production-release-phase", "$_PRODUCTION_RELEASE_PHASE",
        "--staging-approved-image-digest", "$_STAGING_APPROVED_IMAGE_DIGEST",
        "--production-approved-image-digest", "$_PRODUCTION_APPROVED_IMAGE_DIGEST",
        "--staging-handoff-phase", "$_STAGING_HANDOFF_PHASE",
        "--production-handoff-phase", "$_PRODUCTION_HANDOFF_PHASE",
        "--staging-run-resources", "$_STAGING_RUN_RESOURCES",
        "--production-run-resources", "$_PRODUCTION_RUN_RESOURCES",
        "--staging-environment-tfvars-uri", "$_STAGING_ENVIRONMENT_TFVARS_URI",
        "--production-environment-tfvars-uri", "$_PRODUCTION_ENVIRONMENT_TFVARS_URI",
        "--staging-existing-secret-ids", "$_STAGING_EXISTING_SECRET_IDS",
        "--production-existing-secret-ids", "$_PRODUCTION_EXISTING_SECRET_IDS",
        "--cicd-bootstrap-controller-digest", "$_CICD_BOOTSTRAP_CONTROLLER_DIGEST",
        "--gate-approver-accounts-json", "$_GATE_APPROVER_ACCOUNTS_JSON",
        "--production-release-group-email", "$_PRODUCTION_RELEASE_GROUP_EMAIL",
        "--enable-legacy-trigger-neutralization", "$_ENABLE_LEGACY_TRIGGER_NEUTRALIZATION",
        "--controller-digest", var.cicd_bootstrap.release_controller_image_digest,
      ]
      env = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "platform_apply" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-platform-apply"
  location        = "global"
  service_account = google_service_account.apply_platform[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = [
        "apply", "platform",
        "--plan-uri", "$_PLAN_URI",
        "--plan-sha256", "$_PLAN_SHA256",
        "--gate-receipts", "$_GATE_RECEIPTS",
        "--prepare-smoke-uri", "$_PREPARE_SMOKE_URI",
        "--controller-digest", var.cicd_bootstrap.release_controller_image_digest,
      ]
      env = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "staging_plan" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-staging-plan"
  location        = "global"
  service_account = google_service_account.plan_staging[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["plan", "staging", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--release-phase", "$_RELEASE_PHASE", "--platform-outputs-uri", "$_PLATFORM_OUTPUTS_URI", "--environment-tfvars-uri", "$_ENVIRONMENT_TFVARS_URI", "--controller-digest", var.cicd_bootstrap.release_controller_image_digest]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "staging_apply" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-staging-apply"
  location        = "global"
  service_account = google_service_account.apply_staging[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = [
        "apply", "staging",
        "--plan-uri", "$_PLAN_URI",
        "--plan-sha256", "$_PLAN_SHA256",
        "--gate-receipts", "$_GATE_RECEIPTS",
        "--controller-digest", var.cicd_bootstrap.release_controller_image_digest,
      ]
      env = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "production_plan" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-production-plan"
  location        = "global"
  service_account = google_service_account.plan_production[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["plan", "production", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--release-phase", "$_RELEASE_PHASE", "--promotion-uri", "$_PROMOTION_URI", "--platform-outputs-uri", "$_PLATFORM_OUTPUTS_URI", "--environment-tfvars-uri", "$_ENVIRONMENT_TFVARS_URI", "--controller-digest", var.cicd_bootstrap.release_controller_image_digest]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "production_apply" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-production-apply"
  location        = "global"
  service_account = google_service_account.apply_production[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = [
        "apply", "production",
        "--plan-uri", "$_PLAN_URI",
        "--plan-sha256", "$_PLAN_SHA256",
        "--promotion-uri", "$_PROMOTION_URI",
        "--gate-receipts", "$_GATE_RECEIPTS",
        "--controller-digest", var.cicd_bootstrap.release_controller_image_digest,
      ]
      env = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "staging_attest" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-staging-attest"
  location        = "global"
  service_account = google_service_account.staging_attest[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = {
    _GATE_RECEIPTS = ""
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["staging-attest", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--controller-digest", var.cicd_bootstrap.release_controller_image_digest, "--gate-receipts", "$_GATE_RECEIPTS"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "1800s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "evidence_manifest" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-evidence-manifest"
  location        = "global"
  service_account = google_service_account.evidence_manifest[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = {
    _GATE_RECEIPTS = ""
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["evidence-manifest", "--evidence-sha", "$_EVIDENCE_SHA", "--main-sha", "$_MAIN_SHA", "--image-digest", "$_IMAGE_DIGEST", "--controller-digest", var.cicd_bootstrap.release_controller_image_digest, "--gate-receipts", "$_GATE_RECEIPTS"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "1800s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "test_only" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-test-only"
  location        = "global"
  service_account = google_service_account.test_only[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["test-only", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "e2e_image" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-e2e-image"
  location        = "global"
  service_account = google_service_account.e2e_image[0].id

  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["e2e-image", "--candidate-sha", "$_CANDIDATE_SHA"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "runtime_attest" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-runtime-attest"
  location        = "global"
  service_account = google_service_account.runtime_attest[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = {
    _GATE_RECEIPTS = ""
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["runtime-attest", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--source-build-id", "$_SOURCE_BUILD_ID", "--gate-receipts", "$_GATE_RECEIPTS"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "1800s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "staging_observe" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-staging-observe"
  location        = "global"
  service_account = google_service_account.staging_observer[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["staging-observe", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "1800s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}

resource "google_cloudbuild_trigger" "rollback_observe" {
  count           = var.cicd_bootstrap.enabled ? 1 : 0
  project         = var.project_id
  name            = "handle-ticket-rollback-observe"
  location        = "global"
  service_account = google_service_account.staging_observer[0].id

  approval_config {
    approval_required = true
  }
  source_to_build {
    uri       = "https://github.com/ialvisti/ForUsGuide"
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["rollback-observe", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--baseline-revision", "$_BASELINE_REVISION", "--baseline-image-digest", "$_BASELINE_IMAGE_DIGEST", "--poll-before-uri", "$_POLL_BEFORE_URI", "--poll-after-uri", "$_POLL_AFTER_URI"]
      env  = ["BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID", "COMMIT_SHA=$COMMIT_SHA"]
    }
    timeout = "1800s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}
