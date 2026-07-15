# Firestore: base NOMBRADA + TTL + índices (plan Tarea 10 Paso 6).
# La base — no un prefijo — es el límite de aislamiento IAM. Producción usa
# (default) (importada); staging crea ticket-staging.

resource "google_firestore_database" "ticket" {
  count       = var.firestore_database == "(default)" ? 0 : 1
  project     = var.project_id
  name        = var.firestore_database
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # nunca borrar la base con el destroy del módulo
  deletion_policy = "DELETE_PROTECTION_ENABLED"
}

locals {
  db_name = var.firestore_database
}

# TTL: payloads a 24h (fail-safe de privacidad); controles terminales +
# receipts al horizonte de retención; rate windows. NO sobre controles no
# terminales ni contadores activos.
resource "google_firestore_field" "payload_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_job_payloads"
  field      = "expires_at"
  ttl_config {}
}

resource "google_firestore_field" "control_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_jobs"
  field      = "expires_at"
  ttl_config {}
}

resource "google_firestore_field" "receipt_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_idempotency_receipts"
  field      = "expires_at"
  ttl_config {}
}

resource "google_firestore_field" "rate_window_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_rate_windows"
  field      = "expires_at"
  ttl_config {}
}

resource "google_firestore_field" "ticket_execution_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_executions"
  field      = "expires_at"
  ttl_config {}
}

# Índices compuestos requeridos por las consultas reales.
resource "google_firestore_index" "jobs_principal_state" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_jobs"

  fields {
    field_path = "principal_id"
    order      = "ASCENDING"
  }
  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
}

resource "google_firestore_index" "jobs_state_lease" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_jobs"

  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "lease_expires_at"
    order      = "ASCENDING"
  }
}

resource "google_firestore_index" "jobs_outbox" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_jobs"

  fields {
    field_path = "enqueue_state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "created_at"
    order      = "ASCENDING"
  }
}
