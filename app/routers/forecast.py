"""Forecast & predictive-analysis router — /api/v1/forecast"""
from __future__ import annotations
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.models.forecast import (
    SpendForecastResponse, ForecastPointModel, TrajectoryResponse,
    RoadmapResponse, RoadmapPhaseModel, BudgetBreachResponse,
    AnnualForecastResponse,
)
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services import forecast as fc
from app.services import rollups

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/forecast", tags=["forecast"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _cr() -> str:
    return get_settings().cosmos_container_cost_records


def _wi() -> str:
    return get_settings().cosmos_container_waste_items


async def _daily_series(tenant_id: str, days: int) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = await cosmos.query_items(
        _cr(),
        """SELECT c.record_date, SUM(c.cost_eur) AS daily_cost
           FROM c
           WHERE c.tenant_id = @tid
           AND c.record_date >= @start AND c.record_date <= @end
           GROUP BY c.record_date""",
        parameters=[
            {"name": "@tid", "value": tenant_id},
            {"name": "@start", "value": start.isoformat()},
            {"name": "@end", "value": end.isoformat()},
        ],
        partition_key=tenant_id,
    )
    rows.sort(key=lambda r: r.get("record_date", ""))
    return [{"date": r["record_date"], "cost_eur": round(r["daily_cost"], 2)} for r in rows]


async def _open_waste(tenant_id: str) -> list[dict]:
    rows = await cosmos.query_items(
        _wi(),
        """SELECT c.saving_eur, c.priority, c.waste_type
           FROM c
           WHERE c.tenant_id = @tid AND c.type = 'waste_item'
           AND (NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null)""",
        parameters=[{"name": "@tid", "value": tenant_id}],
        partition_key=tenant_id,
    )
    return rows


def _pts(points) -> list[ForecastPointModel]:
    return [ForecastPointModel(day=p.day, value=p.value, lower=p.lower, upper=p.upper) for p in points]


@router.get("/{tenant_id}", response_model=SpendForecastResponse)
async def get_spend_forecast(
    tenant_id: str,
    horizon_days: int = Query(30, ge=7, le=90),
    history_days: int = Query(90, ge=14, le=90),
) -> SpendForecastResponse:
    """Baseline spend forecast (Holt-Winters, weekly seasonality, with backtest MAPE).

    When >= 13 months of persisted monthly rollups exist, a month-of-year
    (annual) seasonal overlay is applied on top of the weekly forecast.
    """
    try:
        daily = await _daily_series(tenant_id, history_days)
        # Opportunistically persist sealed monthly totals so annual history
        # accumulates over time, then load whatever rollups we have.
        await rollups.persist_monthly_rollups(tenant_id, daily)
        monthly = await rollups.get_monthly_rollups(tenant_id)
        result = fc.forecast_spend(daily, horizon_days=horizon_days, monthly_history=monthly)
        return SpendForecastResponse(
            tenant_id=tenant_id, method=result.method, horizon_days=result.horizon_days,
            history_days=result.history_days, mape=result.mape, confidence=result.confidence,
            month_end_projection=result.month_end_projection,
            annual_seasonality=result.method.endswith("+annual"),
            points=_pts(result.points), notes=result.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/annual", response_model=AnnualForecastResponse)
async def get_annual_forecast(
    tenant_id: str,
    horizon_months: int = Query(12, ge=1, le=36),
) -> AnnualForecastResponse:
    """Long-range MONTHLY forecast with annual seasonality (Holt-Winters period=12).

    Uses persisted monthly rollups. Needs >= 24 months for a full seasonal
    model; with less history it returns a trend-only monthly projection and
    flags low confidence.
    """
    try:
        # Refresh rollups from the last 90 days first so the current window is
        # captured, then forecast on the full persisted monthly history.
        daily = await _daily_series(tenant_id, 90)
        await rollups.persist_monthly_rollups(tenant_id, daily)
        monthly = await rollups.get_monthly_rollups(tenant_id, months=36)
        result = fc.forecast_monthly(monthly, horizon_months=horizon_months)
        return AnnualForecastResponse(
            tenant_id=tenant_id, method=result.method,
            horizon_months=result.horizon_days, history_months=result.history_days,
            mape=result.mape, confidence=result.confidence,
            months=_pts(result.points), notes=result.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/cost-of-inaction", response_model=TrajectoryResponse)
async def get_cost_of_inaction(
    tenant_id: str,
    horizon_days: int = Query(30, ge=7, le=90),
) -> TrajectoryResponse:
    """Dual-trajectory forecast: do-nothing vs. if-you-act, with cost-of-inaction."""
    try:
        daily = await _daily_series(tenant_id, 90)
        waste = await _open_waste(tenant_id)
        baseline = fc.forecast_spend(daily, horizon_days=horizon_days)
        traj = fc.cost_of_inaction(baseline, waste, horizon_days=horizon_days)
        return TrajectoryResponse(
            tenant_id=tenant_id, horizon_days=traj.horizon_days,
            daily_waste_burn_eur=traj.daily_waste_burn_eur,
            cumulative_inaction_eur=traj.cumulative_inaction_eur,
            monthly_recoverable_eur=traj.monthly_recoverable_eur,
            annual_recoverable_eur=traj.annual_recoverable_eur,
            baseline=_pts(traj.baseline), optimized=_pts(traj.optimized),
            notes=traj.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/roadmap", response_model=RoadmapResponse)
async def get_remediation_roadmap(tenant_id: str) -> RoadmapResponse:
    """ROI-ordered, phased remediation roadmap with run-rate bending down per phase."""
    try:
        daily = await _daily_series(tenant_id, 30)
        monthly_spend = sum(d["cost_eur"] for d in daily)
        waste = await _open_waste(tenant_id)
        rr = fc.remediation_roadmap(monthly_spend, waste)
        return RoadmapResponse(
            tenant_id=tenant_id,
            current_run_rate_eur=rr.current_run_rate_eur,
            optimized_run_rate_eur=rr.optimized_run_rate_eur,
            total_monthly_saving_eur=rr.total_monthly_saving_eur,
            phases=[RoadmapPhaseModel(**ph.__dict__) for ph in rr.phases],
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/budget-breach", response_model=BudgetBreachResponse)
async def get_budget_breach(
    tenant_id: str,
    monthly_budget: float = Query(..., gt=0, description="Monthly budget in EUR"),
    horizon_days: int = Query(60, ge=7, le=90),
) -> BudgetBreachResponse:
    """Predict the date cumulative spend breaches the budget, do-nothing vs. if-actioned."""
    try:
        daily = await _daily_series(tenant_id, 90)
        waste = await _open_waste(tenant_id)
        baseline = fc.forecast_spend(daily, horizon_days=horizon_days)
        traj = fc.cost_of_inaction(baseline, waste, horizon_days=horizon_days)
        bb = fc.budget_breach(monthly_budget, baseline, traj)
        return BudgetBreachResponse(
            tenant_id=tenant_id, monthly_budget_eur=bb.monthly_budget_eur,
            breach_date_baseline=bb.breach_date_baseline,
            breach_date_optimized=bb.breach_date_optimized,
            safe_if_actioned=bb.safe_if_actioned, notes=bb.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
