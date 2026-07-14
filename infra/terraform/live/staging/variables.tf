variable "project_id" {
  type    = string
  default = "rag-kb-system"
}

variable "region" {
  type    = string
  default = "us-central1"
}

# Inyectados por el trigger *-plan (Tarea 12): digest probado, fase de
# release y URI del manifest de secret versions numéricas.
variable "image_digest" {
  type    = string
  default = ""
}

variable "release_phase" {
  type    = string
  default = "infra_only"
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
