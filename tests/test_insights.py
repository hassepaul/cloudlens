"""
Tests for the business-intelligence layer: anomaly detection, chargeback/showback,
and the insights synthesis engine, plus router endpoints.
Run: pytest tests/test_insights.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.anomaly import (
    detect_anomalies,
    detect_anomalies_with_isolation_forest,
    detect_anomalies_ensemble,
    _build_if_features,
    _score_isolation_forest,
)
from app.services.chargeback import allocate, AllocationStrategy
from app.services.insights import synthesize, _efficiency_score


def _series(n=60, spike_at=None, spike=1300, seed=11):
    np.random.seed(seed)
    start = date(2026, 4, 12)
    daily, breakdowns = [], {}
    for i in range(n):
        d = start + timedelta(days=i)
        wk = 70 * np.sin(2 * np.pi * d.weekday() / 7)
        weekend = -90 if d.weekday() >= 5 else 0
        cost = 600 + 2 * i + wk + weekend + np.random.normal(0, 15)
        if spike_at is not None and i == spike_at:
            cost += spike
        daily.append({"date": d.isoformat(), "cost_eur": round(max(0, cost), 2)})
        breakdowns[d.isoformat()] = {"service": {
            "Virtual Machines": round(cost * 0.5 + (spike if (spike_at == i) else 0), 2),
            "Storage": round(cost * 0.3, 2)}}
    return daily, breakdowns


# ══════════════════════════════════════════════════════════════════════════════
# ANOMALY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestAnomalyDetection:
    def test_detects_injected_spike(self):
        daily, bd = _series(spike_at=52)
        res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        assert res.method == "holt_winters_prediction_band"
        assert len(res.anomalies) >= 1
        spike = [a for a in res.anomalies if a.direction == "spike"]
        assert spike and spike[0].z_score >= 2.0

    def test_clean_series_few_or_no_anomalies(self):
        daily, bd = _series(spike_at=None)
        res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        # a clean seasonal series should not be riddled with false positives
        assert len(res.anomalies) <= 2

    def test_attribution_identifies_driver(self):
        daily, bd = _series(spike_at=52)
        res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        spike = [a for a in res.anomalies if a.direction == "spike"][0]
        assert spike.drivers
        assert spike.drivers[0].name == "Virtual Machines"
        assert 0 <= spike.drivers[0].share_of_spike <= 1.0   # capped

    def test_insufficient_history(self):
        daily, bd = _series(n=10)
        res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        assert res.method == "insufficient_history"
        assert res.anomalies == []


# ══════════════════════════════════════════════════════════════════════════════
# ISOLATION FOREST ANOMALY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestIsolationForestAnomalyDetection:
    def test_detects_injected_spike(self):
        daily, bd = _series(spike_at=52)
        res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14, per_day_breakdowns=bd)
        assert res.method == "isolation_forest"
        spikes = [a for a in res.anomalies if a.direction == "spike"]
        assert spikes, "IF should flag the injected spike"

    def test_method_label(self):
        daily, _ = _series(spike_at=52)
        res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14)
        assert res.method == "isolation_forest"

    def test_clean_series_low_false_positive_rate(self):
        daily, _ = _series(spike_at=None)
        res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14)
        # clean seasonal series: false-positive rate should be low
        assert len(res.anomalies) <= 3

    def test_insufficient_history_short_series(self):
        daily, _ = _series(n=6)
        res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14)
        assert res.method == "insufficient_history"
        assert res.anomalies == []

    def test_feature_matrix_shape(self):
        daily, _ = _series(n=30)
        y = np.array([d["cost_eur"] for d in daily])
        dates = [d["date"] for d in daily]
        feats = _build_if_features(y, dates)
        assert feats.shape == (30, 6)

    def test_feature_rolling_mean_no_lookahead(self):
        # At index i, rolling mean must only use days 0..i-1
        daily, _ = _series(n=20)
        y = np.array([d["cost_eur"] for d in daily])
        dates = [d["date"] for d in daily]
        feats = _build_if_features(y, dates)
        # At i=10: window is y[3:10] (7 days), mean of those
        expected_mean = float(np.mean(y[3:10]))
        assert abs(feats[10, 2] - expected_mean) < 1e-6

    def test_if_scores_in_range(self):
        daily, _ = _series(n=30)
        y = np.array([d["cost_eur"] for d in daily])
        dates = [d["date"] for d in daily]
        feats = _build_if_features(y, dates)
        scores = _score_isolation_forest(feats[:20], feats[20:])
        assert np.all(scores >= 0) and np.all(scores <= 1)

    def test_spike_scores_higher_than_normal(self):
        """The spike day should get a higher IF score than a typical day."""
        daily_spike, _ = _series(spike_at=52)
        daily_clean, _ = _series(spike_at=None)
        y_s = np.array([d["cost_eur"] for d in daily_spike])
        y_c = np.array([d["cost_eur"] for d in daily_clean])
        dates = [d["date"] for d in daily_spike]
        feats_s = _build_if_features(y_s, dates)
        feats_c = _build_if_features(y_c, dates)
        start = 46  # scan window start
        scores_spike = _score_isolation_forest(feats_s[:start], feats_s[start:])
        scores_clean = _score_isolation_forest(feats_c[:start], feats_c[start:])
        # spike series max score should exceed clean series max
        assert float(scores_spike.max()) > float(scores_clean.max())

    def test_attribution_on_spike(self):
        daily, bd = _series(spike_at=52)
        res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14, per_day_breakdowns=bd)
        spikes = [a for a in res.anomalies if a.direction == "spike"]
        assert spikes
        assert spikes[0].drivers, "Spike should carry attribution drivers"


# ══════════════════════════════════════════════════════════════════════════════
# ENSEMBLE ANOMALY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsembleAnomalyDetection:
    def test_ensemble_flags_spike(self):
        daily, bd = _series(spike_at=52)
        res = detect_anomalies_ensemble(daily, scan_last_days=14, per_day_breakdowns=bd)
        assert res.method == "ensemble_hw_if"
        spikes = [a for a in res.anomalies if a.direction == "spike"]
        assert spikes

    def test_ensemble_method_label(self):
        daily, _ = _series(spike_at=52)
        res = detect_anomalies_ensemble(daily, scan_last_days=14)
        assert res.method == "ensemble_hw_if"

    def test_both_models_escalate_severity(self):
        # Large spike — both HW and IF should flag it → severity == "high"
        daily, bd = _series(spike_at=52, spike=1800)
        res = detect_anomalies_ensemble(daily, scan_last_days=14, per_day_breakdowns=bd)
        hw_res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        if_res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14)
        hw_days = {a.day for a in hw_res.anomalies}
        if_days = {a.day for a in if_res.anomalies}
        jointly_flagged = hw_days & if_days
        if jointly_flagged:
            for a in res.anomalies:
                if a.day in jointly_flagged:
                    assert a.severity == "high", \
                        f"Day {a.day} flagged by both models should be 'high'"

    def test_ensemble_notes_contain_summary(self):
        daily, _ = _series(spike_at=52)
        res = detect_anomalies_ensemble(daily, scan_last_days=14)
        assert any("Ensemble:" in n for n in res.notes)

    def test_ensemble_insufficient_history(self):
        daily, _ = _series(n=6)
        res = detect_anomalies_ensemble(daily, scan_last_days=14)
        assert res.method == "insufficient_history"
        assert res.anomalies == []

    def test_ensemble_all_anomalies_are_union(self):
        daily, bd = _series(spike_at=52)
        hw_res = detect_anomalies(daily, scan_last_days=14, per_day_breakdowns=bd)
        if_res = detect_anomalies_with_isolation_forest(daily, scan_last_days=14, per_day_breakdowns=bd)
        ens_res = detect_anomalies_ensemble(daily, scan_last_days=14, per_day_breakdowns=bd)
        hw_days = {a.day for a in hw_res.anomalies}
        if_days = {a.day for a in if_res.anomalies}
        ens_days = {a.day for a in ens_res.anomalies}
        # Ensemble must be the union of both
        assert ens_days == hw_days | if_days


# ══════════════════════════════════════════════════════════════════════════════
# CHARGEBACK / SHOWBACK
# ══════════════════════════════════════════════════════════════════════════════

def _records():
    def mk(cost, cc):
        return {"cost_eur": cost, "tags": ({"cost_center": cc} if cc else {}),
                "resource_id": f"/r/{cc}/{cost}"}
    recs = [mk(2000, "engineering")] * 5 + [mk(1500, "data")] * 3 + [mk(800, "marketing")] * 2
    recs += [mk(2200, None), mk(1800, None)]
    return recs


class TestChargeback:
    def test_proportional_distributes_untagged(self):
        cb = allocate(_records(), "cost_center", AllocationStrategy.PROPORTIONAL)
        assert cb.total_spend_eur == 20100
        assert cb.untagged_spend_eur == 4000
        # untagged fully distributed → sum of group totals == total
        assert round(sum(g.total_eur for g in cb.groups), 0) == 20100
        eng = next(g for g in cb.groups if g.name == "engineering")
        assert eng.allocated_shared_eur > 0

    def test_even_split(self):
        cb = allocate(_records(), "cost_center", AllocationStrategy.EVEN)
        shares = [g.allocated_shared_eur for g in cb.groups]
        # 4000 / 3 groups ≈ 1333 each
        assert all(abs(s - 4000 / 3) < 1 for s in shares)

    def test_showback_keeps_untagged_separate(self):
        cb = allocate(_records(), "cost_center", AllocationStrategy.SHOWBACK)
        names = [g.name for g in cb.groups]
        assert "Unallocated" in names
        # no shared allocation in showback
        assert all(g.allocated_shared_eur == 0 for g in cb.groups)

    def test_budget_status_flags_breach(self):
        cb = allocate(_records(), "cost_center", AllocationStrategy.PROPORTIONAL,
                      budgets={"engineering": 5000})
        eng = next(g for g in cb.groups if g.name == "engineering")
        assert eng.budget_status == "breach"

    def test_coverage_computed(self):
        cb = allocate(_records(), "cost_center", AllocationStrategy.PROPORTIONAL)
        assert 79 <= cb.tagging_coverage_pct <= 81     # 16100/20100 ≈ 80%


# ══════════════════════════════════════════════════════════════════════════════
# INSIGHTS SYNTHESIS
# ══════════════════════════════════════════════════════════════════════════════

class TestInsights:
    def test_efficiency_score_scales_with_waste(self):
        assert _efficiency_score(10000, 0) == 100
        assert _efficiency_score(10000, 2500) < _efficiency_score(10000, 500)
        assert _efficiency_score(0, 0) == 100

    def test_digest_ranks_by_severity(self):
        waste = [{"saving_eur": 3000, "priority": "critical", "waste_type": "idle_vm",
                  "resource_name": "vm-1"}]
        dig = synthesize("t1", "Acme", monthly_spend=10000, waste_items=waste)
        assert dig.insights
        assert dig.insights[0].rank == 1
        assert dig.efficiency_score < 100
        assert "Acme" in dig.headline_summary
        assert dig.headline_summary_it  # bilingual present

    def test_digest_empty_when_clean(self):
        dig = synthesize("t1", "Lean Corp", monthly_spend=10000, waste_items=[])
        assert dig.efficiency_score == 100
        assert dig.monthly_recoverable_eur == 0

    def test_forecast_drift_insight(self):
        dig = synthesize("t1", "Acme", monthly_spend=10000, waste_items=[],
                         forecast_month_end=12000)
        cats = [i.category for i in dig.insights]
        assert "forecast" in cats


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestInsightsRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def test_anomalies_endpoint(self):
        daily, bd = _series(spike_at=52)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in daily]
        svc_rows = []
        for d, dim in bd.items():
            for name, cost in dim["service"].items():
                svc_rows.append({"record_date": d, "service_name": name, "cost": cost})

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "c.service_name" in query and "GROUP BY c.record_date, c.service_name" in query:
                return svc_rows
            return cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/insights/t-1/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert body["method"] == "holt_winters_prediction_band"

    def test_anomalies_endpoint_isolation_forest(self):
        daily, bd = _series(spike_at=52)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in daily]
        svc_rows = []
        for d, dim in bd.items():
            for name, cost in dim["service"].items():
                svc_rows.append({"record_date": d, "service_name": name, "cost": cost})

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "c.service_name" in query:
                return svc_rows
            return cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/insights/t-1/anomalies?method=isolation_forest")
        assert resp.status_code == 200
        assert resp.json()["method"] == "isolation_forest"

    def test_anomalies_endpoint_ensemble(self):
        daily, bd = _series(spike_at=52)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in daily]
        svc_rows = []
        for d, dim in bd.items():
            for name, cost in dim["service"].items():
                svc_rows.append({"record_date": d, "service_name": name, "cost": cost})

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "c.service_name" in query:
                return svc_rows
            return cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/insights/t-1/anomalies?method=ensemble")
        assert resp.status_code == 200
        assert resp.json()["method"] == "ensemble_hw_if"

    def test_anomalies_endpoint_rejects_bad_method(self):
        resp = self._client().get("/api/v1/insights/t-1/anomalies?method=banana")
        assert resp.status_code == 422

    def test_chargeback_endpoint(self):
        recs = _records()

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return recs

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/insights/t-1/chargeback?strategy=proportional")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_spend_eur"] == 20100
        assert len(body["groups"]) >= 3

    def test_chargeback_rejects_bad_strategy(self):
        resp = self._client().get("/api/v1/insights/t-1/chargeback?strategy=banana")
        assert resp.status_code == 422

    def test_digest_endpoint(self):
        daily, bd = _series(spike_at=52)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in daily]
        svc_rows = []
        for d, dim in bd.items():
            for name, cost in dim["service"].items():
                svc_rows.append({"record_date": d, "service_name": name, "cost": cost})
        waste_rows = [{"saving_eur": 2465, "priority": "critical", "waste_type": "idle_vm",
                       "resource_name": "vm-1"}]
        tag_rows = _records()

        async def fake_get(container, item_id, pk):
            return {"tenant_name": "Acme Manufacturing"}

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "waste_item" in query:
                return waste_rows
            if "c.service_name" in query:
                return svc_rows
            if "c.tags" in query:
                return tag_rows
            return cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query), \
             patch("app.services.cosmos.get_item", new=fake_get):
            resp = self._client().get("/api/v1/insights/t-1/digest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["efficiency_score"] <= 100
        assert "Acme Manufacturing" in body["headline_summary"]
        assert isinstance(body["insights"], list) and len(body["insights"]) >= 1
