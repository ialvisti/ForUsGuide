# Platform: APIs, Artifact Registry, bucket de evidencia, SAs compartidas y
# service accounts de runtime de ambos entornos (plan Tarea 10 Paso 3). El
# apply platform ocurre SÓLO con G1B y antes del merge.

locals {
  apis = [
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "firestore.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudscheduler.googleapis.com",
    "containeranalysis.googleapis.com",
    "containerscanning.googleapis.com",
    "ondemandscanning.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false # APIs compartidas: no desactivar al destruir
}

# Repositorio existente importado por G1B. Los tres artefactos conservan image
# names distintos y toda promoción está ligada al digest, nunca a un tag.
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "kb-rag"
  description   = "Runtime, E2E y release-controller de kb-rag-system"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.enabled["artifactregistry.googleapis.com"]]
}

# Bucket de evidencia versionado (reemplaza la referencia inexistente a
# gs://rag-kb-system-build-artifacts).
resource "google_storage_bucket" "evidence" {
  project                     = var.project_id
  name                        = "rag-kb-system-ticket-evidence-900340137010"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
}

# Service accounts de runtime (staging y producción). kb-rag-runner permanece
# intacta para la revisión rollback legacy; la candidata usa una SA dedicada.
locals {
  runtime_sas = {
    "ticket-worker-stg"       = "Worker runtime (staging)"
    "ticket-reconciler-stg"   = "Reconciler runtime (staging)"
    "ticket-task-signer-stg"  = "Task signer (staging)"
    "ticket-scheduler-stg"    = "Scheduler (staging)"
    "ticket-producer-stg"     = "Producer runtime (staging)"
    "n8n-ticket-invoker-stg"  = "n8n WIF invoker (staging)"
    "ticket-e2e-stg"          = "E2E runner (staging)"
    "ticket-producer-prod"    = "Producer runtime (production)"
    "ticket-worker-prod"      = "Worker runtime (production)"
    "ticket-reconciler-prod"  = "Reconciler runtime (production)"
    "ticket-task-signer-prod" = "Task signer (production)"
    "ticket-scheduler-prod"   = "Scheduler (production)"
    "n8n-ticket-invoker-prod" = "n8n WIF invoker (production)"
  }
}

resource "google_service_account" "runtime" {
  for_each     = local.runtime_sas
  project      = var.project_id
  account_id   = each.key
  display_name = each.value
}
