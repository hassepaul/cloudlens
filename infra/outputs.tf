# ── Outputs consumed by deploy.sh and operators ─────────────────────────────

output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "acr_name" {
  value = azurerm_container_registry.main.name
}

output "api_fqdn" {
  description = "Public hostname of the CloudLens API"
  value       = azurerm_container_app.api.ingress[0].fqdn
}

output "api_url" {
  description = "Base URL of the CloudLens API"
  value       = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "container_app_name" {
  value = azurerm_container_app.api.name
}

output "ingest_job_name" {
  value = azurerm_container_app_job.ingest.name
}

output "managed_identity_client_id" {
  description = "Client ID of the user-assigned identity used by the API + ingest job"
  value       = azurerm_user_assigned_identity.api.client_id
}

output "managed_identity_principal_id" {
  value = azurerm_user_assigned_identity.api.principal_id
}

output "cosmos_endpoint" {
  value = azurerm_cosmosdb_account.main.endpoint
}

output "key_vault_name" {
  value = azurerm_key_vault.main.name
}

output "storage_account_name" {
  value = azurerm_storage_account.main.name
}
