"""
Commitment management
=====================

Reserved Instances, Savings Plans (AWS), Committed Use Discounts (GCP), and
reserved capacity (Azure/Alibaba/OCI) are the biggest savings lever after waste.
This service answers three questions across all providers:

  Coverage    — what % of commitment-eligible spend is actually covered by a
                commitment vs. paid on-demand?
  Utilization — of the commitments held, what % is actually being used (waste if
                a reservation sits idle)?
  Opportunity — how much could be saved by buying additional commitments to
                cover steady on-demand usage, at a conservative discount rate?

Recommendations are deliberately conservative: we only recommend committing to
the stable on-demand baseline (the floor of recent usage), never the peak, so a
recommendation never over-commits a customer into a reservation they can't use.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Conservative blended discount assumptions by commitment type (1-yr, no-upfront).
# These are intentionally cautious; real rates vary by service/region/term.
_DISCOUNT = {
    "Reserved": 0.40,
    "Savings Plan": 0.27,
    "Committed Use Discount": 0.37,
}


@dataclass
class ProviderCommitmentSummary:
    provider: str
    commitment_eligible_eur: float       # compute/db spend that could be committed
    covered_eur: float                   # already covered by commitments
    on_demand_eur: float                 # eligible spend paid on-demand
    coverage_pct: float
    utilization_pct: float               # of held commitments, how much is used
    idle_commitment_eur: float           # commitment paid for but unused (waste)


@dataclass
class CommitmentRecommendation:
    provider: str
    commitment_type: str
    service: str
    recommended_hourly_eur: float
    term_months: int
    estimated_monthly_saving_eur: float
    rationale: str
    rationale_it: str = ""


@dataclass
class CommitmentReport:
    total_eligible_eur: float
    total_covered_eur: float
    blended_coverage_pct: float
    blended_utilization_pct: float
    total_idle_commitment_eur: float
    monthly_opportunity_eur: float
    by_provider: list[ProviderCommitmentSummary] = field(default_factory=list)
    recommendations: list[CommitmentRecommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# FOCUS service categories eligible for commitments (compute + databases mainly).
_ELIGIBLE_CATEGORIES = {"Compute", "Databases"}


def analyze_commitments(
    focus_records: list[dict],          # need effective_cost, service_category, provider_name, commitment_discount_type
    held_commitments: list[dict],       # need provider_name, hourly_commitment_eur, utilization_pct, commitment_type, service
    days: int = 30,
) -> CommitmentReport:
    """Compute coverage / utilization / opportunity across all providers."""
    # ── aggregate eligible spend & current coverage per provider ──
    per_provider: dict[str, dict] = {}
    for r in focus_records:
        cat = r.get("service_category", "")
        if cat not in _ELIGIBLE_CATEGORIES:
            continue
        prov = r.get("provider_name", "Unknown")
        cost = float(r.get("effective_cost", r.get("billed_cost", 0.0)))
        committed = r.get("commitment_discount_type", "") not in ("", None)
        p = per_provider.setdefault(prov, {"eligible": 0.0, "covered": 0.0, "on_demand": 0.0})
        p["eligible"] += cost
        if committed:
            p["covered"] += cost
        else:
            p["on_demand"] += cost

    # ── utilization & idle from held commitments ──
    util_by_provider: dict[str, list] = {}
    monthly_commit_by_provider: dict[str, float] = {}
    for c in held_commitments:
        prov = c.get("provider_name", "Unknown")
        util = float(c.get("utilization_pct", 0.0))
        monthly = float(c.get("hourly_commitment_eur", 0.0)) * 24 * 30
        util_by_provider.setdefault(prov, []).append(util)
        monthly_commit_by_provider[prov] = monthly_commit_by_provider.get(prov, 0.0) + monthly

    summaries: list[ProviderCommitmentSummary] = []
    total_eligible = total_covered = total_idle = 0.0
    for prov, p in per_provider.items():
        eligible = p["eligible"]
        covered = p["covered"]
        on_demand = p["on_demand"]
        cov_pct = (covered / eligible * 100) if eligible > 0 else 0.0
        utils = util_by_provider.get(prov, [])
        util_pct = (sum(utils) / len(utils)) if utils else 0.0
        idle = monthly_commit_by_provider.get(prov, 0.0) * (1 - util_pct / 100)
        summaries.append(ProviderCommitmentSummary(
            provider=prov, commitment_eligible_eur=round(eligible, 2),
            covered_eur=round(covered, 2), on_demand_eur=round(on_demand, 2),
            coverage_pct=round(cov_pct, 1), utilization_pct=round(util_pct, 1),
            idle_commitment_eur=round(idle, 2),
        ))
        total_eligible += eligible
        total_covered += covered
        total_idle += idle

    # ── recommendations: commit the stable on-demand baseline ──
    recs: list[CommitmentRecommendation] = []
    monthly_opportunity = 0.0
    for s in summaries:
        if s.on_demand_eur <= 0:
            continue
        # treat 70% of current on-demand eligible spend as the stable baseline
        baseline_monthly = s.on_demand_eur * 0.70 * (30 / days)
        ctype, disc = _commitment_for_provider(s.provider)
        saving = baseline_monthly * disc
        if saving < 5:
            continue
        monthly_opportunity += saving
        recs.append(CommitmentRecommendation(
            provider=s.provider, commitment_type=ctype, service="Compute",
            recommended_hourly_eur=round(baseline_monthly / (24 * 30), 4),
            term_months=12,
            estimated_monthly_saving_eur=round(saving, 2),
            rationale=(f"~€{baseline_monthly:,.0f}/mo of {s.provider} compute runs on-demand "
                       f"at a stable baseline; a 1-year {ctype} would save ~€{saving:,.0f}/mo "
                       f"(~{int(disc*100)}% on that baseline)."),
            rationale_it=(f"~€{baseline_monthly:,.0f}/mese di compute {s.provider} è on-demand "
                          f"a baseline stabile; un {ctype} annuale risparmierebbe ~€{saving:,.0f}/mese."),
        ))

    notes = []
    if total_idle > 0:
        notes.append(f"€{total_idle:,.0f}/mo of held commitments is idle (low utilization) — "
                     "review before buying more.")

    blended_cov = (total_covered / total_eligible * 100) if total_eligible > 0 else 0.0
    all_utils = [u for lst in util_by_provider.values() for u in lst]
    blended_util = (sum(all_utils) / len(all_utils)) if all_utils else 0.0

    return CommitmentReport(
        total_eligible_eur=round(total_eligible, 2),
        total_covered_eur=round(total_covered, 2),
        blended_coverage_pct=round(blended_cov, 1),
        blended_utilization_pct=round(blended_util, 1),
        total_idle_commitment_eur=round(total_idle, 2),
        monthly_opportunity_eur=round(monthly_opportunity, 2),
        by_provider=sorted(summaries, key=lambda s: s.on_demand_eur, reverse=True),
        recommendations=sorted(recs, key=lambda r: r.estimated_monthly_saving_eur, reverse=True),
        notes=notes,
    )


def _commitment_for_provider(provider: str) -> tuple[str, float]:
    p = provider.lower()
    if "aws" in p or "amazon" in p:
        return "Savings Plan", _DISCOUNT["Savings Plan"]
    if "google" in p or "gcp" in p:
        return "Committed Use Discount", _DISCOUNT["Committed Use Discount"]
    return "Reserved", _DISCOUNT["Reserved"]
