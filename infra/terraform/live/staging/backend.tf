terraform {
  backend "gcs" {
    bucket = "rag-kb-system-tfstate-staging-900340137010"
    prefix = "state"
  }
}
