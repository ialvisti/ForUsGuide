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

# Cloud Build no ofrece IAM por trigger. El apply production combina aprobación
# manual, controller/root fijados y actAs limitado a este grupo de release.
# Sin owner contractual no se habilita el bootstrap privilegiado.
variable "production_release_group_email" {
  type        = string
  default     = ""
  description = "Email exacto del Google Group autorizado a aprobar/usar production apply."

  validation {
    condition = (
      !var.cicd_bootstrap.enabled ||
      can(regex("^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", var.production_release_group_email))
    )
    error_message = "G1B exige el email exacto del release group para production apply."
  }
}

# Cloud Build registra un solo approver por build. Cada rol requerido obtiene
# por ello un trigger/receipt independiente con una allowlist literal fijada
# en la configuración trusted del trigger. Los mismos owners pueden reaparecer
# en gates distintos, pero dentro de un quorum/phase deben ser disjuntos.
variable "gate_approver_accounts" {
  type        = map(set(string))
  default     = {}
  description = "Emails humanos exactos permitidos para cada receipt gate/rol."

  validation {
    condition = !var.cicd_bootstrap.enabled || (
      toset(keys(var.gate_approver_accounts)) == toset([
        "g1b-gcp-owner",
        "g1b-release-owner",
        "g2-gcp-owner",
        "g6b-gcp-owner",
        "g6b-release-owner",
        "g6b-forusbots-owner",
        "g1c-prepare-gcp-owner",
        "g1c-prepare-api-owner",
        "g1c-prepare-operations",
        "g1c-enforce-gcp-owner",
        "g1c-enforce-api-owner",
        "g1c-enforce-operations",
        "g4-requester",
        "g4-n8n-owner",
        "g4-participant-plan-owner",
        "g4-forusbots-owner",
        "g4-delivery-owner",
        "g5-maintainer",
        "g5-requester",
        "g5v-security-owner",
        "g5v-release-owner",
        "g5v-requester",
      ]) &&
      alltrue([
        for key in [
          "g1b-gcp-owner",
          "g1b-release-owner",
          "g2-gcp-owner",
          "g6b-gcp-owner",
          "g6b-release-owner",
          "g6b-forusbots-owner",
          "g1c-prepare-gcp-owner",
          "g1c-prepare-api-owner",
          "g1c-prepare-operations",
          "g1c-enforce-gcp-owner",
          "g1c-enforce-api-owner",
          "g1c-enforce-operations",
          "g4-requester",
          "g4-n8n-owner",
          "g4-participant-plan-owner",
          "g4-forusbots-owner",
          "g4-delivery-owner",
          "g5-maintainer",
          "g5-requester",
          "g5v-security-owner",
          "g5v-release-owner",
          "g5v-requester",
        ] : try(length(var.gate_approver_accounts[key]) > 0, false)
      ]) &&
      alltrue([
        for account in flatten([
          for accounts in values(var.gate_approver_accounts) : tolist(accounts)
        ]) :
        account == lower(trimspace(account)) &&
        !endswith(account, ".gserviceaccount.com") &&
        can(regex("^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\\.[a-z]{2,}$", account))
      ]) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g1b-gcp-owner"],
          var.gate_approver_accounts["g1b-release-owner"],
          )) == (
          length(var.gate_approver_accounts["g1b-gcp-owner"]) +
          length(var.gate_approver_accounts["g1b-release-owner"])
        ), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g6b-gcp-owner"],
          var.gate_approver_accounts["g6b-release-owner"],
          var.gate_approver_accounts["g6b-forusbots-owner"],
          )) == sum([
          length(var.gate_approver_accounts["g6b-gcp-owner"]),
          length(var.gate_approver_accounts["g6b-release-owner"]),
          length(var.gate_approver_accounts["g6b-forusbots-owner"]),
        ]), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g1c-prepare-gcp-owner"],
          var.gate_approver_accounts["g1c-prepare-api-owner"],
          var.gate_approver_accounts["g1c-prepare-operations"],
          )) == sum([
          length(var.gate_approver_accounts["g1c-prepare-gcp-owner"]),
          length(var.gate_approver_accounts["g1c-prepare-api-owner"]),
          length(var.gate_approver_accounts["g1c-prepare-operations"]),
        ]), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g1c-enforce-gcp-owner"],
          var.gate_approver_accounts["g1c-enforce-api-owner"],
          var.gate_approver_accounts["g1c-enforce-operations"],
          )) == sum([
          length(var.gate_approver_accounts["g1c-enforce-gcp-owner"]),
          length(var.gate_approver_accounts["g1c-enforce-api-owner"]),
          length(var.gate_approver_accounts["g1c-enforce-operations"]),
        ]), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g4-requester"],
          var.gate_approver_accounts["g4-n8n-owner"],
          var.gate_approver_accounts["g4-participant-plan-owner"],
          var.gate_approver_accounts["g4-forusbots-owner"],
          var.gate_approver_accounts["g4-delivery-owner"],
          )) == sum([
          length(var.gate_approver_accounts["g4-requester"]),
          length(var.gate_approver_accounts["g4-n8n-owner"]),
          length(var.gate_approver_accounts["g4-participant-plan-owner"]),
          length(var.gate_approver_accounts["g4-forusbots-owner"]),
          length(var.gate_approver_accounts["g4-delivery-owner"]),
        ]), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g5-maintainer"],
          var.gate_approver_accounts["g5-requester"],
          )) == (
          length(var.gate_approver_accounts["g5-maintainer"]) +
          length(var.gate_approver_accounts["g5-requester"])
        ), false
      ) &&
      try(
        length(setunion(
          var.gate_approver_accounts["g5v-security-owner"],
          var.gate_approver_accounts["g5v-release-owner"],
          var.gate_approver_accounts["g5v-requester"],
          )) == sum([
          length(var.gate_approver_accounts["g5v-security-owner"]),
          length(var.gate_approver_accounts["g5v-release-owner"]),
          length(var.gate_approver_accounts["g5v-requester"]),
        ]), false
      )
    )
    error_message = "G1B/G2/G1C/G4/G5/G5V/G6B exigen allowlists humanas exactas, no vacías y disjuntas dentro de cada quorum."
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

