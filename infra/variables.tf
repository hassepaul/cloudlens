variable "environment"          { type = string }
variable "location"             { type = string    default = "italynorth" }
variable "resource_group_name"  { type = string }
variable "acr_name"             { type = string }
variable "cosmos_db_name"       { type = string }
variable "storage_account_name" { type = string }
variable "key_vault_name"       { type = string }
variable "alert_email"          { type = string }
variable "internal_api_key"     { type = string    sensitive = true }
variable "backend_image_tag"    { type = string    default = "latest" }
