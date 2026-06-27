"""
Cost hierarchy / portfolio rollup service.

Builds the cost tree from CostNode records and computes per-node spend by
joining against cost_records (Azure CostRecord) and focus_records (all clouds).
Both record types live in the same Cosmos container, discriminated by 'type'.

Tag matching is cloud-agnostic: every record carries a 'tags' dict regardless
of which provider ingested it (the FOCUS normaliser preserves tags).
"""
from __future__ import annotations
from datetime import date
from typing import Optional

from app.config import get_settings
from app.logging_config import get_logger
from app.models.hierarchy import (
    CostNode, NodeRollup, HierarchyRollup, BudgetStatus, NodeType,
)
from app.services import cosmos

log = get_logger(__name__)


# ── Tag matching ──────────────────────────────────────────────────────────────

def _matches_tag_filters(record_tags: dict, filters: dict) -> bool:
    """
    Return True if the record's tags satisfy all filter criteria.
    Filter values can be a single string (exact match) or list (any-of match).
    Comparisons are case-insensitive on both sides.
    """
    if not filters:
        return False  # node with empty filters claims nothing directly

    normalised = {k.lower(): str(v).lower() for k, v in (record_tags or {}).items()}

    for fk, fv in filters.items():
        norm_key = fk.lower()
        if norm_key not in normalised:
            return False
        record_val = normalised[norm_key]
        if isinstance(fv, list):
            if not any(record_val == str(v).lower() for v in fv):
                return False
        else:
            if record_val != str(fv).lower():
                return False
    return True


# ── Data loading ──────────────────────────────────────────────────────────────

async def _load_cost_records(
    tenant_id: str,
    start: date,
    end: date,
    container: str,
) -> list[dict]:
    """
    Load cost + tag data for every record in the period from both Azure
    CostRecords and FOCUS records (all other clouds).
    Returns a flat list of dicts: {cost_eur, tags, service, cloud, record_date}.
    """
    rows = await cosmos.query_items(
        container,
        """SELECT
               COALESCE(c.cost_eur, c.effective_cost, 0)     AS cost_eur,
               COALESCE(c.tags, {})                          AS tags,
               COALESCE(c.service_name, 'Unknown')           AS service,
               COALESCE(c.provider_name, 'azure')            AS cloud,
               COALESCE(c.record_date, c.charge_period_start, '') AS rdate
           FROM c
           WHERE c.tenant_id = @tid
             AND (
               (c.record_date    >= @start AND c.record_date    <= @end)
               OR
               (c.charge_period_start >= @start AND c.charge_period_start <= @end)
             )""",
        [
            {"name": "@tid",   "value": tenant_id},
            {"name": "@start", "value": start.isoformat()},
            {"name": "@end",   "value": end.isoformat()},
        ],
        partition_key=tenant_id,
    )
    return [
        {
            "cost_eur": float(r.get("cost_eur") or 0.0),
            "tags":     r.get("tags") or {},
            "service":  r.get("service", "Unknown"),
            "cloud":    r.get("cloud", "azure"),
            "date":     r.get("rdate", ""),
        }
        for r in rows
        if float(r.get("cost_eur") or 0.0) > 0
    ]


# ── Tree assembly ─────────────────────────────────────────────────────────────

def _build_tree(
    nodes: list[CostNode],
) -> tuple[list[CostNode], dict[str, list[CostNode]]]:
    """Return (root_nodes, children_map) where children_map maps parent_id → children."""
    children: dict[str, list[CostNode]] = {n.id: [] for n in nodes}
    roots: list[CostNode] = []
    for node in nodes:
        if node.parent_id and node.parent_id in children:
            children[node.parent_id].append(node)
        elif node.parent_id is None:
            roots.append(node)
        else:
            # orphan (parent deleted) — treat as root
            roots.append(node)
    return roots, children


def _compute_budget_status(
    total: float, budget: Optional[float]
) -> tuple[BudgetStatus, Optional[float]]:
    if budget is None or budget <= 0:
        return BudgetStatus.NO_BUDGET, None
    pct = (total / budget) * 100
    if pct >= 100:
        return BudgetStatus.BREACH, round(pct, 1)
    if pct >= 80:
        return BudgetStatus.WARNING, round(pct, 1)
    return BudgetStatus.OK, round(pct, 1)


def _rollup_node(
    node: CostNode,
    children_map: dict[str, list[CostNode]],
    records: list[dict],
    claimed: set[int],   # indices into records already claimed by a descendant
    depth: int = 0,
) -> NodeRollup:
    """
    Recursively compute rollup for a node.
    Records that match this node's tag_filters and are NOT yet claimed by a
    child are attributed as direct cost.  Children recurse first so they get
    priority over parent tag matches.
    """
    # Recurse into children first
    child_rollups: list[NodeRollup] = []
    for child in children_map.get(node.id, []):
        cr = _rollup_node(child, children_map, records, claimed, depth + 1)
        child_rollups.append(cr)

    # Cloud and service scope filters
    scope_clouds = {c.lower() for c in node.cloud_scope} if node.cloud_scope else set()

    # Attribute records to this node
    direct_cost = 0.0
    cloud_breakdown: dict[str, float] = {}
    service_breakdown: dict[str, float] = {}

    for idx, rec in enumerate(records):
        if idx in claimed:
            continue
        if scope_clouds and rec["cloud"].lower() not in scope_clouds:
            continue
        if not _matches_tag_filters(rec["tags"], node.tag_filters):
            continue
        claimed.add(idx)
        direct_cost += rec["cost_eur"]
        cloud_breakdown[rec["cloud"]] = cloud_breakdown.get(rec["cloud"], 0.0) + rec["cost_eur"]
        service_breakdown[rec["service"]] = service_breakdown.get(rec["service"], 0.0) + rec["cost_eur"]

    children_cost = sum(c.total_cost_eur for c in child_rollups)
    total_cost = direct_cost + children_cost
    budget_status, consumed_pct = _compute_budget_status(total_cost, node.budget_eur)

    return NodeRollup(
        node_id=node.id,
        node_name=node.name,
        node_type=node.node_type,
        parent_id=node.parent_id,
        depth=depth,
        direct_cost_eur=round(direct_cost, 4),
        children_cost_eur=round(children_cost, 4),
        total_cost_eur=round(total_cost, 4),
        budget_eur=node.budget_eur,
        budget_consumed_pct=consumed_pct,
        budget_status=budget_status,
        cloud_breakdown={k: round(v, 4) for k, v in cloud_breakdown.items()},
        service_breakdown=sorted(
            [{"service": k, "cost_eur": round(v, 4)} for k, v in service_breakdown.items()],
            key=lambda x: x["cost_eur"], reverse=True,
        )[:20],
        children=child_rollups,
    )


