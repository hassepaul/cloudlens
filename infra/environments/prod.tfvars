environment           = "prod"
location              = "italynorth"
# Uncomment to enable Cosmos DB geo-replication and deploy a second Container App
# region. Any Azure region works — run a second `terraform apply` with location
# set to the secondary and the same image tag to get full active-active.
# secondary_location    = "westeurope"
resource_group_name   = "rg-cloudlens-prod"
acr_name              = "acrcloudlensprod"
cosmos_db_name        = "cosmos-cloudlens-prod"
storage_account_name  = "stcloudlensprod"
key_vault_name        = "kv-cloudlens-prod"
alert_email           = "ops@cloudlens.io"
# internal_api_key set via TF_VAR_internal_api_key env var — never in tfvars
