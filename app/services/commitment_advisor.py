"""
Smart Commitment Advisor
========================

Calendar-aware RI / Savings Plan timing recommendations backed by
Holt-Winters stability analysis.

For each service / cloud combination with on-demand eligible spend the advisor:
  1. Builds a daily cost series from FOCUS records (90-day lookback).
  2. Fits Holt-Winters to measure forecast stability (MAPE as volatility proxy).
  3. Detects trend direction (stable / growing / declining) over trailing 30 d.
  4. Factors in operator-supplied planned events (migrations, redesigns) that
     would make it unsafe to commit right now.
  5. Outputs a CommitmentAdvisory per service: timing (commit_now | wait),
     confidence score, estimated saving, and human-readable calendar notes.

Recommendation philosophy
-------------------------
  confidence ≥ 0.70 → commit_now   (stable, predictable workload)
  0.45 ≤ conf < 0.70 → wait 2 mo   (let usage plateau / stabilise)
  conf < 0.45        → wait 3+ mo  (too volatile to commit safely)
  direction=declining → wait 3 mo  (workload may be decommissioned)
  direction=growing >20%/mo → wait 2 mo (spend hasn't plateaued)
  planned_events within 90 d → push earliest commit date past them
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos
from app.services.forecast import (
    _hw_additive, _select_params, MIN_SEASONAL_POINTS, SEASON,
)

log = get_logger(__name__)

# Conservative no-upfront discount rates by cloud / commitment type.
_DISCOUNT_RATES: dict[str, dict[str, float]] = {
    "azure":   {"savings-plan-1yr": 0.35, "savings-plan-3yr": 0.52},
    "aws":     {"savings-plan-1yr": 0.27, "1yr-ri": 0.38, "3yr-ri": 0.57},
    "gcp":     {"committed-use-1yr": 0.37, "committed-use-3yr": 0.55},
    "default": {"savings-plan-1yr": 0.30, "savings-plan-3yr": 0.50},
}

# Minimum average monthly spend to generate an advisory (filters noise).
_MIN_MONTHLY_EUR = 50.0

# FOCUS service categories eligible for commitments.
_ELIGIBLE_CATEGORIES = {"Compute", "Databases", "Storage", "Networking"}


# ── Data containers ─────────────────────────────────────────────────────────

@dataclass
class CommitmentAdvisory:
    service: str
    cloud: str
    current_monthly_eur: float
    on_demand_monthly_eur: float
    recommended_type: str          # e.g. "savings-plan-1yr"
    commitment_horizon_months: int # 12 or 36
    estimated_monthly_saving_eur: float
    saving_pct: float
    confidence_score: float        # 0.0 – 1.0
    confidence_label: str          # "high" | "medium" | "low"
    timing: str                    # "commit_now" | "wait"
    wait_months: int               # 0 = commit now
    earliest_commit_date: str      # ISO-8601 first-of-month
    stability_score: float         # 0.0 – 1.0
    trend_direction: str           # "stable" | "growing" | "declining"
    trend_pct_30d: float
    forecast_mape: Optional[float]
    calendar_notes: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class CommitmentAdvisoryReport:
    tenant_id: str
    period_start: str
    period_end: str
    total_on_demand_eligible_eur: float
    total_estimated_saving_eur: float
    advisories: list[CommitmentAdvisory] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Pure analysis helpers ─────────────────────────────────────────────────────

def _series_stats(
    daily_costs: list[float],
) -> tuple[float, str, float, Optional[float]]:
    """
    Return (stability_score 0-1, trend_direction, trend_pct_30d, mape).
    stability_score = 1 means perfectly flat and predictable.
    """
    arr = np.array(daily_costs, dtype=float)
    if arr.size < 7:
        return 0.3, "unknown", 0.0, None

    window = arr[-30:] if arr.size >= 30 else arr
    mid = len(window) // 2
    if mid > 0:
        first_mean = float(window[:mid].mean())
        second_mean = float(window[mid:].mean())
        trend_pct = (second_mean - first_mean) / max(first_mean, 0.01) * 100.0
    else:
        trend_pct = 0.0

    direction: str
    if abs(trend_pct) < 10:
        direction = "stable"
    elif trend_pct > 0:
        direction = "growing"
    else:
        direction = "declining"

    # In-sample MAPE as a stability proxy via Holt-Winters
    mape: Optional[float] = None
    if arr.size >= MIN_SEASONAL_POINTS:
        try:
            holdout = min(7, arr.size // 4)
            params, _ = _select_params(arr, SEASON, holdout=holdout)
            fitted, _ = _hw_additive(arr, SEASON, *params, h=0)
            mask = arr > 1.0
            if mask.any():
                mape = float(
                    np.mean(np.abs((arr[mask] - fitted[mask]) / arr[mask])) * 100
                )
        except Exception:
            pass

    # Coefficient of variation on the trailing 30-day window
    mean_val = float(window.mean())
    cv = float(window.std() / mean_val) if mean_val > 0.01 else 1.0

    base = max(0.0, 1.0 - cv * 1.5)
    if mape is not None:
        mape_factor = max(0.0, 1.0 - mape / 40.0)
        base = (base + mape_factor) / 2.0

    stability = round(min(1.0, max(0.0, base)), 3)
    return stability, direction, round(trend_pct, 1), mape


def _choose_commitment(cloud: str, horizon_months: int) -> tuple[str, float]:
    """Return (recommended_type, discount_rate) for the given cloud + horizon."""
    rates = _DISCOUNT_RATES.get(cloud.lower(), _DISCOUNT_RATES["default"])
    if horizon_months >= 36:
        for k, v in rates.items():
            if "3yr" in k or "3-yr" in k:
                return k, v
    for k, v in rates.items():
        if "1yr" in k or "1-yr" in k:
            return k, v
    k = next(iter(rates))
    return k, rates[k]


def _calendar_notes(daily_costs: list[float]) -> list[str]:
    """Detect notable weekday patterns in the daily series."""
    notes: list[str] = []
    if len(daily_costs) < 28:
        return notes
    arr = np.array(daily_costs[-28:], dtype=float)
    weekday_means = []
    for wd in range(7):
        vals = arr[wd::7]
        if vals.size:
            weekday_means.append((wd, float(vals.mean())))
    if not weekday_means:
        return notes
    _DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    peak_wd, peak_val = max(weekday_means, key=lambda x: x[1])
    min_wd, min_val = min(weekday_means, key=lambda x: x[1])
    if min_val > 0 and (peak_val - min_val) / peak_val > 0.20:
        notes.append(
            f"Weekly peak on {_DAYS[peak_wd]} "
            f"(+{(peak_val / min_val - 1) * 100:.0f}% vs {_DAYS[min_wd]})"
        )
    return notes


def build_advisory(
    service: str,
    cloud: str,
    daily_costs: list[float],
    planned_events: list[dict],
    today: date,
) -> Optional[CommitmentAdvisory]:
    """Build a single CommitmentAdvisory for a service/cloud pair.

    Parameters
    ----------
    planned_events:
        List of ``{"date": "YYYY-MM-DD", "description": "…"}`` dicts
        representing upcoming migrations or redesigns.
    """
    if not daily_costs:
        return None

    monthly_eur = (
        float(np.array(daily_costs[-30:]).mean() * 30)
        if len(daily_costs) >= 30
        else float(np.mean(daily_costs) * 30)
    )
    if monthly_eur < _MIN_MONTHLY_EUR:
        return None

    stability, direction, trend_pct, mape = _series_stats(daily_costs)

    # Confidence = weighted combination of stability, trend-stability, and event-free horizon
    trend_factor = max(0.0, 1.0 - abs(trend_pct) / 100.0)
    confidence = stability * 0.5 + trend_factor * 0.3

    blocking_events = []
    for e in planned_events:
        try:
            ev_date = date.fromisoformat(e["date"])
            if today <= ev_date <= today + timedelta(days=90):
                blocking_events.append(e)
        except (KeyError, ValueError):
            pass  # skip malformed event
    confidence += 0.0 if blocking_events else 0.2
    confidence = round(min(1.0, max(0.0, confidence)), 3)
    confidence_label = (
        "high" if confidence >= 0.70
        else ("medium" if confidence >= 0.45 else "low")
    )

    # Timing
    wait_months = 0
    if direction == "declining":
        wait_months = 3
    elif direction == "growing" and abs(trend_pct) > 20:
        wait_months = 2
    elif blocking_events:
        latest = max(date.fromisoformat(e["date"]) for e in blocking_events)
        months_away = (latest.year - today.year) * 12 + (latest.month - today.month)
        wait_months = max(1, months_away + 1)
    elif confidence < 0.45:
        wait_months = 3
    elif confidence < 0.70:
        wait_months = 2

    timing = "commit_now" if wait_months == 0 else "wait"

    # Earliest commit date = first of the month after wait_months
    d = today.replace(day=1)
    for _ in range(wait_months):
        d = (d + timedelta(days=32)).replace(day=1)

    # Commitment horizon: 3-year only when high-confidence stable
    horizon_months = 36 if (confidence >= 0.75 and direction == "stable") else 12
    recommended_type, discount_rate = _choose_commitment(cloud, horizon_months)
    estimated_saving = round(monthly_eur * discount_rate, 2)
    saving_pct = round(discount_rate * 100, 1)

    notes = _calendar_notes(daily_costs)
    for ev in blocking_events[:2]:
        notes.append(
            f"Planned event: {ev.get('description', ev['date'])} ({ev['date']})"
        )

    rationale = (
        f"€{monthly_eur:.0f}/mo on-demand spend on {service} ({cloud.upper()}). "
        f"Stability score {stability:.2f}, {direction} trend ({trend_pct:+.1f}% over 30 d). "
    )
    if mape is not None:
        rationale += f"Forecast MAPE {mape:.1f}%. "
    if timing == "commit_now":
        rationale += (
            f"Confidence {confidence:.2f} — recommend {recommended_type}, "
            f"save ~€{estimated_saving:.0f}/mo ({saving_pct:.0f}%)."
        )
    else:
        rationale += (
            f"Confidence {confidence:.2f} — wait {wait_months} month(s) before "
            f"committing ({direction} workload)."
        )

    return CommitmentAdvisory(
        service=service,
        cloud=cloud,
        current_monthly_eur=round(monthly_eur, 2),
        on_demand_monthly_eur=round(monthly_eur, 2),
        recommended_type=recommended_type,
        commitment_horizon_months=horizon_months,
        estimated_monthly_saving_eur=estimated_saving,
        saving_pct=saving_pct,
        confidence_score=confidence,
        confidence_label=confidence_label,
        timing=timing,
        wait_months=wait_months,
        earliest_commit_date=d.isoformat(),
        stability_score=stability,
        trend_direction=direction,
        trend_pct_30d=trend_pct,
        forecast_mape=mape,
        calendar_notes=notes,
        rationale=rationale,
    )


# ── Public async API ──────────────────────────────────────────────────────────

async def generate_advisories(
    tenant_id: str,
    lookback_days: int = 90,
    planned_events: list[dict] | None = None,
) -> CommitmentAdvisoryReport:
    """
    Generate commitment advisories for a tenant from live FOCUS data.

    Parameters
    ----------
    planned_events:
        Optional list of upcoming migration/redesign events that should delay
        commitment purchases.  Shape: ``{"date": "YYYY-MM-DD", "description": "…"}``.
    """
    settings = get_settings()
    today = date.today()
    period_start = (today - timedelta(days=lookback_days)).isoformat()
    period_end = today.isoformat()

    query = """
        SELECT c.service_name, c.provider_name, c.charge_period_start,
               c.effective_cost, c.commitment_discount_type, c.service_category
        FROM c
        WHERE c.tenant_id = @tenant_id
          AND c.type = 'focus_record'
          AND c.charge_period_start >= @start
        ORDER BY c.charge_period_start
    """
    try:
        records = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            query,
            parameters=[
                {"name": "@tenant_id", "value": tenant_id},
                {"name": "@start",     "value": period_start},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        records = []

    # Group into daily series keyed by (service_name, cloud)
    series: dict[tuple[str, str], dict[str, float]] = {}
    for r in records:
        cat = r.get("service_category", "")
        if cat not in _ELIGIBLE_CATEGORIES:
            continue
        discount_type = (r.get("commitment_discount_type") or "").strip().lower()
        if discount_type and discount_type not in ("", "none", "null"):
            continue  # already committed
        key = (
            r.get("service_name", "Unknown"),
            r.get("provider_name", "unknown").lower(),
        )
        day = r.get("charge_period_start", "")[:10]
        cost = float(r.get("effective_cost") or 0.0)
        if day:
            series.setdefault(key, {})
            series[key][day] = series[key].get(day, 0.0) + cost

    advisories: list[CommitmentAdvisory] = []
    total_on_demand = 0.0
    events = planned_events or []

    for (service, cloud), day_map in series.items():
        if not day_map:
            continue
        daily_costs = [day_map[d] for d in sorted(day_map)]
        advisory = build_advisory(service, cloud, daily_costs, events, today)
        if advisory is not None:
            advisories.append(advisory)
            total_on_demand += advisory.on_demand_monthly_eur

    advisories.sort(key=lambda a: a.estimated_monthly_saving_eur, reverse=True)
    total_saving = sum(a.estimated_monthly_saving_eur for a in advisories)

    notes: list[str] = []
    if not advisories:
        notes.append("No on-demand eligible spend found in the selected period.")
    else:
        commit_now = sum(1 for a in advisories if a.timing == "commit_now")
        notes.append(
            f"{commit_now} of {len(advisories)} service(s) ready for immediate commitment."
        )

    return CommitmentAdvisoryReport(
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
        total_on_demand_eligible_eur=round(total_on_demand, 2),
        total_estimated_saving_eur=round(total_saving, 2),
        advisories=advisories,
        notes=notes,
    )
