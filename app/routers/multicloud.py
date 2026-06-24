"""Multi-cloud router — cross-cloud spend, AI/LLM, allocation, commitments, i18n."""
from __future__ import annotations
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Depends, Header
from pydantic import BaseModel

from app.auth import require_cloud
from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.tenant import CloudProvider, ADDON_CLOUDS
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.allocation import (
    allocate_full, AllocationRuleSet, AllocationRule, RuleKind,
)
from app.services.commitments import analyze_commitments
from app.i18n import labels_for, normalize_lang, t

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/multicloud", tags=["multicloud"],
    dependencies=[Depends(rate_limit_tenant)],
)
# labels has no tenant_id in the path, so it can't use the per-tenant limiter
labels_router = APIRouter(prefix="/api/v1/multicloud", tags=["multicloud"])


# ── Request models for typed validation ────────────────────────────────────────────

class AllocationRuleRequest(BaseModel):
    kind: str
    cost_center: str = ""
    match_key: str = ""
    match_value: str = ""
    source_key: str = ""
    value_map: dict[str, str] = {}
    accounts: list[str] = []
    pattern: str = ""


class AllocationRuleSetRequest(BaseModel):
    dimension: str = "cost_center"
    shared_strategy: str = "proportional"
    rules: list[AllocationRuleRequest] = []


def _fr() -> str:
    # FOCUS records share the cost_records container, discriminated by type
    return get_settings().cosmos_container_cost_records


def _cm() -> str:
    return get_settings().cosmos_container_waste_items   # commitments co-located


