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
  description = "Imagen inmutable por digest. Puede quedar vacía sólo cuando infra_only no crea servicios."
  validation {
    condition     = var.image_digest == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest debe quedar vacío para infra_only o fijarse por @sha256:<digest>."
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

# Configuración core NO secreta del producer existente. El root de producción
# exige todas las claves observadas; el módulo impide solaparlas con variables
# ticket gestionadas declarativamente.
variable "producer_core_env" {
  type    = map(string)
  default = {}
}

# Revisión segura que conserva 100% del tráfico durante dark_no_traffic. Nunca
# se infiere "latest": el gate debe citar un nombre de revisión inmutable.
variable "producer_baseline_revision" {
  type    = string
  default = ""
  validation {
    condition = (
      var.producer_baseline_revision == "" ||
      can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.producer_baseline_revision))
    )
    error_message = "producer_baseline_revision debe ser un nombre de revisión Cloud Run inmutable."
  }
}

variable "producer_candidate_tag" {
  type    = string
  default = "candidate"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.producer_candidate_tag))
    error_message = "producer_candidate_tag debe ser un tag Cloud Run válido."
  }
}

variable "producer_ingress" {
  type    = string
  default = "INGRESS_TRAFFIC_ALL"
  validation {
    condition = contains([
      "INGRESS_TRAFFIC_ALL",
      "INGRESS_TRAFFIC_INTERNAL_ONLY",
      "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
    ], var.producer_ingress)
    error_message = "producer_ingress no es un valor admitido por Cloud Run v2."
  }
}

variable "producer_max_instances" {
  type    = number
  default = 5
  validation {
    condition     = var.producer_max_instances >= 1
    error_message = "producer_max_instances debe ser al menos 1."
  }
}

variable "producer_min_instances" {
  type    = number
  default = 0
  validation {
    condition     = var.producer_min_instances >= 0
    error_message = "producer_min_instances no puede ser negativo."
  }
}

variable "producer_concurrency" {
  type    = number
  default = 80
  validation {
    condition     = var.producer_concurrency >= 1
    error_message = "producer_concurrency debe ser al menos 1."
  }
}

variable "producer_timeout" {
  type    = string
  default = "300s"
  validation {
    condition     = can(regex("^[1-9][0-9]*s$", var.producer_timeout))
    error_message = "producer_timeout debe expresarse como segundos, por ejemplo 300s."
  }
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
  validation {
    condition     = var.producer_port > 0 && var.producer_port < 65536
    error_message = "producer_port debe ser un puerto TCP válido."
  }
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

# URL/audiencia estable del worker y segundo factor WIF del producer. Los
# valores WIF pueden quedar vacíos mientras el handler esté disabled; una fase
# activa de producción los exige mediante preconditions del servicio.
variable "worker_url" {
  type    = string
  default = ""
  validation {
    condition     = var.worker_url == "" || can(regex("^https://", var.worker_url))
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
  validation {
    condition = (
      var.ticket_wif_expected_email == "" ||
      can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.ticket_wif_expected_email))
    )
    error_message = "ticket_wif_expected_email debe ser un email de service account válido."
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

# production/kb-rag-runner pertenece al state platform durante G1C. null usa
# la ruta segura: staging gestiona producer; production lo excluye para que un
# mismo binding nunca viva en dos states.
variable "manage_producer_firestore_iam" {
  type     = bool
  default  = null
  nullable = true
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

# Sólo crea containers y bindings IAM; las versiones/payloads se cargan fuera
# de Terraform y los runtimes siguen recibiendo referencias numéricas.
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

  validation {
    condition = (
      !var.secret_containers.enabled ||
      (
        length(var.secret_containers.ids) > 0 &&
        length(setsubtract(
          toset(keys(var.secret_containers.ids)),
          toset(keys(var.secret_containers.accessor_roles)),
        )) == 0 &&
        length(setsubtract(
          toset(keys(var.secret_containers.accessor_roles)),
          toset(keys(var.secret_containers.ids)),
        )) == 0 &&
        alltrue([
          for secret_id in values(var.secret_containers.ids) :
          can(regex("^[a-zA-Z0-9_-]{1,255}$", secret_id))
        ]) &&
        alltrue(flatten([
          for roles in values(var.secret_containers.accessor_roles) : [
            for role in roles : contains(["producer", "worker", "e2e"], role)
          ]
        ]))
      )
    )
    error_message = "secret_containers habilitado exige IDs válidos, las mismas claves y roles producer/worker/e2e."
  }
}

# Runner de contrato. enabled=false por defecto; jamás se crea en production,
# jamás usa Compute default y su digest/secret versions deben ser inmutables.
variable "e2e_job" {
  type = object({
    enabled               = bool
    image_digest          = string
    service_account_email = string
    producer_url          = string
    secret_version_refs   = map(string)
  })
  default = {
    enabled               = false
    image_digest          = ""
    service_account_email = ""
    producer_url          = ""
    secret_version_refs   = {}
  }

  validation {
    condition = (
      !var.e2e_job.enabled ||
      (
        can(regex("@sha256:[0-9a-f]{64}$", var.e2e_job.image_digest)) &&
        can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.iam\\.gserviceaccount\\.com$", var.e2e_job.service_account_email)) &&
        can(regex("^https://", var.e2e_job.producer_url)) &&
        alltrue([
          for ref in values(var.e2e_job.secret_version_refs) :
          can(regex("^projects/[^/]+/secrets/[^/]+/versions/[0-9]+$", ref))
        ])
      )
    )
    error_message = "e2e_job habilitado exige digest, SA explícita, URL https y secret versions numéricas."
  }
}
