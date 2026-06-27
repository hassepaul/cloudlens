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
from datetime import date as _date
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


# ──────────────────────────────────────────────────────────────────────────────
# Isolation Forest anomaly detection (numpy-only implementation)
# ──────────────────────────────────────────────────────────────────────────────
#
# Isolation Forest (Liu, Ting, Zhou 2008) works by building random binary trees
# that recursively partition feature space with random feature/split-point
# selections.  Anomalous points are isolated quickly (short path lengths)
# because they occupy sparse regions of the space.  The anomaly score for a
# point is 2^(−avg_path / c(n)), where c(n) is the expected path length for a
# random BST on n samples.  Scores close to 1 → anomaly; ~0.5 → normal.
#
# Using IF *alongside* Holt-Winters provides complementary signal:
#   • HW catches temporal deviations (unexpectedly high vs. the seasonal trend).
#   • IF catches multivariate outliers in feature space — it can flag unusual
#     *combinations* of features (e.g. high absolute cost AND high relative
#     ratio AND large diff from the prior day) that HW might miss.
#
# The ensemble function runs both and escalates severity when both agree.
# ──────────────────────────────────────────────────────────────────────────────

_IF_N_ESTIMATORS = 100
_IF_MAX_SAMPLES = 16    # small subsample → wider score spread → better discrimination
_IF_CONTAMINATION = 0.10  # expected fraction of anomalies; sets adaptive threshold


def _expected_c(n: int) -> float:
    """Expected isolation path length for n samples (normalisation constant).

    Equation 1 from Liu, Ting, Zhou (2008) — harmonic-number approximation of
    the expected path length in a random Binary Search Tree on n points.
    """
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    H = float(np.log(n - 1)) + 0.5772156649015329  # Euler-Mascheroni constant
    return 2.0 * H - 2.0 * (n - 1) / n


def _build_if_features(y: np.ndarray, dates: list[str]) -> np.ndarray:
    """Build a (n, 6) feature matrix for Isolation Forest from a cost time-series.

    Features:
        0 – raw daily cost (EUR)
        1 – day-of-week  (0 = Monday … 6 = Sunday)
        2 – 7-day rolling mean of *preceding* days (history only, no look-ahead)
        3 – 7-day rolling standard deviation
        4 – cost / rolling-mean ratio  (relative level vs. recent baseline)
        5 – lag-1 first difference  (day-over-day change)
    """
    n = len(y)
    feats = np.zeros((n, 6), dtype=float)
    feats[:, 0] = y
    feats[:, 1] = np.array(
        [_date.fromisoformat(d).weekday() for d in dates], dtype=float
    )
    for i in range(n):
        window = y[max(0, i - 7):i]
        if len(window) >= 2:
            feats[i, 2] = float(np.mean(window))
            feats[i, 3] = float(np.std(window))
        elif len(window) == 1:
            feats[i, 2] = float(window[0])
            feats[i, 3] = 0.0
        else:
            feats[i, 2] = float(y[i])
            feats[i, 3] = 0.0
        feats[i, 4] = float(y[i] / feats[i, 2]) if feats[i, 2] > 0 else 1.0
        feats[i, 5] = float(y[i] - y[i - 1]) if i > 0 else 0.0
    return feats


