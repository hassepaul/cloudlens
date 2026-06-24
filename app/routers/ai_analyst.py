"""
AI Cost Analyst router — /api/v1/insights/{tenant_id}/explain
=============================================================

GET  /api/v1/insights/{tenant_id}/explain/{day}
  Auto-fetches anomaly detection + service breakdown for the day and returns
  a plain-English explanation of why spend spiked or dipped.

POST /api/v1/insights/{tenant_id}/explain
  Accepts a pre-computed AnomalyContext payload (useful when calling from the
  alert delivery pipeline that already has the anomaly data in memory).
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.ai_analyst import (
    AnomalyContext, DriverContext, AnalystResponse,
    build_context_for_day, explain_anomaly,
)

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/insights", tags=["ai-analyst"],
    dependencies=[Depends(rate_limit_tenant)],
)


# ── Pydantic models for the POST body / response ─────────────────────────────

class DriverContextBody(BaseModel):
    dimension: str
    name: str
    delta_eur: float
    share_pct: float = 0.0
    baseline_eur: float = 0.0
    anomaly_day_eur: float = 0.0
    tags: dict = Field(default_factory=dict)


class ExplainRequest(BaseModel):
    """Caller-supplied anomaly context for the POST endpoint."""
    anomaly_day: str = Field(..., description="YYYY-MM-DD of the anomalous day")
    actual_eur: float
    expected_eur: float
    excess_eur: float
    z_score: float
    direction: str = Field(default="spike", pattern="^(spike|dip)$")
    severity: str = Field(default="medium", pattern="^(high|medium)$")
    drivers: list[DriverContextBody] = Field(default_factory=list)
    trailing_7d_avg_eur: float = 0.0
    deployment_events: list[dict] = Field(default_factory=list)


class ExplainResponse(BaseModel):
    tenant_id: str
    anomaly_day: str
    anomaly_summary: dict
    explanation: str
    confidence: str
    factors: list[str]
    action_recommendation: str
    generated_by: str
    cached: bool
    token_usage: dict = Field(default_factory=dict)
    note: Optional[str] = None


def _to_response(r: AnalystResponse, ctx: AnomalyContext) -> ExplainResponse:
    pct = round((ctx.excess_eur / max(ctx.expected_eur, 0.01)) * 100, 1)
    return ExplainResponse(
        tenant_id=r.tenant_id,
        anomaly_day=r.anomaly_day,
        anomaly_summary={
            "actual_eur": ctx.actual_eur,
            "expected_eur": ctx.expected_eur,
            "excess_eur": ctx.excess_eur,
            "excess_pct": pct,
            "z_score": ctx.z_score,
            "direction": ctx.direction,
            "severity": ctx.severity,
            "top_driver": ctx.drivers[0].name if ctx.drivers else None,
        },
        explanation=r.explanation,
        confidence=r.confidence,
        factors=r.factors,
        action_recommendation=r.action_recommendation,
        generated_by=r.generated_by,
        cached=r.cached,
        token_usage=r.token_usage,
        note=(
            None if get_settings().openai_api_key
            else "AI analyst running in rule-based mode. Set OPENAI_API_KEY to enable LLM explanations."
        ),
    )


# ── Helper: fetch 90-day cost data from Cosmos ────────────────────────────────

async def _load_cost_context(tenant_id: str) -> tuple[list[dict], dict]:
    """Fetch daily series + per-day service/RG breakdown for the last 90 days."""
    settings = get_settings()
    cr = settings.cosmos_container_cost_records
    end = date.today()
    start = end - timedelta(days=89)

    # Daily totals
    daily_rows = await cosmos.query_items(
        cr,
        """SELECT c.record_date, SUM(c.cost_eur) AS daily_cost
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e
              AND (NOT IS_DEFINED(c.estimated) OR c.estimated=false)
           GROUP BY c.record_date""",
        parameters=[
            {"name": "@t", "value": tenant_id},
            {"name": "@s", "value": start.isoformat()},
            {"name": "@e", "value": end.isoformat()},
        ],
        partition_key=tenant_id,
    )
    daily = sorted(
        [{"date": r["record_date"], "cost_eur": float(r["daily_cost"])} for r in daily_rows],
        key=lambda d: d["date"],
    )

    # Service + resource_group breakdown per day
    breakdown_rows = await cosmos.query_items(
        cr,
        """SELECT c.record_date, c.service_name, c.resource_group, SUM(c.cost_eur) AS cost
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e
              AND (NOT IS_DEFINED(c.estimated) OR c.estimated=false)
           GROUP BY c.record_date, c.service_name, c.resource_group""",
        parameters=[
            {"name": "@t", "value": tenant_id},
            {"name": "@s", "value": start.isoformat()},
            {"name": "@e", "value": end.isoformat()},
        ],
        partition_key=tenant_id,
    )
    breakdowns: dict = {}
    for r in breakdown_rows:
        d = r["record_date"]
        c = float(r.get("cost", 0))
        breakdowns.setdefault(d, {}).setdefault("service", {})[r["service_name"]] = \
            breakdowns.get(d, {}).get("service", {}).get(r["service_name"], 0.0) + c
        breakdowns.setdefault(d, {}).setdefault("resource_group", {})[r["resource_group"]] = \
            breakdowns.get(d, {}).get("resource_group", {}).get(r["resource_group"], 0.0) + c

    return daily, breakdowns


# ── GET: auto-detect + explain ─────────────────────────────────────────────

@router.get("/{tenant_id}/explain/{day}", response_model=ExplainResponse)
async def explain_day(
    tenant_id: str,
    day: str,
    force_refresh: bool = Query(
        default=False,
        description="Bypass cache and regenerate the explanation.",
    ),
) -> ExplainResponse:
    """
    Explain why cloud spend was anomalous on `day` (YYYY-MM-DD).

    - Runs Holt-Winters anomaly detection on the tenant's 90-day cost history.
    - If the day is not anomalous (z-score < 2.0), returns 404.
    - Calls GPT-4o (or rule-based fallback) to produce a plain-English explanation.
    - Caches the result for 7 days.
    """
    # Validate date format
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "INVALID_DATE", "message": "Use YYYY-MM-DD format."})

    # Verify tenant exists
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        tenant_name = doc.get("name", tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    # If force_refresh, bust the cache by removing the cached doc before building context
    if force_refresh:
        from app.services.ai_analyst import _cache_key, _CACHE_TYPE
        ck = _cache_key(tenant_id, day, settings.openai_model)
        try:
            await cosmos.delete_item(settings.cosmos_container_waste_items, ck, tenant_id)
        except Exception:
            pass  # Not in cache is fine

    try:
        daily, breakdowns = await _load_cost_context(tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    ctx = await build_context_for_day(
        tenant_id=tenant_id,
        day=day,
        daily_series=daily,
        per_day_breakdowns=breakdowns,
        tenant_name=tenant_name,
    )

    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "NOT_ANOMALOUS",
                "message": (
                    f"No statistically significant anomaly detected on {day} "
                    f"(z-score < {2.0}). Use GET /insights/{{tenant_id}}/anomalies "
                    f"to see which days were flagged."
                ),
            },
        )

    result = await explain_anomaly(ctx)
    return _to_response(result, ctx)


# ── POST: explain with caller-supplied context ────────────────────────────────

@router.post("/{tenant_id}/explain", response_model=ExplainResponse)
async def explain_with_context(tenant_id: str, body: ExplainRequest) -> ExplainResponse:
    """
    Explain an anomaly with a caller-supplied context payload.

    Useful when the alert delivery pipeline already holds the anomaly data —
    skips the Cosmos queries and goes straight to the LLM.
    """
    ctx = AnomalyContext(
        tenant_id=tenant_id,
        anomaly_day=body.anomaly_day,
        actual_eur=body.actual_eur,
        expected_eur=body.expected_eur,
        excess_eur=body.excess_eur,
        z_score=body.z_score,
        direction=body.direction,
        severity=body.severity,
        drivers=[
            DriverContext(
                dimension=d.dimension,
                name=d.name,
                delta_eur=d.delta_eur,
                share_pct=d.share_pct,
                baseline_eur=d.baseline_eur,
                anomaly_day_eur=d.anomaly_day_eur,
                tags=d.tags,
            )
            for d in body.drivers
        ],
        trailing_7d_avg_eur=body.trailing_7d_avg_eur,
        deployment_events=body.deployment_events,
    )
    result = await explain_anomaly(ctx)
    return _to_response(result, ctx)
