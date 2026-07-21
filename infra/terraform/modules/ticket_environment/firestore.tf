# Firestore TTL + índices (plan Tarea 10 Paso 6). Las databases se
# crean/importan/protegen en platform para que environment apply no necesite
# datastore.databases.create sobre el parent project.

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

resource "google_firestore_field" "core_execution_ttl" {
  project    = var.project_id
  database   = local.db_name
  collection = "execution_logs"
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

# active_job_stats() filtra queued/running mediante IN y obtiene tanto el
# count agregado como el job más antiguo ordenado por created_at.
resource "google_firestore_index" "jobs_state_created_at" {
  project    = var.project_id
  database   = local.db_name
  collection = "ticket_jobs"

  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "created_at"
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
