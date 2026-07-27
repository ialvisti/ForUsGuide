# IAM least-privilege con rutas excluyentes (plan Tarea 10 Paso 5).
# Custom role queue-scoped: sólo create/get task + get queue; NUNCA
# list/delete/run/pause/purge/update ni roles/cloudtasks.viewer a nivel
# proyecto.

# Dependencias core por rol. Vertex AI sólo lo usan producer/worker; el
# reconciliador permanece sin permisos de proveedor. El bucket core se concede
# a nivel de bucket al producer dedicado de cada entorno.
locals {
  runtime_uses_vertex_ai = lower(trimspace(
    lookup(var.producer_core_env, "USE_VERTEX_AI", "false")
  )) == "true"
  producer_core_bucket = trimspace(
    lookup(var.producer_core_env, "GCS_BUCKET", "")
  )
}

resource "google_storage_bucket_iam_member" "producer_core_objects" {
  count  = local.create_services && local.producer_core_bucket != "" ? 1 : 0
  bucket = local.producer_core_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.producer_sa_email}"

  lifecycle {
    precondition {
      condition = can(regex(
        "^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$",
        local.producer_core_bucket,
      ))
      error_message = "un producer activo exige GCS_BUCKET válido para sus rutas core."
    }
  }
}
