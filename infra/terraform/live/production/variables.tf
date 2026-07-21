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
        "ticket-producer-prod",
        "ticket-worker-prod",
        "ticket-reconciler-prod",
        "ticket-task-signer-prod",
        "ticket-scheduler-prod",
        "n8n-ticket-invoker-prod",
      ]), toset(keys(var.runtime_service_accounts)))) == 0 &&
      length(var.runtime_service_accounts) == 6 &&
      alltrue([
        for email in values(var.runtime_service_accounts) :
        can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.iam\\.gserviceaccount\\.com$", email))
      ])
    )
    error_message = "runtime_service_accounts debe contener exactamente las seis SAs production como emails GCP válidos."
  }
}

variable "image_digest" {
  type        = string
  description = "Digest canónico aprobado; no se aceptan tags mutables."
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest debe fijar un digest sha256 inmutable."
  }
}

# Producción arranca en dark_no_traffic y sólo avanza por Terraform con G6B.
variable "release_phase" {
  type    = string
  default = "dark_no_traffic"
  validation {
    condition = contains([
      "dark_no_traffic", "dark_100", "shadow", "knowledge_only", "full",
    ], var.release_phase)
    error_message = "release_phase de producción está fuera del conjunto cerrado."
  }
}

variable "shadow_sample_rate" {
  type    = number
  default = 0
  validation {
    condition     = var.shadow_sample_rate == 0 || var.shadow_sample_rate == 100
    error_message = "shadow_sample_rate sólo puede ser 0 o 100."
  }
}

# Inputs no secretos observados en la revisión productiva existente. No tienen
# default: el plan aprobado debe transportar el inventario exacto y no puede
# borrar silenciosamente configuración core al importar el servicio.
variable "producer_core_env" {
  type = map(string)
  validation {
    condition = (
      toset(keys(var.producer_core_env)) == toset([
        "ENABLE_EXECUTION_LOGGING",
        "FORUSBOTS_BASE_URL",
        "GCS_BUCKET",
        "INDEX_NAME",
        "LLM_ROUTE_CLASSIFY",
        "LLM_ROUTE_DECOMPOSE",
        "LLM_ROUTE_GR_OUTCOME",
        "LLM_ROUTE_GR_RESPONSE",
        "LLM_ROUTE_KNOWLEDGE",
        "LLM_ROUTE_REQUIRED_DATA",
        "LLM_ROUTE_EXTRACT_INQUIRIES",
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
        "LLM_ROUTE_FORUSBOTS_FIELD_MAP",
        "LLM_ROUTE_GR_BODY_BUILD",
        "LLM_ROUTE_TICKET_FIELD_EXTRACT",
        "LOG_LEVEL",
        "NAMESPACE",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
        "TICKET_LLM_PRICING_JSON",
        "USE_VERTEX_AI",
      ]) &&
      alltrue([
        for value in values(var.producer_core_env) : trimspace(value) != ""
      ]) &&
      try(
        jsondecode(var.producer_core_env["TICKET_LLM_PRICING_JSON"]).pricing_as_of == "2026-07-21" &&
        jsondecode(var.producer_core_env["TICKET_LLM_PRICING_JSON"]).source == "openai-google-official-public-pricing",
        false,
      )
    )
    error_message = "producer_core_env debe coincidir exactamente con el inventario observado, sin extras/vacíos y con pricing 2026-07-21 revisado."
  }
}

# Sólo referencias a versiones NUMÉRICAS; nunca valores de secretos, latest ni
# referencias implícitas. Sin contrato completo el plan falla cerrado.
variable "secret_version_refs" {
  type = map(string)
  validation {
    condition = (
      alltrue([
        for key in [
          "API_KEY", "API_CLIENT_KEYS", "API_CLIENT_TENANTS",
          "PARTICIPANT_PLAN_SOURCE", "FORUSBOTS_AUTH_TOKEN",
          "OPENAI_API_KEY", "PINECONE_API_KEY",
        ] : trimspace(lookup(var.secret_version_refs, key, "")) != ""
      ]) &&
      alltrue([
        for ref in values(var.secret_version_refs) :
        can(regex("^projects/[^/]+/secrets/[^/]+/versions/[0-9]+$", ref))
      ])
    )
    error_message = "secret_version_refs exige los siete secretos observados por versión numérica."
  }
}

