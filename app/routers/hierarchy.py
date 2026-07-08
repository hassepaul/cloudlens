"""Cost hierarchy / portfolio router — /api/v1/hierarchy"""
from __future__ import annotations
from datetime import date, timedelta, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Depends, status

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.hierarchy import (
    CostNode, CostNodeCreate, CostNodeUpdate, HierarchyRollup,
)
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services import hierarchy as hier_svc

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/hierarchy",
    tags=["hierarchy"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _c() -> str:
    return get_settings().cosmos_container_hierarchy


# ── Node CRUD ─────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/nodes", response_model=list[CostNode])
async def list_nodes(tenant_id: str) -> list[CostNode]:
    """List all hierarchy nodes for a tenant."""
    try:
        docs = await cosmos.query_items(
            _c(),
            "SELECT * FROM c WHERE c.tenant_id=@tid AND c.type='hierarchy_node' ORDER BY c.created_at ASC",
            [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
        )
        return [CostNode.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/nodes", response_model=CostNode, status_code=status.HTTP_201_CREATED)
async def create_node(tenant_id: str, payload: CostNodeCreate) -> CostNode:
    """
    Create a hierarchy node.

    Model the tree by setting ``parent_id`` to an existing node's ID. Roots
    have ``parent_id = null``. Tag filters are used to claim cost records:

        {"cost_center": "engineering", "team": ["platform", "product"]}

    A parent node's ``total_cost_eur`` includes the sum of its children, so
    don't double-enter the same tag filters at multiple levels.
    """
    if payload.parent_id:
        # Verify parent exists in this tenant
        try:
            parent_doc = await cosmos.get_item(_c(), payload.parent_id, tenant_id)
            if parent_doc.get("type") != "hierarchy_node":
                raise NotFoundError(f"Parent node {payload.parent_id} not found")
        except NotFoundError:
            raise HTTPException(status_code=404, detail={
                "error": "NOT_FOUND", "message": f"Parent node {payload.parent_id} not found"})
        except CosmosError as exc:
            raise HTTPException(status_code=503, detail=exc.to_dict())

    node = CostNode(tenant_id=tenant_id, **payload.model_dump())
    try:
        await cosmos.upsert_item(_c(), node.to_cosmos())
        log.info("hierarchy.node_created", tenant_id=tenant_id, node_id=node.id, name=node.name)
        return node
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/nodes/{node_id}", response_model=CostNode)
async def get_node(tenant_id: str, node_id: str) -> CostNode:
    try:
        doc = await cosmos.get_item(_c(), node_id, tenant_id)
        if doc.get("type") != "hierarchy_node":
            raise NotFoundError(f"Node {node_id} not found")
        return CostNode.from_cosmos(doc)
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Node {node_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{tenant_id}/nodes/{node_id}", response_model=CostNode)
async def update_node(tenant_id: str, node_id: str, payload: CostNodeUpdate) -> CostNode:
    """Update a node's metadata, tag filters, budget, or parent."""
    try:
        doc = await cosmos.get_item(_c(), node_id, tenant_id)
        if doc.get("type") != "hierarchy_node":
            raise NotFoundError(f"Node {node_id} not found")
        node = CostNode.from_cosmos(doc)
        node = node.model_copy(update={
            **payload.model_dump(exclude_unset=True),
            "updated_at": datetime.now(timezone.utc),
        })
        await cosmos.upsert_item(_c(), node.to_cosmos())
        log.info("hierarchy.node_updated", tenant_id=tenant_id, node_id=node_id)
        return node
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Node {node_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_node(tenant_id: str, node_id: str):
    """
    Delete a hierarchy node. Children of this node become orphans and are
    promoted to root level on the next rollup.
    """
    try:
        doc = await cosmos.get_item(_c(), node_id, tenant_id)
        if doc.get("type") != "hierarchy_node":
            raise NotFoundError(f"Node {node_id} not found")
        await cosmos.delete_item(_c(), node_id, tenant_id)
        log.info("hierarchy.node_deleted", tenant_id=tenant_id, node_id=node_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Node {node_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ── Rollup ────────────────────────────────────────────────────────────────────

@router.get(
    "/{tenant_id}/rollup",
    response_model=HierarchyRollup,
    summary="Compute full portfolio cost rollup",
    description=(
        "Walks the cost hierarchy tree and attributes spend from every cloud "
        "(Azure, AWS, GCP, etc.) to each node based on its tag_filters. "
        "Parent nodes aggregate their children. Unallocated spend is the "
        "portion not matched by any node's tag filters."
    ),
)
async def full_rollup(
    tenant_id: str,
    start: date = Query(default=None, description="Period start (YYYY-MM-DD). Defaults to first of current month."),
    end: date = Query(default=None, description="Period end (YYYY-MM-DD). Defaults to today."),
) -> HierarchyRollup:
    if end is None:
        end = date.today()
    if start is None:
        start = end.replace(day=1)
    if start > end:
        raise HTTPException(status_code=422, detail={"error": "VALIDATION_ERROR", "message": "start must be <= end"})
    try:
        return await hier_svc.rollup(tenant_id, start, end)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except Exception as exc:
        log.error("hierarchy.rollup_error", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "ROLLUP_ERROR", "message": "Failed to compute hierarchy rollup"})


@router.get(
    "/{tenant_id}/nodes/{node_id}/breakdown",
    summary="Detailed cost breakdown for a single hierarchy node",
)
async def node_breakdown(
    tenant_id: str,
    node_id: str,
    start: date = Query(default=None),
    end: date = Query(default=None),
    days: int = Query(default=30, ge=1, le=365, description="Lookback days (ignored if start/end provided)"),
) -> dict:
    """
    Return daily series, service breakdown, cloud split, and budget burn-rate
    for a single node's directly attributed spend.
    """
    if end is None:
        end = date.today()
    if start is None:
        start = end - timedelta(days=days - 1)
    try:
        result = await hier_svc.node_breakdown(tenant_id, node_id, start, end)
        if not result:
            raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Node {node_id} not found"})
        return result
    except HTTPException:
        raise
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except Exception as exc:
        log.error("hierarchy.breakdown_error", tenant_id=tenant_id, node_id=node_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "BREAKDOWN_ERROR", "message": "Failed to compute node breakdown"})
