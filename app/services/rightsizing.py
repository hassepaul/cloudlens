"""
Rightsizing engine
==================

Generates CloudLens's *own* rightsizing recommendations (rather than reselling a
cloud provider's native advisor). For each compute resource it takes observed
utilization — CPU and memory, ideally p95/peak over a 14-90 day window — plus the
current instance type and billed cost, and finds the cheapest instance (across
families) that still satisfies the workload's requirements with a safety buffer.

Key design points:
  * Uses BOTH CPU and memory. Memory-bound workloads (analytics, in-memory DBs,
    ML) are exactly where CPU-only tools under-recommend — this is a deliberate
    edge over memory-blind incumbents.
  * Headroom buffer (default 30%) is applied above observed peak so we never
    recommend a target that would throttle the workload.
  * Cross-family: a low-CPU/high-memory box can move to a memory-optimized family
    that's cheaper than staying in general-purpose.
  * Confidence scales with the observation window — a 14-day window on a workload
    with monthly seasonality is explicitly down-weighted.
  * Terminate candidate when utilization is effectively zero.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from app.services.instance_catalog import InstanceType, lookup, candidates_for

DEFAULT_HEADROOM = 0.30          # 30% buffer above observed peak
TERMINATE_CPU = 1.0             # avg CPU% below this + near-zero mem ⇒ terminate
TERMINATE_MEM = 2.0
# BUG-015: FX rate was hardcoded. It now reads from the environment so ops can
# override it without a code deploy.  Wire to the live ECB API in production:
# https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A
import os as _os
USD_TO_EUR: float = float(_os.environ.get("USD_TO_EUR", "0.92"))


@dataclass
class RightsizingRecommendation:
    resource_id: str
    resource_name: str
    provider: str
    current_type: str
    recommended_type: Optional[str]      # None when no change / terminate
    action: str                          # "downsize" | "terminate" | "no_change" | "upsize"
    current_monthly_eur: float
    projected_monthly_eur: float
    monthly_saving_eur: float
    cpu_peak_pct: float
    mem_peak_pct: float
    required_vcpu: float
    required_mem_gib: float
    confidence: str                      # "high" | "medium" | "low"
    rationale: str
    rationale_it: str = ""
    cross_family: bool = False


@dataclass
class RightsizingResult:
    scanned: int
    recommendations: list[RightsizingRecommendation] = field(default_factory=list)
    total_monthly_saving_eur: float = 0.0
    notes: list[str] = field(default_factory=list)


def _confidence(observation_days: int, samples: int) -> str:
    if observation_days >= 30 and samples >= 20:
        return "high"
    if observation_days >= 14 and samples >= 10:
        return "medium"
    return "low"


def recommend_one(
    resource_id: str,
    resource_name: str,
    provider: str,
    current_type: str,
    current_monthly_eur: float,
    cpu_peak_pct: float,
    mem_peak_pct: float,
    observation_days: int = 30,
    samples: int = 30,
    headroom: float = DEFAULT_HEADROOM,
) -> Optional[RightsizingRecommendation]:
    """Recommend a target instance for one resource (None if unknown SKU)."""
    cur = lookup(provider, current_type)
    if cur is None:
        return None   # can't reason without knowing the current SKU's capacity

    # required capacity = observed peak (as a fraction of current capacity) + buffer
    req_vcpu = cur.vcpu * (cpu_peak_pct / 100.0) * (1 + headroom)
    req_mem = cur.memory_gib * (mem_peak_pct / 100.0) * (1 + headroom)
    conf = _confidence(observation_days, samples)

    # terminate candidate
    if cpu_peak_pct < TERMINATE_CPU and mem_peak_pct < TERMINATE_MEM:
        return RightsizingRecommendation(
            resource_id=resource_id, resource_name=resource_name, provider=provider,
            current_type=current_type, recommended_type=None, action="terminate",
            current_monthly_eur=round(current_monthly_eur, 2),
            projected_monthly_eur=0.0, monthly_saving_eur=round(current_monthly_eur, 2),
            cpu_peak_pct=cpu_peak_pct, mem_peak_pct=mem_peak_pct,
            required_vcpu=round(req_vcpu, 2), required_mem_gib=round(req_mem, 2),
            confidence=conf,
            rationale=(f"{resource_name} peaked at {cpu_peak_pct:.1f}% CPU / {mem_peak_pct:.1f}% "
                       f"memory over {observation_days} days — effectively idle. Consider "
                       f"terminating to recover €{current_monthly_eur:,.0f}/mo."),
            rationale_it=(f"{resource_name} ha raggiunto un picco del {cpu_peak_pct:.1f}% CPU / "
                          f"{mem_peak_pct:.1f}% memoria — di fatto inattiva. Valutare l'eliminazione "
                          f"per recuperare €{current_monthly_eur:,.0f}/mese."),
        )

    # find cheapest candidate that satisfies BOTH requirements, cheaper than current
    best: Optional[InstanceType] = None
    for cand in candidates_for(provider):          # cheapest first
        if cand.vcpu >= req_vcpu and cand.memory_gib >= req_mem and cand.hourly_usd < cur.hourly_usd:
            best = cand
            break

    if best is None:
        return RightsizingRecommendation(
            resource_id=resource_id, resource_name=resource_name, provider=provider,
            current_type=current_type, recommended_type=current_type, action="no_change",
            current_monthly_eur=round(current_monthly_eur, 2),
            projected_monthly_eur=round(current_monthly_eur, 2), monthly_saving_eur=0.0,
            cpu_peak_pct=cpu_peak_pct, mem_peak_pct=mem_peak_pct,
            required_vcpu=round(req_vcpu, 2), required_mem_gib=round(req_mem, 2),
            confidence=conf,
            rationale=f"{resource_name} is appropriately sized for its CPU/memory profile.",
            rationale_it=f"{resource_name} è dimensionata correttamente per il suo profilo CPU/memoria.",
        )

    # project new cost from the price ratio against the actual billed cost
    ratio = best.hourly_usd / cur.hourly_usd
    projected = current_monthly_eur * ratio
    saving = current_monthly_eur - projected
    cross = best.family != cur.family
    return RightsizingRecommendation(
        resource_id=resource_id, resource_name=resource_name, provider=provider,
        current_type=current_type, recommended_type=best.name, action="downsize",
        current_monthly_eur=round(current_monthly_eur, 2),
        projected_monthly_eur=round(projected, 2), monthly_saving_eur=round(saving, 2),
        cpu_peak_pct=cpu_peak_pct, mem_peak_pct=mem_peak_pct,
        required_vcpu=round(req_vcpu, 2), required_mem_gib=round(req_mem, 2),
        confidence=conf, cross_family=cross,
        rationale=(f"{resource_name} ({current_type}) peaked at {cpu_peak_pct:.0f}% CPU / "
                   f"{mem_peak_pct:.0f}% memory. Needs ~{req_vcpu:.1f} vCPU / {req_mem:.0f} GiB "
                   f"with a {int(headroom*100)}% buffer — {best.name}"
                   f"{' (cross-family)' if cross else ''} covers it for €{saving:,.0f}/mo less."),
        rationale_it=(f"{resource_name} ({current_type}) ha raggiunto {cpu_peak_pct:.0f}% CPU / "
                      f"{mem_peak_pct:.0f}% memoria. Serve ~{req_vcpu:.1f} vCPU / {req_mem:.0f} GiB; "
                      f"{best.name} basta per €{saving:,.0f}/mese in meno."),
    )


def recommend(resources: list[dict], headroom: float = DEFAULT_HEADROOM) -> RightsizingResult:
    """
    resources: [{resource_id, resource_name, provider, instance_type,
                 monthly_eur, cpu_peak_pct, mem_peak_pct, observation_days, samples}]
    """
    recs: list[RightsizingRecommendation] = []
    unknown = 0
    for r in resources:
        rec = recommend_one(
            resource_id=r.get("resource_id", ""),
            resource_name=r.get("resource_name", r.get("resource_id", "")),
            provider=r.get("provider", ""),
            current_type=r.get("instance_type", ""),
            current_monthly_eur=float(r.get("monthly_eur", 0.0)),
            cpu_peak_pct=float(r.get("cpu_peak_pct", 0.0)),
            mem_peak_pct=float(r.get("mem_peak_pct", 0.0)),
            observation_days=int(r.get("observation_days", 30)),
            samples=int(r.get("samples", 30)),
            headroom=headroom,
        )
        if rec is None:
            unknown += 1
            continue
        if rec.action in ("downsize", "terminate"):
            recs.append(rec)

    recs.sort(key=lambda x: x.monthly_saving_eur, reverse=True)
    total = round(sum(r.monthly_saving_eur for r in recs), 2)
    notes = []
    if unknown:
        notes.append(f"{unknown} resource(s) skipped — instance type not in the catalog "
                     "(extend the catalog from the provider pricing API to cover them).")
    return RightsizingResult(
        scanned=len(resources), recommendations=recs,
        total_monthly_saving_eur=total, notes=notes,
    )
