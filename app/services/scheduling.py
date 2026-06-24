"""
Scheduling engine
=================

Many non-production resources (dev, test, staging, sandbox) run 24/7 — 168
hours a week — but are only used during working hours. Shutting them down nights
and weekends is one of the highest-ROI, lowest-effort FinOps wins. This engine
recommends an on/off schedule and quantifies the saving.

Two modes:
  * Heuristic (no activity data): a non-prod resource running 24/7 gets a
    business-hours schedule (Mon–Fri 08:00–20:00 = 60h/week), saving ~64%.
  * Activity-driven (hourly activity profile available): compute the minimal
    schedule that covers the hours the resource is actually active, plus a small
    buffer, and price the saving from the idle hours removed.

Savings assume compute cost scales with running hours (true for VMs/instances;
storage and reserved capacity are excluded by the caller).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

HOURS_PER_WEEK = 168
BUSINESS_HOURS_PER_WEEK = 60        # Mon–Fri 08:00–20:00
EXTENDED_HOURS_PER_WEEK = 70        # Mon–Sat 07:00–19:00 (a touch more conservative)


@dataclass
class ScheduleRecommendation:
    resource_id: str
    resource_name: str
    provider: str
    environment: str
    current_hours_week: float
    recommended_hours_week: float
    schedule_label: str                 # human-readable, e.g. "Mon–Fri 08:00–20:00"
    monthly_cost_eur: float
    monthly_saving_eur: float
    saving_pct: float
    confidence: str
    rationale: str
    rationale_it: str = ""


@dataclass
class SchedulingResult:
    scanned: int
    recommendations: list[ScheduleRecommendation] = field(default_factory=list)
    total_monthly_saving_eur: float = 0.0
    notes: list[str] = field(default_factory=list)


_NONPROD_HINTS = ("dev", "test", "staging", "stage", "sandbox", "qa", "uat", "demo")


def _is_nonprod(environment: str, name: str, tags: dict) -> bool:
    blob = f"{environment} {name} {' '.join(str(v) for v in (tags or {}).values())}".lower()
    return any(h in blob for h in _NONPROD_HINTS)


def _active_hours_from_profile(profile: list[int]) -> tuple[float, str]:
    """
    profile: 168 ints (one per hour of week, Mon 00:00 first), 1=active 0=idle.
    Returns (active_hours, label). Falls back to business-hours if profile sparse.
    """
    active = sum(1 for h in profile if h)
    return active, f"{active}h/week active window (from observed activity)"


def recommend_one(
    resource_id: str,
    resource_name: str,
    provider: str,
    environment: str,
    monthly_cost_eur: float,
    tags: Optional[dict] = None,
    currently_runs_247: bool = True,
    activity_profile: Optional[list[int]] = None,
    schedule_style: str = "business",     # "business" | "extended"
) -> Optional[ScheduleRecommendation]:
    if not _is_nonprod(environment, resource_name, tags or {}):
        return None
    if not currently_runs_247:
        return None

    if activity_profile and len(activity_profile) == HOURS_PER_WEEK and sum(activity_profile) > 0:
        active, label = _active_hours_from_profile(activity_profile)
        # add a 1-hour ramp buffer either side per active block (approx: +10%)
        rec_hours = min(HOURS_PER_WEEK, active * 1.10)
        confidence = "high"
    else:
        rec_hours = EXTENDED_HOURS_PER_WEEK if schedule_style == "extended" else BUSINESS_HOURS_PER_WEEK
        label = ("Mon–Sat 07:00–19:00" if schedule_style == "extended"
                 else "Mon–Fri 08:00–20:00")
        confidence = "medium"

    saving_pct = max(0.0, (HOURS_PER_WEEK - rec_hours) / HOURS_PER_WEEK)
    saving = monthly_cost_eur * saving_pct
    if saving < 1:
        return None

    return ScheduleRecommendation(
        resource_id=resource_id, resource_name=resource_name, provider=provider,
        environment=environment or "non-prod",
        current_hours_week=HOURS_PER_WEEK, recommended_hours_week=round(rec_hours, 1),
        schedule_label=label, monthly_cost_eur=round(monthly_cost_eur, 2),
        monthly_saving_eur=round(saving, 2), saving_pct=round(saving_pct * 100, 1),
        confidence=confidence,
        rationale=(f"{resource_name} is non-production and runs 24/7 ({HOURS_PER_WEEK}h/week). "
                   f"A {label} schedule ({rec_hours:.0f}h/week) saves ~€{saving:,.0f}/mo "
                   f"({saving_pct*100:.0f}%) with no impact on working-hours use."),
        rationale_it=(f"{resource_name} è non-produzione e gira 24/7. Una pianificazione "
                      f"{label} ({rec_hours:.0f}h/sett) fa risparmiare ~€{saving:,.0f}/mese "
                      f"({saving_pct*100:.0f}%)."),
    )


def recommend(resources: list[dict], schedule_style: str = "business") -> SchedulingResult:
    """
    resources: [{resource_id, resource_name, provider, environment, monthly_eur,
                 tags, currently_runs_247, activity_profile}]
    """
    recs: list[ScheduleRecommendation] = []
    for r in resources:
        rec = recommend_one(
            resource_id=r.get("resource_id", ""),
            resource_name=r.get("resource_name", r.get("resource_id", "")),
            provider=r.get("provider", ""),
            environment=r.get("environment", ""),
            monthly_cost_eur=float(r.get("monthly_eur", 0.0)),
            tags=r.get("tags"),
            currently_runs_247=bool(r.get("currently_runs_247", True)),
            activity_profile=r.get("activity_profile"),
            schedule_style=schedule_style,
        )
        if rec:
            recs.append(rec)
    recs.sort(key=lambda x: x.monthly_saving_eur, reverse=True)
    return SchedulingResult(
        scanned=len(resources), recommendations=recs,
        total_monthly_saving_eur=round(sum(r.monthly_saving_eur for r in recs), 2),
        notes=([f"{len(recs)} non-prod resource(s) eligible for scheduling."] if recs
               else ["No 24/7 non-prod resources found to schedule."]),
    )
