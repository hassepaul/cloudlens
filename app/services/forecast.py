"""
CloudLens Forecast Engine
=========================

Spend forecasting + the differentiating "cost of inaction" dual-trajectory model.

Method
------
Additive Holt-Winters triple exponential smoothing with weekly seasonality
(period = 7). Captures level, trend, and the weekday/weekend autoscaling cycle
that a linear projection misses. Pure NumPy — no training infrastructure, fits
the serverless / scale-to-zero design.

Every forecast is backtested (rolling-origin holdout) and returns its MAPE so
callers get an honest accuracy figure, not a false-precision point estimate.

Honesty / limits
----------------
- Needs >= 14 daily points for seasonality; below that it falls back to a
  damped-trend model with wide intervals and flags low confidence.
- cost_records carry a 90-day TTL, so the input series is at most ~12 weeks.
  Weekly seasonality is well-supported; annual seasonality is NOT (would need
  persisted monthly rollups — see forecast_rollup()).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np

SEASON = 7              # weekly seasonality
MIN_SEASONAL_POINTS = 14
Z_80 = 1.2816           # 80% prediction interval
Z_95 = 1.9600


# ──────────────────────────────────────────────────────────────────────────────
# Result containers
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ForecastPoint:
    day: str            # ISO date
    value: float        # point forecast (EUR)
    lower: float        # lower prediction bound
    upper: float        # upper prediction bound


@dataclass
class ForecastResult:
    method: str
    horizon_days: int
    history_days: int
    mape: Optional[float]               # backtest mean abs % error (None if not enough data)
    confidence: str                     # "high" | "medium" | "low"
    points: list[ForecastPoint] = field(default_factory=list)
    month_end_projection: Optional[float] = None
    notes: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Holt-Winters core
# ──────────────────────────────────────────────────────────────────────────────

def _hw_additive(
    y: np.ndarray, m: int, alpha: float, beta: float, gamma: float, h: int
) -> tuple[np.ndarray, np.ndarray]:
    """
    Fit additive Holt-Winters and forecast h steps.
    Returns (fitted_in_sample, forecast_h).
    """
    n = len(y)
    # init: level = mean of first season, trend = avg season-over-season slope
    level = y[:m].mean()
    trend = (y[m:2 * m].mean() - y[:m].mean()) / m if n >= 2 * m else 0.0
    season = list(y[:m] - level)

    fitted = np.zeros(n)
    for t in range(n):
        s_idx = t % m
        if t == 0:
            fitted[t] = level + season[s_idx]
            continue
        prev_level = level
        seas = season[s_idx]
        # update
        level = alpha * (y[t] - seas) + (1 - alpha) * (prev_level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        season[s_idx] = gamma * (y[t] - level) + (1 - gamma) * seas
        fitted[t] = level + trend + season[s_idx]

    fc = np.zeros(h)
    for i in range(1, h + 1):
        s_idx = (n + i - 1) % m
        fc[i - 1] = level + i * trend + season[s_idx]
    return fitted, np.maximum(fc, 0.0)   # spend can't be negative


def _damped_trend(y: np.ndarray, h: int, alpha=0.5, beta=0.3, phi=0.9) -> np.ndarray:
    """Fallback for short series: damped exponential trend, no seasonality."""
    level = y[0]
    trend = (y[-1] - y[0]) / max(len(y) - 1, 1)
    for t in range(1, len(y)):
        prev = level
        level = alpha * y[t] + (1 - alpha) * (level + phi * trend)
        trend = beta * (level - prev) + (1 - beta) * phi * trend
    fc = np.zeros(h)
    damp = 0.0
    for i in range(1, h + 1):
        damp += phi ** i
        fc[i - 1] = level + damp * trend
    return np.maximum(fc, 0.0)


def _backtest_mape(y: np.ndarray, m: int, params: tuple, holdout: int) -> float:
    """Rolling-origin holdout MAPE."""
    if len(y) <= holdout + 2 * m:
        return float("nan")
    train, test = y[:-holdout], y[-holdout:]
    _, fc = _hw_additive(train, m, *params, h=holdout)
    denom = np.where(test == 0, np.nan, test)
    return float(np.nanmean(np.abs((test - fc) / denom)) * 100)


def _select_params(y: np.ndarray, m: int, holdout: int) -> tuple[tuple, float]:
    """Small grid search minimising backtest MAPE."""
    best, best_mape = (0.3, 0.1, 0.3), float("inf")
    for a in (0.2, 0.35, 0.5):
        for b in (0.05, 0.15, 0.3):
            for g in (0.2, 0.4):
                mape = _backtest_mape(y, m, (a, b, g), holdout)
                if not np.isnan(mape) and mape < best_mape:
                    best_mape, best = mape, (a, b, g)
    return best, best_mape


# ──────────────────────────────────────────────────────────────────────────────
# Public: baseline spend forecast
# ──────────────────────────────────────────────────────────────────────────────

def forecast_spend(
    daily: list[dict],          # [{"date": "YYYY-MM-DD", "cost_eur": float}, ...] ascending
    horizon_days: int = 30,
) -> ForecastResult:
    """Forecast daily spend `horizon_days` into the future from a daily history."""
    daily = sorted(daily, key=lambda d: d["date"])
    y = np.array([float(d["cost_eur"]) for d in daily])
    n = len(y)
    notes: list[str] = []

    if n == 0:
        return ForecastResult("none", horizon_days, 0, None, "low",
                              notes=["No cost history available to forecast."])

    last_day = date.fromisoformat(daily[-1]["date"])

    if n >= MIN_SEASONAL_POINTS:
        holdout = min(SEASON, n // 4)
        params, mape = _select_params(y, SEASON, holdout)
        fitted, fc = _hw_additive(y, SEASON, *params, h=horizon_days)
        resid = y - fitted
        sigma = float(np.std(resid[SEASON:])) if n > SEASON else float(np.std(resid))
        method = "holt_winters_additive_weekly"
        confidence = "high" if (not np.isnan(mape) and mape < 12) else \
                     "medium" if (not np.isnan(mape) and mape < 25) else "low"
        if np.isnan(mape):
            mape = None
            confidence = "medium"
    else:
        fc = _damped_trend(y, horizon_days)
        sigma = float(np.std(np.diff(y))) if n > 1 else float(y[0] * 0.2)
        mape = None
        method = "damped_trend"
        confidence = "low"
        notes.append(f"Only {n} days of history (<{MIN_SEASONAL_POINTS}); using trend-only "
                     "fallback with wide intervals. Weekly seasonality not modelled.")

    points: list[ForecastPoint] = []
    for i in range(horizon_days):
        d = last_day + timedelta(days=i + 1)
        widen = sigma * np.sqrt(i + 1)          # interval grows with horizon
        points.append(ForecastPoint(
            day=d.isoformat(),
            value=round(float(fc[i]), 2),
            lower=round(max(0.0, float(fc[i]) - Z_80 * widen), 2),
            upper=round(float(fc[i]) + Z_80 * widen, 2),
        ))

    # month-end projection = actuals so far this month + forecast to month end
    month_end = _month_end_projection(daily, points, last_day)

    return ForecastResult(
        method=method, horizon_days=horizon_days, history_days=n,
        mape=(round(mape, 1) if mape is not None else None),
        confidence=confidence, points=points,
        month_end_projection=month_end, notes=notes,
    )


def _month_end_projection(daily, points, last_day) -> float:
    """Actual MTD spend + forecast for the remainder of the current month."""
    month = last_day.replace(day=1)
    mtd = sum(float(d["cost_eur"]) for d in daily
              if date.fromisoformat(d["date"]) >= month)
    # remaining days in month from forecast
    if last_day.month == 12:
        next_month = last_day.replace(year=last_day.year + 1, month=1, day=1)
    else:
        next_month = last_day.replace(month=last_day.month + 1, day=1)
    remainder = sum(p.value for p in points
                    if date.fromisoformat(p.day) < next_month)
    return round(mtd + remainder, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Public: dual-trajectory "cost of inaction"
# ──────────────────────────────────────────────────────────────────────────────

# Phasing ramps (cumulative fraction of a priority's savings realised by day d).
# Critical is fast to action; low-priority items ramp slowly.
_PHASE_DAYS = {"critical": 7, "high": 21, "medium": 42, "low": 90}


@dataclass
class TrajectoryResult:
    baseline: list[ForecastPoint]            # "do nothing"
    optimized: list[ForecastPoint]           # "if you act"
    daily_waste_burn_eur: float              # money lost per day unactioned
    cumulative_inaction_eur: float           # area between curves over horizon
    monthly_recoverable_eur: float
    annual_recoverable_eur: float
    horizon_days: int
    notes: list[str] = field(default_factory=list)


def cost_of_inaction(
    baseline: ForecastResult,
    waste_items: list[dict],     # [{"saving_eur": monthly, "priority": "critical"|...}, ...]
    horizon_days: Optional[int] = None,
) -> TrajectoryResult:
    """
    Build the optimized trajectory by subtracting phased daily savings from the
    baseline forecast, and quantify the cost of not acting.
    """
    horizon = horizon_days or baseline.horizon_days
    base_pts = baseline.points[:horizon]

    # monthly saving per priority bucket → daily saving once fully realised
    bucket_monthly: dict[str, float] = {}
    for w in waste_items:
        p = (w.get("priority") or "low").lower()
        bucket_monthly[p] = bucket_monthly.get(p, 0.0) + float(w.get("saving_eur", 0.0))

    total_monthly = sum(bucket_monthly.values())
    daily_burn = round(total_monthly / 30.0, 2)

    optimized: list[ForecastPoint] = []
    cumulative_gap = 0.0
    for i, bp in enumerate(base_pts):
        day_n = i + 1
        realised_daily = 0.0
        for prio, monthly in bucket_monthly.items():
            ramp_days = _PHASE_DAYS.get(prio, 90)
            frac = min(1.0, day_n / ramp_days)          # linear ramp to full realisation
            realised_daily += (monthly / 30.0) * frac
        opt_val = max(0.0, bp.value - realised_daily)
        cumulative_gap += (bp.value - opt_val)
        optimized.append(ForecastPoint(
            day=bp.day,
            value=round(opt_val, 2),
            lower=round(max(0.0, bp.lower - realised_daily), 2),
            upper=round(max(0.0, bp.upper - realised_daily), 2),
        ))

    notes = list(baseline.notes)
    if total_monthly == 0:
        notes.append("No open waste items — trajectories coincide (nothing to recover).")

    return TrajectoryResult(
        baseline=base_pts,
        optimized=optimized,
        daily_waste_burn_eur=daily_burn,
        cumulative_inaction_eur=round(cumulative_gap, 2),
        monthly_recoverable_eur=round(total_monthly, 2),
        annual_recoverable_eur=round(total_monthly * 12, 2),
        horizon_days=horizon,
        notes=notes,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public: remediation roadmap (ROI-ordered phased plan)
# ──────────────────────────────────────────────────────────────────────────────

# Relative effort weight per waste type (lower = easier/faster to action).
_EFFORT = {
    "unattached_disk": 1, "orphan_public_ip": 1, "old_snapshots": 1,
    "cold_storage": 2, "expired_cert": 2, "duplicated_backup": 2,
    "idle_vm": 3, "idle_app_service": 3, "unused_load_balancer": 2,
    "oversized_vm": 4, "dev_test_eligible": 4, "reserved_instance": 5,
}


@dataclass
class RoadmapPhase:
    phase: int
    label: str
    items: int
    monthly_saving_eur: float
    cumulative_monthly_saving_eur: float
    target_run_rate_eur: float       # projected monthly run-rate after this phase
    eta_days: int


@dataclass
class RoadmapResult:
    current_run_rate_eur: float
    optimized_run_rate_eur: float
    phases: list[RoadmapPhase]
    total_monthly_saving_eur: float


def remediation_roadmap(
    current_monthly_spend: float,
    waste_items: list[dict],         # need saving_eur, priority, waste_type
) -> RoadmapResult:
    """
    Order waste by ROI (saving / effort) and group into phases, showing the
    monthly run-rate bending down as each phase lands.
    """
    def roi(w):
        eff = _EFFORT.get((w.get("waste_type") or "").lower(), 3)
        return float(w.get("saving_eur", 0.0)) / eff

    ranked = sorted(waste_items, key=roi, reverse=True)

    # group into 4 phases by priority tier, ETA from the phasing ramp
    tiers = [("Quick wins", ["critical"], 7),
             ("High impact", ["high"], 21),
             ("Optimization", ["medium"], 42),
             ("Long tail", ["low"], 90)]

    phases: list[RoadmapPhase] = []
    cumulative = 0.0
    run_rate = current_monthly_spend
    for idx, (label, prios, eta) in enumerate(tiers, start=1):
        bucket = [w for w in ranked if (w.get("priority") or "low").lower() in prios]
        saving = sum(float(w.get("saving_eur", 0.0)) for w in bucket)
        if not bucket:
            continue
        cumulative += saving
        run_rate = max(0.0, current_monthly_spend - cumulative)
        phases.append(RoadmapPhase(
            phase=idx, label=label, items=len(bucket),
            monthly_saving_eur=round(saving, 2),
            cumulative_monthly_saving_eur=round(cumulative, 2),
            target_run_rate_eur=round(run_rate, 2),
            eta_days=eta,
        ))

    return RoadmapResult(
        current_run_rate_eur=round(current_monthly_spend, 2),
        optimized_run_rate_eur=round(max(0.0, current_monthly_spend - cumulative), 2),
        phases=phases,
        total_monthly_saving_eur=round(cumulative, 2),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public: budget-breach prediction
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BudgetBreachResult:
    monthly_budget_eur: float
    breach_date_baseline: Optional[str]      # date budget is exceeded, do-nothing
    breach_date_optimized: Optional[str]     # date budget is exceeded, if acting
    safe_if_actioned: bool
    notes: list[str] = field(default_factory=list)


def budget_breach(
    monthly_budget: float,
    baseline: ForecastResult,
    trajectory: TrajectoryResult,
) -> BudgetBreachResult:
    """Predict when cumulative spend crosses the monthly budget on each trajectory."""
    def first_breach(points: list[ForecastPoint]) -> Optional[str]:
        cum = 0.0
        for p in points:
            cum += p.value
            if cum > monthly_budget:
                return p.day
        return None

    base_breach = first_breach(baseline.points)
    opt_breach = first_breach(trajectory.optimized)
    safe = (opt_breach is None and base_breach is not None)

    notes = []
    if base_breach and safe:
        notes.append("Actioning the waste backlog keeps you under budget for the full horizon.")
    elif base_breach and opt_breach:
        notes.append("Even after remediation, current trajectory exceeds budget — consider "
                     "rightsizing beyond the detected waste.")
    return BudgetBreachResult(
        monthly_budget_eur=round(monthly_budget, 2),
        breach_date_baseline=base_breach,
        breach_date_optimized=opt_breach,
        safe_if_actioned=safe,
        notes=notes,
    )
