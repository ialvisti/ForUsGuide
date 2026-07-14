# Módulo ticket_environment — entradas (plan de finalización, Tarea 10).
# La MISMA imagen inmutable se despliega con APP_ROLE distinto por servicio;
# la base Firestore NOMBRADA (no un prefijo) es el límite de aislamiento.

variable "project_id" {
  type        = string
  description = "Proyecto GCP (rag-kb-system)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "env" {
  type        = string
  description = "staging | production"
  validation {
    condition     = contains(["staging", "production"], var.env)
    error_message = "env debe ser staging o production."
  }
}

variable "image_digest" {
  type        = string
  description = "Imagen inmutable por digest (REGISTRY/IMAGE@sha256:...). Prohibido 'latest' o tag mutable."
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest debe fijarse por @sha256:<digest>."
  }
}

# Fase de release CERRADA (Tarea 10 Paso 4). infra_only no crea servicios Run.
variable "release_phase" {
  type    = string
  default = "infra_only"
  validation {
    condition = contains([
      "infra_only", "dark_no_traffic", "dark_100", "shadow",
      "knowledge_only", "full",
    ], var.release_phase)
    error_message = "release_phase fuera del conjunto cerrado."
  }
}

# n8n es el ÚNICO sampler de cohorts. shadow_sample_rate es un INVARIANT:
# 100 sólo cuando release_phase=shadow; 0 en cualquier otra fase.
variable "shadow_sample_rate" {
  type    = number
  default = 0
  validation {
    condition     = var.shadow_sample_rate == 0 || var.shadow_sample_rate == 100
    error_message = "shadow_sample_rate sólo puede ser 0 o 100 (invariant, no porcentaje de cohort)."
  }
}

variable "firestore_database" {
  type        = string
  description = "(default) en producción, ticket-staging en staging."
}

variable "ticket_handler_mode" {
  type        = string
  default     = "disabled"
  description = "disabled hasta que el rollout lo promueva por Terraform."
  validation {
    condition     = contains(["disabled", "shadow", "knowledge_only", "full"], var.ticket_handler_mode)
    error_message = "modo de handler inválido."
  }
}

# Manifest de secret versions NUMÉRICAS (nunca 'latest'). Mapa
# nombre_lógico -> "projects/<p>/secrets/<s>/versions/<N>".
variable "secret_version_refs" {
  type    = map(string)
  default = {}
  validation {
    condition = alltrue([
      for v in values(var.secret_version_refs) : can(regex("/versions/[0-9]+$", v))
    ])
    error_message = "cada secret ref debe fijar una versión NUMÉRICA (no 'latest')."
  }
}

variable "producer_service_name" {
  type = string
}

variable "worker_service_name" {
  type = string
}

variable "reconciler_job_name" {
  type = string
}

variable "queue_name" {
  type = string
}

variable "producer_sa_email" {
  type = string
}

variable "worker_sa_email" {
  type = string
}

variable "reconciler_sa_email" {
  type = string
}

variable "task_signer_sa_email" {
  type = string
}

variable "scheduler_sa_email" {
  type = string
}

variable "n8n_invoker_sa_email" {
  type = string
}

# Preservar la invoker policy existente del producer (no retirar allUsers ni
# el binding actual en este rollout). Vacío = no gestionar invoker aquí.
variable "producer_invoker_members" {
  type    = list(string)
  default = []
}

variable "worker_cpu" {
  type    = string
  default = "1"
}

variable "worker_memory" {
  type    = string
  default = "1Gi"
}

variable "worker_max_instances" {
  type = number
}

variable "queue_max_concurrent_dispatches" {
  type = number
}

variable "queue_max_dispatches_per_second" {
  type    = number
  default = 1
}

variable "enable_services" {
  type        = bool
  default     = false
  description = "false en infra_only (sólo base/cola/SAs/monitoring)."
}

variable "notification_channels" {
  type        = list(string)
  default     = []
  description = "IDs de canales de notificación (Tarea 11); probados por entorno."
}

variable "idempotency_retention_days" {
  type    = number
  default = 90
  validation {
    condition     = var.idempotency_retention_days >= 90
    error_message = "la retención de receipts/tombstones nunca es menor a 90 días."
  }
}
