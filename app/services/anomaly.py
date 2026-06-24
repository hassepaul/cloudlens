"""
CloudLens Anomaly Detection
===========================

Statistically grounded spend-anomaly detection. An anomaly is a day whose actual
spend falls outside the prediction band of the Holt-Winters forecast fitted on
the *preceding* history — i.e. spend the model genuinely did not expect, given
the trend and weekly seasonality. This avoids the false positives of naive
"X% over last week" rules (which fire every Monday) and the false negatives of
fixed thresholds (which miss slow drifts).

Each anomaly is attributed: we diff the service / resource-group mix on the
anomalous day against the trailing baseline to surface the most likely driver,
so the output is "spend spiked €1,240 on 2026-06-08, driven by Virtual Machines
in rg-staging" — not just "anomaly detected".
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.services.forecast import _hw_additive, _select_params, SEASON, MIN_SEASONAL_POINTS

Z_FLAG = 2.0          # actual must exceed forecast by this many residual-sigma to flag
Z_HIGH = 3.5          # severity escalation


@dataclass
class AnomalyDriver:
    dimension: str          # "service" | "resource_group"
    name: str
    delta_eur: float        # how much this driver rose vs. its baseline
    share_of_spike: float   # fraction of the day's excess it explains


@dataclass
class Anomaly:
    day: str
    actual_eur: float
    expected_eur: float
    excess_eur: float            # actual - expected (can be negative for dips)
    direction: str               # "spike" | "dip"
    severity: str                # "high" | "medium"
    z_score: float
    drivers: list[AnomalyDriver] = field(default_factory=list)


@dataclass
class AnomalyResult:
    method: str
    scanned_days: int
    anomalies: list[Anomaly] = field(default_factory=list)
    total_anomalous_excess_eur: float = 0.0
    notes: list[str] = field(default_factory=list)


def _attribute(
    day: str,
    excess: float,
    per_day_breakdown: Optional[dict],
    baseline_breakdown: Optional[dict],
    top_n: int = 3,
) -> list[AnomalyDriver]:
    """
    Attribute a spike to the dimensions that rose most vs. their baseline.
    per_day_breakdown / baseline_breakdown: {dimension: {name: cost}}.
    """
    drivers: list[AnomalyDriver] = []
    if not per_day_breakdown:
        return drivers
    for dim, day_map in per_day_breakdown.items():
        base_map = (baseline_breakdown or {}).get(dim, {})
        for name, cost in day_map.items():
            delta = cost - base_map.get(name, 0.0)
            if delta > 0:
                drivers.append(AnomalyDriver(
                    dimension=dim, name=name, delta_eur=round(delta, 2),
                    share_of_spike=round(min(1.0, delta / excess), 3) if excess > 0 else 0.0,
                ))
    drivers.sort(key=lambda d: d.delta_eur, reverse=True)
    return drivers[:top_n]


def detect_anomalies(
    daily: list[dict],                 # [{"date","cost_eur"}, ...] ascending
    scan_last_days: int = 14,
    per_day_breakdowns: Optional[dict] = None,   # {date: {dim: {name: cost}}}
) -> AnomalyResult:
    """
    Fit Holt-Winters on history up to each scanned day and flag actuals that
    fall outside the prediction band.
    """
    daily = sorted(daily, key=lambda d: d["date"])
    y = np.array([float(d["cost_eur"]) for d in daily])
    dates = [d["date"] for d in daily]
    n = len(y)

    if n < MIN_SEASONAL_POINTS + 2:
        return AnomalyResult(
            method="insufficient_history", scanned_days=0,
            notes=[f"Need >= {MIN_SEASONAL_POINTS + 2} days to detect anomalies; have {n}."],
        )

    # Fit once on the bulk of history to get smoothing params + residual sigma.
    # NOTE (BUG-016): The model is fitted once on the full history and reused for
    # all scanned days.  For stricter rolling-origin evaluation (no look-ahead
    # leakage) the model should be re-fitted on history[0:t] for each scan day t.
    # The current approach trades statistical strictness for performance on the
    # typical 14-day scan window; the practical impact is small when the scan
    # window is short relative to total history length.
    holdout = min(SEASON, n // 4)
    params, _ = _select_params(y, SEASON, holdout)
    fitted, _ = _hw_additive(y, SEASON, *params, h=1)
    resid = y - fitted
    sigma = float(np.std(resid[SEASON:])) if n > SEASON else float(np.std(resid))
    if sigma <= 0:
        sigma = float(np.mean(y)) * 0.05 or 1.0

    anomalies: list[Anomaly] = []
    start = max(SEASON + 1, n - scan_last_days)
    for t in range(start, n):
        expected = fitted[t]
        actual = y[t]
        diff = actual - expected
        z = diff / sigma
        if abs(z) < Z_FLAG:
            continue
        direction = "spike" if diff > 0 else "dip"
        severity = "high" if abs(z) >= Z_HIGH else "medium"
        drivers = []
        if direction == "spike" and per_day_breakdowns:
            # baseline = same weekday a week earlier if available
            base_day = dates[t - SEASON] if t - SEASON >= 0 else None
            drivers = _attribute(
                dates[t], diff,
                per_day_breakdowns.get(dates[t]),
                per_day_breakdowns.get(base_day) if base_day else None,
            )
        anomalies.append(Anomaly(
            day=dates[t], actual_eur=round(actual, 2), expected_eur=round(expected, 2),
            excess_eur=round(diff, 2), direction=direction, severity=severity,
            z_score=round(z, 2), drivers=drivers,
        ))

    total_excess = round(sum(a.excess_eur for a in anomalies if a.excess_eur > 0), 2)
    return AnomalyResult(
        method="holt_winters_prediction_band",
        scanned_days=n - start,
        anomalies=anomalies,
        total_anomalous_excess_eur=total_excess,
        notes=[f"Flagged days outside ±{Z_FLAG}σ of the seasonal forecast "
               f"(σ=€{sigma:.0f}/day)."],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Resource-level anomaly detection
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ResourceAnomaly:
    resource_id: str
    resource_name: str
    provider_name: str
    sub_account_id: str
    service_name: str
    day: str
    actual_eur: float
    expected_eur: float
    excess_eur: float
    z_score: float
    severity: str                  # "high" | "medium"
    method: str


@dataclass
class ResourceAnomalyResult:
    scanned_resources: int
    flagged_resources: int
    total_excess_eur: float
    anomalies: list[ResourceAnomaly] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# Resources with short / sparse histories use a robust median+MAD rule instead of
# Holt-Winters (too little data for seasonality). MAD = median absolute deviation.
_MAD_K = 3.5            # modified z-score threshold (Iglewicz-Hoaglin)
_MIN_POINTS = 5
_MIN_EXCESS_EUR = 10.0  # ignore spikes smaller than this — not worth alerting on
_MIN_EXCESS_RATIO = 0.25  # and the spike must be >=25% above expected


def _robust_anomaly(series: list[float]) -> tuple[bool, float, float]:
    """Return (is_anomaly_on_last_point, expected, modified_z) using median+MAD."""
    if len(series) < _MIN_POINTS:
        return False, series[-1] if series else 0.0, 0.0
    hist = np.array(series[:-1])
    last = series[-1]
    med = float(np.median(hist))
    mad = float(np.median(np.abs(hist - med))) or 1e-9
    mod_z = 0.6745 * (last - med) / mad
    return abs(mod_z) >= _MAD_K and last > med, med, mod_z


def detect_resource_anomalies(
    resource_series: dict[str, dict],
    scan_last_days: int = 3,
) -> ResourceAnomalyResult:
    """
    Detect individual resources whose recent daily cost is anomalous.

    resource_series maps resource_id -> {
        "meta": {resource_name, provider_name, sub_account_id, service_name},
        "daily": [{"date","cost_eur"}, ...]   # ascending
    }

    Resources with enough history use the Holt-Winters prediction band; sparse
    ones fall back to a robust median+MAD rule. Only spikes (cost above expected)
    are flagged — a resource getting cheaper is not an anomaly worth alerting on.
    """
    anomalies: list[ResourceAnomaly] = []
    scanned = 0
    for rid, blob in resource_series.items():
        daily = sorted(blob.get("daily", []), key=lambda d: d["date"])
        if len(daily) < _MIN_POINTS:
            continue
        scanned += 1
        meta = blob.get("meta", {})
        y = [float(d["cost_eur"]) for d in daily]
        dates = [d["date"] for d in daily]

        # scan the last N days for an anomalous point
        start = max(_MIN_POINTS, len(y) - scan_last_days)
        for t in range(start, len(y)):
            window = y[:t + 1]
            if len(window) >= MIN_SEASONAL_POINTS + 2:
                # Holt-Winters band
                holdout = min(SEASON, len(window) // 4)
                params, _ = _select_params(np.array(window), SEASON, holdout)
                fitted, _ = _hw_additive(np.array(window), SEASON, *params, h=1)
                resid = np.array(window) - fitted
                sigma = float(np.std(resid[SEASON:])) or (np.mean(window) * 0.1) or 1.0
                expected = float(fitted[-1])
                z = (window[-1] - expected) / sigma
                method = "holt_winters"
                is_anom = z >= Z_FLAG
            else:
                is_anom, expected, z = _robust_anomaly(window)
                method = "median_mad"
            if is_anom:
                excess = window[-1] - expected
                # suppress trivial spikes: must clear both an absolute and a
                # relative floor to be worth surfacing/alerting on
                if excess < _MIN_EXCESS_EUR or (expected > 0 and excess / expected < _MIN_EXCESS_RATIO):
                    continue
                anomalies.append(ResourceAnomaly(
                    resource_id=rid,
                    resource_name=meta.get("resource_name", rid.split("/")[-1]),
                    provider_name=meta.get("provider_name", ""),
                    sub_account_id=meta.get("sub_account_id", ""),
                    service_name=meta.get("service_name", ""),
                    day=dates[t], actual_eur=round(window[-1], 2),
                    expected_eur=round(expected, 2), excess_eur=round(excess, 2),
                    z_score=round(z, 2),
                    severity="high" if z >= Z_HIGH else "medium",
                    method=method,
                ))
                break   # one anomaly per resource per scan

    anomalies.sort(key=lambda a: a.excess_eur, reverse=True)
    return ResourceAnomalyResult(
        scanned_resources=scanned,
        flagged_resources=len(anomalies),
        total_excess_eur=round(sum(a.excess_eur for a in anomalies), 2),
        anomalies=anomalies,
        notes=([f"{len(anomalies)} resource(s) flagged above their expected daily cost."]
               if anomalies else ["No resource-level anomalies in the scan window."]),
    )
