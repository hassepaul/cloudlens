"""Hierarchy / portfolio models — cost nodes and rollup results."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid

from pydantic import BaseModel, Field


class NodeType(str, Enum):
    COMPANY       = "company"
    DIVISION      = "division"
    BUSINESS_UNIT = "business_unit"
    TEAM          = "team"
    PROJECT       = "project"
    ENVIRONMENT   = "environment"   # e.g. prod / staging / dev across all teams


class CostNode(BaseModel):
    """One node in the cost hierarchy tree."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=160)
    node_type: NodeType
    parent_id: Optional[str] = None   # null = root node
    description: str = ""

    # Tag-based cost attribution: cost records whose tags match ALL key-value
    # pairs here are attributed to this node (before children re-attribute).
    # Values can be a single string or a list (any match).
    # Example: {"cost_center": "engineering", "team": ["platform", "product"]}
    tag_filters: dict = Field(default_factory=dict)

    # Optional cloud scope — empty = all clouds
    cloud_scope: list[str] = Field(default_factory=list)

    # Budget for this node (not counting children's budgets separately)
    budget_eur: Optional[float] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["type"] = "hierarchy_node"
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "CostNode":
        d = {k: v for k, v in doc.items()
             if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        d.pop("type", None)
        return cls(**d)


class CostNodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    node_type: NodeType
    parent_id: Optional[str] = None
    description: str = ""
    tag_filters: dict = Field(default_factory=dict)
    cloud_scope: list[str] = Field(default_factory=list)
    budget_eur: Optional[float] = None


class CostNodeUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None
    description: Optional[str] = None
    tag_filters: Optional[dict] = None
    cloud_scope: Optional[list[str]] = None
    budget_eur: Optional[float] = None


# ── Rollup results ────────────────────────────────────────────────────────────

class BudgetStatus(str, Enum):
    OK         = "ok"           # < 80 % consumed
    WARNING    = "warning"      # 80–99 % consumed
    BREACH     = "breach"       # >= 100 % consumed
    NO_BUDGET  = "no_budget"    # no budget set


class NodeRollup(BaseModel):
    """Cost rollup result for a single node in the tree."""
    node_id: str
    node_name: str
    node_type: NodeType
    parent_id: Optional[str]
    depth: int                          # 0 = root

    direct_cost_eur: float              # cost attributed directly to this node's tag_filters
    children_cost_eur: float            # sum of all children's total_cost_eur
    total_cost_eur: float               # direct + children

    budget_eur: Optional[float]
    budget_consumed_pct: Optional[float]
    budget_status: BudgetStatus

    cloud_breakdown: dict = Field(default_factory=dict)    # {cloud: cost_eur}
    service_breakdown: list[dict] = Field(default_factory=list)  # [{service, cost_eur}]

    children: list["NodeRollup"] = Field(default_factory=list)


NodeRollup.model_rebuild()


class HierarchyRollup(BaseModel):
    """Full hierarchy rollup result for a tenant and time range."""
    tenant_id: str
    period_start: str
    period_end: str
    total_cost_eur: float
    unallocated_eur: float   # cost that doesn't match any node's tag_filters
    roots: list[NodeRollup]  # top-level nodes (parent_id = null)
    node_count: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
