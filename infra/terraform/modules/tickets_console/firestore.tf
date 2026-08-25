locals {
  # File interchange was removed.  Only caches and idempotency receipts have
  # native TTL; immutable evaluation runs and review/audit ledgers use their
  # explicit retention workflow and never a CSV staging collection.
  console_ttl_collections = toset([
    "devrev_message_cache",
    "idempotency_keys",
    "ticket_console_cache",
  ])

  review_index_facets = toset([
    "assigned_reviewer.email",
    "observation_type",
    "rating",
    "remediation_target",
    "severity",
    "topic",
  ])
}

resource "google_firestore_database" "console" {
  project     = var.project_id
  name        = local.console_database
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"
}

resource "google_firestore_field" "console_ttl" {
  for_each   = local.console_ttl_collections
  project    = var.project_id
  database   = google_firestore_database.console.name
  collection = each.value
  field      = "expires_at"
  ttl_config {}
}

# Queue query: status + stable updated-at/review-id ordering.
resource "google_firestore_index" "review_queue" {
  project     = var.project_id
  database    = google_firestore_database.console.name
  collection  = "ticket_reviews"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "updated_at"
    order      = "DESCENDING"
  }
  fields {
    field_path = "review_id"
    order      = "ASCENDING"
  }

  deletion_policy = "PREVENT"
}

# The reviewer list must filter successful hydration in Firestore before
# pagination.  This includes legacy authorized rows written before the
# authorization_status field existed, while the application still validates
# the authorization invariant.  updated_at is the moment the row became ready.
resource "google_firestore_index" "ticket_evaluations_hydrated_updated_at" {
  project     = var.project_id
  database    = google_firestore_database.console.name
  collection  = "ticket_evaluation_runs"
  query_scope = "COLLECTION"

  fields {
    field_path = "hydration_status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "updated_at"
    order      = "DESCENDING"
  }
  fields {
    field_path = "__name__"
    order      = "DESCENDING"
  }

  deletion_policy = "PREVENT"
}

# Exactly one approved facet may accompany status.  No Cartesian product of
# user-controlled filters is provisioned.
resource "google_firestore_index" "review_facet" {
  for_each    = local.review_index_facets
  project     = var.project_id
  database    = google_firestore_database.console.name
  collection  = "ticket_reviews"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = each.value
    order      = "ASCENDING"
  }
  fields {
    field_path = "updated_at"
    order      = "DESCENDING"
  }
  fields {
    field_path = "review_id"
    order      = "ASCENDING"
  }

  deletion_policy = "PREVENT"
}