# ── Public interface ──────────────────────────────────────────────────────────

async def rollup(
    tenant_id: str,
    start: date,
    end: date,
) -> HierarchyRollup:
    """
    Compute the full cost hierarchy rollup for a tenant and time period.
    Works across all clouds — Azure CostRecords and FOCUS records alike.
    """
    settings = get_settings()
    cost_container = settings.cosmos_container_cost_records
    hier_container = settings.cosmos_container_hierarchy

    # Load nodes
    node_docs = await cosmos.query_items(
        hier_container,
        "SELECT * FROM c WHERE c.tenant_id=@tid AND c.type='hierarchy_node'",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    nodes = [CostNode.from_cosmos(d) for d in node_docs]

    if not nodes:
        return HierarchyRollup(
            tenant_id=tenant_id,
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            total_cost_eur=0.0,
            unallocated_eur=0.0,
            roots=[],
            node_count=0,
        )

    # Load cost records
    records = await _load_cost_records(tenant_id, start, end, cost_container)
    total_cost = sum(r["cost_eur"] for r in records)

    # Build and rollup the tree
    roots, children_map = _build_tree(nodes)
    claimed: set[int] = set()

    root_rollups = [
        _rollup_node(root, children_map, records, claimed)
        for root in roots
    ]

    allocated = sum(r.total_cost_eur for r in root_rollups)
    unallocated = max(0.0, total_cost - allocated)

    log.info(
        "hierarchy.rollup_complete",
        tenant_id=tenant_id,
        nodes=len(nodes),
        records=len(records),
        total_eur=round(total_cost, 2),
        allocated_eur=round(allocated, 2),
        unallocated_eur=round(unallocated, 2),
    )

    return HierarchyRollup(
        tenant_id=tenant_id,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        total_cost_eur=round(total_cost, 4),
        unallocated_eur=round(unallocated, 4),
        roots=root_rollups,
        node_count=len(nodes),
    )


async def node_breakdown(
    tenant_id: str,
    node_id: str,
    start: date,
    end: date,
) -> dict:
    """
    Return a detailed cost breakdown for a single hierarchy node:
    daily series, top services, cloud split, and budget burn-rate.
    """
    settings = get_settings()
    hier_container = settings.cosmos_container_hierarchy
    cost_container = settings.cosmos_container_cost_records

    docs = await cosmos.query_items(
        hier_container,
        "SELECT * FROM c WHERE c.tenant_id=@tid AND c.id=@nid AND c.type='hierarchy_node'",
        [{"name": "@tid", "value": tenant_id}, {"name": "@nid", "value": node_id}],
        partition_key=tenant_id,
    )
    if not docs:
        return {}
    node = CostNode.from_cosmos(docs[0])

    records = await _load_cost_records(tenant_id, start, end, cost_container)
    scope_clouds = {c.lower() for c in node.cloud_scope} if node.cloud_scope else set()

    matched = [
        r for r in records
        if (not scope_clouds or r["cloud"].lower() in scope_clouds)
        and _matches_tag_filters(r["tags"], node.tag_filters)
    ]

    total = sum(r["cost_eur"] for r in matched)
    by_cloud: dict[str, float] = {}
    by_service: dict[str, float] = {}
    by_day: dict[str, float] = {}

    for r in matched:
        by_cloud[r["cloud"]] = by_cloud.get(r["cloud"], 0.0) + r["cost_eur"]
        by_service[r["service"]] = by_service.get(r["service"], 0.0) + r["cost_eur"]
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["cost_eur"]

    budget_status, consumed_pct = _compute_budget_status(total, node.budget_eur)

    return {
        "node_id": node_id,
        "node_name": node.name,
        "node_type": node.node_type,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "total_cost_eur": round(total, 4),
        "budget_eur": node.budget_eur,
        "budget_consumed_pct": consumed_pct,
        "budget_status": budget_status,
        "tag_filters": node.tag_filters,
        "cloud_scope": node.cloud_scope,
        "by_cloud": {k: round(v, 4) for k, v in by_cloud.items()},
        "by_service": sorted(
            [{"service": k, "cost_eur": round(v, 4)} for k, v in by_service.items()],
            key=lambda x: x["cost_eur"], reverse=True,
        )[:25],
        "daily_series": sorted(
            [{"date": d, "cost_eur": round(c, 4)} for d, c in by_day.items()],
            key=lambda x: x["date"],
        ),
        "matched_records": len(matched),
    }
