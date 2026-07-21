variable "project_id" {
  type    = string
  default = "rag-kb-system"

  validation {
    condition     = var.project_id == "rag-kb-system"
    error_message = "This root is bound to the canonical project rag-kb-system and its imported resources/state."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "runtime_service_accounts" {
  type        = map(string)
  description = "Outputs platform firmados e inyectados por el release-controller; nunca remote state."

  validation {
    condition = (
      length(setsubtract(toset([
        "ticket-producer-stg",
        "ticket-worker-stg",
        "ticket-reconciler-stg",
        "ticket-task-signer-stg",
        "ticket-scheduler-stg",
        "n8n-ticket-invoker-stg",
      ]), toset(keys(var.runtime_service_accounts)))) == 0 &&
      alltrue([
        for email in values(var.runtime_service_accounts) :
        can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.iam\\.gserviceaccount\\.com$", email))
      ])
    )
    error_message = "runtime_service_accounts debe incluir las seis SAs staging como emails GCP válidos."
  }
}

# Inyectados por el trigger *-plan (Tarea 12): digest probado, fase de
# release y URI del manifest de secret versions numéricas.
variable "image_digest" {
  type    = string
  default = ""

  validation {
    condition     = var.image_digest == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest staging debe estar vacío en infra_only o usar @sha256 inmutable."
  }
}

variable "release_phase" {
  type    = string
  default = "infra_only"

  validation {
    condition = contains([
      "infra_only", "dark_no_traffic", "dark_100", "shadow",
      "knowledge_only", "full",
    ], var.release_phase)
    error_message = "release_phase staging está fuera del conjunto cerrado."
  }
}

variable "shadow_sample_rate" {
  type    = number
  default = 0
}

variable "producer_baseline_revision" {
  type    = string
  default = ""
  validation {
    condition = (
      var.producer_baseline_revision == "" ||
      can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.producer_baseline_revision))
    )
    error_message = "producer_baseline_revision debe ser una revisión Cloud Run inmutable."
  }
}

variable "producer_baseline_tag" {
  type    = string
  default = "baseline"
}

variable "producer_candidate_tag" {
  type    = string
  default = "candidate"
}

variable "secret_version_refs" {
  type    = map(string)
  default = {}
}

variable "producer_core_env" {
  type    = map(string)
  default = {}
}

variable "ticket_wif_audience" {
  type    = string
  default = ""
}

variable "ticket_wif_expected_email" {
  type    = string
  default = ""
}

variable "ticket_wif_allowed_emails" {
  type    = list(string)
  default = []
}

variable "secret_containers" {
  type = object({
    enabled        = bool
    ids            = map(string)
    accessor_roles = map(set(string))
  })
  default = {
    enabled        = false
    ids            = {}
    accessor_roles = {}
  }
}

variable "e2e_job" {
  type = object({
    enabled               = bool
    image_digest          = string
    service_account_email = string
    nonsecret_env         = map(string)
    secret_version_refs   = map(string)
  })
  default = {
    enabled               = false
    image_digest          = ""
    service_account_email = ""
    nonsecret_env         = {}
    secret_version_refs   = {}
  }
}

variable "e2e_secret_containers" {
  type    = map(string)
  default = {}
}

variable "notification_channels" {
  type    = list(string)
  default = []
}
