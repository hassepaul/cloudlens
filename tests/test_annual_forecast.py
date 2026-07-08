"""
Tests for annual (month-of-year) seasonality in the forecast engine, monthly
long-range forecasting, and the monthly-rollup persistence helper.
Run: pytest tests/test_annual_forecast.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("INTERNAL_API_KEY", "k")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.forecast import (
    annual_seasonal_factors, forecast_spend, forecast_monthly, _add_months,
    MIN_ANNUAL_MONTHS,
)


def _months_series(values_by_month: dict[int, float], years: int = 2) -> list[dict]:
    """Build a monthly rollup series repeating a month-of-year pattern."""
    out = []
    y0 = 2024
    for yr in range(years):
        for mo in range(1, 13):
            out.append({"month": f"{y0 + yr}-{mo:02d}", "cost_eur": values_by_month[mo]})
    return out


def _flat_pattern(base=1000.0, dec=1600.0, jun=650.0) -> dict[int, float]:
    p = {mo: base for mo in range(1, 13)}
    p[12] = dec   # December spike
    p[6] = jun    # June dip
    return p


# ══════════════════════════════════════════════════════════════════════════════
# annual_seasonal_factors
# ══════════════════════════════════════════════════════════════════════════════

class TestAnnualFactors:
    def test_insufficient_history_returns_none(self):
        short = [{"month": f"2025-{m:02d}", "cost_eur": 100.0} for m in range(1, MIN_ANNUAL_MONTHS)]
        assert annual_seasonal_factors(short) is None

    def test_none_for_empty(self):
        assert annual_seasonal_factors([]) is None

    def test_factors_capture_month_of_year_shape(self):
        series = _months_series(_flat_pattern())
        factors = annual_seasonal_factors(series)
        assert factors is not None
        assert set(factors.keys()) == set(range(1, 13))
        # December runs hot, June runs cold, relative to the annual average.
        assert factors[12] > 1.2
        assert factors[6] < 0.85
        # Mean factor across the year normalises to ~1.0
        assert abs(sum(factors.values()) / 12 - 1.0) < 0.01

    def test_degenerate_zero_series_returns_none(self):
        series = _months_series({mo: 0.0 for mo in range(1, 13)})
        assert annual_seasonal_factors(series) is None


# ══════════════════════════════════════════════════════════════════════════════
# forecast_spend annual overlay
# ══════════════════════════════════════════════════════════════════════════════

def _daily(n: int, base: float = 100.0) -> list[dict]:
    today = date.today()
    return [{"date": (today - timedelta(days=n - 1 - i)).isoformat(), "cost_eur": base}
            for i in range(n)]


class TestForecastSpendAnnualOverlay:
    def test_no_monthly_history_no_annual(self):
        res = forecast_spend(_daily(60), horizon_days=30)
        assert not res.method.endswith("+annual")

    def test_annual_overlay_applied_and_changes_path(self):
        daily = _daily(60)
        # Strictly month-varying pattern so adjacent months always differ,
        # regardless of the current calendar month the test runs in.
        pattern = {mo: 800.0 + mo * 40 for mo in range(1, 13)}
        series = _months_series(pattern)
        base = forecast_spend(daily, horizon_days=45)
        annual = forecast_spend(daily, horizon_days=45, monthly_history=series)
        assert annual.method.endswith("+annual")
        assert any("Annual" in n for n in annual.notes)
        # The 45-day horizon crosses at least one month boundary, so at least one
        # forecast point must differ once the month-of-year factor is applied.
        base_vals = [p.value for p in base.points]
        ann_vals = [p.value for p in annual.points]
        assert base_vals != ann_vals

    def test_short_monthly_history_ignored(self):
        daily = _daily(60)
        short = [{"month": f"2025-{m:02d}", "cost_eur": 1000.0} for m in range(1, 6)]
        res = forecast_spend(daily, horizon_days=30, monthly_history=short)
        assert not res.method.endswith("+annual")


# ══════════════════════════════════════════════════════════════════════════════
# forecast_monthly
# ══════════════════════════════════════════════════════════════════════════════

class TestForecastMonthly:
    def test_add_months_wraps_year(self):
        assert _add_months("2025-11", 3) == "2026-02"
        assert _add_months("2025-01", 0) == "2025-01"
        assert _add_months("2024-12", 1) == "2025-01"

    def test_full_seasonal_model_with_two_years(self):
        series = _months_series(_flat_pattern(), years=2)   # 24 months
        res = forecast_monthly(series, horizon_months=12)
        assert res.method == "holt_winters_additive_annual"
        assert len(res.points) == 12
        # forecast month labels are YYYY-MM and strictly after the last history
        assert all(len(p.day) == 7 and p.day > series[-1]["month"] for p in res.points)

    def test_short_history_falls_back(self):
        series = [{"month": f"2025-{m:02d}", "cost_eur": 1000.0 + m} for m in range(1, 7)]
        res = forecast_monthly(series, horizon_months=6)
        assert res.method == "damped_trend_monthly"
        assert res.confidence == "low"
        assert len(res.points) == 6

    def test_empty_history(self):
        res = forecast_monthly([], horizon_months=6)
        assert res.method == "none"
        assert res.points == []


# ══════════════════════════════════════════════════════════════════════════════
# rollups persistence
# ══════════════════════════════════════════════════════════════════════════════

class TestRollups:
    def test_aggregate_monthly(self):
        from app.services.rollups import _aggregate_monthly
        daily = [
            {"date": "2025-01-05", "cost_eur": 100.0},
            {"date": "2025-01-06", "cost_eur": 50.0},
            {"date": "2025-02-01", "cost_eur": 200.0},
        ]
        agg = _aggregate_monthly(daily)
        assert agg["2025-01"] == 150.0
        assert agg["2025-02"] == 200.0

    @pytest.mark.asyncio
    async def test_persist_skips_current_month(self, monkeypatch):
        from app.services import rollups, cosmos as real
        written = {}

        async def fake_upsert(container, item):
            written[item["month"]] = item
            return item

        monkeypatch.setattr(real, "upsert_item", fake_upsert)
        cur = date.today().strftime("%Y-%m")
        daily = [
            {"date": "2024-01-15", "cost_eur": 100.0},           # sealed → written
            {"date": f"{cur}-01", "cost_eur": 50.0},             # current → skipped
        ]
        n = await rollups.persist_monthly_rollups("t1", daily)
        assert "2024-01" in written
        assert cur not in written
        assert n == 1
        assert written["2024-01"]["_partitionKey"] == "t1"
        assert written["2024-01"]["ttl"] > 0

    @pytest.mark.asyncio
    async def test_get_monthly_rollups_sorted(self, monkeypatch):
        from app.services import rollups, cosmos as real

        async def fake_query(container, query, parameters=None, partition_key=None, max_item_count=100):
            return [
                {"month": "2025-03", "cost_eur": 300.0},
                {"month": "2025-01", "cost_eur": 100.0},
            ]

        monkeypatch.setattr(real, "query_items", fake_query)
        rows = await rollups.get_monthly_rollups("t1")
        assert [r["month"] for r in rows] == ["2025-01", "2025-03"]