# Inventario aprobado de containers que cada environment root puede gestionar.
# Son IDs (no versiones/payloads) y permiten condicionar el custom role exacto.
variable "environment_secret_ids" {
  type = object({
    staging    = set(string)
    production = set(string)
  })
  default = {
    staging    = []
    production = []
  }

  validation {
    condition = (
      alltrue(flatten([
        for ids in values(var.environment_secret_ids) : [
          for secret_id in ids : can(regex("^[A-Za-z0-9_-]{1,255}$", secret_id))
        ]
      ])) &&
      length(setintersection(
        var.environment_secret_ids.staging,
        var.environment_secret_ids.production,
      )) == 0 &&
      alltrue([
        for environment, ids in var.environment_secret_ids :
        var.environment_container_phase[environment] == "managed" || length(ids) == 0
      ])
    )
    error_message = "Los IDs Secret Manager deben ser válidos, disjuntos y vacíos hasta que su container phase sea managed."
  }
}

# Ownership único en platform, activado únicamente por el gate combinado del
# entorno. G1B por sí solo conserva ambos en disabled y crea cero containers.
variable "environment_container_phase" {
  type = object({
    staging    = string
    production = string
  })
  default = {
    staging    = "disabled"
    production = "disabled"
  }
  validation {
    condition = (
      alltrue([
        for phase in values(var.environment_container_phase) :
        contains(["disabled", "managed"], phase)
      ]) &&
      (
        var.cicd_bootstrap.enabled ||
        alltrue([
          for phase in values(var.environment_container_phase) : phase == "disabled"
        ])
      )
    )
    error_message = "environment_container_phase sólo admite disabled/managed y managed exige G1B/cicd_bootstrap habilitado."
  }
}

