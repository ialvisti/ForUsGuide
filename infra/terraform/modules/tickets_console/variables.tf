variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id debe ser un ID de proyecto GCP válido."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "region debe ser una región GCP canónica."
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment debe ser staging o production."
  }
}

variable "deployment_phase" {
  type    = string
  default = "foundation"
  validation {
    condition     = contains(["foundation", "workload"], var.deployment_phase)
    error_message = "deployment_phase debe ser foundation o workload."
  }
}

variable "image_digest" {
  type    = string
  default = ""
  validation {
    condition     = var.image_digest == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest debe estar vacío en foundation o fijarse por digest sha256."
  }
}

variable "publisher_service_account_email" {
  type    = string
  default = ""
  validation {
    condition = (
      var.publisher_service_account_email == "" ||
      can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.iam\\.gserviceaccount\\.com$", var.publisher_service_account_email))
    )
    error_message = "publisher_service_account_email debe ser una service account exacta."
  }
}

variable "reviewer_iap_members" {
  type    = set(string)
  default = []
  validation {
    condition = alltrue([
      for member in var.reviewer_iap_members :
      can(regex("^(user|group):[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", member))
    ])
    error_message = "reviewer_iap_members sólo admite users/groups explícitos; no públicos ni wildcards."
  }
}

variable "allowed_email_domains" {
  type    = set(string)
  default = []
  validation {
    condition = alltrue([
      for domain in var.allowed_email_domains :
      can(regex("^[A-Za-z0-9][A-Za-z0-9.-]*\\.[A-Za-z]{2,}$", domain)) &&
      !strcontains(domain, "*")
    ])
    error_message = "allowed_email_domains exige dominios explícitos con punto y sin wildcard."
  }
}

variable "devrev_allowed_part_dons" {
  type    = set(string)
  default = []
  validation {
    condition = alltrue([
      for value in var.devrev_allowed_part_dons :
      can(regex("^don:[A-Za-z0-9_.:/+-]+$", value)) && length(value) <= 512
    ])
    error_message = "devrev_allowed_part_dons sólo admite DONs exactos."
  }
}

variable "devrev_allowed_ticket_visibility_ids" {
  type    = set(number)
  default = []
  validation {
    condition = alltrue([
      for value in var.devrev_allowed_ticket_visibility_ids :
      floor(value) == value && value >= 0
    ])
    error_message = "las visibility IDs deben ser enteros no negativos."
  }
}

variable "devrev_allowed_timeline_visibilities" {
  type    = set(string)
  default = []
  validation {
    condition = length(setsubtract(
      var.devrev_allowed_timeline_visibilities,
      toset(["private", "internal", "external", "public"]),
    )) == 0
    error_message = "las timeline visibilities deben pertenecer al enum cerrado de DevRev."
  }
}

variable "devrev_ai_author_ids" {
  type    = set(string)
  default = []
  validation {
    condition = length(var.devrev_ai_author_ids) <= 100 && alltrue([
      for value in var.devrev_ai_author_ids :
      can(regex("^(don:[A-Za-z0-9_.:/+-]+|[A-Za-z0-9_.:/+-]{3,256})$", value))
    ])
    error_message = "DevRev DONs or opaque identity IDs are required for at most 100 AI authors."
  }
}

variable "devrev_system_author_ids" {
  type    = set(string)
  default = []
  validation {
    condition = length(var.devrev_system_author_ids) <= 100 && alltrue([
      for value in var.devrev_system_author_ids :
      can(regex("^(don:[A-Za-z0-9_.:/+-]+|[A-Za-z0-9_.:/+-]{3,256})$", value))
    ])
    error_message = "DevRev DONs or opaque identity IDs are required for at most 100 system authors."
  }
}

variable "devrev_human_author_ids" {
  type    = set(string)
  default = []
  validation {
    condition = length(var.devrev_human_author_ids) <= 100 && alltrue([
      for value in var.devrev_human_author_ids :
      can(regex("^(don:[A-Za-z0-9_.:/+-]+|[A-Za-z0-9_.:/+-]{3,256})$", value))
    ])
    error_message = "DevRev DONs or opaque identity IDs are required for at most 100 human authors."
  }
}

variable "correlation_lookup_allowed_key_versions" {
  type    = set(number)
  default = []
  validation {
    condition = alltrue([
      for value in var.correlation_lookup_allowed_key_versions :
      floor(value) == value && value >= 1
    ])
    error_message = "las versiones de correlation lookup deben ser enteros positivos."
  }
}

variable "devrev_token_secret_version" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition = (
      var.devrev_token_secret_version == "" ||
      can(regex("^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$", var.devrev_token_secret_version))
    )
    error_message = "devrev_token_secret_version debe ser una versión numérica completa."
  }
}

variable "cursor_aead_key_secret_version" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition = (
      var.cursor_aead_key_secret_version == "" ||
      can(regex("^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$", var.cursor_aead_key_secret_version))
    )
    error_message = "cursor_aead_key_secret_version debe ser una versión numérica completa."
  }
}

variable "role_bindings_secret_version" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition = (
      var.role_bindings_secret_version == "" ||
      can(regex("^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$", var.role_bindings_secret_version))
    )
    error_message = "role_bindings_secret_version debe ser una versión numérica completa."
  }
}

variable "csrf_signing_secret_version" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition = (
      var.csrf_signing_secret_version == "" ||
      can(regex("^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$", var.csrf_signing_secret_version))
    )
    error_message = "csrf_signing_secret_version debe ser una versión numérica completa."
  }
}

variable "correlation_keyring_secret_version" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition = (
      var.correlation_keyring_secret_version == "" ||
      can(regex("^projects/[^/]+/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]+$", var.correlation_keyring_secret_version))
    )
    error_message = "correlation_keyring_secret_version debe ser una versión numérica completa."
  }
}

variable "console_max_instances" {
  type    = number
  default = 3
  validation {
    condition     = floor(var.console_max_instances) == var.console_max_instances && var.console_max_instances >= 1 && var.console_max_instances <= 20
    error_message = "console_max_instances debe ser un entero entre 1 y 20."
  }
}

variable "ingest_max_instances" {
  type    = number
  default = 3
  validation {
    condition     = floor(var.ingest_max_instances) == var.ingest_max_instances && var.ingest_max_instances >= 1 && var.ingest_max_instances <= 20
    error_message = "ingest_max_instances debe ser un entero entre 1 y 20."
  }
}

variable "broker_max_instances" {
  type    = number
  default = 3
  validation {
    condition     = floor(var.broker_max_instances) == var.broker_max_instances && var.broker_max_instances >= 1 && var.broker_max_instances <= 20
    error_message = "broker_max_instances debe ser un entero entre 1 y 20."
  }
}
