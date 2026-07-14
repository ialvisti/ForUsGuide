variable "project_id" {
  type    = string
  default = "rag-kb-system"
}

variable "region" {
  type    = string
  default = "us-central1"
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

variable "n8n_aws_role_arn" {
  type    = string
  default = ""
}

variable "producer_audience" {
  type        = string
  default     = ""
  description = "Audiencia del ID token WIF (URL del producer)."
}
