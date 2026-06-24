"""
Drill-down router — /api/v1/drilldown
=====================================

Walks the multi-cloud hierarchy:

  Portfolio → Provider → Account/Subscription → Service → Resource → (detail)

One endpoint takes the current path (the filters chosen so far) and a `level`
(the dimension to group the children by) and returns the aggregated children
with spend + waste, sorted by spend. At the resource level it also flags which
specific resources are cost anomalies, so a user can drill straight to the
resource that is the problem.
"""
from __future__ import annotations
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.anomaly import detect_resource_anomalies

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/drilldown", tags=["drilldown"],
    dependencies=[Depends(rate_limit_tenant)],
)

# the hierarchy, in order; each maps to a FOCUS field
_LEVEL_FIELD = {
    "provider": "provider_name",
    "account": "sub_account_id",
    "service": "service_name",
    "resource": "resource_id",
}
_NEXT_LEVEL = {"provider": "account", "account": "service",
               "service": "resource", "resource": None}


def _fr() -> str:
    return get_settings().cosmos_container_cost_records


def _wi() -> str:
    return get_settings().cosmos_container_waste_items


def _filter_clause(filters: dict) -> tuple[str, list]:
    clauses, params = [], []
    for i, (field, value) in enumerate(filters.items()):
        clauses.append(f"c.{field}=@f{i}")
        params.append({"name": f"@f{i}", "value": value})
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


@router.get("/{tenant_id}")
async def drilldown(
    tenant_id: str,
    level: str = Query("provider", pattern="^(provider|account|service|resource)$"),
    provider: str | None = None,
    account: str | None = None,
    service: str | None = None,
    days: int = Query(30, ge=1, le=90),
) -> dict:
    """
    Aggregate FOCUS spend at `level`, filtered by any parent selections.
    At the resource level, resource anomalies are flagged inline.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)

    # build parent filters from the path provided
    filters: dict = {}
    if provider:
        filters["provider_name"] = provider
    if account:
        filters["sub_account_id"] = account
    if service:
        filters["service_name"] = service

    group_field = _LEVEL_FIELD[level]
    where, fparams = _filter_clause(filters)
    base_params = [
        {"name": "@t", "value": tenant_id},
        {"name": "@s", "value": start.isoformat()},
        {"name": "@e", "value": end.isoformat()},
    ] + fparams

    try:
        # aggregate spend by the requested level
        rows = await cosmos.query_items(
            _fr(),
            f"""SELECT c.{group_field} AS key, SUM(c.effective_cost) AS spend,
                       COUNT(1) AS records
                FROM c WHERE c.tenant_id=@t AND c.type='focus_record'
                AND c.charge_period_start>=@s AND c.charge_period_start<=@e{where}
                GROUP BY c.{group_field}""",
            parameters=base_params, partition_key=tenant_id,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    total = sum(float(r.get("spend", 0.0)) for r in rows)
    children = []
    for r in rows:
        spend = float(r.get("spend", 0.0))
        children.append({
            "key": r.get("key") or "(none)",
            "spend_eur": round(spend, 2),
            "pct": round(spend / total * 100, 1) if total > 0 else 0.0,
            "records": r.get("records", 0),
            "has_children": _NEXT_LEVEL[level] is not None,
            "next_level": _NEXT_LEVEL[level],
        })
    children.sort(key=lambda c: c["spend_eur"], reverse=True)

    result = {
        "tenant_id": tenant_id, "level": level, "filters": filters,
        "next_level": _NEXT_LEVEL[level], "total_eur": round(total, 2),
        "children": children,
    }

    # at the resource level, flag anomalies on the specific resources shown
    if level == "resource" and children:
        anomalies = await _resource_anomalies(tenant_id, filters, days)
        anom_by_id = {a["resource_id"]: a for a in anomalies}
        for c in children:
            a = anom_by_id.get(c["key"])
            c["anomaly"] = a   # None or {severity, excess_eur, day, z_score, ...}
        result["anomaly_count"] = len(anomalies)

    return result


async def _resource_anomalies(tenant_id: str, filters: dict, days: int) -> list[dict]:
    """Build per-resource daily series under the current filter and detect spikes."""
    end = date.today()
    start = end - timedelta(days=max(days, 30) - 1)
    where, fparams = _filter_clause(filters)
    rows = await cosmos.query_items(
        _fr(),
        f"""SELECT c.resource_id, c.resource_name, c.provider_name, c.sub_account_id,
                   c.service_name, c.charge_period_start AS day,
                   SUM(c.effective_cost) AS cost
            FROM c WHERE c.tenant_id=@t AND c.type='focus_record'
            AND c.charge_period_start>=@s AND c.charge_period_start<=@e{where}
            AND IS_DEFINED(c.resource_id) AND c.resource_id != ''
            GROUP BY c.resource_id, c.resource_name, c.provider_name,
                     c.sub_account_id, c.service_name, c.charge_period_start""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}] + fparams,
        partition_key=tenant_id,
    )
    series: dict = {}
    for r in rows:
        rid = r["resource_id"]
        blob = series.setdefault(rid, {"meta": {
            "resource_name": r.get("resource_name", ""),
            "provider_name": r.get("provider_name", ""),
            "sub_account_id": r.get("sub_account_id", ""),
            "service_name": r.get("service_name", "")}, "daily": []})
        blob["daily"].append({"date": r["day"], "cost_eur": float(r.get("cost", 0.0))})

    res = detect_resource_anomalies(series, scan_last_days=3)
    return [{
        "resource_id": a.resource_id, "resource_name": a.resource_name,
        "provider_name": a.provider_name, "sub_account_id": a.sub_account_id,
        "service_name": a.service_name, "day": a.day, "actual_eur": a.actual_eur,
        "expected_eur": a.expected_eur, "excess_eur": a.excess_eur,
        "z_score": a.z_score, "severity": a.severity, "method": a.method,
    } for a in res.anomalies]


@router.get("/{tenant_id}/resource-anomalies")
async def resource_anomalies(
    tenant_id: str,
    provider: str | None = None,
    account: str | None = None,
    days: int = Query(30, ge=1, le=90),
) -> dict:
    """Standalone list of resource-level anomalies across the (optionally filtered) estate."""
    filters: dict = {}
    if provider:
        filters["provider_name"] = provider
    if account:
        filters["sub_account_id"] = account
    try:
        anomalies = await _resource_anomalies(tenant_id, filters, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {
        "tenant_id": tenant_id, "filters": filters,
        "flagged": len(anomalies),
        "total_excess_eur": round(sum(a["excess_eur"] for a in anomalies), 2),
        "anomalies": anomalies,
    }
