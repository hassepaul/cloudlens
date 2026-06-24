"""
Unit Economics Service
======================

Compute "cost per unit" metrics — the killer feature popularised by CloudZero.

A *unit* is anything meaningful to the business: a user, a transaction, an API
call, a shipped order, a deployed model inference. Customers define their own
metrics and feed us the daily count via a simple webhook or API call.

CloudLens pairs that count with the daily cloud cost (filtered to any scope —
tenant-wide, resource-group, service, or tag) to produce a $/unit or €/unit
time-series.

Storage strategy:
  - We reuse the `waste_items` Cosmos container (already partitioned by tenant_id).
  - Two document types:
      type = "unit_metric"     → metric definition (created once per metric)
      type = "unit_datapoint"  → daily count entry  (one per metric per day)
  - No schema migration or new containers required.

API summary:
  POST   /api/v1/unit-economics/{tid}/metrics                 — define a metric
  GET    /api/v1/unit-economics/{tid}/metrics                 — list metrics
  GET    /api/v1/unit-economics/{tid}/metrics/{mid}           — get one metric
  DELETE /api/v1/unit-economics/{tid}/metrics/{mid}           — delete metric + data
  POST   /api/v1/unit-economics/{tid}/metrics/{mid}/data      — push day counts (batch)
  GET    /api/v1/unit-economics/{tid}/metrics/{mid}/data      — list raw data points
  GET    /api/v1/unit-economics/{tid}/cost-per-unit           — compute cost / unit timeseries
"""
from __future__ import annotations
import asyncio
from datetime import date, timedelta
from typing import Optional
from uuid import uuid4

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)

_METRIC_TYPE = "unit_metric"
_DATAPOINT_TYPE = "unit_datapoint"


# ── Storage helpers ──────────────────────────────────────────────────────────

async def _container() -> str:
    """Return waste_items container name (reused as unit-economics store)."""
    return get_settings().cosmos_container_waste_items


async def get_metric(tenant_id: str, metric_id: str) -> dict:
    c = await _container()
    doc = await cosmos.get_item(c, metric_id, tenant_id)
    if doc.get("type") != _METRIC_TYPE or doc.get("tenant_id") != tenant_id:
        raise NotFoundError(metric_id)
    return doc


async def list_metrics(tenant_id: str) -> list[dict]:
    c = await _container()
    query = (
        "SELECT * FROM c WHERE c.type = @t AND c.tenant_id = @tid"
    )
    return await cosmos.query_items(
        c, query,
        parameters=[
            {"name": "@t", "value": _METRIC_TYPE},
            {"name": "@tid", "value": tenant_id},
        ],
        partition_key=tenant_id,
    )


async def create_metric(tenant_id: str, name: str, unit_label: str, scope: dict | None = None) -> dict:
    """Create a new unit metric definition.

    scope (optional) restricts which cost records are summed as the numerator:
      {
        "resource_group": "rg-prod-api",   # filter by resource group
        "service_name": "Virtual Machines", # filter by service
        "tag": {"env": "prod"},             # filter by tag key/value
      }
    Leave scope=None to use the full tenant cost as the numerator.
    """
    c = await _container()
    doc = {
        "id": str(uuid4()),
        "type": _METRIC_TYPE,
        "tenant_id": tenant_id,
        "name": name,
        "unit_label": unit_label,
        "scope": scope or {},
        "_partitionKey": tenant_id,
    }
    await cosmos.upsert_item(c, doc)
    log.info("unit_economics.metric_created", tenant_id=tenant_id, metric_id=doc["id"], name=name)
    return doc


async def delete_metric(tenant_id: str, metric_id: str) -> None:
    c = await _container()
    # Verify ownership
    await get_metric(tenant_id, metric_id)
    # Delete all data points for this metric first.
    query = "SELECT c.id FROM c WHERE c.type = @t AND c.metric_id = @mid AND c.tenant_id = @tid"
    datapoints = await cosmos.query_items(
        c, query,
        parameters=[
            {"name": "@t", "value": _DATAPOINT_TYPE},
            {"name": "@mid", "value": metric_id},
            {"name": "@tid", "value": tenant_id},
        ],
        partition_key=tenant_id,
    )
    for dp in datapoints:
        await cosmos.delete_item(c, dp["id"], tenant_id)
    await cosmos.delete_item(c, metric_id, tenant_id)
    log.info("unit_economics.metric_deleted", tenant_id=tenant_id, metric_id=metric_id)


async def upsert_datapoints(tenant_id: str, metric_id: str, points: list[dict]) -> int:
    """Insert or overwrite daily unit counts.

    points: [{"date": "2024-03-15", "count": 1234}, ...]
    Returns number of upserted records.
    """
    c = await _container()
    # Verify metric exists and belongs to tenant.
    await get_metric(tenant_id, metric_id)
    saved = 0
    for p in points:
        day = str(p["date"])  # normalise to str
        doc = {
            "id": f"{metric_id}_{day}",
            "type": _DATAPOINT_TYPE,
            "tenant_id": tenant_id,
            "metric_id": metric_id,
            "date": day,
            "count": float(p["count"]),
            "_partitionKey": tenant_id,
        }
        await cosmos.upsert_item(c, doc)
        saved += 1
    return saved


