"""Business Context Auto-Mapping router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.rate_limit import rate_limit_tenant
from app.services.context_mapper import map_context, ContextMapping
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/context",
    tags=["context"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _product_to_dict(p) -> dict:
    return {
        "name": p.name,
        "cost_eur": p.cost_eur,
        "pct_of_total": p.pct_of_total,
        "resource_count": p.resource_count,
        "clouds": p.clouds,
        "teams": p.teams,
        "inference_method": p.inference_method,
        "top_services": p.top_services,
        "features": [
            {
                "name": f.name,
                "cost_eur": f.cost_eur,
                "pct_of_product": f.pct_of_product,
                "resource_count": f.resource_count,
                "clouds": f.clouds,
            }
            for f in p.features
        ],
    }


@router.get("/{tenant_id}/map")
async def get_context_map(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=90),
) -> dict:
    """
    Return the full business context mapping for a tenant.

    Automatically attributes spend to product lines and features by scanning
    resource tags and inferring from resource names / K8s namespace patterns.
    """
    mapping = await map_context(tenant_id, lookback_days=lookback_days)
    return {
        "tenant_id": mapping.tenant_id,
        "period_start": mapping.period_start,
        "period_end": mapping.period_end,
        "total_cost_eur": mapping.total_cost_eur,
        "attributed_eur": mapping.attributed_eur,
        "unattributed_eur": mapping.unattributed_eur,
        "attribution_pct": mapping.attribution_pct,
        "inference_notes": mapping.inference_notes,
        "products": [_product_to_dict(p) for p in mapping.products],
    }


@router.get("/{tenant_id}/products")
async def get_products(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=90),
) -> dict:
    """Return product-level cost breakdown (summary, no features)."""
    mapping = await map_context(tenant_id, lookback_days=lookback_days)
    return {
        "tenant_id": mapping.tenant_id,
        "period_start": mapping.period_start,
        "period_end": mapping.period_end,
        "total_cost_eur": mapping.total_cost_eur,
        "attributed_eur": mapping.attributed_eur,
        "attribution_pct": mapping.attribution_pct,
        "products": [
            {
                "name": p.name,
                "cost_eur": p.cost_eur,
                "pct_of_total": p.pct_of_total,
                "resource_count": p.resource_count,
                "clouds": p.clouds,
                "teams": p.teams,
                "inference_method": p.inference_method,
            }
            for p in mapping.products
        ],
    }


@router.get("/{tenant_id}/features")
async def get_features(
    tenant_id: str,
    lookback_days: int = Query(default=30, ge=1, le=90),
) -> dict:
    """Return feature-level cost breakdown across all products."""
    mapping = await map_context(tenant_id, lookback_days=lookback_days)
    all_features = []
    for p in mapping.products:
        for f in p.features:
            all_features.append({
                "product": p.name,
                "feature": f.name,
                "cost_eur": f.cost_eur,
                "pct_of_product": f.pct_of_product,
                "resource_count": f.resource_count,
                "clouds": f.clouds,
            })
    all_features.sort(key=lambda x: x["cost_eur"], reverse=True)
    return {
        "tenant_id": mapping.tenant_id,
        "period_start": mapping.period_start,
        "period_end": mapping.period_end,
        "total_attributed_eur": mapping.attributed_eur,
        "features": all_features,
    }
