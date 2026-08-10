# Isolated RAG ticket-evaluation platform.  This module deliberately owns a
# named Firestore database and dedicated runtime identities; it never reuses
# the RAG handler's `(default)` database or service accounts.

terraform {
  required_version = ">= 1.9.8, < 1.10.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.41.0, < 8.0.0"
    }
  }
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  workload_enabled = var.deployment_phase == "workload"

  console_database = var.environment == "production" ? "tickets-console-prod" : "tickets-console-staging"
  suffix           = var.environment == "production" ? "prod" : "stg"

  console_service_name = "rag-tickets-console-${local.suffix}"
  ingest_service_name  = "ticket-evaluation-ingest-${local.suffix}"
  broker_service_name  = "tickets-evidence-broker-${local.suffix}"

  expected_publisher_service_account = "ticket-reconciler-${local.suffix}@${var.project_id}.iam.gserviceaccount.com"

  ingest_audience = "https://${local.ingest_service_name}.${var.project_id}.tickets.internal"
  broker_audience = "https://${local.broker_service_name}.${var.project_id}.tickets.internal"
  iap_audience    = "/projects/${data.google_project.current.number}/locations/${var.region}/services/${local.console_service_name}"

  # Cloud Run's deterministic URL is known before create and stays stable.
  # Publishing this exact origin avoids a bootstrap placeholder and binds CSRF
  # to the same single URL reviewers receive.
  console_origin = "https://${local.console_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  ingest_origin  = "https://${local.ingest_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  broker_origin  = "https://${local.broker_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  console_url    = "${local.console_origin}/tickets"

  numeric_secret_version_pattern = "^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$"
  secret_version_refs = {
    DEVREV_TOKEN        = var.devrev_token_secret_version
    CURSOR_AEAD_KEY     = var.cursor_aead_key_secret_version
    ROLE_BINDINGS       = var.role_bindings_secret_version
    CSRF_SIGNING_SECRET = var.csrf_signing_secret_version
    CORRELATION_KEYRING = var.correlation_keyring_secret_version
  }
  secret_refs_are_numeric = alltrue([
    for ref in values(local.secret_version_refs) :
    can(regex(local.numeric_secret_version_pattern, ref)) &&
    startswith(ref, "projects/${var.project_id}/secrets/")
  ])
  secret_containers_are_distinct = length(toset([
    for ref in values(local.secret_version_refs) :
    try(split("/versions/", ref)[0], "")
  ])) == length(local.secret_version_refs)
  author_id_sets_are_disjoint = length(setunion(
    var.devrev_ai_author_ids,
    var.devrev_system_author_ids,
    var.devrev_human_author_ids,
    )) == (
    length(var.devrev_ai_author_ids) +
    length(var.devrev_system_author_ids) +
    length(var.devrev_human_author_ids)
  )

  workload_configuration_complete = (
    can(regex("@sha256:[0-9a-f]{64}$", var.image_digest)) &&
    var.publisher_service_account_email == local.expected_publisher_service_account &&
    length("${local.console_service_name}-${data.google_project.current.number}") <= 63 &&
    length("${local.ingest_service_name}-${data.google_project.current.number}") <= 63 &&
    length("${local.broker_service_name}-${data.google_project.current.number}") <= 63 &&
    length(var.reviewer_iap_members) > 0 &&
    length(var.allowed_email_domains) > 0 &&
    length(var.devrev_allowed_part_dons) > 0 &&
    length(var.devrev_allowed_ticket_visibility_ids) > 0 &&
    length(var.devrev_allowed_timeline_visibilities) > 0 &&
    length(var.devrev_ai_author_ids) > 0 &&
    length(var.devrev_system_author_ids) > 0 &&
    length(var.devrev_human_author_ids) > 0 &&
    local.author_id_sets_are_disjoint &&
    length(var.correlation_lookup_allowed_key_versions) > 0 &&
    local.secret_refs_are_numeric &&
    local.secret_containers_are_distinct
  )

  secret_parts = {
    for name, ref in local.secret_version_refs : name => {
      secret  = try(split("/versions/", ref)[0], "")
      version = try(split("/versions/", ref)[1], "")
    }
  }
}

resource "google_service_account" "console" {
  project      = var.project_id
  account_id   = "tickets-console-${local.suffix}"
  display_name = "RAG ticket evaluation console (${var.environment})"
}

resource "google_service_account" "ingest" {
  project      = var.project_id
  account_id   = "tickets-eval-ingest-${local.suffix}"
  display_name = "RAG ticket evaluation ingest (${var.environment})"
}

resource "google_service_account" "evidence_broker" {
  project      = var.project_id
  account_id   = "tickets-evidence-${local.suffix}"
  display_name = "Ticket evidence broker (${var.environment})"
}

check "workload_configuration" {
  assert {
    condition     = !local.workload_enabled || local.workload_configuration_complete
    error_message = "workload exige digest, project number, publisher, origen real, IAP/DevRev allowlists y secret versions numéricas completos."
  }
}
