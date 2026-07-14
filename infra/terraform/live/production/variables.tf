variable "project_id" {
  type    = string
  default = "rag-kb-system"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image_digest" {
  type    = string
  default = ""
}

# Producción arranca en dark_no_traffic y sólo avanza por Terraform con G6B.
variable "release_phase" {
  type    = string
  default = "dark_no_traffic"
}

variable "shadow_sample_rate" {
  type    = number
  default = 0
}

variable "secret_version_refs" {
  type    = map(string)
  default = {}
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
