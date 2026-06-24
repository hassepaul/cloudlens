from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from enum import Enum

from pydantic import BaseModel, Field


class SavingsStatus(str, Enum):
    IDENTIFIED = "identified"      # recommendation surfaced, not yet acted on
    ACTIONED = "actioned"          # customer applied the change
    REALIZED = "realized"          # measured saving confirmed on the bill
    DISMISSED = "dismissed"        # customer chose not to act


class SavingsCategory(str, Enum):
    WASTE = "waste"
    RIGHTSIZE = "rightsize"
    SCHEDULE = "schedule"
    COMMITMENT = "commitment"
    ANOMALY = "anomaly"


class SavingsRecordCreate(BaseModel):
    tenant_id: str
    category: SavingsCategory
    resource_id: str = ""
    resource_name: str = ""
    description: str = ""
    estimated_monthly_eur: float = Field(..., ge=0)


class SavingsRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="savings_record")
    tenant_id: str
    category: SavingsCategory
    resource_id: str = ""
    resource_name: str = ""
    description: str = ""
    estimated_monthly_eur: float = 0.0
    status: SavingsStatus = SavingsStatus.IDENTIFIED
    actioned_at: Optional[datetime] = None
    realized_monthly_eur: Optional[float] = None    # measured after action
    baseline_monthly_eur: Optional[float] = None     # cost before action (for measurement)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "SavingsRecord":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


class SavingsLedger(BaseModel):
    tenant_id: str
    identified_monthly_eur: float          # sum of open opportunities
    actioned_monthly_eur: float            # actioned but not yet measured
    realized_monthly_eur: float            # confirmed on the bill
    realized_annual_eur: float
    realization_rate_pct: float            # realized / (actioned + realized)
    by_category: dict = Field(default_factory=dict)
    record_count: int = 0
