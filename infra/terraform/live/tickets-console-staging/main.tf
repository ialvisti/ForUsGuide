module "tickets_console" {
  source = "../../modules/tickets_console"

  project_id       = var.project_id
  region           = var.region
  environment      = "staging"
  deployment_phase = var.deployment_phase
  image_digest     = var.image_digest

  publisher_service_account_email = var.publisher_service_account_email
  reviewer_iap_members            = var.reviewer_iap_members
  allowed_email_domains           = var.allowed_email_domains

  devrev_allowed_part_dons                = var.devrev_allowed_part_dons
  devrev_allowed_ticket_visibility_ids    = var.devrev_allowed_ticket_visibility_ids
  devrev_allowed_timeline_visibilities    = var.devrev_allowed_timeline_visibilities
  devrev_ai_author_ids                    = var.devrev_ai_author_ids
  devrev_system_author_ids                = var.devrev_system_author_ids
  devrev_human_author_ids                 = var.devrev_human_author_ids
  correlation_lookup_allowed_key_versions = var.correlation_lookup_allowed_key_versions

  devrev_token_secret_version        = var.devrev_token_secret_version
  cursor_aead_key_secret_version     = var.cursor_aead_key_secret_version
  role_bindings_secret_version       = var.role_bindings_secret_version
  csrf_signing_secret_version        = var.csrf_signing_secret_version
  correlation_keyring_secret_version = var.correlation_keyring_secret_version
}
