# Migración G1C del único grant Firestore productivo que pertenece al state de
# platform. Ningún recurso equivalente se declara en production.
locals {
  kb_rag_runner_member       = "serviceAccount:kb-rag-runner@${var.project_id}.iam.gserviceaccount.com"
  default_firestore_resource = "projects/${var.project_id}/databases/(default)"
}

# prepare: importado y preservado; enforce: ausente, por lo que el plan retira
# sólo este member no autoritativo y deja intactos otros miembros/roles.
resource "google_project_iam_member" "kb_rag_runner_firestore_legacy" {
  for_each = (
    var.firestore_scope_migration.enabled &&
    var.firestore_scope_migration.phase == "prepare"
  ) ? { legacy = true } : {}

  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.kb_rag_runner_member
}

# prepare y enforce conservan el acceso a (default) mediante el patrón oficial
# de Firestore: IAM Condition sobre el nombre exacto de la database.
resource "google_project_iam_member" "kb_rag_runner_firestore_scoped" {
  for_each = var.firestore_scope_migration.enabled ? { scoped = true } : {}

  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.kb_rag_runner_member

  condition {
    title       = "kb_rag_runner_default_database"
    description = "G1C: kb-rag-runner sólo puede acceder a Firestore (default)."
    expression  = "resource.name == \"${local.default_firestore_resource}\""
  }
}
