environment          = "staging"
location             = "italynorth"
resource_group_name  = "rg-cloudlens-staging"
acr_name             = "acrcloudlensstaging"
cosmos_db_name       = "cosmos-cloudlens-staging"
storage_account_name = "stcloudlensstaging"
key_vault_name       = "kv-cloudlens-staging"
alert_email          = "ops@cloudlens.io"
# internal_api_key set via TF_VAR_internal_api_key env var — never in tfvars
