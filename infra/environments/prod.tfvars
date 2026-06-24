environment           = "prod"
location              = "italynorth"
resource_group_name   = "rg-cloudlens-prod"
acr_name              = "acrcloudlensprod"
cosmos_db_name        = "cosmos-cloudlens-prod"
storage_account_name  = "stcloudlensprod"
key_vault_name        = "kv-cloudlens-prod"
alert_email           = "ops@cloudlens.io"
# internal_api_key set via TF_VAR_internal_api_key env var — never in tfvars
