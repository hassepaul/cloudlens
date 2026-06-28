"""
GenAI Cost router — /api/v1/genai
==================================

POST  /{tenant_id}/usage           — ingest a single usage record
POST  /{tenant_id}/usage/batch     — ingest an array of usage records
GET   /{tenant_id}/summary         — total cost, by-model, daily trend
GET   /{tenant_id}/models          — per-model stats + efficiency metrics
GET   /{tenant_id}/trends          — daily spend trends (group by model/provider/app)
GET   /{tenant_id}/apps            — top apps by GenAI spend
POST  /{tenant_id}/budgets         — create a token budget alert
GET   /{tenant_id}/budgets         — list budgets with live utilisation
DELETE/{tenant_id}/budgets/{id}    — delete a budget
GET   /{tenant_id}/alerts          — budgets in warning or breach state
GET   /{tenant_id}/pricing         — list the built-in pricing table
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services.genai_cost import (
    GenAISummary, ModelStats, ModelComparison, GenAIBudget, BudgetUtilisation,
    ingest_usage, ingest_batch, get_summary, get_model_breakdown,
    get_daily_trends, get_top_apps, create_budget, list_budgets,
    delete_budget, check_budget_alerts, _PRICING, _KNOWN_PROVIDERS,
)

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/genai",
    tags=["genai-cost"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_tenant)],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class UsageIn(BaseModel):
    provider: str = Field(..., description="openai | azure_openai | bedrock | vertex_ai | custom")
    model: str
    deployment_name: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    request_type: str = Field(default="chat", pattern="^(chat|embedding|image|speech|fine_tune|completion)$")
    quantity: int = Field(default=1, ge=1, description="Number of images (DALL·E) or requests")
    duration_seconds: float = Field(default=0.0, ge=0, description="Audio duration for Whisper/TTS")
    app_name: str = ""
    environment: str = ""
    user_id: str = ""
    tags: dict = Field(default_factory=dict)
    latency_ms: int = Field(default=0, ge=0)
    # Optional caller-supplied cost override (custom models)
    total_cost_usd: Optional[float] = None
    custom_input_price: float = Field(default=0.0, ge=0, description="USD per 1M input tokens (custom models)")
    custom_output_price: float = Field(default=0.0, ge=0, description="USD per 1M output tokens (custom models)")


class UsageOut(BaseModel):
    id: str
    tenant_id: str
    provider: str
    model: str
    total_tokens: int
    total_cost_usd: float
    total_cost_eur: float
    period_date: str
    recorded_at: str


class BatchIn(BaseModel):
    records: list[UsageIn] = Field(..., min_length=1, max_length=1000)


class BatchOut(BaseModel):
    ingested: int
    failed: int
    total: int


class ModelStatsOut(BaseModel):
    provider: str
    model: str
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: float
    total_cost_eur: float
    avg_cost_per_request_usd: float
    avg_tokens_per_request: float
    blended_cost_per_1m_tokens_usd: float
    cost_trend_pct: float


class ComparisonOut(BaseModel):
    current_model: str
    current_provider: str
    alternative_model: str
    alternative_provider: str
    current_cost_usd: float
    alternative_cost_usd: float
    saving_usd: float
    saving_pct: float
    caveat: str


class SummaryOut(BaseModel):
    tenant_id: str
    period_days: int
    total_cost_usd: float
    total_cost_eur: float
    total_requests: int
    total_tokens: int
    by_provider: list[dict]
    by_model: list[ModelStatsOut]
    daily_trend: list[dict]
    top_model: str
    cost_per_1m_tokens_usd: float
    comparisons: list[ComparisonOut]


class BudgetIn(BaseModel):
    name: str = Field(..., min_length=1)
    monthly_limit_usd: float = Field(..., gt=0)
    model_filter: str = ""
    provider_filter: str = ""
    app_filter: str = ""
    alert_threshold_pct: float = Field(default=80.0, ge=1, le=100)


class BudgetOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    monthly_limit_usd: float
    model_filter: str
    provider_filter: str
    app_filter: str
    alert_threshold_pct: float
    created_at: str


class BudgetAlertOut(BaseModel):
    budget_id: str
    budget_name: str
    current_spend_usd: float
    monthly_limit_usd: float
    utilisation_pct: float
    status: str
    projected_monthly_usd: float


def _budget_out(b: GenAIBudget) -> BudgetOut:
    return BudgetOut(
        id=b.id, tenant_id=b.tenant_id, name=b.name,
        monthly_limit_usd=b.monthly_limit_usd,
        model_filter=b.model_filter, provider_filter=b.provider_filter,
        app_filter=b.app_filter, alert_threshold_pct=b.alert_threshold_pct,
        created_at=b.created_at,
    )


def _ms_out(ms: ModelStats) -> ModelStatsOut:
    return ModelStatsOut(**ms.__dict__)


def _cmp_out(c: ModelComparison) -> ComparisonOut:
    return ComparisonOut(
        current_model=c.current_model, current_provider=c.current_provider,
        alternative_model=c.alternative_model, alternative_provider=c.alternative_provider,
        current_cost_usd=c.current_cost_usd, alternative_cost_usd=c.alternative_cost_usd,
        saving_usd=c.saving_usd, saving_pct=c.saving_pct, caveat=c.caveat,
    )


# ── Usage ingestion ───────────────────────────────────────────────────────────

@router.post("/{tenant_id}/usage", response_model=UsageOut, status_code=status.HTTP_201_CREATED)
async def record_usage(tenant_id: str, body: UsageIn) -> UsageOut:
    """Ingest a single GenAI usage record. Cost is calculated from the built-in pricing table."""
    if body.provider not in _KNOWN_PROVIDERS:
        raise HTTPException(status_code=422, detail={"error": "UNKNOWN_PROVIDER", "message": f"Provider must be one of: {', '.join(sorted(_KNOWN_PROVIDERS))}"})
    record = await ingest_usage(tenant_id, body.model_dump())
    return UsageOut(
        id=record.id, tenant_id=record.tenant_id, provider=record.provider,
        model=record.model, total_tokens=record.total_tokens,
        total_cost_usd=record.total_cost_usd, total_cost_eur=record.total_cost_eur,
        period_date=record.period_date, recorded_at=record.recorded_at,
    )


@router.post("/{tenant_id}/usage/batch", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def record_usage_batch(tenant_id: str, body: BatchIn) -> BatchOut:
    """Ingest up to 1,000 usage records in one call."""
    result = await ingest_batch(tenant_id, [r.model_dump() for r in body.records])
    return BatchOut(**result)


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/summary", response_model=SummaryOut)
async def genai_summary(
    tenant_id: str,
    period_days: int = Query(default=30, ge=1, le=90),
) -> SummaryOut:
    """Full GenAI cost summary: totals, by-provider, by-model, daily trend, model comparison."""
    summary = await get_summary(tenant_id, period_days=period_days)
    return SummaryOut(
        tenant_id=summary.tenant_id,
        period_days=summary.period_days,
        total_cost_usd=summary.total_cost_usd,
        total_cost_eur=summary.total_cost_eur,
        total_requests=summary.total_requests,
        total_tokens=summary.total_tokens,
        by_provider=summary.by_provider,
        by_model=[_ms_out(ms) for ms in summary.by_model],
        daily_trend=summary.daily_trend,
        top_model=summary.top_model,
        cost_per_1m_tokens_usd=summary.cost_per_1m_tokens_usd,
        comparisons=[_cmp_out(c) for c in summary.comparisons],
    )


@router.get("/{tenant_id}/models", response_model=list[ModelStatsOut])
async def model_breakdown(
    tenant_id: str,
    period_days: int = Query(default=30, ge=1, le=90),
) -> list[ModelStatsOut]:
    """Per-model stats including efficiency metrics."""
    stats = await get_model_breakdown(tenant_id, period_days=period_days)
    return [_ms_out(ms) for ms in stats]


@router.get("/{tenant_id}/trends", response_model=list[dict])
async def daily_trends(
    tenant_id: str,
    period_days: int = Query(default=30, ge=1, le=90),
    group_by: str = Query(default="model", pattern="^(model|provider|app|total)$"),
) -> list[dict]:
    """Daily spend trends grouped by model, provider, app, or total."""
    return await get_daily_trends(tenant_id, period_days=period_days, group_by=group_by)


@router.get("/{tenant_id}/apps", response_model=list[dict])
async def top_apps(
    tenant_id: str,
    period_days: int = Query(default=30, ge=1, le=90),
) -> list[dict]:
    """Top applications by GenAI spend."""
    return await get_top_apps(tenant_id, period_days=period_days)


# ── Budget management ─────────────────────────────────────────────────────────

@router.post("/{tenant_id}/budgets", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
async def create_genai_budget(tenant_id: str, body: BudgetIn) -> BudgetOut:
    """Create a monthly GenAI spend budget with alert threshold."""
    budget = await create_budget(tenant_id, body.model_dump())
    return _budget_out(budget)


@router.get("/{tenant_id}/budgets", response_model=list[BudgetOut])
async def list_genai_budgets(tenant_id: str) -> list[BudgetOut]:
    """List all GenAI budgets for this tenant."""
    budgets = await list_budgets(tenant_id)
    return [_budget_out(b) for b in budgets]


@router.delete("/{tenant_id}/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_genai_budget(tenant_id: str, budget_id: str) -> None:
    """Delete a GenAI budget."""
    deleted = await delete_budget(tenant_id, budget_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Budget {budget_id} not found"})


@router.get("/{tenant_id}/alerts", response_model=list[BudgetAlertOut])
async def budget_alerts(tenant_id: str) -> list[BudgetAlertOut]:
    """List all GenAI budgets in warning (≥threshold%) or breach (≥100%) state."""
    all_alerts = await check_budget_alerts(tenant_id)
    active = [a for a in all_alerts if a.status in ("warning", "breach")]
    return [
        BudgetAlertOut(
            budget_id=a.budget.id,
            budget_name=a.budget.name,
            current_spend_usd=a.current_spend_usd,
            monthly_limit_usd=a.budget.monthly_limit_usd,
            utilisation_pct=a.utilisation_pct,
            status=a.status,
            projected_monthly_usd=a.projected_monthly_usd,
        )
        for a in active
    ]


# ── Pricing reference ─────────────────────────────────────────────────────────

@router.get("/{tenant_id}/pricing")
async def pricing_table(tenant_id: str) -> dict:
    """Return the built-in GenAI pricing table for reference."""
    return {
        "note": "USD per 1M tokens (input/output), unless noted as per_image / per_minute / per_1m_chars",
        "prices": _PRICING,
    }
