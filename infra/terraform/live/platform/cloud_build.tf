# Cloud Build bootstrap declarativo. El trigger histórico conserva su nombre e
# ID pero cambia in-place a CI sin deploy; los diez triggers privilegiados sólo
# existen cuando G1B fija un release-controller por digest.

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "ticket-ci"
  display_name = "Ticket CI (sin Run/IAM Admin ni state prod)"
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

# CI de rama: fuente del repo permitida porque no posee deploy/state.
resource "google_cloudbuild_trigger" "ci" {
  project         = var.project_id
  name            = "handle-ticket-ci"
  location        = "global"
  service_account = google_service_account.ci.id
  filename        = "kb-rag-system/cloudbuild.yaml"

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
  for_each        = var.enable_legacy_trigger_neutralization ? { legacy = true } : {}
  project         = var.project_id
  name            = "deploy-kb-rag-system"
  location        = "global"
  service_account = google_service_account.ci.id
  filename        = "kb-rag-system/cloudbuild.yaml"

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
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["plan", "platform", "--candidate-sha", "$_CANDIDATE_SHA", "--firestore-scope-phase", "$_FIRESTORE_SCOPE_PHASE"]
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
      args = ["apply", "platform", "--plan-uri", "$_PLAN_URI", "--plan-sha256", "$_PLAN_SHA256"]
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
      args = ["plan", "staging", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--release-phase", "$_RELEASE_PHASE"]
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
      args = ["apply", "staging", "--plan-uri", "$_PLAN_URI", "--plan-sha256", "$_PLAN_SHA256"]
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
      args = ["plan", "production", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST", "--release-phase", "$_RELEASE_PHASE", "--promotion-uri", "$_PROMOTION_URI"]
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
      args = ["apply", "production", "--plan-uri", "$_PLAN_URI", "--plan-sha256", "$_PLAN_SHA256", "--promotion-uri", "$_PROMOTION_URI"]
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
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["staging-attest", "--candidate-sha", "$_CANDIDATE_SHA", "--image-digest", "$_IMAGE_DIGEST"]
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
  build {
    step {
      name = var.cicd_bootstrap.release_controller_image_digest
      args = ["evidence-manifest", "--evidence-sha", "$_EVIDENCE_SHA", "--main-sha", "$_MAIN_SHA", "--image-digest", "$_IMAGE_DIGEST"]
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
    }
    timeout = "3600s"
    options {
      logging                 = "CLOUD_LOGGING_ONLY"
      requested_verify_option = "VERIFIED"
    }
  }
}
