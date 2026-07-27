terraform {
  # Las validaciones cruzadas entre variables exigen Terraform 1.9; el rango
  # menor evita ejecutar el pipeline fuera de la minor verificada y fijada en CI.
  required_version = ">= 1.9.0, < 1.10.0"
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
