"""Business-intelligence router — anomaly detection, chargeback, insights."""
from __future__ import annotations
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.insights import (
    AnomalyResponse, AnomalyModel, AnomalyDriverModel,
    ChargebackResponse, AllocationGroupModel,
    InsightDigestResponse, InsightModel,
)
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services import anomaly as anomaly_svc
from app.services import chargeback as cb_svc
from app.services import insights as insights_svc
from app.services import forecast as fc_svc

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/insights", tags=["insights"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _cr() -> str:
    return get_settings().cosmos_container_cost_records


def _wi() -> str:
    return get_settings().cosmos_container_waste_items


def _tn() -> str:
    return get_settings().cosmos_container_tenants


async def _daily_series(tenant_id: str, days: int) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = await cosmos.query_items(
        _cr(),
        """SELECT c.record_date, SUM(c.cost_eur) AS daily_cost
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e
           GROUP BY c.record_date""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}],
        partition_key=tenant_id,
    )
    rows.sort(key=lambda r: r.get("record_date", ""))
    return [{"date": r["record_date"], "cost_eur": round(r["daily_cost"], 2)} for r in rows]


async def _per_day_service_breakdown(tenant_id: str, days: int) -> dict:
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = await cosmos.query_items(
        _cr(),
        """SELECT c.record_date, c.service_name, SUM(c.cost_eur) AS cost
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e
           GROUP BY c.record_date, c.service_name""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}],
        partition_key=tenant_id,
    )
    out: dict = {}
    for r in rows:
        d = r["record_date"]
        out.setdefault(d, {}).setdefault("service", {})[r["service_name"]] = round(r["cost"], 2)
    return out


async def _cost_records_with_tags(tenant_id: str, days: int) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return await cosmos.query_items(
        _cr(),
        """SELECT c.cost_eur, c.tags, c.resource_id
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}],
        partition_key=tenant_id,
    )


async def _open_waste(tenant_id: str) -> list[dict]:
    return await cosmos.query_items(
        _wi(),
        """SELECT c.saving_eur, c.priority, c.waste_type, c.resource_name
           FROM c WHERE c.tenant_id=@t AND c.type='waste_item'
           AND (NOT IS_DEFINED(c.resolved_at) OR c.resolved_at=null)""",
        parameters=[{"name": "@t", "value": tenant_id}],
        partition_key=tenant_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Anomaly detection
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/anomalies", response_model=AnomalyResponse)
async def get_anomalies(
    tenant_id: str,
    scan_last_days: int = Query(14, ge=1, le=60),
    method: str = Query(
        "holt_winters",
        pattern="^(holt_winters|isolation_forest|ensemble)$",
        description="Detection model: holt_winters, isolation_forest, or ensemble (both).",
    ),
) -> AnomalyResponse:
    """Detect spend anomalies with choice of model: holt_winters, isolation_forest, or ensemble."""
    try:
        daily = await _daily_series(tenant_id, 90)
        breakdowns = await _per_day_service_breakdown(tenant_id, 90)
        if method == "isolation_forest":
            res = anomaly_svc.detect_anomalies_with_isolation_forest(daily, scan_last_days, breakdowns)
        elif method == "ensemble":
            res = anomaly_svc.detect_anomalies_ensemble(daily, scan_last_days, breakdowns)
        else:
            res = anomaly_svc.detect_anomalies(daily, scan_last_days, breakdowns)
        return AnomalyResponse(
            tenant_id=tenant_id, method=res.method, scanned_days=res.scanned_days,
            total_anomalous_excess_eur=res.total_anomalous_excess_eur,
            anomalies=[
                AnomalyModel(
                    day=a.day, actual_eur=a.actual_eur, expected_eur=a.expected_eur,
                    excess_eur=a.excess_eur, direction=a.direction, severity=a.severity,
                    z_score=a.z_score,
                    drivers=[AnomalyDriverModel(**d.__dict__) for d in a.drivers],
                ) for a in res.anomalies
            ],
            notes=res.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
# Chargeback / showback
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/chargeback", response_model=ChargebackResponse)
async def get_chargeback(
    tenant_id: str,
    dimension: str = Query("cost_center", description="Tag key to allocate by"),
    strategy: str = Query("proportional", pattern="^(showback|proportional|even)$"),
    days: int = Query(30, ge=1, le=90),
) -> ChargebackResponse:
    """Allocate spend to cost-centers by tag, with shared-cost distribution."""
    try:
        end = date.today()
        start = end - timedelta(days=days - 1)
        records = await _cost_records_with_tags(tenant_id, days)
        res = cb_svc.allocate(
            records, dimension=dimension,
            strategy=cb_svc.AllocationStrategy(strategy),
            period_start=start.isoformat(), period_end=end.isoformat(),
        )
        return ChargebackResponse(
            tenant_id=tenant_id, dimension=res.dimension, strategy=res.strategy,
            period_start=res.period_start, period_end=res.period_end,
            total_spend_eur=res.total_spend_eur, tagged_spend_eur=res.tagged_spend_eur,
            untagged_spend_eur=res.untagged_spend_eur,
            tagging_coverage_pct=res.tagging_coverage_pct,
            groups=[AllocationGroupModel(**g.__dict__) for g in res.groups],
            notes=res.notes,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
# Business insights digest (the consolidation layer)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/digest", response_model=InsightDigestResponse)
async def get_insight_digest(tenant_id: str) -> InsightDigestResponse:
    """
    The flagship endpoint: fuse waste, anomalies, chargeback, and forecast into a
    ranked, business-language digest with an executive summary.
    """
    try:
        # tenant name
        try:
            tdoc = await cosmos.get_item(_tn(), tenant_id, tenant_id)
            tenant_name = tdoc.get("tenant_name", tenant_id)
        except NotFoundError:
            tenant_name = tenant_id

        daily = await _daily_series(tenant_id, 90)
        monthly_spend = sum(d["cost_eur"] for d in daily[-30:])
        waste = await _open_waste(tenant_id)
        breakdowns = await _per_day_service_breakdown(tenant_id, 90)
        records = await _cost_records_with_tags(tenant_id, 30)

        anomalies = anomaly_svc.detect_anomalies(daily, 14, breakdowns).anomalies
        chargeback = cb_svc.allocate(records, "cost_center",
                                     cb_svc.AllocationStrategy.PROPORTIONAL)
        fc = fc_svc.forecast_spend(daily, horizon_days=30)

        digest = insights_svc.synthesize(
            tenant_id=tenant_id, tenant_name=tenant_name,
            monthly_spend=monthly_spend, waste_items=waste,
            anomalies=anomalies, chargeback=chargeback,
            forecast_month_end=fc.month_end_projection,
        )
        return InsightDigestResponse(
            tenant_id=digest.tenant_id, monthly_spend_eur=digest.monthly_spend_eur,
            monthly_recoverable_eur=digest.monthly_recoverable_eur,
            efficiency_score=digest.efficiency_score,
            headline_summary=digest.headline_summary,
            headline_summary_it=digest.headline_summary_it,
            insights=[InsightModel(**i.__dict__) for i in digest.insights],
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
