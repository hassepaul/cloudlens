"""Waste items router — /api/v1/waste"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.exceptions import NotFoundError, CosmosError
from app.logging_config import get_logger
from app.models.waste import WasteItem, WasteResolve, Priority
from app.rate_limit import rate_limit_tenant
from app.services import cosmos

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/waste", tags=["waste"], dependencies=[Depends(rate_limit_tenant)]
)


def _wi_container() -> str:
    return get_settings().cosmos_container_waste_items


@router.get("/{tenant_id}", response_model=list[WasteItem])
async def list_waste_items(
    tenant_id: str,
    priority: Optional[Priority] = Query(None),
    resolved: bool = Query(False, description="Include resolved items"),
    limit: int = Query(100, ge=1, le=500),
) -> list[WasteItem]:
    """Return waste items for a tenant, optionally filtered by priority."""
    conditions = [
        "c.tenant_id = @tid",
        "c.type = 'waste_item'",
    ]
    params = [{"name": "@tid", "value": tenant_id}]

    if not resolved:
        conditions.append("(NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null)")
    if priority:
        conditions.append("c.priority = @priority")
        params.append({"name": "@priority", "value": priority.value})

    query = (
        f"SELECT * FROM c WHERE {' AND '.join(conditions)} "
        f"ORDER BY c.saving_eur DESC OFFSET 0 LIMIT {limit}"
    )
    try:
        docs = await cosmos.query_items(_wi_container(), query, params, partition_key=tenant_id)
        return [WasteItem(**d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{item_id}/resolve", response_model=WasteItem)
async def resolve_waste_item(item_id: str, tenant_id: str, payload: WasteResolve) -> WasteItem:
    """Mark a waste item as resolved."""
    try:
        doc = await cosmos.get_item(_wi_container(), item_id, tenant_id)
        item = WasteItem(**doc)
        updated = item.model_copy(update={
            "resolved_at": datetime.now(timezone.utc),
            "resolved_by": payload.resolved_by,
        })
        await cosmos.upsert_item(_wi_container(), updated.to_cosmos())
        log.info("waste.resolved", item_id=item_id, by=payload.resolved_by, saving=item.saving_eur)
        return updated
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
