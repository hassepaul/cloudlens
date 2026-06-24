"""Costs query router — /api/v1/costs"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.models.cost import CostSummary, CostBreakdown, CostTrend
from app.rate_limit import rate_limit_tenant
from app.services import cosmos

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/costs", tags=["costs"], dependencies=[Depends(rate_limit_tenant)]
)

VALID_DIMENSIONS = {"service", "resource_group", "tag", "location"}
# Dimensions supported by Cosmos DB GROUP BY (tag requires client-side aggregation).
_GROUPBY_DIMENSIONS = {"service", "resource_group", "location"}


def _cr_container() -> str:
    return get_settings().cosmos_container_cost_records


def _default_period() -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=29)
    return start, end


@router.get("/{tenant_id}", response_model=CostSummary)
async def get_cost_summary(
    tenant_id: str,
    start: Optional[date] = Query(None, description="ISO date, default: 30 days ago"),
    end: Optional[date] = Query(None, description="ISO date, default: today"),
) -> CostSummary:
    """Return aggregated cost summary for a tenant."""
    period_start, period_end = start or _default_period()[0], end or _default_period()[1]
    if period_start >= period_end:
        raise HTTPException(status_code=422, detail="start must be before end")

    try:
        rows = await cosmos.query_items(
            _cr_container(),
            """SELECT c.service_name, SUM(c.cost_eur) AS total
               FROM c
               WHERE c.tenant_id = @tid
               AND c.record_date >= @start AND c.record_date <= @end
               GROUP BY c.service_name""",
            parameters=[
                {"name": "@tid", "value": tenant_id},
                {"name": "@start", "value": period_start.isoformat()},
                {"name": "@end", "value": period_end.isoformat()},
            ],
            partition_key=tenant_id,
        )
        # Cosmos does not support ORDER BY on an aggregate alias in a GROUP BY
        # query, so sort and slice the top 10 services client-side.
        rows.sort(key=lambda r: r.get("total", 0.0), reverse=True)
        rows = rows[:10]
        total = sum(r.get("total", 0.0) for r in rows)

        # Previous period for change %
        delta = (period_end - period_start).days + 1
        prev_start = period_start - timedelta(days=delta)
        prev_end = period_start - timedelta(days=1)
        prev_rows = await cosmos.query_items(
            _cr_container(),
            "SELECT VALUE SUM(c.cost_eur) FROM c WHERE c.tenant_id = @tid AND c.record_date >= @start AND c.record_date <= @end",
            parameters=[
                {"name": "@tid", "value": tenant_id},
                {"name": "@start", "value": prev_start.isoformat()},
                {"name": "@end", "value": prev_end.isoformat()},
            ],
            partition_key=tenant_id,
        )
        prev_total = float(prev_rows[0]) if prev_rows else None
        change_pct = None
        if prev_total is not None and prev_total > 0:
            change_pct = round((total - prev_total) / prev_total * 100, 1)

        return CostSummary(
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
            total_cost_eur=round(total, 2),
            previous_period_cost_eur=round(prev_total, 2) if prev_total is not None else None,
            change_pct=change_pct,
            top_services=[{"service": r["service_name"], "cost_eur": round(r["total"], 2)} for r in rows],
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/breakdown", response_model=CostBreakdown)
async def get_cost_breakdown(
    tenant_id: str,
    dimension: str = Query("service", description="service | resource_group | location"),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
) -> CostBreakdown:
    if dimension not in VALID_DIMENSIONS:
        raise HTTPException(status_code=422, detail=f"dimension must be one of {VALID_DIMENSIONS}")
    if dimension == "tag":
        raise HTTPException(
            status_code=501,
            detail={
                "error": "NOT_IMPLEMENTED",
                "message": (
                    "Tag-dimension breakdown is not supported on this endpoint — "
                    "Cosmos DB cannot GROUP BY a JSON sub-property. "
                    "Use GET /api/v1/insights/{tenant_id}/chargeback?dimension=<tag_key> instead."
                ),
            },
        )
    period_start, period_end = start or _default_period()[0], end or _default_period()[1]

    col_map = {
        "service": "c.service_name",
        "resource_group": "c.resource_group",
        "location": "c.location",
    }
    col = col_map[dimension]  # dimension is now guaranteed to be in col_map

    try:
        rows = await cosmos.query_items(
            _cr_container(),
            f"""SELECT {col} AS dim_value, SUM(c.cost_eur) AS total
                FROM c
                WHERE c.tenant_id = @tid
                AND c.record_date >= @start AND c.record_date <= @end
                GROUP BY {col}""",
            parameters=[
                {"name": "@tid", "value": tenant_id},
                {"name": "@start", "value": period_start.isoformat()},
                {"name": "@end", "value": period_end.isoformat()},
            ],
            partition_key=tenant_id,
        )
        rows.sort(key=lambda r: r.get("total", 0.0), reverse=True)
        rows = rows[:50]
        return CostBreakdown(
            tenant_id=tenant_id,
            dimension=dimension,
            period_start=period_start,
            period_end=period_end,
            items=[{"label": r.get("dim_value", "unknown"), "cost_eur": round(r.get("total", 0), 2)} for r in rows],
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/trend", response_model=CostTrend)
async def get_cost_trend(
    tenant_id: str,
    days: int = Query(30, ge=7, le=90),
) -> CostTrend:
    end = date.today()
    start = end - timedelta(days=days - 1)
    try:
        rows = await cosmos.query_items(
            _cr_container(),
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
        data_points = [{"date": r["record_date"], "cost_eur": round(r["daily_cost"], 2)} for r in rows]
        costs = [p["cost_eur"] for p in data_points]
        avg = sum(costs) / len(costs) if costs else 0.0
        peak_idx = costs.index(max(costs)) if costs else 0
        return CostTrend(
            tenant_id=tenant_id,
            days=days,
            data_points=data_points,
            average_daily_eur=round(avg, 2),
            peak_day=data_points[peak_idx]["date"] if data_points else None,
            peak_cost_eur=data_points[peak_idx]["cost_eur"] if data_points else 0.0,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