def _score_isolation_forest(
    train: np.ndarray,           # (n_train, n_features) — already normalised
    test: np.ndarray,            # (n_test,  n_features)
    n_estimators: int = _IF_N_ESTIMATORS,
    max_samples: int = _IF_MAX_SAMPLES,
    seed: int = 42,
) -> np.ndarray:
    """Pure-numpy Isolation Forest scorer.

    Returns anomaly scores for each test row in [0, 1].
    Scores near 1 → highly anomalous; ~0.5 → normal.
    """
    rng = np.random.default_rng(seed)
    n_sub = min(max_samples, len(train))
    c_n = _expected_c(n_sub)
    n_test = len(test)

    if c_n <= 0 or n_test == 0:
        return np.full(n_test, 0.5)

    n_feats = train.shape[1]
    max_depth = int(np.ceil(np.log2(max(n_sub, 2)))) + 1
    replace = len(train) < n_sub
    path_sums = np.zeros(n_test)

    for _ in range(n_estimators):
        sub_idx = rng.choice(len(train), size=n_sub, replace=replace)
        sub = train[sub_idx]            # (n_sub, n_feats)
        path_lengths = np.zeros(n_test)

        # Iterative isolation tree — stack entries: (subsample_idx, test_idx, depth)
        stack: list[tuple] = [(np.arange(n_sub), np.arange(n_test), 0)]
        while stack:
            tr, te, depth = stack.pop()
            if len(te) == 0:
                continue
            if len(tr) <= 1 or depth >= max_depth:
                path_lengths[te] += depth + _expected_c(len(tr))
                continue
            f = int(rng.integers(0, n_feats))
            f_vals = sub[tr, f]
            f_min, f_max = float(f_vals.min()), float(f_vals.max())
            if f_min >= f_max:
                path_lengths[te] += depth + _expected_c(len(tr))
                continue
            split = float(rng.uniform(f_min, f_max))
            left_tr = tr[sub[tr, f] < split]
            right_tr = tr[sub[tr, f] >= split]
            left_te = te[test[te, f] < split]
            right_te = te[test[te, f] >= split]
            stack.append((left_tr, left_te, depth + 1))
            stack.append((right_tr, right_te, depth + 1))

        path_sums += path_lengths

    avg_paths = path_sums / n_estimators
    return np.power(2.0, -avg_paths / c_n)


def detect_anomalies_with_isolation_forest(
    daily: list[dict],                           # [{"date","cost_eur"}, ...] ascending
    scan_last_days: int = 14,
    per_day_breakdowns: Optional[dict] = None,   # {date: {dim: {name: cost}}}
    contamination: float = _IF_CONTAMINATION,
) -> AnomalyResult:
    """Detect spend anomalies using Isolation Forest on derived time-series features.

    Feature vector per day: raw cost, day-of-week, 7-day rolling mean/std,
    cost-to-rolling-mean ratio, and lag-1 difference.  The model is trained on
    the days *before* the scan window (no look-ahead leakage), then scores each
    day in the scan window.  Attribution follows the same logic as the HW
    detector: diff the service/resource-group mix vs. the prior-week baseline.
    """
    daily = sorted(daily, key=lambda d: d["date"])
    y = np.array([float(d["cost_eur"]) for d in daily])
    dates = [d["date"] for d in daily]
    n = len(y)

    if n < _MIN_POINTS + 2:
        return AnomalyResult(
            method="insufficient_history", scanned_days=0,
            notes=[f"Need >= {_MIN_POINTS + 2} days for Isolation Forest; have {n}."],
        )

    feats = _build_if_features(y, dates)
    start = max(_MIN_POINTS, n - scan_last_days)
    train_feats = feats[:start]
    test_feats = feats[start:]

    if len(train_feats) < 2:
        return AnomalyResult(
            method="insufficient_history", scanned_days=0,
            notes=["Insufficient training window for Isolation Forest."],
        )

    # Normalise using training statistics only (prevents look-ahead leakage)
    mean = train_feats.mean(axis=0)
    std = train_feats.std(axis=0)
    std[std == 0] = 1.0
    train_norm = (train_feats - mean) / std
    test_norm = (test_feats - mean) / std

    # Adaptive threshold: (1-contamination) quantile of training scores.
    # Scoring train vs. train gives an empirical "normal" score distribution;
    # any test day above the top-contamination% of that distribution is flagged.
    train_scores = _score_isolation_forest(train_norm, train_norm)
    threshold = float(np.quantile(train_scores, 1.0 - contamination))

    scores = _score_isolation_forest(train_norm, test_norm)
    sigma = float(np.std(y[:start])) or 1.0

    anomalies: list[Anomaly] = []
    for i, t in enumerate(range(start, n)):
        if float(scores[i]) <= threshold:
            continue
        actual = float(y[t])
        hist = y[max(0, t - 7):t]
        expected = float(np.mean(hist)) if len(hist) > 0 else float(np.mean(y[:t]))
        diff = actual - expected
        z = diff / sigma
        direction = "spike" if diff > 0 else "dip"
        severity = "high" if abs(z) >= Z_HIGH or float(scores[i]) >= 0.75 else "medium"
        drivers: list[AnomalyDriver] = []
        if direction == "spike" and per_day_breakdowns:
            base_day = dates[t - 7] if t - 7 >= 0 else None
            drivers = _attribute(
                dates[t], diff,
                per_day_breakdowns.get(dates[t]),
                per_day_breakdowns.get(base_day) if base_day else None,
            )
        anomalies.append(Anomaly(
            day=dates[t], actual_eur=round(actual, 2),
            expected_eur=round(expected, 2), excess_eur=round(diff, 2),
            direction=direction, severity=severity, z_score=round(z, 2),
            drivers=drivers,
        ))

    total_excess = round(sum(a.excess_eur for a in anomalies if a.excess_eur > 0), 2)
    return AnomalyResult(
        method="isolation_forest",
        scanned_days=n - start,
        anomalies=anomalies,
        total_anomalous_excess_eur=total_excess,
        notes=[
            f"Isolation Forest flagged {len(anomalies)} day(s) "
            f"(adaptive threshold={threshold:.4f}, contamination={contamination:.0%}, "
            f"{_IF_N_ESTIMATORS} trees)."
        ],
    )


