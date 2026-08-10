output "ingest_url" {
  value = module.tickets_console.ingest_url
}

output "ingest_audience" {
  value = module.tickets_console.ingest_audience
}

output "console_url" {
  value = module.tickets_console.console_url
}

output "console_database" {
  value = module.tickets_console.console_database
}

output "runtime_service_accounts" {
  value = {
    console = module.tickets_console.console_service_account_email
    ingest  = module.tickets_console.ingest_service_account_email
    broker  = module.tickets_console.evidence_broker_service_account_email
  }
}
