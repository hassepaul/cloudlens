terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    azuread = { source = "hashicorp/azuread",  version = "~> 2.0" }
  }
  backend "azurerm" {
    # Configured via -backend-config=environments/<env>.backend.tfvars
  }
}

provider "azurerm" {
  features {
    key_vault { purge_soft_delete_on_destroy = false }
  }
  use_oidc = true
}

# ── Data ─────────────────────────────────────────────────────────────────────
data "azurerm_client_config" "current" {}

# ── Resource Group ────────────────────────────────────────────────────────────
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

# ── Log Analytics ─────────────────────────────────────────────────────────────
resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-cloudlens-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  # Cost control: 30 days is the free-included retention floor; beyond it is
  # billed per GB/month. The daily ingestion cap stops a runaway log loop from
  # generating a surprise bill.
  retention_in_days  = 30
  daily_quota_gb     = var.environment == "prod" ? 1 : 0.5
  tags               = local.tags
}

# ── Container Apps Environment ────────────────────────────────────────────────
resource "azurerm_container_app_environment" "main" {
  name                       = "cae-cloudlens-${var.environment}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.tags
}

# ── Container Registry ────────────────────────────────────────────────────────
resource "azurerm_container_registry" "main" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

# ── User-Assigned Managed Identity ───────────────────────────────────────────
resource "azurerm_user_assigned_identity" "api" {
  name                = "id-cloudlens-api-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}

# ── Cosmos DB ─────────────────────────────────────────────────────────────────
resource "azurerm_cosmosdb_account" "main" {
  name                = var.cosmos_db_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"
  tags                = local.tags

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }

  capabilities { name = "EnableServerless" }

  # ── SOC 2 hardening ──
  # A1.2 recoverability: continuous backup enables point-in-time restore.
  backup {
    type = "Continuous"
  }
  # CC6.2 least privilege: prefer AAD/RBAC data-plane auth over key auth.
  # (Key auth left enabled for the bootstrap/ingest path; tighten once the
  # data-plane RBAC role assignments are provisioned.)
  local_authentication_disabled = false

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }
}

resource "azurerm_cosmosdb_sql_database" "main" {
  name                = "cloudlens"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

locals {
  cosmos_containers = {
    tenants      = { partition_key = "/id",        ttl = null }
    cost_records = { partition_key = "/tenant_id", ttl = 7776000 }
    waste_items  = { partition_key = "/tenant_id", ttl = null }
    reports      = { partition_key = "/tenant_id", ttl = null }
  }
  tags = {
    environment = var.environment
    project     = "cloudlens"
    managed_by  = "terraform"
  }
}

resource "azurerm_cosmosdb_sql_container" "containers" {
  for_each = local.cosmos_containers

  name                = each.key
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.main.name
  partition_key_paths = [each.value.partition_key]

  dynamic "default_ttl" {
    for_each = each.value.ttl != null ? [each.value.ttl] : []
    content { ttl = default_ttl.value }
  }
}

# ── Storage Account ───────────────────────────────────────────────────────────
resource "azurerm_storage_account" "main" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  # ── SOC 2 hardening (CC6.6 / CC6.7) ──
  https_traffic_only_enabled      = true   # reject plaintext (CC6.6 in transit)
  infrastructure_encryption_enabled = true # double-encrypt at rest (CC6.7)
  allow_nested_items_to_be_public = false  # no anonymous blob access
  shared_access_key_enabled       = true   # SAS used for time-boxed report URLs

  blob_properties {
    delete_retention_policy { days = 7 }   # recoverability (A1.2)
  }

  tags = local.tags
}

resource "azurerm_storage_container" "reports" {
  name                  = "reports"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

# ── Static website hosting for the SPA frontend ───────────────────────────────
# Serving the dashboard from the storage account's built-in $web container is
# effectively free (pay only for the few hundred KB of assets + requests).
# This replaces a dedicated Static Web App / Power BI Embedded — the single
# biggest cost saving in the whole stack.
resource "azurerm_storage_account_static_website" "spa" {
  storage_account_id = azurerm_storage_account.main.id
  index_document     = "index.html"
  error_404_document = "index.html" # SPA fallback for client-side routing
}

# ── Key Vault ─────────────────────────────────────────────────────────────────
resource "azurerm_key_vault" "main" {
  name                       = var.key_vault_name
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = true
  soft_delete_retention_days = 7
  tags                       = local.tags
}

resource "azurerm_key_vault_access_policy" "api_identity" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_user_assigned_identity.api.principal_id

  secret_permissions = ["Get", "List", "Set"]
}

