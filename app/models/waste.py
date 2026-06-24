from __future__ import annotations
from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class WasteType(str, Enum):
    IDLE_VM = "idle_vm"
    UNATTACHED_DISK = "unattached_disk"
    ORPHAN_PUBLIC_IP = "orphan_public_ip"
    OVERSIZED_VM = "oversized_vm"
    DEV_TEST_ELIGIBLE = "dev_test_eligible"
    RESERVED_INSTANCE = "reserved_instance"
    IDLE_APP_SERVICE = "idle_app_service"
    UNUSED_LOAD_BALANCER = "unused_load_balancer"
    OLD_SNAPSHOTS = "old_snapshots"
    COLD_STORAGE = "cold_storage"
    DUPLICATED_BACKUP = "duplicated_backup"
    EXPIRED_CERT = "expired_cert"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WasteItem(BaseModel):
    """Detected waste item — Cosmos container: waste_items"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="waste_item")
    tenant_id: str
    subscription_id: str
    resource_id: str
    resource_name: str
    resource_group: str
    resource_type: str = ""
    waste_type: WasteType
    monthly_cost_eur: float = Field(..., ge=0.0, description="Current monthly cost of this resource")
    saving_eur: float = Field(..., ge=0.0, description="Estimated saving if remediated")
    saving_pct: float = Field(default=0.0, description="Saving as % of current cost")
    priority: Priority
    recommendation: str = Field(..., description="Human-readable action (EN)")
    recommendation_it: str = Field(..., description="Raccomandazione in italiano")
    advisor_ref: Optional[str] = Field(None, description="Azure Advisor recommendation ID")
    evidence: dict = Field(default_factory=dict, description="Supporting metrics: cpu_avg, disk_state, etc.")
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    snoozed_until: Optional[datetime] = None


    def to_cosmos(self) -> dict:
        data = self.model_dump(mode="json")
        data["_partitionKey"] = self.tenant_id
        return data

    @classmethod
    def from_cosmos(cls, doc: dict) -> "WasteItem":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None

    @property
    def is_snoozed(self) -> bool:
        if self.snoozed_until is None:
            return False
        return datetime.now(timezone.utc) < self.snoozed_until


class WasteResolve(BaseModel):
    resolved_by: str = Field(..., min_length=1)
    notes: Optional[str] = None
