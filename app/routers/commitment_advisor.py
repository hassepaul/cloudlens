"""Commitment Advisor router — calendar-aware RI / Savings Plan recommendations."""
from __future__ import annotations
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.rate_limit import rate_limit_tenant
from app.services.commitment_advisor import generate_advisories, CommitmentAdvisoryReport
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/commitment-advisor",
    tags=["commitment-advisor"],
    dependencies=[Depends(rate_limit_tenant)],
)


class PlannedEvent(BaseModel):
    date: str
    description: str = ""

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("date must be ISO-8601 (YYYY-MM-DD)") from exc
        return v


class AdvisoryRequest(BaseModel):
    lookback_days: int = 90
    planned_events: list[PlannedEvent] = []

    @field_validator("lookback_days")
    @classmethod
    def _check_lookback(cls, v: int) -> int:
        if not (14 <= v <= 365):
            raise ValueError("lookback_days must be between 14 and 365")
        return v


def _to_dict(report: CommitmentAdvisoryReport) -> dict:
    return {
        "tenant_id": report.tenant_id,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "total_on_demand_eligible_eur": report.total_on_demand_eligible_eur,
        "total_estimated_saving_eur": report.total_estimated_saving_eur,
        "advisories": [
            {
                "service": a.service,
                "cloud": a.cloud,
                "current_monthly_eur": a.current_monthly_eur,
                "on_demand_monthly_eur": a.on_demand_monthly_eur,
                "recommended_type": a.recommended_type,
                "commitment_horizon_months": a.commitment_horizon_months,
                "estimated_monthly_saving_eur": a.estimated_monthly_saving_eur,
                "saving_pct": a.saving_pct,
                "confidence_score": a.confidence_score,
                "confidence_label": a.confidence_label,
                "timing": a.timing,
                "wait_months": a.wait_months,
                "earliest_commit_date": a.earliest_commit_date,
                "stability_score": a.stability_score,
                "trend_direction": a.trend_direction,
                "trend_pct_30d": a.trend_pct_30d,
                "forecast_mape": a.forecast_mape,
                "calendar_notes": a.calendar_notes,
                "rationale": a.rationale,
            }
            for a in report.advisories
        ],
        "notes": report.notes,
    }


@router.get("/{tenant_id}")
async def get_advisories(
    tenant_id: str,
    lookback_days: int = Query(default=90, ge=14, le=365),
) -> dict:
    """
    Return smart commitment (RI / Savings Plan) advisories for a tenant.

    Advisories are ranked by estimated monthly saving and annotated with:
    - stability score (0–1, from Holt-Winters volatility analysis)
    - trend direction and 30-day change %
    - confidence score and timing recommendation (commit_now vs wait)
    - calendar notes (weekday patterns, planned events)
    """
    report = await generate_advisories(tenant_id, lookback_days=lookback_days)
    return _to_dict(report)


@router.post("/{tenant_id}")
async def get_advisories_with_events(
    tenant_id: str,
    body: AdvisoryRequest,
) -> dict:
    """
    Same as GET but accepts a JSON body with planned_events to factor
    into the timing recommendation (e.g. upcoming migrations).
    """
    events = [{"date": e.date, "description": e.description} for e in body.planned_events]
    report = await generate_advisories(
        tenant_id,
        lookback_days=body.lookback_days,
        planned_events=events,
    )
    return _to_dict(report)
