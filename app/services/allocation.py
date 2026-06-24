"""
Allocation engine — 100% allocation without perfect tags
========================================================

The CloudZero-class problem: real environments are 40–70% tagged, so naive
tag-only chargeback leaves a large "Unallocated" bucket that finance can't act
on. CloudLens allocates 100% of spend by applying an ordered rule chain to every
FOCUS record, falling back through progressively coarser signals:

  1. Direct tag         — the record carries the allocation tag (cost_center=...)
  2. Tag inheritance    — another tag implies the cost-center via a mapping
                          (e.g. team=payments → cost_center=engineering)
  3. Account/project    — sub_account_id maps to a cost-center
                          (e.g. AWS account 1234 → data-platform)
  4. Name pattern       — resource_name/service matches a regex rule
                          (e.g. ^prod-erp- → erp)
  5. Shared split       — whatever is still unallocated is distributed across
                          allocated cost-centers (proportional or even), so the
                          final Unallocated bucket is €0.

Rules are data (an AllocationRuleSet), so each tenant configures their own
without code changes. Every allocated euro records *which rule* assigned it, so
the allocation is fully auditable.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RuleKind(str, Enum):
    TAG = "tag"                      # match on a tag key=value
    TAG_MAP = "tag_map"              # map one tag's value to a cost-center
    ACCOUNT = "account"              # map sub_account_id to a cost-center
    NAME_PATTERN = "name_pattern"    # regex on resource_name / service_name


@dataclass
class AllocationRule:
    kind: RuleKind
    cost_center: str
    # for TAG: match_key + match_value; for TAG_MAP: source_key (+ value_map)
    match_key: str = ""
    match_value: str = ""
    source_key: str = ""
    value_map: dict = field(default_factory=dict)
    accounts: tuple = ()
    pattern: str = ""


@dataclass
class AllocationRuleSet:
    dimension: str = "cost_center"
    rules: list[AllocationRule] = field(default_factory=list)
    shared_strategy: str = "proportional"   # "proportional" | "even" | "none"


@dataclass
class AllocatedGroup:
    name: str
    direct_eur: float
    shared_eur: float
    total_eur: float
    pct_of_total: float
    rule_breakdown: dict = field(default_factory=dict)   # rule_kind -> €


@dataclass
class AllocationResult:
    dimension: str
    total_eur: float
    allocated_pct: float                      # should be ~100 after shared split
    unallocated_eur: float
    groups: list[AllocatedGroup] = field(default_factory=list)
    coverage_before_shared_pct: float = 0.0   # how much the rules covered directly
    notes: list[str] = field(default_factory=list)


def _apply_rules(record: dict, ruleset: AllocationRuleSet) -> Optional[tuple[str, str]]:
    """Return (cost_center, rule_kind) for the first matching rule, else None."""
    tags = {k.lower(): v for k, v in (record.get("tags") or {}).items()}
    name = (record.get("resource_name") or record.get("service_name") or "").lower()
    account = str(record.get("sub_account_id") or "")

    for rule in ruleset.rules:
        if rule.kind == RuleKind.TAG:
            if tags.get(rule.match_key.lower()) == rule.match_value:
                return rule.cost_center, "tag"
        elif rule.kind == RuleKind.TAG_MAP:
            val = tags.get(rule.source_key.lower())
            if val and val in rule.value_map:
                return rule.value_map[val], "tag_map"
        elif rule.kind == RuleKind.ACCOUNT:
            if account in rule.accounts:
                return rule.cost_center, "account"
        elif rule.kind == RuleKind.NAME_PATTERN:
            if rule.pattern and re.search(rule.pattern, name):
                return rule.cost_center, "name_pattern"
    # implicit direct tag on the dimension itself
    direct = tags.get(ruleset.dimension.lower())
    if direct:
        return direct, "tag"
    return None


def allocate_full(records: list[dict], ruleset: AllocationRuleSet) -> AllocationResult:
    """Allocate 100% of spend using the rule chain + shared split."""
    direct: dict[str, float] = {}
    breakdown: dict[str, dict] = {}
    unallocated = 0.0
    total = 0.0

    for r in records:
        cost = float(r.get("effective_cost", r.get("billed_cost", r.get("cost_eur", 0.0))))
        total += cost
        match = _apply_rules(r, ruleset)
        if match:
            cc, kind = match
            direct[cc] = direct.get(cc, 0.0) + cost
            breakdown.setdefault(cc, {})
            breakdown[cc][kind] = breakdown[cc].get(kind, 0.0) + cost
        else:
            unallocated += cost

    allocated_direct = total - unallocated
    coverage = (allocated_direct / total * 100) if total > 0 else 0.0

    # shared split of the still-unallocated remainder
    shared: dict[str, float] = {g: 0.0 for g in direct}
    notes = []
    if unallocated > 0 and direct and ruleset.shared_strategy != "none":
        if ruleset.shared_strategy == "proportional" and allocated_direct > 0:
            for g, d in direct.items():
                shared[g] = unallocated * (d / allocated_direct)
            notes.append(f"€{unallocated:,.0f} residual allocated proportionally → 100% coverage.")
        elif ruleset.shared_strategy == "even":
            split = unallocated / len(direct)
            for g in direct:
                shared[g] = split
            notes.append(f"€{unallocated:,.0f} residual split evenly → 100% coverage.")
        unallocated_final = 0.0
    else:
        unallocated_final = unallocated

    groups = []
    for g, d in sorted(direct.items(), key=lambda kv: kv[1], reverse=True):
        s = round(shared.get(g, 0.0), 2)
        tot = round(d + s, 2)
        groups.append(AllocatedGroup(
            name=g, direct_eur=round(d, 2), shared_eur=s, total_eur=tot,
            pct_of_total=round(tot / total * 100, 1) if total > 0 else 0.0,
            rule_breakdown={k: round(v, 2) for k, v in breakdown.get(g, {}).items()},
        ))
    if unallocated_final > 0:
        groups.append(AllocatedGroup(
            name="Unallocated", direct_eur=round(unallocated_final, 2), shared_eur=0.0,
            total_eur=round(unallocated_final, 2),
            pct_of_total=round(unallocated_final / total * 100, 1) if total > 0 else 0.0,
        ))

    allocated_pct = round((total - unallocated_final) / total * 100, 1) if total > 0 else 0.0
    if coverage < 50:
        notes.append(f"Direct rule coverage is {coverage:.0f}%. Add account/name-pattern "
                     "rules to raise auditable allocation before the shared split.")
    return AllocationResult(
        dimension=ruleset.dimension, total_eur=round(total, 2),
        allocated_pct=allocated_pct, unallocated_eur=round(unallocated_final, 2),
        groups=groups, coverage_before_shared_pct=round(coverage, 1), notes=notes,
    )
