# Triggers de Cloud Build gestionados por Terraform (plan Tarea 12). El
# trigger de `main` construye UNA sola vez el artefacto canónico y NO
# despliega; los YAML/scripts privilegiados del repo son fuente auditable para
# construir el controller durante G1B (un cambio posterior no altera triggers
# automáticamente).

# SAs distintas por pipeline (nunca la Compute default).
resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "ticket-ci"
  display_name = "Ticket CI (sin Run/IAM Admin ni state prod)"
}

resource "google_service_account" "plan_platform" {
  project      = var.project_id
  account_id   = "ticket-plan-platform"
  display_name = "Ticket platform plan"
}

resource "google_service_account" "apply_platform" {
  project      = var.project_id
  account_id   = "ticket-apply-platform"
  display_name = "Ticket platform apply"
}

resource "google_service_account" "plan_staging" {
  project      = var.project_id
  account_id   = "ticket-plan-staging"
  display_name = "Ticket staging plan"
}

resource "google_service_account" "apply_staging" {
  project      = var.project_id
  account_id   = "ticket-apply-staging"
  display_name = "Ticket staging apply"
}

resource "google_service_account" "plan_production" {
  project      = var.project_id
  account_id   = "ticket-plan-production"
  display_name = "Ticket production plan"
}

resource "google_service_account" "apply_production" {
  project      = var.project_id
  account_id   = "ticket-apply-production"
  display_name = "Ticket production apply (sin state staging)"
}

# Trigger de CI de rama/PR: sólo la config del repo con SA no privilegiada.
resource "google_cloudbuild_trigger" "ci" {
  project         = var.project_id
  name            = "handle-ticket-ci"
  location        = "global"
  service_account = google_service_account.ci.id
  filename        = "kb-rag-system/cloudbuild.yaml"

  github {
    owner = "ialvist"
    name  = "ForUsGuide"
    push {
      branch = "^handle-ticket-production-finalization$"
    }
  }
}

# Trigger de `main`: construye UNA vez el canónico y publica su digest; NO
# despliega. ignored_files EXACTO: sólo evidencia docs-only no dispara build.
resource "google_cloudbuild_trigger" "main_canonical" {
  project         = var.project_id
  name            = "handle-ticket-main-canonical"
  location        = "global"
  service_account = google_service_account.ci.id
  filename        = "kb-rag-system/cloudbuild.yaml"

  ignored_files = [
    "docs/verification/**",
    "kb-rag-system/Development Docs/**",
    "**/README.md",
  ]

  github {
    owner = "ialvist"
    name  = "ForUsGuide"
    push {
      branch = "^main$"
    }
  }
}

# El trigger legacy deploy-kb-rag-system se NEUTRALIZA en G1B (Tarea 12
# Paso 3): este apply cambia `main` a CI-sin-deploy. El recurso legacy se
# importa y se reemplaza por main_canonical; no se deja un trigger que
# despliegue directo a producción.

# Los triggers privilegiados (*-plan/*-apply/staging-attest/evidence-manifest/
# test-only/e2e-image) usan build config INLINE o una release-controller image
# revisada/fijada por digest, NUNCA el filename del candidate SHA. Se declaran
# aquí con su SA dedicada; el cuerpo inline se materializa durante el bootstrap
# G1B desde los YAML auditables del repo (kb-rag-system/cloudbuild.*.yaml).
