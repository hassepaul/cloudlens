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


class TenantBase(BaseModel):
    tenant_name: str = Field(..., min_length=2, max_length=120, description="Display name of the customer")
    subscription_ids: list[str] = Field(..., min_length=1, description="Azure subscription IDs to monitor")
    plan_tier: PlanTier = Field(default=PlanTier.GROWTH)
    alert_email: str = Field(..., description="Recipient of weekly digest and alerts")
    active: bool = Field(default=True)

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


class TenantConfig(TenantBase):
    """Cosmos DB document — partition key: id"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="tenant", description="Cosmos discriminator")
    sp_secret_ref: str = Field(..., description="Key Vault secret name for SP credentials")
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
        doc.pop("_partitionKey", None)
        doc.pop("_rid", None)
        doc.pop("_self", None)
        doc.pop("_etag", None)
        doc.pop("_attachments", None)
        doc.pop("_ts", None)
        return cls(**doc)
