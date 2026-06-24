"""
Forecast engine + router tests.
Run: pytest tests/test_forecast.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("ENVIRONMENT", "development")

from app.services import forecast as fc


def _seasonal_series(n=90, base=600, trend=2.0, seed=7):
    np.random.seed(seed)
    start = date(2026, 3, 1)
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        weekly = 80 * np.sin(2 * np.pi * d.weekday() / 7)
        weekend = -100 if d.weekday() >= 5 else 0
        noise = np.random.normal(0, 20)
        cost = max(0, base + trend * i + weekly + weekend + noise)
        out.append({"date": d.isoformat(), "cost_eur": round(cost, 2)})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FORECAST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TestForecastSpend:
    def test_seasonal_series_uses_holt_winters(self):
        r = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        assert r.method == "holt_winters_additive_weekly"
        assert len(r.points) == 30
        assert r.history_days == 90

    def test_backtest_mape_is_reasonable(self):
        r = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        # On a clean seasonal series the model should be well under 20% error
        assert r.mape is not None and r.mape < 20

    def test_forecast_values_nonnegative(self):
        r = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        assert all(p.value >= 0 for p in r.points)
        assert all(p.lower <= p.value <= p.upper for p in r.points)

    def test_intervals_widen_with_horizon(self):
        r = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        w_first = r.points[0].upper - r.points[0].lower
        w_last = r.points[-1].upper - r.points[-1].lower
        assert w_last > w_first

    def test_short_series_falls_back(self):
        short = _seasonal_series(n=8)
        r = fc.forecast_spend(short, horizon_days=14)
        assert r.method == "damped_trend"
        assert r.confidence == "low"
        assert r.mape is None
        assert any("fallback" in n.lower() or "history" in n.lower() for n in r.notes)

    def test_empty_series(self):
        r = fc.forecast_spend([], horizon_days=30)
        assert r.method == "none"
        assert r.points == []

    def test_month_end_projection_present(self):
        r = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        assert r.month_end_projection is not None and r.month_end_projection > 0


class TestCostOfInaction:
    def test_optimized_below_baseline_when_waste_exists(self):
        base = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        waste = [
            {"saving_eur": 2400, "priority": "critical", "waste_type": "idle_vm"},
            {"saving_eur": 300, "priority": "high", "waste_type": "oversized_vm"},
        ]
        t = fc.cost_of_inaction(base, waste, horizon_days=30)
        # By the end of the horizon the optimized line should sit below baseline
        assert t.optimized[-1].value < t.baseline[-1].value
        assert t.monthly_recoverable_eur == 2700
        assert t.annual_recoverable_eur == 2700 * 12
        assert t.daily_waste_burn_eur == round(2700 / 30, 2)
        assert t.cumulative_inaction_eur > 0

    def test_trajectories_coincide_with_no_waste(self):
        base = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        t = fc.cost_of_inaction(base, [], horizon_days=30)
        assert t.monthly_recoverable_eur == 0
        assert t.cumulative_inaction_eur == 0
        for b, o in zip(t.baseline, t.optimized):
            assert b.value == o.value
        assert any("nothing to recover" in n.lower() for n in t.notes)

    def test_critical_ramps_faster_than_low(self):
        base = fc.forecast_spend(_seasonal_series(), horizon_days=30)
        crit = fc.cost_of_inaction(base, [{"saving_eur": 600, "priority": "critical", "waste_type": "idle_vm"}], 30)
        low = fc.cost_of_inaction(base, [{"saving_eur": 600, "priority": "low", "waste_type": "old_snapshots"}], 30)
        # critical realises savings sooner → larger cumulative gap by day 30
        assert crit.cumulative_inaction_eur > low.cumulative_inaction_eur


class TestRoadmap:
    def test_run_rate_bends_down_per_phase(self):
        waste = [
            {"saving_eur": 2400, "priority": "critical", "waste_type": "idle_vm"},
            {"saving_eur": 300, "priority": "high", "waste_type": "oversized_vm"},
            {"saving_eur": 100, "priority": "medium", "waste_type": "idle_app_service"},
            {"saving_eur": 50, "priority": "low", "waste_type": "old_snapshots"},
        ]
        rr = fc.remediation_roadmap(20000, waste)
        assert rr.current_run_rate_eur == 20000
        assert rr.optimized_run_rate_eur == 20000 - 2850
        # each phase's target run-rate is monotonically lower
        rates = [p.target_run_rate_eur for p in rr.phases]
        assert rates == sorted(rates, reverse=True)
        assert len(rr.phases) == 4

    def test_empty_waste_no_phases(self):
        rr = fc.remediation_roadmap(10000, [])
        assert rr.phases == []
        assert rr.optimized_run_rate_eur == 10000


class TestBudgetBreach:
    def test_detects_breach_dates(self):
        base = fc.forecast_spend(_seasonal_series(base=700, trend=3), horizon_days=60)
        waste = [{"saving_eur": 3000, "priority": "critical", "waste_type": "idle_vm"}]
        t = fc.cost_of_inaction(base, waste, horizon_days=60)
        bb = fc.budget_breach(monthly_budget=10000, baseline=base, trajectory=t)
        assert bb.monthly_budget_eur == 10000
        # do-nothing should breach at/earlier than optimized (or optimized stays safe)
        if bb.breach_date_baseline and bb.breach_date_optimized:
            assert bb.breach_date_optimized >= bb.breach_date_baseline


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestForecastRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def test_forecast_endpoint(self):
        series = _seasonal_series()
        rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in series]

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/forecast/t-1?horizon_days=30")
        assert resp.status_code == 200
        body = resp.json()
        assert body["method"] == "holt_winters_additive_weekly"
        assert len(body["points"]) == 30

    def test_cost_of_inaction_endpoint(self):
        series = _seasonal_series()
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in series]
        waste_rows = [{"saving_eur": 2400, "priority": "critical", "waste_type": "idle_vm"}]

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            # waste container query contains "waste_item"
            return waste_rows if "waste_item" in query else cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/forecast/t-1/cost-of-inaction?horizon_days=30")
        assert resp.status_code == 200
        body = resp.json()
        assert body["monthly_recoverable_eur"] == 2400
        assert body["daily_waste_burn_eur"] > 0
        assert len(body["baseline"]) == len(body["optimized"]) == 30

    def test_roadmap_endpoint(self):
        series = _seasonal_series(n=30)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in series]
        waste_rows = [
            {"saving_eur": 2400, "priority": "critical", "waste_type": "idle_vm"},
            {"saving_eur": 200, "priority": "high", "waste_type": "oversized_vm"},
        ]

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return waste_rows if "waste_item" in query else cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/forecast/t-1/roadmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_monthly_saving_eur"] == 2600
        assert len(body["phases"]) == 2

    def test_budget_breach_endpoint(self):
        series = _seasonal_series(base=700, trend=3)
        cost_rows = [{"record_date": d["date"], "daily_cost": d["cost_eur"]} for d in series]
        waste_rows = [{"saving_eur": 3000, "priority": "critical", "waste_type": "idle_vm"}]

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return waste_rows if "waste_item" in query else cost_rows

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = self._client().get("/api/v1/forecast/t-1/budget-breach?monthly_budget=10000")
        assert resp.status_code == 200
        body = resp.json()
        assert body["monthly_budget_eur"] == 10000
        assert "safe_if_actioned" in body

    def test_budget_breach_requires_budget(self):
        resp = self._client().get("/api/v1/forecast/t-1/budget-breach")
        assert resp.status_code == 422
