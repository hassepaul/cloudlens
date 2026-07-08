variable "environment"          { type = string }
variable "location"             { type = string    default = "italynorth" }
# Optional: set to any Azure region to add a read replica + Cosmos geo-replica.
# Leave empty ("") for single-region deployments. The Container App in the
# secondary region is an independent Terraform deployment — point a second
# workspace at the same tfvars with a different location value.
variable "secondary_location"   {
  type        = string
  default     = ""
  description = "Secondary Azure region for Cosmos DB geo-replication. Empty = disabled."
}
variable "resource_group_name"  { type = string }
variable "acr_name"             { type = string }
variable "cosmos_db_name"       { type = string }
variable "storage_account_name" { type = string }
variable "key_vault_name"       { type = string }
variable "alert_email"          { type = string }
variable "internal_api_key"     { type = string    sensitive = true }
variable "backend_image_tag"    { type = string    default = "latest" }

# ── Distributed rate limiting + Enterprise SSO ────────────────────────────────
variable "enable_redis" {
  type        = bool
  default     = false
  description = "Provision Azure Cache for Redis and enable GLOBAL (cross-replica) rate limiting. When false the API uses the in-process limiter."
}
variable "session_jwt_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "HS256 secret for CloudLens SSO session tokens. Empty = SSO session issuance disabled."
}
variable "public_base_url" {
  type        = string
  default     = ""
  description = "External API base URL for SAML ACS/metadata (e.g. https://api.cloudlens.io). Empty = derive from request."
}
variable "frontend_base_url" {
  type        = string
  default     = ""
  description = "SPA base URL for post-SSO redirect."
}
