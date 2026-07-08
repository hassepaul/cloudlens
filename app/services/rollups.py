"""
Monthly cost rollups.

Daily ``cost_records`` carry a 90-day TTL, so they cannot support annual
(month-of-year) seasonality on their own. This module persists per-tenant
monthly totals in a dedicated container with a multi-year TTL, building the
long history that ``forecast.annual_seasonal_factors`` / ``forecast_monthly``
need. Rollups are written opportunistically whenever a forecast runs, so the
annual history accumulates organically with no separate pipeline.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)

_ROLLUP_TTL_SECONDS = 157_680_000   # ~5 years


def _container() -> str:
    return get_settings().cosmos_container_cost_rollups_monthly


def _aggregate_monthly(daily: list[dict]) -> dict[str, float]:
    """Sum a daily series ([{date, cost_eur}]) into {'YYYY-MM': total}."""
    agg: dict[str, float] = defaultdict(float)
    for d in daily:
        dt = str(d.get("date", ""))
        if len(dt) >= 7:
            agg[dt[:7]] += float(d.get("cost_eur", 0.0))
    return dict(agg)


async def get_monthly_rollups(tenant_id: str, months: int = 36) -> list[dict]:
    """Return persisted monthly totals ascending: [{'month','cost_eur'}, ...]."""
    try:
        rows = await cosmos.query_items(
            _container(),
            "SELECT c.month, c.cost_eur FROM c "
            "WHERE c.tenant_id=@t AND c.type='monthly_rollup' "
            "ORDER BY c.month DESC OFFSET 0 LIMIT @n",
            parameters=[{"name": "@t", "value": tenant_id},
                        {"name": "@n", "value": months}],
            partition_key=tenant_id,
        )
    except CosmosError:
        return []
    rows.sort(key=lambda r: r.get("month", ""))
    return [{"month": r["month"], "cost_eur": float(r["cost_eur"])}
            for r in rows if r.get("month")]


async def persist_monthly_rollups(
    tenant_id: str, daily: list[dict], include_current_month: bool = False
) -> int:
    """
    Upsert monthly totals derived from a daily series. By default the current
    (still-incomplete) month is skipped so only sealed months are persisted;
    pass include_current_month=True to also refresh the running month total.
    Returns the number of months written. Never raises — persistence is
    best-effort and must not break the forecast request.
    """
    agg = _aggregate_monthly(daily)
    if not agg:
        return 0
    current = date.today().strftime("%Y-%m")
    written = 0
    for month, total in agg.items():
        if not include_current_month and month >= current:
            continue
        doc = {
            "id": f"{tenant_id}:{month}",
            "type": "monthly_rollup",
            "tenant_id": tenant_id,
            "month": month,
            "cost_eur": round(total, 2),
            "_partitionKey": tenant_id,
            "ttl": _ROLLUP_TTL_SECONDS,
        }
        try:
            await cosmos.upsert_item(_container(), doc)
            written += 1
        except CosmosError as exc:
            log.warning("rollups.persist_failed", tenant_id=tenant_id, month=month, error=str(exc))
    return written
