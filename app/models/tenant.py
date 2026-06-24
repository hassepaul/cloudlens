from __future__ import annotations
from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
import re

from pydantic import BaseModel, Field, field_validator


class PlanTier(str, Enum):
    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


# Canonical cloud provider keys — lower-case stable identifiers.
class CloudProvider(str, Enum):
    AZURE = "azure"
    AWS = "aws"
    GCP = "gcp"
    ALIBABA = "alibaba"
    OCI = "oci"


# Clouds that require extra entitlement (i.e. cost an add-on).
# Azure is the included default and is always enabled.
ADDON_CLOUDS: frozenset[CloudProvider] = frozenset({
    CloudProvider.AWS,
    CloudProvider.GCP,
    CloudProvider.ALIBABA,
    CloudProvider.OCI,
})


class CloudCredentialRef(BaseModel):
    """Key Vault secret reference for a non-Azure cloud provider credential."""
    secret_ref: str = Field(..., description="Key Vault secret name")
    account_ids: list[str] = Field(default_factory=list, description="Cloud account / project IDs to monitor")


class TenantBase(BaseModel):
    tenant_name: str = Field(..., min_length=2, max_length=120, description="Display name of the customer")
    subscription_ids: list[str] = Field(..., min_length=1, description="Azure subscription IDs to monitor")
    plan_tier: PlanTier = Field(default=PlanTier.GROWTH)
    alert_email: str = Field(..., description="Recipient of weekly digest and alerts")
    active: bool = Field(default=True)
    # ── Currency preference ──────────────────────────────────────────────────
    # All values are stored internally in EUR. This field tells the API which
    # currency to present to this tenant (ISO 4217, e.g. "USD", "GBP").
    preferred_currency: str = Field(
        default="EUR",
        description="ISO 4217 currency code for API responses (e.g. 'USD', 'GBP').",
        pattern=r"^[A-Z]{3}$",
    )
    # ── Cloud entitlements ───────────────────────────────────────────────────
    # "azure" is always included (it is the default cloud). Additional clouds
    # are enabled per-tenant by ops once the customer has purchased the add-on.
    enabled_clouds: list[str] = Field(
        default_factory=lambda: [CloudProvider.AZURE],
        description="Cloud providers this tenant is entitled to monitor.",
    )
    # Non-Azure cloud account identifiers, keyed by CloudProvider value.
    # e.g. {"aws": ["123456789012"], "gcp": ["my-gcp-project"]}
    cloud_accounts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Cloud account/project IDs per non-Azure provider.",
    )

    @field_validator("subscription_ids")
    @classmethod
    def validate_subscription_ids(cls, v: list[str]) -> list[str]:
        pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE,
        )
        for sub_id in v:
            if not pattern.match(sub_id):
                raise ValueError(f"Invalid Azure subscription ID format: {sub_id}")
        return [s.lower() for s in v]

    @field_validator("alert_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"Invalid email address: {v}")
        return v.lower().strip()

    @field_validator("enabled_clouds")
    @classmethod
    def validate_enabled_clouds(cls, v: list[str]) -> list[str]:
        valid = {c.value for c in CloudProvider}
        for c in v:
            if c not in valid:
                raise ValueError(f"Unknown cloud provider '{c}'. Valid values: {sorted(valid)}")
        # Azure must always be present.
        if CloudProvider.AZURE not in v:
            v = [CloudProvider.AZURE] + [x for x in v if x != CloudProvider.AZURE]
        return list(dict.fromkeys(v))  # deduplicate while preserving order

    def has_cloud(self, cloud: str) -> bool:
        """Return True if this tenant is entitled to monitor the given cloud."""
        return cloud in self.enabled_clouds

    def is_multicloud(self) -> bool:
        """Return True if the tenant has any add-on cloud beyond Azure."""
        return any(c for c in self.enabled_clouds if c != CloudProvider.AZURE)


class TenantCreate(TenantBase):
    """Payload for POST /api/v1/tenants"""
    sp_client_id: str = Field(..., description="Service principal client ID")
    sp_client_secret: str = Field(..., description="Service principal secret (stored to Key Vault)")
    sp_tenant_id: str = Field(..., description="Customer Azure AD tenant ID")


class TenantUpdate(BaseModel):
    """Payload for PATCH /api/v1/tenants/{id} — all fields optional"""
    tenant_name: Optional[str] = Field(None, min_length=2, max_length=120)
    subscription_ids: Optional[list[str]] = None
    plan_tier: Optional[PlanTier] = None
    alert_email: Optional[str] = None
    active: Optional[bool] = None
    preferred_currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")


class CloudEnableRequest(BaseModel):
    """Payload for POST /api/v1/tenants/{id}/clouds — enable an add-on cloud."""
    cloud: str = Field(..., description="Cloud provider key, e.g. 'aws', 'gcp'")
    account_ids: list[str] = Field(..., min_length=1, description="Cloud account / project IDs")
    credential_secret_ref: str = Field(
        ...,
        description="Key Vault secret name holding the provider credentials JSON",
    )

    @field_validator("cloud")
    @classmethod
    def validate_cloud(cls, v: str) -> str:
        valid = {c.value for c in ADDON_CLOUDS}
        if v not in valid:
            raise ValueError(f"'{v}' is not an add-on cloud. Valid add-on clouds: {sorted(valid)}")
        return v


class TenantConfig(TenantBase):
    """Cosmos DB document — partition key: id"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="tenant", description="Cosmos discriminator")
    sp_secret_ref: str = Field(..., description="Key Vault secret name for SP credentials")
    # Key Vault secret refs for non-Azure provider credentials.
    cloud_credential_refs: dict[str, str] = Field(
        default_factory=dict,
        description="Key Vault secret name per non-Azure cloud, keyed by provider.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_ingested_at: Optional[datetime] = None
    last_ingest_error: Optional[str] = None


    def to_cosmos(self) -> dict:
        """Serialise for Cosmos DB upsert"""
        data = self.model_dump(mode="json")
        data["_partitionKey"] = self.id
        return data

    @classmethod
    def from_cosmos(cls, doc: dict) -> "TenantConfig":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        # Back-compat: old documents without enabled_clouds default to azure-only.
        doc.setdefault("enabled_clouds", [CloudProvider.AZURE])
        doc.setdefault("cloud_accounts", {})
        doc.setdefault("cloud_credential_refs", {})
        doc.setdefault("preferred_currency", "EUR")
        return cls(**doc)
