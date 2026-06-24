from __future__ import annotations
from functools import lru_cache


from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────────
    app_name: str = "CloudLens"
    app_version: str = "1.0.0"
    environment: str = Field(default="development", pattern="^(development|staging|production)$")
    debug: bool = False
    log_level: str = "INFO"

    # ── API Security ───────────────────────────────────────────────────────
    api_key_header: str = "X-API-Key"
    internal_api_key: str = Field(..., description="Internal service-to-service API key")

    # ── Azure AD ───────────────────────────────────────────────────────────
    azure_tenant_id: str = Field(..., description="CloudLens own Azure AD tenant ID")
    azure_client_id: str = Field(..., description="CloudLens managed identity / app reg client ID")

    # ── Cosmos DB ─────────────────────────────────────────────────────────
    cosmos_endpoint: str = Field(..., description="https://<account>.documents.azure.com:443/")
    cosmos_database: str = Field(default="cloudlens")
    cosmos_container_tenants: str = Field(default="tenants")
    cosmos_container_cost_records: str = Field(default="cost_records")
    cosmos_container_waste_items: str = Field(default="waste_items")
    cosmos_container_reports: str = Field(default="reports")

    # ── Blob Storage ──────────────────────────────────────────────────────
    storage_account_name: str = Field(..., description="Azure Storage account name")
    storage_container_reports: str = Field(default="reports")
    blob_sas_expiry_hours: int = Field(default=1, ge=1, le=24)

    # ── Service Bus ───────────────────────────────────────────────────────
    # Service Bus is OPTIONAL. The default deployment runs ingestion inline in
    # the nightly Container Apps Job (cheapest path — no queue resource). These
    # settings only matter if you re-introduce async queueing at higher scale.
    service_bus_namespace: str = Field(default="", description="<namespace>.servicebus.windows.net")
    service_bus_queue_ingest: str = Field(default="cost-ingest")
    service_bus_max_message_count: int = Field(default=10)

    # ── CORS ──────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins for the SPA. In production this is
    # the storage static-website endpoint (and any custom domain).
    cors_allowed_origins: str = Field(
        default="https://cloudlens.io,https://app.cloudlens.io",
        description="Comma-separated allowed CORS origins",
    )

    # ── Key Vault ─────────────────────────────────────────────────────────
    key_vault_name: str = Field(..., description="Azure Key Vault name")

    # ── Ingest ────────────────────────────────────────────────────────────
    ingest_lookback_days: int = Field(default=30, ge=1, le=90)
    ingest_max_retries: int = Field(default=3)
    ingest_retry_backoff_seconds: float = Field(default=2.0)

    # ── Rate limits (requests per minute per tenant) ───────────────────────
    rate_limit_starter: int = Field(default=60)
    rate_limit_growth: int = Field(default=200)
    rate_limit_enterprise: int = Field(default=600)

    # ── FX / Currency ─────────────────────────────────────────────────────
    # ECB rate cache TTL in seconds. Set to 0 to disable caching (tests only).
    fx_cache_ttl_seconds: int = Field(default=3600, ge=0)
    # Comma-separated list of currencies to pre-warm at startup.
    fx_prefetch_currencies: str = Field(
        default="USD,GBP,CHF,JPY,CAD,AUD,SEK,PLN,NOK,DKK",
        description="Currencies to warm FX cache for at startup.",
    )

    # ── AI Cost Analyst ────────────────────────────────────────────────────
    # Set openai_api_key (or store in Key Vault as "openai-api-key") to enable
    # LLM-powered root-cause explanations. Leave empty to use rule-based fallback.
    # For Azure OpenAI, set openai_base_url to your deployment endpoint:
    #   https://<resource>.openai.azure.com/openai/deployments/<deployment>
    openai_api_key: str = Field(default="", description="OpenAI API key (empty = disabled)")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="Base URL for the OpenAI-compatible API")
    openai_model: str = Field(default="gpt-4o", description="Model name / Azure deployment name")
    ai_analyst_max_tokens: int = Field(default=700, ge=100, le=2000)
    ai_explanation_cache_ttl: int = Field(default=604_800, ge=0, description="Cache AI explanations for N seconds (default 7 days)")

    @property
    def fx_prefetch_list(self) -> list[str]:
        return [c.strip().upper() for c in self.fx_prefetch_currencies.split(",") if c.strip()]

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def key_vault_uri(self) -> str:
        return f"https://{self.key_vault_name}.vault.azure.net/"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton — call once, reuse everywhere."""
    return Settings()
