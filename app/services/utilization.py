"""
Utilization analysis
====================

Turns raw per-resource CPU/memory utilization into the "are we over-capacitied?"
view: a per-resource over-capacity score and an estate-level rollup (how much of
the fleet is over-provisioned, and how much spend is reclaimable).

over_capacity_score (0–100): how over-provisioned a resource is. 100 = totally
idle; 0 = fully utilized. Driven by the *headroom* on the busier of the two
dimensions (CPU vs memory) — a box at 80% memory but 5% CPU is NOT very
over-capacitied, because memory is the binding constraint.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ResourceUtilization:
    resource_id: str
    resource_name: str
    provider: str
    service: str
    cpu_peak_pct: float
    mem_peak_pct: float
    monthly_eur: float
    over_capacity_score: int
    band: str                  # "idle" | "over" | "balanced" | "hot"


@dataclass
class UtilizationSummary:
    resources: int
    avg_cpu_pct: float
    avg_mem_pct: float
    over_provisioned_count: int
    idle_count: int
    hot_count: int
    reclaimable_monthly_eur: float       # spend on the over-provisioned headroom
    by_band: dict = field(default_factory=dict)
    worst: list[ResourceUtilization] = field(default_factory=list)


def _score_and_band(cpu: float, mem: float) -> tuple[int, str]:
    """Over-capacity = headroom on the *binding* (higher-utilized) dimension."""
    binding = max(cpu, mem)
    score = int(round(max(0.0, 100.0 - binding)))
    if binding < 5:
        band = "idle"
    elif binding < 40:
        band = "over"
    elif binding < 85:
        band = "balanced"
    else:
        band = "hot"        # near capacity — candidate for UPsizing, not down
    return score, band


def analyze(resources: list[dict]) -> tuple[list[ResourceUtilization], UtilizationSummary]:
    """
    resources: [{resource_id, resource_name, provider, service,
                 cpu_peak_pct, mem_peak_pct, monthly_eur}]
    """
    rows: list[ResourceUtilization] = []
    bands = {"idle": 0, "over": 0, "balanced": 0, "hot": 0}
    reclaimable = 0.0
    cpu_sum = mem_sum = 0.0

    for r in resources:
        cpu = float(r.get("cpu_peak_pct", 0.0))
        mem = float(r.get("mem_peak_pct", 0.0))
        cost = float(r.get("monthly_eur", 0.0))
        score, band = _score_and_band(cpu, mem)
        bands[band] += 1
        cpu_sum += cpu
        mem_sum += mem
        # reclaimable = the share of cost attributable to unused headroom on the
        # binding dimension, only for over/idle resources
        if band in ("idle", "over"):
            reclaimable += cost * (score / 100.0)
        rows.append(ResourceUtilization(
            resource_id=r.get("resource_id", ""),
            resource_name=r.get("resource_name", r.get("resource_id", "")),
            provider=r.get("provider", ""), service=r.get("service", ""),
            cpu_peak_pct=round(cpu, 1), mem_peak_pct=round(mem, 1),
            monthly_eur=round(cost, 2), over_capacity_score=score, band=band,
        ))

    n = len(resources) or 1
    rows.sort(key=lambda x: (x.over_capacity_score, x.monthly_eur), reverse=True)
    summary = UtilizationSummary(
        resources=len(resources),
        avg_cpu_pct=round(cpu_sum / n, 1), avg_mem_pct=round(mem_sum / n, 1),
        over_provisioned_count=bands["over"], idle_count=bands["idle"],
        hot_count=bands["hot"], reclaimable_monthly_eur=round(reclaimable, 2),
        by_band=bands, worst=rows[:10],
    )
    return rows, summary