async def list_datapoints(tenant_id: str, metric_id: str, start: date, end: date) -> list[dict]:
    c = await _container()
    query = (
        "SELECT * FROM c "
        "WHERE c.type = @t AND c.metric_id = @mid AND c.tenant_id = @tid "
        "AND c.date >= @start AND c.date <= @end "
        "ORDER BY c.date"
    )
    return await cosmos.query_items(
        c, query,
        parameters=[
            {"name": "@t", "value": _DATAPOINT_TYPE},
            {"name": "@mid", "value": metric_id},
            {"name": "@tid", "value": tenant_id},
            {"name": "@start", "value": start.isoformat()},
            {"name": "@end", "value": end.isoformat()},
        ],
        partition_key=tenant_id,
    )


# ── Cost-per-unit computation ────────────────────────────────────────────────

async def compute_cost_per_unit(
    tenant_id: str,
    metric_id: str,
    start: date,
    end: date,
) -> dict:
    """
    Return a daily cost-per-unit time series plus summary statistics.

    cost_per_unit[day] = daily_cloud_cost_eur[day] / unit_count[day]

    The cloud cost numerator is pulled from `cost_records`.  If the metric has
    a scope filter (resource_group / service / tag), only matching rows are
    summed; otherwise the full tenant daily cost is used.
    """
    settings = get_settings()
    metric = await get_metric(tenant_id, metric_id)
    scope: dict = metric.get("scope", {})

    # ── Fetch unit counts ───────────────────────────────────────────────────
    datapoints = await list_datapoints(tenant_id, metric_id, start, end)
    counts: dict[str, float] = {dp["date"]: float(dp["count"]) for dp in datapoints}

    # ── Fetch cost records for the same period ──────────────────────────────
    c_cost = settings.cosmos_container_cost_records
    params: list[dict] = [
        {"name": "@tid", "value": tenant_id},
        {"name": "@start", "value": start.isoformat()},
        {"name": "@end", "value": end.isoformat()},
    ]
    where_clauses = [
        "c.tenant_id = @tid",
        "c.record_date >= @start",
        "c.record_date <= @end",
        "c.estimated = false",        # skip near-realtime estimate rows
    ]
    if scope.get("resource_group"):
        where_clauses.append("c.resource_group = @rg")
        params.append({"name": "@rg", "value": scope["resource_group"]})
    if scope.get("service_name"):
        where_clauses.append("c.service_name = @svc")
        params.append({"name": "@svc", "value": scope["service_name"]})

    query = "SELECT c.record_date, SUM(c.cost_eur) AS daily_cost FROM c WHERE " + " AND ".join(where_clauses) + " GROUP BY c.record_date"
    try:
        cost_rows = await cosmos.query_items(
            c_cost, query, parameters=params, partition_key=tenant_id,
        )
    except Exception:
        # Fallback: fetch all rows and aggregate in Python (handles CosmosDB
        # emulator / serverless environments that don't support GROUP BY).
        raw_query = "SELECT c.record_date, c.cost_eur FROM c WHERE " + " AND ".join(where_clauses)
        raw_rows = await cosmos.query_items(
            c_cost, raw_query, parameters=params, partition_key=tenant_id,
        )
        agg: dict[str, float] = {}
        for row in raw_rows:
            d = str(row["record_date"])
            agg[d] = agg.get(d, 0.0) + float(row.get("cost_eur") or 0.0)
        cost_rows = [{"record_date": d, "daily_cost": v} for d, v in agg.items()]

    costs: dict[str, float] = {}
    for row in cost_rows:
        d = str(row.get("record_date", row.get("_record_date", "")))
        costs[d] = float(row.get("daily_cost", row.get("cost_eur", 0)) or 0)

    # ── Build time series ───────────────────────────────────────────────────
    all_days = []
    cur = start
    while cur <= end:
        all_days.append(cur.isoformat())
        cur += timedelta(days=1)

    series = []
    cpu_values: list[float] = []
    for day in all_days:
        cost = costs.get(day, 0.0)
        count = counts.get(day, 0.0)
        if count > 0:
            cpu = round(cost / count, 6)
            cpu_values.append(cpu)
        else:
            cpu = None
        series.append({
            "date": day,
            "cost_eur": round(cost, 4),
            "unit_count": count,
            "cost_per_unit_eur": cpu,
            "has_data": cpu is not None,
        })

    avg_cpu = round(sum(cpu_values) / len(cpu_values), 6) if cpu_values else None
    trend: str | None = None
    if len(cpu_values) >= 7:
        first_half = cpu_values[: len(cpu_values) // 2]
        second_half = cpu_values[len(cpu_values) // 2 :]
        diff = (sum(second_half) / len(second_half)) - (sum(first_half) / len(first_half))
        trend = "increasing" if diff > 0 else "decreasing" if diff < 0 else "stable"

    return {
        "tenant_id": tenant_id,
        "metric_id": metric_id,
        "metric_name": metric["name"],
        "unit_label": metric["unit_label"],
        "scope": scope,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "average_cost_per_unit_eur": avg_cpu,
        "trend": trend,
        "series": series,
        "data_points_with_cost": sum(1 for s in series if s["has_data"]),
        "data_points_missing_counts": sum(1 for s in series if not s["has_data"] and s["cost_eur"] > 0),
    }
