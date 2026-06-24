environment          = "dev"
location             = "westeurope"
resource_group_name  = "rg-cloudlens-dev"
acr_name             = "acrcloudlensdev"
cosmos_db_name       = "cosmos-cloudlens-dev"
storage_account_name = "stcloudlensdev"
key_vault_name       = "kv-cloudlens-dev"
alert_email          = "ops@cloudlens.io"
# internal_api_key set via TF_VAR_internal_api_key env var — never in tfvars