# Subconjunto de containers ya existentes que el primer platform apply debe
# importar (por ejemplo producción). Los demás IDs se crean una sola vez por
# el bootstrap privilegiado; ningún environment apply crea secretos.
variable "existing_environment_secret_ids" {
  type = object({
    staging    = set(string)
    production = set(string)
  })
  default = {
    staging    = []
    production = []
  }
  validation {
    condition = alltrue(flatten([
      for environment, secret_ids in var.existing_environment_secret_ids : [
        for secret_id in secret_ids :
        can(regex("^[A-Za-z0-9_-]{1,255}$", secret_id)) &&
        contains(var.environment_secret_ids[environment], secret_id)
      ]
    ]))
    error_message = "existing_environment_secret_ids debe ser un subconjunto válido del inventario de su mismo environment."
  }
}

# Handoff bifásico de Cloud Run. bootstrap concede únicamente create sobre el
# parent y conserva Developer sólo en recursos ya inventariados; managed
# revoca create. El inventario aumenta después de cada creación atestada.
variable "environment_handoff_phase" {
  type = object({
    staging    = string
    production = string
  })
  default = {
    staging    = "disabled"
    production = "disabled"
  }
  validation {
    condition = (
      alltrue([
        for phase in values(var.environment_handoff_phase) :
        contains(["disabled", "bootstrap", "managed"], phase)
      ]) &&
      alltrue([
        for environment, phase in var.environment_handoff_phase :
        phase == "disabled" || var.environment_container_phase[environment] == "managed"
      ])
    )
    error_message = "environment_handoff_phase sólo admite disabled/bootstrap/managed y exige containers managed."
  }
}

variable "environment_run_resources" {
  type = object({
    staging    = set(string)
    production = set(string)
  })
  default = {
    staging    = []
    production = []
  }
  validation {
    condition = (
      length(setsubtract(var.environment_run_resources.staging, toset([
        "services/kb-rag-system-staging",
        "services/kb-rag-ticket-worker-staging",
        "jobs/ticket-reconciler-staging",
        "jobs/ticket-e2e-staging",
      ]))) == 0 &&
      length(setsubtract(var.environment_run_resources.production, toset([
        "services/kb-rag-system",
        "services/kb-rag-ticket-worker",
        "jobs/ticket-reconciler-prod",
      ]))) == 0 &&
      alltrue([
        for environment, resources in var.environment_run_resources :
        (
          var.environment_handoff_phase[environment] != "disabled" ||
          length(resources) == 0
        )
      ])
    )
    error_message = "environment_run_resources debe estar en el inventario cerrado y vacío durante handoff disabled."
  }
}

# Fase observada/atestada del root de cada environment. Platform usa este dato
# únicamente para mantener el scheduler del reconciliador fail-closed: infra y
# dark siempre quedan pausados; una fase activa además exige container, handoff
# e inventario Run materializados. El release-controller liga el mapa al plan.
variable "environment_release_phase" {
  type = object({
    staging    = string
    production = string
  })
  default = {
    staging    = "disabled"
    production = "disabled"
  }

  validation {
    condition = (
      alltrue([
        for phase in values(var.environment_release_phase) :
        contains([
          "disabled", "infra_only", "dark_no_traffic", "dark_100",
          "shadow", "knowledge_only", "full",
        ], phase)
      ]) &&
      alltrue([
        for environment, phase in var.environment_release_phase :
        phase == "disabled" || (
          var.environment_container_phase[environment] == "managed" &&
          var.environment_handoff_phase[environment] != "disabled"
        )
      ]) &&
      alltrue([
        for environment, phase in var.environment_release_phase :
        !contains(["shadow", "knowledge_only", "full"], phase) || contains(
          var.environment_run_resources[environment],
          environment == "staging" ?
          "jobs/ticket-reconciler-staging" :
          "jobs/ticket-reconciler-prod",
        )
      ])
    )
    error_message = "environment_release_phase debe ser cerrada, usar containers/handoff administrados y una fase activa exige reconciler inventariado."
  }
}
