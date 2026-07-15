variable "project_id" {
  type    = string
  default = "rag-kb-system"
}

variable "region" {
  type    = string
  default = "us-central1"
}

# El bootstrap privilegiado queda apagado hasta que G1B aprueba el digest del
# release-controller. El digest vive en Terraform, no en substitutions que un
# caller del trigger pueda reemplazar.
variable "cicd_bootstrap" {
  type = object({
    enabled                         = bool
    release_controller_image_digest = string
  })
  default = {
    enabled                         = false
    release_controller_image_digest = ""
  }

  validation {
    condition = (
      !var.cicd_bootstrap.enabled ||
      can(regex("@sha256:[0-9a-f]{64}$", var.cicd_bootstrap.release_controller_image_digest))
    )
    error_message = "G1B exige un release-controller fijado por digest sha256."
  }
}

# El import del trigger existente es una mutación G1B separada y explícita.
# false evita que un validate/test local intente adoptar recursos reales.
variable "enable_legacy_trigger_neutralization" {
  type    = bool
  default = false
}

# G1C siempre es dos fases. disabled no importa ni toca el grant existente;
# prepare importa/conserva el legacy y añade el conditional; enforce retira
# únicamente el legacy tras el gate y smoke documentados.
variable "firestore_scope_migration" {
  type = object({
    enabled       = bool
    phase         = string
    import_legacy = bool
  })
  default = {
    enabled       = false
    phase         = "disabled"
    import_legacy = false
  }

  validation {
    condition = (
      (!var.firestore_scope_migration.enabled && var.firestore_scope_migration.phase == "disabled") ||
      (
        var.firestore_scope_migration.enabled &&
        contains(["prepare", "enforce"], var.firestore_scope_migration.phase) &&
        (var.firestore_scope_migration.phase == "enforce" || var.firestore_scope_migration.import_legacy)
      )
    )
    error_message = "G1C debe estar disabled o en prepare/enforce; prepare exige importar el grant legacy."
  }
}

# WIF apagado por defecto (Tarea 10 Paso 5): el bootstrap G1B puede crear
# pipelines sin el binding si aún falta el ARN de n8n; cuando llegue el
# contrato, un nuevo platform plan→G1B+G3→apply lo habilita.
variable "enable_n8n_wif" {
  type    = bool
  default = false
}

# Cuenta AWS y ARN del execution role de n8n (Tarea 1 Paso 3). Sin estos NO
# se inventa/wildcardea el provider.
variable "n8n_aws_account_id" {
  type    = string
  default = ""
}

variable "n8n_aws_role_arns" {
  type = object({
    staging    = string
    production = string
  })
  default = {
    staging    = ""
    production = ""
  }

  validation {
    condition = alltrue([
      for arn in values(var.n8n_aws_role_arns) :
      arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "cada role de n8n debe ser un ARN IAM exacto, nunca wildcard."
  }
}

variable "producer_audience" {
  type        = string
  default     = ""
  description = "Audiencia del ID token WIF (URL del producer)."
}
