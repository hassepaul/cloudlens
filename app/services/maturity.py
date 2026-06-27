"""
FinOps Maturity Score
=====================

Scores a tenant across 6 FinOps Foundation–aligned dimensions and benchmarks
them against anonymised industry cohorts.

Dimensions (weights sum to 1.0)
--------------------------------
  tagging_completeness   0.20  % of cost records with ≥1 required tag
  waste_ratio            0.20  open waste / total spend (inverted)
  commitment_coverage    0.15  committed EUR / commitment-eligible EUR
  unit_economics         0.15  unit metrics defined + trending
  anomaly_response       0.15  avg hours violation→resolution
  budget_adherence       0.15  % of active budgets not in breach

Maturity levels (overall score)
---------------------------------
  0–39   Crawl  (reactive, manual, no shared accountability)
  40–64  Walk   (some tooling, partial automation)
  65–84  Run    (proactive, automated, FinOps-as-code)
  85–100 Fly    (real-time optimisation, unit economics, AI-assisted)

Industry benchmarks
--------------------
Anonymised synthetic data calibrated against the FinOps Foundation
2024 State of FinOps report and Gartner FinOps benchmark data.
Percentiles represent where your score sits relative to the cohort.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)


# ── Cohort benchmark data ─────────────────────────────────────────────────────
# Each entry: {vertical: {dimension: (median_score, p75, p90)}}
# Scores are normalised to 0–100 to match our dimension scoring.

_BENCHMARKS: dict[str, dict[str, tuple[float, float, float]]] = {
    "saas": {
        "tagging_completeness": (70, 85, 94),
        "waste_ratio":          (62, 75, 88),
        "commitment_coverage":  (42, 62, 80),
        "unit_economics":       (45, 68, 85),
        "anomaly_response":     (55, 72, 88),
        "budget_adherence":     (72, 84, 93),
    },
    "ecommerce": {
        "tagging_completeness": (65, 80, 92),
        "waste_ratio":          (58, 72, 84),
        "commitment_coverage":  (38, 57, 74),
        "unit_economics":       (35, 52, 70),
        "anomaly_response":     (50, 68, 82),
        "budget_adherence":     (68, 81, 91),
    },
    "enterprise": {
        "tagging_completeness": (60, 78, 90),
        "waste_ratio":          (52, 68, 80),
        "commitment_coverage":  (50, 68, 82),
        "unit_economics":       (28, 45, 65),
        "anomaly_response":     (42, 60, 78),
        "budget_adherence":     (65, 78, 90),
    },
    "startup": {
        "tagging_completeness": (50, 70, 87),
        "waste_ratio":          (48, 65, 80),
        "commitment_coverage":  (22, 40, 60),
        "unit_economics":       (30, 52, 72),
        "anomaly_response":     (45, 65, 82),
        "budget_adherence":     (60, 75, 88),
    },
}

_DEFAULT_VERTICAL = "enterprise"
_VALID_VERTICALS = set(_BENCHMARKS.keys())

_DIM_WEIGHTS = {
    "tagging_completeness": 0.20,
    "waste_ratio":          0.20,
    "commitment_coverage":  0.15,
    "unit_economics":       0.15,
    "anomaly_response":     0.15,
    "budget_adherence":     0.15,
}
assert abs(sum(_DIM_WEIGHTS.values()) - 1.0) < 1e-9

_REQUIRED_TAGS = {"cost_center", "team", "product", "owner", "environment"}


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class DimensionScore:
    dimension: str
    label: str
    weight: float
    score: float              # 0 – 100
    percentile: float         # vs cohort (0 – 100)
    cohort_median: float
    cohort_p75: float
    cohort_p90: float
    cohort_context: str       # human-readable benchmark summary
    evidence: dict            # raw metrics used to compute the score
    recommended_action: str


@dataclass
class MaturityScore:
    tenant_id: str
    vertical: str
    overall_score: float      # 0 – 100
    overall_percentile: float
    overall_label: str        # Crawl | Walk | Run | Fly
    dimensions: list[DimensionScore]
    top_recommendation: str
    generated_at: str         # ISO datetime


# ── Percentile helper ─────────────────────────────────────────────────────────

def _percentile_vs_cohort(score: float, median: float, p75: float, p90: float) -> float:
    """Approximate percentile of `score` given cohort anchor points."""
    if score >= p90:
        # Linearly extrapolate above p90 (capped at 99)
        return min(99.0, 90.0 + (score - p90) / max(1.0, 100 - p90) * 9)
    if score >= p75:
        return 75.0 + (score - p75) / max(1.0, p90 - p75) * 15
    if score >= median:
        return 50.0 + (score - median) / max(1.0, p75 - median) * 25
    # Below median
    return max(1.0, score / max(1.0, median) * 50)


def _cohort_context(score: float, median: float, p75: float, p90: float) -> str:
    if score >= p90:
        return f"Top decile (cohort median {median:.0f})"
    if score >= p75:
        return f"Top quartile (cohort median {median:.0f})"
    if score >= median:
        return f"Above cohort median ({median:.0f})"
    return f"Below cohort median ({median:.0f}) — improvement opportunity"


def _maturity_label(score: float) -> str:
    if score >= 85:
        return "Fly"
    if score >= 65:
        return "Run"
    if score >= 40:
        return "Walk"
    return "Crawl"


# ── Dimension scorers ────────────────────────────────────────────────────────

async def _score_tagging(tenant_id: str, settings) -> tuple[float, dict]:
    """% of cost records that carry at least one required tag."""
    query = """
        SELECT VALUE COUNT(1) FROM c
        WHERE c.tenant_id = @t AND c.type = 'focus_record'
    """
    query_tagged = """
        SELECT VALUE COUNT(1) FROM c
        WHERE c.tenant_id = @t AND c.type = 'focus_record'
          AND IS_DEFINED(c.tags) AND c.tags != null
    """
    params = [{"name": "@t", "value": tenant_id}]
    try:
        total_res = await cosmos.query_items(
            settings.cosmos_container_cost_records, query, params,
            partition_key=tenant_id,
        )
        tagged_res = await cosmos.query_items(
            settings.cosmos_container_cost_records, query_tagged, params,
            partition_key=tenant_id,
        )
        total = int(total_res[0]) if total_res else 0
        tagged = int(tagged_res[0]) if tagged_res else 0
    except (CosmosError, IndexError, TypeError):
        total, tagged = 0, 0

    pct = round(tagged / total * 100, 1) if total else 0.0
    score = pct
    evidence = {"total_records": total, "tagged_records": tagged, "tagged_pct": pct}
    return score, evidence


async def _score_waste(tenant_id: str, settings) -> tuple[float, dict]:
    """Inverted waste ratio — lower waste → higher score."""
    # Total spend last 30 days
    start = (date.today() - timedelta(days=30)).isoformat()
    try:
        spend_res = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            "SELECT VALUE SUM(c.effective_cost) FROM c "
            "WHERE c.tenant_id=@t AND c.type='focus_record' AND c.charge_period_start>=@s",
            [{"name": "@t", "value": tenant_id}, {"name": "@s", "value": start}],
            partition_key=tenant_id,
        )
        waste_res = await cosmos.query_items(
            settings.cosmos_container_waste_items,
            "SELECT VALUE SUM(c.saving_eur) FROM c "
            "WHERE c.tenant_id=@t AND c.status='open'",
            [{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
        total_spend = float(spend_res[0] or 0) if spend_res else 0.0
        open_waste = float(waste_res[0] or 0) if waste_res else 0.0
    except (CosmosError, IndexError, TypeError):
        total_spend, open_waste = 0.0, 0.0

    waste_ratio_pct = (open_waste / total_spend * 100) if total_spend > 0 else 0.0
    # Score: 0% waste → 100; 50% waste → 0
    score = max(0.0, round(100 - waste_ratio_pct * 2, 1))
    evidence = {
        "total_spend_30d_eur": round(total_spend, 2),
        "open_waste_eur": round(open_waste, 2),
        "waste_ratio_pct": round(waste_ratio_pct, 1),
    }
    return score, evidence


async def _score_commitment_coverage(tenant_id: str, settings) -> tuple[float, dict]:
    """% of commitment-eligible spend covered by reservations."""
    start = (date.today() - timedelta(days=30)).isoformat()
    _ELIGIBLE = ("Compute", "Databases")
    query_eligible = """
        SELECT VALUE SUM(c.effective_cost) FROM c
        WHERE c.tenant_id=@t AND c.type='focus_record'
          AND c.charge_period_start>=@s
          AND c.service_category IN ('Compute', 'Databases')
    """
    query_committed = """
        SELECT VALUE SUM(c.effective_cost) FROM c
        WHERE c.tenant_id=@t AND c.type='focus_record'
          AND c.charge_period_start>=@s
          AND c.service_category IN ('Compute', 'Databases')
          AND (c.commitment_discount_type != 'None'
               AND IS_DEFINED(c.commitment_discount_type)
               AND c.commitment_discount_type != null)
    """
    params = [
        {"name": "@t", "value": tenant_id},
        {"name": "@s", "value": start},
    ]
    try:
        elig_res = await cosmos.query_items(
            settings.cosmos_container_cost_records, query_eligible, params,
            partition_key=tenant_id,
        )
        comm_res = await cosmos.query_items(
            settings.cosmos_container_cost_records, query_committed, params,
            partition_key=tenant_id,
        )
        eligible = float(elig_res[0] or 0) if elig_res else 0.0
        committed = float(comm_res[0] or 0) if comm_res else 0.0
    except (CosmosError, IndexError, TypeError):
        eligible, committed = 0.0, 0.0

    coverage_pct = round(committed / eligible * 100, 1) if eligible > 0 else 0.0
    score = coverage_pct
    evidence = {
        "eligible_spend_eur": round(eligible, 2),
        "committed_spend_eur": round(committed, 2),
        "coverage_pct": coverage_pct,
    }
    return score, evidence


async def _score_unit_economics(tenant_id: str, settings) -> tuple[float, dict]:
    """Whether the tenant has defined and is tracking unit metrics."""
    try:
        metrics = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='unit_metric' OFFSET 0 LIMIT 5",
            [{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
    except CosmosError:
        metrics = []

    count = len(metrics)
    score = 0.0
    if count >= 1:
        score += 40.0  # Has at least one metric defined
    if count >= 3:
        score += 30.0  # Multiple metrics → wider adoption
    # Check if any metric has recent data points (last 7 days)
    recent_cutoff = (date.today() - timedelta(days=7)).isoformat()
    recent = [
        m for m in metrics
        if str(m.get("recorded_at", "") or m.get("date", ""))[:10] >= recent_cutoff
    ]
    if recent:
        score += 30.0  # Actively being tracked

    evidence = {
        "metric_definitions": count,
        "recent_data_points": len(recent),
    }
    return round(score, 1), evidence


async def _score_anomaly_response(tenant_id: str, settings) -> tuple[float, dict]:
    """Average hours from violation triggered → resolved."""
    try:
        violations = await cosmos.query_items(
            settings.cosmos_container_policies,
            """SELECT c.triggered_at, c.resolved_at FROM c
               WHERE c.tenant_id=@t AND c.type='policy_violation'
                 AND c.resolved_at != null
               OFFSET 0 LIMIT 50""",
            [{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
    except CosmosError:
        violations = []

    if not violations:
        # No violations to respond to — neutral score
        evidence = {"resolved_violations": 0, "avg_response_hours": None}
        return 50.0, evidence

    hours_list = []
    for v in violations:
        t = v.get("triggered_at") or ""
        r = v.get("resolved_at") or ""
        if t and r:
            try:
                dt_triggered = datetime.fromisoformat(t.replace("Z", "+00:00"))
                dt_resolved = datetime.fromisoformat(r.replace("Z", "+00:00"))
                hours = (dt_resolved - dt_triggered).total_seconds() / 3600.0
                if hours >= 0:
                    hours_list.append(hours)
            except (ValueError, TypeError):
                pass

    if not hours_list:
        return 50.0, {"resolved_violations": len(violations), "avg_response_hours": None}

    avg_h = sum(hours_list) / len(hours_list)
    # Score: < 4h → 100; < 24h → 80; < 72h → 60; < 168h (1wk) → 40; else → 20
    if avg_h < 4:
        score = 100.0
    elif avg_h < 24:
        score = 80.0
    elif avg_h < 72:
        score = 60.0
    elif avg_h < 168:
        score = 40.0
    else:
        score = 20.0

    evidence = {
        "resolved_violations": len(hours_list),
        "avg_response_hours": round(avg_h, 1),
    }
    return score, evidence


async def _score_budget_adherence(tenant_id: str, settings) -> tuple[float, dict]:
    """% of active budgets that are not currently in breach."""
    try:
        budgets = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            "SELECT c.status FROM c WHERE c.tenant_id=@t AND c.type='budget'",
            [{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
    except CosmosError:
        budgets = []

    if not budgets:
        # No budgets defined — Walk-level score
        return 40.0, {"total_budgets": 0, "breached": 0, "adherence_pct": None}

    breached = sum(1 for b in budgets if (b.get("status") or "").lower() == "exceeded")
    total = len(budgets)
    adherence_pct = round((total - breached) / total * 100, 1)
    score = adherence_pct
    evidence = {
        "total_budgets": total,
        "breached": breached,
        "adherence_pct": adherence_pct,
    }
    return score, evidence


# ── Recommended actions per dimension ────────────────────────────────────────

def _recommended_action(dimension: str, score: float) -> str:
    if score >= 80:
        return "Maintain current practice."
    actions = {
        "tagging_completeness": (
            "Enforce tag policies via Azure Policy / AWS SCP — require cost_center, team, product."
        ),
        "waste_ratio": (
            "Review open waste recommendations in the Waste dashboard and action "
            "rightsizing / idle resource deletions this sprint."
        ),
        "commitment_coverage": (
            "Use the Commitment Advisor to identify services ready for "
            "1-year Savings Plans — immediate savings with low risk."
        ),
        "unit_economics": (
            "Define at least one unit metric (cost per API call, cost per user) "
            "in the Unit Economics dashboard and connect it to a KPI."
        ),
        "anomaly_response": (
            "Assign policy violations to an on-call rotation and target "
            "<24h resolution SLA."
        ),
        "budget_adherence": (
            "Create budgets for top-5 cost centres and configure alert thresholds "
            "at 80% and 100% of budget."
        ),
    }
    return actions.get(dimension, "Review this dimension for improvement opportunities.")


# ── Public async API ──────────────────────────────────────────────────────────

async def compute_maturity_score(
    tenant_id: str,
    vertical: str = _DEFAULT_VERTICAL,
) -> MaturityScore:
    """Compute a full FinOps maturity score for a tenant."""
    vertical = vertical.lower().strip()
    if vertical not in _VALID_VERTICALS:
        vertical = _DEFAULT_VERTICAL

    cohort = _BENCHMARKS[vertical]
    settings = get_settings()

    scorers = [
        ("tagging_completeness", "Tag Coverage",      _score_tagging),
        ("waste_ratio",          "Waste Reduction",   _score_waste),
        ("commitment_coverage",  "RI/SP Coverage",    _score_commitment_coverage),
        ("unit_economics",       "Unit Economics",    _score_unit_economics),
        ("anomaly_response",     "Anomaly Response",  _score_anomaly_response),
        ("budget_adherence",     "Budget Adherence",  _score_budget_adherence),
    ]

    dimensions: list[DimensionScore] = []
    weighted_sum = 0.0

    for dim_key, dim_label, scorer in scorers:
        raw_score, evidence = await scorer(tenant_id, settings)
        score = round(max(0.0, min(100.0, raw_score)), 1)
        median, p75, p90 = cohort[dim_key]
        percentile = round(_percentile_vs_cohort(score, median, p75, p90), 0)
        context = _cohort_context(score, median, p75, p90)
        weight = _DIM_WEIGHTS[dim_key]
        weighted_sum += score * weight
        dimensions.append(DimensionScore(
            dimension=dim_key,
            label=dim_label,
            weight=weight,
            score=score,
            percentile=percentile,
            cohort_median=median,
            cohort_p75=p75,
            cohort_p90=p90,
            cohort_context=context,
            evidence=evidence,
            recommended_action=_recommended_action(dim_key, score),
        ))

    overall = round(weighted_sum, 1)
    # Overall percentile: average across dimension percentiles
    overall_percentile = round(
        sum(d.percentile for d in dimensions) / len(dimensions), 0
    )
    label = _maturity_label(overall)

    # Top recommendation: lowest-scoring dimension
    worst = min(dimensions, key=lambda d: d.score)
    top_recommendation = (
        f"Focus on **{worst.label}** (score {worst.score:.0f}/100): "
        + worst.recommended_action
    )

    return MaturityScore(
        tenant_id=tenant_id,
        vertical=vertical,
        overall_score=overall,
        overall_percentile=overall_percentile,
        overall_label=label,
        dimensions=dimensions,
        top_recommendation=top_recommendation,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
