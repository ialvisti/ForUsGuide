terraform {
  backend "gcs" {
    bucket = "rag-kb-system-tfstate-production-900340137010"
    prefix = "state"
  }
}
