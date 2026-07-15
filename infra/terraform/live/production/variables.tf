variable "project_id" {
  type    = string
  default = "rag-kb-system"
}

variable "region" {
  type    = string
  default = "us-central1"
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
    condition = alltrue([
      for key in [
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
        "LOG_LEVEL",
        "NAMESPACE",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
        "USE_VERTEX_AI",
      ] : trimspace(lookup(var.producer_core_env, key, "")) != ""
    ])
    error_message = "producer_core_env debe incluir todo el inventario core observado, sin valores vacíos."
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
          "API_KEY", "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY",
        ] : trimspace(lookup(var.secret_version_refs, key, "")) != ""
      ]) &&
      alltrue([
        for ref in values(var.secret_version_refs) :
        can(regex("^projects/[^/]+/secrets/[^/]+/versions/[0-9]+$", ref))
      ])
    )
    error_message = "secret_version_refs exige los cuatro secretos observados por versión numérica."
  }
}

# Rollback anchor explícito: dark_no_traffic nunca deriva la baseline de
# "latest". El gate G6B debe citar este nombre y el plan resultante.
variable "producer_baseline_revision" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.producer_baseline_revision))
    error_message = "producer_baseline_revision debe ser un nombre de revisión Cloud Run."
  }
}

variable "producer_candidate_tag" {
  type    = string
  default = "candidate"
}

# URL/audiencia estable del worker. Es input deliberado para evitar una
# autorreferencia Terraform worker.uri -> template del mismo worker.
variable "worker_url" {
  type = string
  validation {
    condition     = can(regex("^https://", var.worker_url))
    error_message = "worker_url debe usar https://."
  }
}

variable "ticket_wif_audience" {
  type    = string
  default = ""
}

variable "ticket_wif_expected_email" {
  type    = string
  default = ""
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
  default = false
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