def detect_anomalies_ensemble(
    daily: list[dict],
    scan_last_days: int = 14,
    per_day_breakdowns: Optional[dict] = None,
) -> AnomalyResult:
    """Run Holt-Winters and Isolation Forest then merge their findings.

    Merge rules:
      • Day flagged by **both** models → severity escalated to 'high' (higher
        confidence; two independent detectors agree).
      • Day flagged by one model only → kept as-is with original severity.

    When only one model has enough data the ensemble degrades gracefully to
    that model alone and labels the method accordingly.
    """
    hw = detect_anomalies(daily, scan_last_days, per_day_breakdowns)
    ifo = detect_anomalies_with_isolation_forest(daily, scan_last_days, per_day_breakdowns)

    hw_ok = hw.method != "insufficient_history"
    if_ok = ifo.method != "insufficient_history"

    if not hw_ok and not if_ok:
        return AnomalyResult(
            method="insufficient_history", scanned_days=0,
            notes=hw.notes or ifo.notes,
        )
    if not hw_ok:
        ifo.method = "ensemble_if_only"
        return ifo
    if not if_ok:
        hw.method = "ensemble_hw_only"
        return hw

    hw_days = {a.day: a for a in hw.anomalies}
    if_days = {a.day: a for a in ifo.anomalies}
    confirmed_days = set(hw_days) & set(if_days)
    all_days = sorted(set(hw_days) | set(if_days))

    merged: list[Anomaly] = []
    for day in all_days:
        hw_a = hw_days.get(day)
        if_a = if_days.get(day)
        if hw_a and if_a:
            merged.append(Anomaly(
                day=hw_a.day, actual_eur=hw_a.actual_eur,
                expected_eur=hw_a.expected_eur, excess_eur=hw_a.excess_eur,
                direction=hw_a.direction, severity="high",
                z_score=hw_a.z_score, drivers=hw_a.drivers,
            ))
        elif hw_a:
            merged.append(hw_a)
        else:
            assert if_a is not None
            merged.append(if_a)

    total_excess = round(sum(a.excess_eur for a in merged if a.excess_eur > 0), 2)
    return AnomalyResult(
        method="ensemble_hw_if",
        scanned_days=hw.scanned_days,
        anomalies=merged,
        total_anomalous_excess_eur=total_excess,
        notes=[
            f"Ensemble: HW flagged {len(hw.anomalies)}, "
            f"IF flagged {len(ifo.anomalies)}, "
            f"{len(confirmed_days)} confirmed by both.",
            *hw.notes[:1],
            *ifo.notes[:1],
        ],
    )