# Containers existentes rotados/cargados en G6A. Este root sólo los adopta y
# concede acceso por secreto/rol; nunca crea versiones ni lee payloads.
variable "secret_containers" {
  type = object({
    enabled        = bool
    ids            = map(string)
    accessor_roles = map(set(string))
  })

  validation {
    condition = (
      var.secret_containers.enabled &&
      length(setsubtract(
        toset([
          "API_KEY", "API_CLIENT_KEYS", "API_CLIENT_TENANTS",
          "PARTICIPANT_PLAN_SOURCE", "FORUSBOTS_AUTH_TOKEN",
          "OPENAI_API_KEY", "PINECONE_API_KEY",
        ]),
        toset(keys(var.secret_containers.ids)),
      )) == 0 &&
      length(setsubtract(
        toset(keys(var.secret_containers.ids)),
        toset([
          "API_KEY", "API_CLIENT_KEYS", "API_CLIENT_TENANTS",
          "PARTICIPANT_PLAN_SOURCE", "FORUSBOTS_AUTH_TOKEN",
          "OPENAI_API_KEY", "PINECONE_API_KEY",
        ]),
      )) == 0 &&
      alltrue([
        for secret_id in values(var.secret_containers.ids) :
        can(regex("^[a-zA-Z0-9_-]{1,255}$", secret_id))
      ]) &&
      try(var.secret_containers.accessor_roles["API_KEY"], toset([])) == toset(["producer"]) &&
      try(var.secret_containers.accessor_roles["API_CLIENT_KEYS"], toset([])) == toset(["producer"]) &&
      try(var.secret_containers.accessor_roles["API_CLIENT_TENANTS"], toset([])) == toset(["producer"]) &&
      try(var.secret_containers.accessor_roles["PARTICIPANT_PLAN_SOURCE"], toset([])) == toset(["producer"]) &&
      try(var.secret_containers.accessor_roles["FORUSBOTS_AUTH_TOKEN"], toset([])) == toset(["worker"]) &&
      try(var.secret_containers.accessor_roles["OPENAI_API_KEY"], toset([])) == toset(["producer", "worker"]) &&
      try(var.secret_containers.accessor_roles["PINECONE_API_KEY"], toset([])) == toset(["producer", "worker"]) &&
      length(keys(var.secret_containers.accessor_roles)) == 7
    )
    error_message = "production exige siete containers existentes y accessors mínimos exactos por producer/worker; nunca reconciler/e2e."
  }
}

# Rollback anchor explícito: dark_no_traffic nunca deriva la baseline de
# "latest". El gate G6B debe citar este nombre y el plan resultante.
variable "producer_baseline_revision" {
  type    = string
  default = "kb-rag-system-00048-bkc"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.producer_baseline_revision))
    error_message = "producer_baseline_revision debe ser un nombre de revisión Cloud Run."
  }
}

variable "producer_candidate_tag" {
  type    = string
  default = "candidate"
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

variable "producer_ingress" {
  type    = string
  default = "INGRESS_TRAFFIC_ALL"
}

variable "producer_max_instances" {
  type    = number
  default = 5
}

variable "producer_min_instances" {
  type    = number
  default = 0
}

variable "producer_concurrency" {
  type    = number
  default = 80
}

variable "producer_timeout" {
  type    = string
  default = "300s"
}

variable "producer_cpu" {
  type    = string
  default = "1"
}

variable "producer_memory" {
  type    = string
  default = "512Mi"
}

variable "producer_cpu_idle" {
  type    = bool
  default = true
}

variable "producer_startup_cpu_boost" {
  type    = bool
  default = true
}

variable "producer_port" {
  type    = number
  default = 8000
}

variable "producer_startup_probe" {
  type = object({
    initial_delay_seconds = number
    timeout_seconds       = number
    period_seconds        = number
    failure_threshold     = number
    tcp_socket_port       = number
  })
  default = {
    initial_delay_seconds = 0
    timeout_seconds       = 240
    period_seconds        = 240
    failure_threshold     = 1
    tcp_socket_port       = 8000
  }
}

variable "producer_liveness_probe" {
  type = object({
    initial_delay_seconds = number
    timeout_seconds       = number
    period_seconds        = number
    failure_threshold     = number
    tcp_socket_port       = number
  })
  default  = null
  nullable = true
}

variable "notification_channels" {
  type    = list(string)
  default = []
}

# Miembros invoker EXISTENTES del producer (kb-rag-client). Se preservan: no
# se retira allUsers ni el binding actual en este rollout.
variable "producer_invoker_members" {
  type    = list(string)
  default = ["serviceAccount:kb-rag-client@rag-kb-system.iam.gserviceaccount.com"]
}
