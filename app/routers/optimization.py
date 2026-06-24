"""Optimization router — /api/v1/optimization (rightsizing, scheduling, utilization, savings)."""
from __future__ import annotations
from datetime import date, timedelta, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Depends, status

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services import rightsizing as rs_svc
from app.services import scheduling as sched_svc
from app.services import utilization as util_svc
from app.models.savings import (
    SavingsRecord, SavingsRecordCreate, SavingsLedger, SavingsStatus,
)

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/optimization", tags=["optimization"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _fr() -> str:
    return get_settings().cosmos_container_cost_records


def _wi() -> str:
    return get_settings().cosmos_container_waste_items


async def _utilization_resources(tenant_id: str, days: int) -> list[dict]:
    """
    Pull per-resource cost + utilization. Utilization (cpu/mem peak) is expected
    to be persisted on the focus/cost records by the ingest job's metrics
    enrichment; resources without metrics are returned with zeros and skipped by
    the engines that require them.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = await cosmos.query_items(
        _fr(),
        """SELECT c.resource_id, c.resource_name, c.provider_name, c.service_name,
                  c.instance_type, c.environment,
                  MAX(c.cpu_peak_pct) AS cpu_peak_pct, MAX(c.mem_peak_pct) AS mem_peak_pct,
                  SUM(c.effective_cost) AS monthly_eur
           FROM c WHERE c.tenant_id=@t AND c.type='focus_record'
           AND c.charge_period_start>=@s AND c.charge_period_start<=@e
           AND IS_DEFINED(c.resource_id) AND c.resource_id != ''
           GROUP BY c.resource_id, c.resource_name, c.provider_name, c.service_name,
                    c.instance_type, c.environment""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}],
        partition_key=tenant_id,
    )
    out = []
    for r in rows:
        out.append({
            "resource_id": r.get("resource_id", ""),
            "resource_name": r.get("resource_name", ""),
            "provider": r.get("provider_name", ""),
            "service": r.get("service_name", ""),
            "instance_type": r.get("instance_type", ""),
            "environment": r.get("environment", ""),
            "cpu_peak_pct": float(r.get("cpu_peak_pct") or 0.0),
            "mem_peak_pct": float(r.get("mem_peak_pct") or 0.0),
            "monthly_eur": float(r.get("monthly_eur") or 0.0),
        })
    return out