# ── RBAC — Cosmos ─────────────────────────────────────────────────────────────
resource "azurerm_cosmosdb_sql_role_assignment" "api" {
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.api.principal_id
  scope               = azurerm_cosmosdb_account.main.id
}

# ── RBAC — Storage ────────────────────────────────────────────────────────────
resource "azurerm_role_assignment" "api_storage" {
  scope                = azurerm_storage_account.main.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.api.principal_id
}

# ── RBAC — ACR Pull ───────────────────────────────────────────────────────────
resource "azurerm_role_assignment" "api_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.api.principal_id
}

# ── Container App — API ───────────────────────────────────────────────────────
resource "azurerm_container_app" "api" {
  name                         = "cloudlens-api"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.api.id
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 5

    http_scale_rule {
      name                = "http-scaling"
      concurrent_requests = "20"
    }

    container {
      name   = "cloudlens-api"
      image  = "${azurerm_container_registry.main.login_server}/cloudlens-api:${var.backend_image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name  = "STORAGE_ACCOUNT_NAME"
        value = azurerm_storage_account.main.name
      }
      env {
        name  = "KEY_VAULT_NAME"
        value = azurerm_key_vault.main.name
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.api.client_id
      }
      env {
        name  = "AZURE_TENANT_ID"
        value = data.azurerm_client_config.current.tenant_id
      }
      env {
        name        = "INTERNAL_API_KEY"
        secret_name = "internal-api-key"
      }
    }

    liveness_probe {
      path             = "/api/v1/health"
      port             = 8000
      transport        = "HTTP"
      initial_delay    = 20
      period_seconds   = 30
      failure_count_threshold = 3
    }
  }

  secret {
    name  = "internal-api-key"
    value = var.internal_api_key
  }
}

# ── Container App Job — Ingest ────────────────────────────────────────────────
resource "azurerm_container_app_job" "ingest" {
  name                         = "cloudlens-ingest"
  resource_group_name          = azurerm_resource_group.main.name
  location                     = azurerm_resource_group.main.location
  container_app_environment_id = azurerm_container_app_environment.main.id
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  replica_timeout_in_seconds = 3600
  replica_retry_limit        = 1

  schedule_trigger_config {
    cron_expression          = "0 2 * * *"   # 02:00 UTC daily
    parallelism              = 1
    replica_completion_count = 1
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.api.id
  }

  template {
    container {
      name    = "ingest"
      image   = "${azurerm_container_registry.main.login_server}/cloudlens-api:${var.backend_image_tag}"
      cpu     = 1.0
      memory  = "2Gi"
      command = ["python", "-m", "app.jobs.ingest"]

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name  = "KEY_VAULT_NAME"
        value = azurerm_key_vault.main.name
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.api.client_id
      }
      env {
        name  = "AZURE_TENANT_ID"
        value = data.azurerm_client_config.current.tenant_id
      }
      env {
        name  = "STORAGE_ACCOUNT_NAME"
        value = azurerm_storage_account.main.name
      }
      env {
        name        = "INTERNAL_API_KEY"
        secret_name = "internal-api-key"
      }
    }
  }

  secret {
    name  = "internal-api-key"
    value = var.internal_api_key
  }
}

# ── Azure Monitor alerts ──────────────────────────────────────────────────────
resource "azurerm_monitor_action_group" "ops" {
  name                = "ag-cloudlens-ops"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "clops"

  email_receiver {
    name          = "ops"
    email_address = var.alert_email
  }
}

resource "azurerm_monitor_metric_alert" "api_error_rate" {
  name                = "cloudlens-api-error-rate"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [azurerm_container_app.api.id]
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.App/containerApps"
    metric_name      = "Requests"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0

    dimension {
      name     = "statusCodeCategory"
      operator = "Include"
      values   = ["5xx"]
    }
  }

  action {
    action_group_id = azurerm_monitor_action_group.ops.id
  }
}
