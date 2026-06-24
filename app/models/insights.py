from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


# ── Anomaly ──────────────────────────────────────────────────────────────────
class AnomalyDriverModel(BaseModel):
    dimension: str
    name: str
    delta_eur: float
    share_of_spike: float


class AnomalyModel(BaseModel):
    day: str
    actual_eur: float
    expected_eur: float
    excess_eur: float
    direction: str
    severity: str
    z_score: float
    drivers: list[AnomalyDriverModel] = Field(default_factory=list)


class AnomalyResponse(BaseModel):
    tenant_id: str
    method: str
    scanned_days: int
    total_anomalous_excess_eur: float
    anomalies: list[AnomalyModel] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── Chargeback ───────────────────────────────────────────────────────────────
class AllocationGroupModel(BaseModel):
    name: str
    direct_eur: float
    allocated_shared_eur: float
    total_eur: float
    pct_of_total: float
    resource_count: int = 0
    budget_eur: Optional[float] = None
    budget_status: Optional[str] = None


class ChargebackResponse(BaseModel):
    tenant_id: str
    dimension: str
    strategy: str
    period_start: str
    period_end: str
    total_spend_eur: float
    tagged_spend_eur: float
    untagged_spend_eur: float
    tagging_coverage_pct: float
    groups: list[AllocationGroupModel] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── Insights ─────────────────────────────────────────────────────────────────
class InsightModel(BaseModel):
    rank: int
    category: str
    severity: str
    headline: str
    headline_it: str
    impact_eur: float
    action: str
    evidence: dict = Field(default_factory=dict)


class InsightDigestResponse(BaseModel):
    tenant_id: str
    monthly_spend_eur: float
    monthly_recoverable_eur: float
    efficiency_score: int
    headline_summary: str
    headline_summary_it: str
    insights: list[InsightModel] = Field(default_factory=list)