# ── Rightsizing ──────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/rightsizing")
async def rightsizing(
    tenant_id: str,
    headroom: float = Query(0.30, ge=0.0, le=1.0),
    days: int = Query(30, ge=7, le=90),
) -> dict:
    """CPU+memory rightsizing recommendations, including cross-family downgrades."""
    try:
        resources = await _utilization_resources(tenant_id, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    for r in resources:
        r["observation_days"] = days
        r["samples"] = days
    res = rs_svc.recommend(resources, headroom=headroom)
    return {
        "tenant_id": tenant_id, "scanned": res.scanned,
        "total_monthly_saving_eur": res.total_monthly_saving_eur,
        "recommendations": [r.__dict__ for r in res.recommendations],
        "notes": res.notes,
    }


# ── Scheduling ───────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/scheduling")
async def scheduling(
    tenant_id: str,
    style: str = Query("business", pattern="^(business|extended)$"),
    days: int = Query(30, ge=7, le=90),
) -> dict:
    """On/off schedule recommendations for non-prod 24/7 resources."""
    try:
        resources = await _utilization_resources(tenant_id, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    for r in resources:
        r["currently_runs_247"] = True
    res = sched_svc.recommend(resources, schedule_style=style)
    return {
        "tenant_id": tenant_id, "scanned": res.scanned,
        "total_monthly_saving_eur": res.total_monthly_saving_eur,
        "recommendations": [r.__dict__ for r in res.recommendations],
        "notes": res.notes,
    }


# ── Utilization ──────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/utilization")
async def utilization(
    tenant_id: str,
    days: int = Query(30, ge=7, le=90),
) -> dict:
    """Estate-wide CPU/memory utilization with over-capacity scoring."""
    try:
        resources = await _utilization_resources(tenant_id, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    rows, summary = util_svc.analyze(resources)
    return {
        "tenant_id": tenant_id,
        "summary": {
            "resources": summary.resources, "avg_cpu_pct": summary.avg_cpu_pct,
            "avg_mem_pct": summary.avg_mem_pct, "over_provisioned_count": summary.over_provisioned_count,
            "idle_count": summary.idle_count, "hot_count": summary.hot_count,
            "reclaimable_monthly_eur": summary.reclaimable_monthly_eur,
            "by_band": summary.by_band,
        },
        "resources": [r.__dict__ for r in rows],
    }


# ── Realized-savings ledger ──────────────────────────────────────────────────

@router.post("/{tenant_id}/savings", response_model=SavingsRecord, status_code=status.HTTP_201_CREATED)
async def create_savings_record(tenant_id: str, payload: SavingsRecordCreate) -> SavingsRecord:
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=422, detail={
            "error": "VALIDATION_ERROR", "message": "Body tenant_id must match path"})
    try:
        rec = SavingsRecord(**payload.model_dump())
        await cosmos.upsert_item(_wi(), rec.to_cosmos())
        return rec
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/savings/{record_id}/action", response_model=SavingsRecord)
async def mark_actioned(tenant_id: str, record_id: str) -> SavingsRecord:
    """Mark a saving as actioned; records the baseline cost for later measurement."""
    try:
        doc = await cosmos.get_item(_wi(), record_id, tenant_id)
        if doc.get("type") != "savings_record":
            raise NotFoundError(f"Savings record {record_id} not found")
        rec = SavingsRecord.from_cosmos(doc)
        rec.status = SavingsStatus.ACTIONED
        rec.actioned_at = datetime.now(timezone.utc)
        # baseline (pre-action cost) is captured by the ingest job's next run so
        # realized saving can be measured against it; left as-is here if unknown.
        await cosmos.upsert_item(_wi(), rec.to_cosmos())
        return rec
    except NotFoundError:
        raise HTTPException(status_code=404, detail={
            "error": "NOT_FOUND", "message": f"Savings record {record_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/savings/ledger", response_model=SavingsLedger)
async def savings_ledger(tenant_id: str) -> SavingsLedger:
    """Roll up identified vs actioned vs realized savings — closes the ROI loop."""
    try:
        docs = await cosmos.query_items(
            _wi(), "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='savings_record'",
            parameters=[{"name": "@t", "value": tenant_id}], partition_key=tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    identified = actioned = realized = 0.0
    by_cat: dict = {}
    for d in docs:
        rec = SavingsRecord.from_cosmos(d)
        cat = rec.category.value
        by_cat.setdefault(cat, {"identified": 0.0, "actioned": 0.0, "realized": 0.0})
        if rec.status == SavingsStatus.IDENTIFIED:
            identified += rec.estimated_monthly_eur
            by_cat[cat]["identified"] += rec.estimated_monthly_eur
        elif rec.status == SavingsStatus.ACTIONED:
            actioned += rec.estimated_monthly_eur
            by_cat[cat]["actioned"] += rec.estimated_monthly_eur
        elif rec.status == SavingsStatus.REALIZED:
            val = rec.realized_monthly_eur if rec.realized_monthly_eur is not None else rec.estimated_monthly_eur
            realized += val
            by_cat[cat]["realized"] += val

    denom = actioned + realized
    return SavingsLedger(
        tenant_id=tenant_id,
        identified_monthly_eur=round(identified, 2),
        actioned_monthly_eur=round(actioned, 2),
        realized_monthly_eur=round(realized, 2),
        realized_annual_eur=round(realized * 12, 2),
        realization_rate_pct=round(realized / denom * 100, 1) if denom > 0 else 0.0,
        by_category={k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by_cat.items()},
        record_count=len(docs),
    )
