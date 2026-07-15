# Módulo ticket_environment — orquestación (plan Tarea 10).
# Terraform es el ÚNICO controlador de Cloud Run/config/tráfico. Cloud Build
# construye/atesta el digest; ningún YAML usa `gcloud run deploy/update`.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0, < 6.0.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.0.0, < 6.0.0"
    }
  }
}

locals {
  image_is_immutable = can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))

  # invariant: shadow_sample_rate=100 sólo en fase shadow; 0 en el resto.
  shadow_rate_ok = (
    var.release_phase == "shadow" ? var.shadow_sample_rate == 100
    : var.shadow_sample_rate == 0
  )

  # Cada fase tiene exactamente un modo posible. Esto impide combinar, por
  # ejemplo, release_phase=shadow con un handler full.
  expected_ticket_handler_modes = {
    infra_only      = "disabled"
    dark_no_traffic = "disabled"
    dark_100        = "disabled"
    shadow          = "shadow"
    knowledge_only  = "knowledge_only"
    full            = "full"
  }
  expected_ticket_handler_mode = local.expected_ticket_handler_modes[var.release_phase]
  mode_ok                      = var.ticket_handler_mode == local.expected_ticket_handler_mode

  create_services = var.enable_services && var.release_phase != "infra_only"
}
