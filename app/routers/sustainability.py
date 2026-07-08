"""
Sustainability / CO₂ Emissions Router
======================================

GET /api/v1/sustainability/{tenant_id}/summary
GET /api/v1/sustainability/{tenant_id}/by-region
GET /api/v1/sustainability/{tenant_id}/by-service
GET /api/v1/sustainability/{tenant_id}/trend
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.rate_limit import rate_limit_tenant
from app.logging_config import get_logger
from app.services.sustainability import (
    get_emissions_summary,
    get_emissions_by_region,
    get_emissions_by_service,
    get_emissions_trend,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/sustainability",
    tags=["sustainability"],
    dependencies=[Depends(rate_limit_tenant)],
)


@router.get("/{tenant_id}/summary")
async def emissions_summary(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=365, description="Days of history to analyse"),
) -> dict:
    """
    Headline CO₂ totals with per-cloud breakdown.

    Returns total kgCO₂e / tCO₂e for the period, split by cloud provider,
    plus the top-emitting region and service.
    """
    return await get_emissions_summary(tenant_id, lookback_days=lookback_days)


@router.get("/{tenant_id}/by-region")
async def emissions_by_region(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=365),
    top_n: int = Query(default=15, ge=1, le=50),
) -> list[dict]:
    """
    Top emitting cloud regions with grid intensity metadata.
    Useful for identifying low-hanging fruit: migrating workloads from
    high-intensity (coal) regions to low-intensity (hydro/nuclear) regions.
    """
    return await get_emissions_by_region(
        tenant_id, lookback_days=lookback_days, top_n=top_n
    )


@router.get("/{tenant_id}/by-service")
async def emissions_by_service(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=365),
    top_n: int = Query(default=15, ge=1, le=50),
) -> list[dict]:
    """
    Top emitting cloud services with kg_co2e per EUR of spend.
    Highlights carbon-inefficient service choices.
    """
    return await get_emissions_by_service(
        tenant_id, lookback_days=lookback_days, top_n=top_n
    )


@router.get("/{tenant_id}/trend")
async def emissions_trend(
    tenant_id: str,
    days: int = Query(default=30, ge=7, le=365, description="Number of days for trend"),
) -> list[dict]:
    """
    Daily kgCO₂e timeseries.  Use to track progress against reduction targets.
    """
    return await get_emissions_trend(tenant_id, days=days)
