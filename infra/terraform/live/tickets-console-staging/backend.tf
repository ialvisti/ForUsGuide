terraform {
  backend "gcs" {
    # The approved controller supplies the bucket; the state namespace is
    # fixed here so it cannot overlap any existing environment root.
    prefix = "tickets-console/staging"
  }
}
