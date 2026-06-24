from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class BudgetBase(BaseModel):
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=120)
    amount_eur: float = Field(..., gt=0)
    period: str = Field(default="monthly", pattern="^(monthly)$")
    # scope: whole tenant, or a tag dimension+value (e.g. cost_center=engineering)
    scope_dimension: Optional[str] = Field(None, description="Tag key, e.g. cost_center")
    scope_value: Optional[str] = Field(None, description="Tag value, e.g. engineering")
    warning_threshold_pct: int = Field(default=85, ge=1, le=100)


class BudgetCreate(BudgetBase):
    pass


class BudgetUpdate(BaseModel):
    name: Optional[str] = None
    amount_eur: Optional[float] = Field(None, gt=0)
    warning_threshold_pct: Optional[int] = Field(None, ge=1, le=100)


class Budget(BudgetBase):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="budget")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_cosmos(self) -> dict:
        data = self.model_dump(mode="json")
        data["_partitionKey"] = self.tenant_id
        return data

    @classmethod
    def from_cosmos(cls, doc: dict) -> "Budget":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


class BudgetStatus(BaseModel):
    budget_id: str
    name: str
    amount_eur: float
    spend_to_date_eur: float
    consumed_pct: float
    projected_month_end_eur: Optional[float] = None
    projected_consumed_pct: Optional[float] = None
    status: str                      # "ok" | "warning" | "breach" | "projected_breach"
    breach_date: Optional[str] = None
    scope: str = "tenant"
    headroom_eur: Optional[float] = None
