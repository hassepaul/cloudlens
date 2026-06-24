from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class CostRecord(BaseModel):
    """Raw daily cost snapshot — Cosmos container: cost_records, TTL: 90 days"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="cost_record")
    tenant_id: str
    subscription_id: str
    record_date: date
    service_name: str
    resource_id: str
    resource_group: str
    resource_name: str
    location: str = ""
    cost_eur: float = Field(..., ge=0.0)
    currency: str = "EUR"
    quantity: float = 0.0
    unit_of_measure: str = ""
    tags: dict[str, str] = Field(default_factory=dict)
    meter_category: str = ""
    meter_sub_category: str = ""
    ttl: int = Field(default=7_776_000, description="90 days in seconds")
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


    def to_cosmos(self) -> dict:
        data = self.model_dump(mode="json")
        data["_partitionKey"] = self.tenant_id
        return data


class CostSummary(BaseModel):
    """Aggregated cost summary for a tenant/period"""
    tenant_id: str
    period_start: date
    period_end: date
    total_cost_eur: float
    previous_period_cost_eur: Optional[float] = None
    change_pct: Optional[float] = None
    top_services: list[dict] = Field(default_factory=list)
    top_resource_groups: list[dict] = Field(default_factory=list)


class CostBreakdown(BaseModel):
    """Cost breakdown by dimension"""
    tenant_id: str
    dimension: str  # service | resource_group | tag | location
    period_start: date
    period_end: date
    items: list[dict] = Field(default_factory=list)


class CostTrend(BaseModel):
    """Daily cost trend for sparkline / chart"""
    tenant_id: str
    days: int  # 30 | 60 | 90
    data_points: list[dict] = Field(default_factory=list)  # [{date, cost_eur}]
    average_daily_eur: float = 0.0
    peak_day: Optional[str] = None
    peak_cost_eur: float = 0.0
