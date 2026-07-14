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
  # invariant: shadow_sample_rate=100 sólo en fase shadow; 0 en el resto.
  shadow_rate_ok = (
    var.release_phase == "shadow" ? var.shadow_sample_rate == 100
    : var.shadow_sample_rate == 0
  )

  # dark_no_traffic / dark_100 fuerzan handler disabled (baseline endurecida).
  mode_ok = (
    contains(["dark_no_traffic", "dark_100", "infra_only"], var.release_phase)
    ? var.ticket_handler_mode == "disabled"
    : true
  )

  create_services = var.enable_services && var.release_phase != "infra_only"
}

# Guards de coherencia declarativos: un plan con combinación inválida falla
# en `validate`/`plan`, no en producción.
resource "null_resource" "release_phase_invariants" {
  lifecycle {
    precondition {
      condition     = local.shadow_rate_ok
      error_message = "shadow_sample_rate=100 sólo es válido con release_phase=shadow."
    }
    precondition {
      condition     = local.mode_ok
      error_message = "las fases dark_* / infra_only exigen ticket_handler_mode=disabled."
    }
  }
}
