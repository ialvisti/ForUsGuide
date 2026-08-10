variable "project_id" {
  type    = string
  default = "rag-kb-system"
  validation {
    condition     = var.project_id == "rag-kb-system"
    error_message = "Este root está ligado al proyecto canónico rag-kb-system."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "deployment_phase" {
  type    = string
  default = "foundation"
}

variable "image_digest" {
  type    = string
  default = ""
}

variable "publisher_service_account_email" {
  type    = string
  default = ""
}

variable "reviewer_iap_members" {
  type    = set(string)
  default = []
}

variable "allowed_email_domains" {
  type    = set(string)
  default = []
}

variable "devrev_allowed_part_dons" {
  type    = set(string)
  default = []
}

variable "devrev_allowed_ticket_visibility_ids" {
  type    = set(number)
  default = []
}

variable "devrev_allowed_timeline_visibilities" {
  type    = set(string)
  default = []
}

variable "devrev_ai_author_ids" {
  type    = set(string)
  default = []
}

variable "devrev_system_author_ids" {
  type    = set(string)
  default = []
}

variable "devrev_human_author_ids" {
  type    = set(string)
  default = []
}

variable "correlation_lookup_allowed_key_versions" {
  type    = set(number)
  default = []
}

variable "devrev_token_secret_version" {
  type      = string
  default   = ""
  sensitive = true
}

variable "cursor_aead_key_secret_version" {
  type      = string
  default   = ""
  sensitive = true
}

variable "role_bindings_secret_version" {
  type      = string
  default   = ""
  sensitive = true
}

variable "csrf_signing_secret_version" {
  type      = string
  default   = ""
  sensitive = true
}

variable "correlation_keyring_secret_version" {
  type      = string
  default   = ""
  sensitive = true
}
