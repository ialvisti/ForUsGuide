# Backend de estado remoto (creado con G1A, ver Tarea 10 Paso 2/10). El
# bucket se aprovisiona ANTES del primer init con backend; nunca se versiona
# state ni payloads en git.
terraform {
  backend "gcs" {
    bucket = "rag-kb-system-tfstate-platform-900340137010"
    prefix = "state"
  }
}
