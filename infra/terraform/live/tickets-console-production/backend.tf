terraform {
  backend "gcs" {
    # Bucket is an approved init input; this prefix is immutable source policy.
    prefix = "tickets-console/production"
  }
}
