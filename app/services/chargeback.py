"""
CloudLens Chargeback / Showback Engine
======================================

Turns raw tagged cost data into business cost-allocation views. The hard part of
chargeback — the part most tools get wrong — is what to do with shared and
untagged spend. CloudLens supports three explicit strategies:

  showback     — visibility only. Each cost-center sees its directly-tagged
                 spend; untagged spend is reported separately as "Unallocated".

  proportional — chargeback. Untagged / shared spend is distributed across
                 cost-centers in proportion to their directly-tagged spend
                 (the most defensible default — heavy users absorb more shared
                 cost).

  even         — chargeback. Untagged / shared spend is split evenly across all
                 cost-centers (useful for truly common platform costs).

Allocation is by any tag key (cost_center, team, project, environment, owner).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AllocationStrategy(str, Enum):
    SHOWBACK = "showback"
    PROPORTIONAL = "proportional"
    EVEN = "even"


@dataclass
class AllocationGroup:
    name: str                       # tag value, e.g. "engineering"
    direct_eur: float               # directly-tagged spend
    allocated_shared_eur: float     # share of untagged/shared spend assigned here
    total_eur: float
    pct_of_total: float
    resource_count: int = 0
    budget_eur: Optional[float] = None
    budget_status: Optional[str] = None     # "ok" | "warning" | "breach"


@dataclass
class ChargebackResult:
    dimension: str                  # the tag key allocated by
    strategy: str
    period_start: str
    period_end: str
    total_spend_eur: float
    tagged_spend_eur: float
    untagged_spend_eur: float
    tagging_coverage_pct: float     # % of spend that carried the dimension tag
    groups: list[AllocationGroup] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def allocate(
    cost_records: list[dict],            # need: cost_eur, tags{}, resource_id
    dimension: str = "cost_center",
    strategy: AllocationStrategy = AllocationStrategy.PROPORTIONAL,
    period_start: str = "",
    period_end: str = "",
    budgets: Optional[dict] = None,      # {group_name: monthly_budget_eur}
) -> ChargebackResult:
    """Allocate cost records to cost-centers by a tag dimension."""
    budgets = budgets or {}
    direct: dict[str, float] = {}
    counts: dict[str, set] = {}
    untagged = 0.0
    total = 0.0

    for r in cost_records:
        cost = float(r.get("cost_eur", 0.0))
        total += cost
        tags = r.get("tags") or {}
        # tag lookup is case-insensitive on the key
        val = None
        for k, v in tags.items():
            if k.lower() == dimension.lower() and v:
                val = str(v)
                break
        if val is None:
            untagged += cost
        else:
            direct[val] = direct.get(val, 0.0) + cost
            counts.setdefault(val, set()).add(r.get("resource_id", ""))

    tagged = total - untagged
    coverage = (tagged / total * 100) if total > 0 else 0.0

    # distribute untagged/shared spend per strategy
    allocated_shared: dict[str, float] = {g: 0.0 for g in direct}
    notes: list[str] = []
    if strategy == AllocationStrategy.SHOWBACK:
        notes.append("Showback mode: untagged spend shown separately, not charged back.")
    elif untagged > 0 and direct:
        if strategy == AllocationStrategy.PROPORTIONAL and tagged > 0:
            for g, d in direct.items():
                allocated_shared[g] = untagged * (d / tagged)
            notes.append(f"€{untagged:,.0f} untagged spend distributed proportionally "
                         "to each cost-center's tagged share.")
        elif strategy == AllocationStrategy.EVEN:
            split = untagged / len(direct)
            for g in direct:
                allocated_shared[g] = split
            notes.append(f"€{untagged:,.0f} untagged spend split evenly across "
                         f"{len(direct)} cost-centers.")

    groups: list[AllocationGroup] = []
    for g, d in sorted(direct.items(), key=lambda kv: kv[1], reverse=True):
        shared = round(allocated_shared.get(g, 0.0), 2)
        tot = round(d + shared, 2)
        bud = budgets.get(g)
        status = None
        if bud:
            ratio = tot / bud if bud > 0 else 0
            status = "breach" if ratio > 1.0 else "warning" if ratio >= 0.85 else "ok"
        groups.append(AllocationGroup(
            name=g, direct_eur=round(d, 2), allocated_shared_eur=shared,
            total_eur=tot, pct_of_total=round(tot / total * 100, 1) if total > 0 else 0.0,
            resource_count=len(counts.get(g, set())),
            budget_eur=bud, budget_status=status,
        ))

    # In showback mode, surface untagged as its own group for visibility.
    if strategy == AllocationStrategy.SHOWBACK and untagged > 0:
        groups.append(AllocationGroup(
            name="Unallocated", direct_eur=round(untagged, 2), allocated_shared_eur=0.0,
            total_eur=round(untagged, 2),
            pct_of_total=round(untagged / total * 100, 1) if total > 0 else 0.0,
        ))

    if coverage < 60:
        notes.append(f"Tagging coverage is {coverage:.0f}% — improving the '{dimension}' "
                     "tag across resources will sharpen allocation accuracy.")

    return ChargebackResult(
        dimension=dimension, strategy=strategy.value,
        period_start=period_start, period_end=period_end,
        total_spend_eur=round(total, 2), tagged_spend_eur=round(tagged, 2),
        untagged_spend_eur=round(untagged, 2), tagging_coverage_pct=round(coverage, 1),
        groups=groups, notes=notes,
    )