async def _focus_records(tenant_id: str, days: int) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return await cosmos.query_items(
        _fr(),
        """SELECT c.provider_name, c.service_name, c.service_category,
                  c.effective_cost, c.billed_cost, c.charge_period_start,
                  c.commitment_discount_type, c.tags, c.sub_account_id,
                  c.resource_name
           FROM c WHERE c.tenant_id=@t AND c.type='focus_record'
           AND c.charge_period_start>=@s AND c.charge_period_start<=@e""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": start.isoformat()},
                    {"name": "@e", "value": end.isoformat()}],
        partition_key=tenant_id,
    )


async def _held_commitments(tenant_id: str) -> list[dict]:
    return await cosmos.query_items(
        _cm(),
        "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='commitment'",
        parameters=[{"name": "@t", "value": tenant_id}],
        partition_key=tenant_id,
    )


# ── Cloud entitlement status (no extra auth needed — rate-limited already) ───

@router.get("/{tenant_id}/clouds")
async def tenant_cloud_status(tenant_id: str) -> dict:
    """Return which clouds are enabled for this tenant and which are available as add-ons."""
    from app.services import cosmos as _cosmos
    from app.models.tenant import TenantConfig
    settings = get_settings()
    try:
        doc = await _cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Tenant {tenant_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    return {
        "tenant_id": tenant_id,
        "enabled_clouds": config.enabled_clouds,
        "is_multicloud": config.is_multicloud(),
        "available_addons": [c.value for c in ADDON_CLOUDS if c.value not in config.enabled_clouds],
        "cloud_accounts": config.cloud_accounts,
    }


# ── Cross-cloud spend summary ─────────────────────────────────────────────────

@router.get("/{tenant_id}/spend")
async def multicloud_spend(
    tenant_id: str,
    days: int = Query(30, ge=1, le=90),
    lang: str = Query("en"),
    accept_language: str | None = Header(default=None),
) -> dict:
    """Spend grouped by cloud provider, plus AI/LLM spend broken out.

    Returns data for enabled clouds only. The `locked_clouds` field lists add-on
    clouds available for this tenant to unlock.
    """
    from app.services import cosmos as _cosmos
    from app.models.tenant import TenantConfig
    settings = get_settings()
    language = normalize_lang(lang or accept_language)

    # Load tenant to determine entitlements.
    try:
        doc = await _cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Tenant {tenant_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    enabled = set(config.enabled_clouds)
    locked_clouds = [c.value for c in ADDON_CLOUDS if c.value not in enabled]

    try:
        records = await _focus_records(tenant_id, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    by_provider: dict[str, float] = {}
    ai_by_service: dict[str, float] = {}
    total = 0.0
    for r in records:
        cost = float(r.get("effective_cost", r.get("billed_cost", 0.0)))
        total += cost
        by_provider[r.get("provider_name", "Unknown")] = \
            by_provider.get(r.get("provider_name", "Unknown"), 0.0) + cost
        if r.get("service_category") == "AI and Machine Learning":
            ai_by_service[r.get("service_name", "AI")] = \
                ai_by_service.get(r.get("service_name", "AI"), 0.0) + cost

    providers = [{"provider": p, "spend_eur": round(v, 2),
                  "pct": round(v / total * 100, 1) if total > 0 else 0.0}
                 for p, v in sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)]
    ai = [{"service": s, "spend_eur": round(v, 2)}
          for s, v in sorted(ai_by_service.items(), key=lambda kv: kv[1], reverse=True)]
    ai_total = round(sum(ai_by_service.values()), 2)

    return {
        "tenant_id": tenant_id,
        "lang": language,
        "labels": {k: t(k, language) for k in
                   ("total_spend", "by_provider", "ai_spend", "recoverable_monthly")},
        "total_spend_eur": round(total, 2),
        "providers": providers,
        "ai_llm": {"total_eur": ai_total, "pct_of_spend":
                   round(ai_total / total * 100, 1) if total > 0 else 0.0,
                   "by_service": ai},
        "period_days": days,
        "enabled_clouds": sorted(enabled),
        "locked_clouds": locked_clouds,
        "is_multicloud": config.is_multicloud(),
    }


# ── 100% allocation ──────────────────────────────────────────────────────────

@router.post("/{tenant_id}/allocate")
async def allocate(
    tenant_id: str,
    ruleset: AllocationRuleSetRequest,
    days: int = Query(30, ge=1, le=90),
) -> dict:
    """Run rule-based 100% allocation over FOCUS records."""
    try:
        records = await _focus_records(tenant_id, days)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    rules = []
    for r in ruleset.rules:
        try:
            rules.append(AllocationRule(
                kind=RuleKind(r.kind), cost_center=r.cost_center,
                match_key=r.match_key, match_value=r.match_value,
                source_key=r.source_key, value_map=r.value_map,
                accounts=tuple(r.accounts), pattern=r.pattern,
            ))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422,
                                detail={"error": "VALIDATION_ERROR", "message": f"Bad rule: {exc}"})

    rs = AllocationRuleSet(
        dimension=ruleset.dimension,
        shared_strategy=ruleset.shared_strategy,
        rules=rules,
    )
    res = allocate_full(records, rs)
    return {
        "tenant_id": tenant_id, "dimension": res.dimension,
        "total_eur": res.total_eur, "allocated_pct": res.allocated_pct,
        "coverage_before_shared_pct": res.coverage_before_shared_pct,
        "unallocated_eur": res.unallocated_eur,
        "groups": [g.__dict__ for g in res.groups], "notes": res.notes,
    }


# ── Commitment management ────────────────────────────────────────────────────

@router.get("/{tenant_id}/commitments")
async def commitments(
    tenant_id: str,
    days: int = Query(30, ge=1, le=90),
) -> dict:
    """Coverage, utilization, and commitment-purchase recommendations."""
    try:
        records = await _focus_records(tenant_id, days)
        held = await _held_commitments(tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    rep = analyze_commitments(records, held, days=days)
    return {
        "tenant_id": tenant_id,
        "total_eligible_eur": rep.total_eligible_eur,
        "total_covered_eur": rep.total_covered_eur,
        "blended_coverage_pct": rep.blended_coverage_pct,
        "blended_utilization_pct": rep.blended_utilization_pct,
        "total_idle_commitment_eur": rep.total_idle_commitment_eur,
        "monthly_opportunity_eur": rep.monthly_opportunity_eur,
        "by_provider": [s.__dict__ for s in rep.by_provider],
        "recommendations": [r.__dict__ for r in rep.recommendations],
        "notes": rep.notes,
    }


# ── i18n labels (frontend bootstrap) ─────────────────────────────────────────

@labels_router.get("/labels")
async def labels(lang: str = Query("en")) -> dict:
    """Return the full UI label set for a language (frontend i18n bootstrap)."""
    language = normalize_lang(lang)
    return {"lang": language, "labels": labels_for(language)}
