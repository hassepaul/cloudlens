from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ForecastPointModel(BaseModel):
    day: str
    value: float
    lower: float
    upper: float


class SpendForecastResponse(BaseModel):
    tenant_id: str
    method: str
    horizon_days: int
    history_days: int
    mape: Optional[float] = Field(None, description="Backtest mean abs % error")
    confidence: str
    month_end_projection: Optional[float] = None
    annual_seasonality: bool = Field(default=False, description="True when a month-of-year seasonal overlay was applied")
    points: list[ForecastPointModel] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AnnualForecastResponse(BaseModel):
    tenant_id: str
    method: str
    horizon_months: int
    history_months: int
    mape: Optional[float] = None
    confidence: str
    months: list[ForecastPointModel] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TrajectoryResponse(BaseModel):
    tenant_id: str
    horizon_days: int
    daily_waste_burn_eur: float
    cumulative_inaction_eur: float
    monthly_recoverable_eur: float
    annual_recoverable_eur: float
    baseline: list[ForecastPointModel] = Field(default_factory=list)
    optimized: list[ForecastPointModel] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RoadmapPhaseModel(BaseModel):
    phase: int
    label: str
    items: int
    monthly_saving_eur: float
    cumulative_monthly_saving_eur: float
    target_run_rate_eur: float
    eta_days: int


class RoadmapResponse(BaseModel):
    tenant_id: str
    current_run_rate_eur: float
    optimized_run_rate_eur: float
    total_monthly_saving_eur: float
    phases: list[RoadmapPhaseModel] = Field(default_factory=list)


class BudgetBreachResponse(BaseModel):
    tenant_id: str
    monthly_budget_eur: float
    breach_date_baseline: Optional[str] = None
    breach_date_optimized: Optional[str] = None
    safe_if_actioned: bool
    notes: list[str] = Field(default_factory=list)
