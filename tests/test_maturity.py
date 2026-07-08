"""
Tests for FinOps Maturity Score.
Run: pytest tests/test_maturity.py -v
"""
from __future__ import annotations
import os
from unittest.mock import AsyncMock, patch, call

import pytest

os.environ.setdefault("INTERNAL_API_KEY",       "test-key")
os.environ.setdefault("AZURE_TENANT_ID",        "t")
os.environ.setdefault("AZURE_CLIENT_ID",        "c")
os.environ.setdefault("COSMOS_ENDPOINT",        "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME",   "s")
os.environ.setdefault("KEY_VAULT_NAME",         "k")

from app.services.maturity import (
    _percentile_vs_cohort,
    _cohort_context,
    _maturity_label,
    _recommended_action,
    _score_tagging,
    _score_waste,
    _score_commitment_coverage,
    _score_unit_economics,
    _score_anomaly_response,
    _score_budget_adherence,
    compute_maturity_score,
    MaturityScore,
    DimensionScore,
    _VALID_VERTICALS,
    _DIM_WEIGHTS,
    _BENCHMARKS,
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

class TestPercentileHelper:
    def test_above_p90_over_90(self):
        pct = _percentile_vs_cohort(95, 70, 85, 90)
        assert pct > 90

    def test_between_p75_p90(self):
        pct = _percentile_vs_cohort(87, 70, 85, 90)
        assert 75 <= pct < 90

    def test_between_median_p75(self):
        pct = _percentile_vs_cohort(77, 70, 85, 90)
        assert 50 <= pct < 75

    def test_below_median(self):
        pct = _percentile_vs_cohort(30, 70, 85, 90)
        assert pct < 50

    def test_at_median_is_50(self):
        pct = _percentile_vs_cohort(70, 70, 85, 90)
        assert abs(pct - 50) < 1.0


class TestCohortContext:
    def test_top_decile_message(self):
        msg = _cohort_context(95, 70, 85, 90)
        assert "Top decile" in msg

    def test_below_median_message(self):
        msg = _cohort_context(40, 70, 85, 90)
        assert "Below cohort median" in msg


class TestMaturityLabel:
    def test_fly(self):     assert _maturity_label(90) == "Fly"
    def test_run(self):     assert _maturity_label(70) == "Run"
    def test_walk(self):    assert _maturity_label(50) == "Walk"
    def test_crawl(self):   assert _maturity_label(20) == "Crawl"
    def test_boundary_40(self): assert _maturity_label(40) in ("Walk", "Crawl")
    def test_boundary_65(self): assert _maturity_label(65) in ("Run", "Walk")


class TestRecommendedAction:
    def test_high_score_maintain(self):
        assert _recommended_action("tagging_completeness", 90) == "Maintain current practice."

    def test_low_score_has_action(self):
        action = _recommended_action("tagging_completeness", 40)
        assert len(action) > 20

    def test_all_dimensions_covered(self):
        for dim in _DIM_WEIGHTS:
            action = _recommended_action(dim, 30)
            assert action  # should never be empty


# ── Dimension scorers (mocked Cosmos) ─────────────────────────────────────────

class MockSettings:
    cosmos_container_cost_records = "cost_records"
    cosmos_container_waste_items  = "waste_items"
    cosmos_container_policies     = "policies"


_SETTINGS = MockSettings()


class TestScoringTagging:
    @pytest.mark.asyncio
    async def test_full_tagging_score_100(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[100], [100]]  # total=100, tagged=100
            score, evidence = await _score_tagging("t-1", _SETTINGS)
        assert score == 100.0
        assert evidence["tagged_pct"] == 100.0

    @pytest.mark.asyncio
    async def test_half_tagging_score_50(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[100], [50]]
            score, evidence = await _score_tagging("t-1", _SETTINGS)
        assert score == 50.0

    @pytest.mark.asyncio
    async def test_no_records_score_0(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[0], [0]]
            score, _ = await _score_tagging("t-1", _SETTINGS)
        assert score == 0.0


class TestScoringWaste:
    @pytest.mark.asyncio
    async def test_zero_waste_score_100(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[10000.0], [0.0]]
            score, _ = await _score_waste("t-1", _SETTINGS)
        assert score == 100.0

    @pytest.mark.asyncio
    async def test_50pct_waste_score_zero(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[1000.0], [500.0]]  # 50% waste
            score, _ = await _score_waste("t-1", _SETTINGS)
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_10pct_waste_reasonable_score(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = [[1000.0], [100.0]]  # 10% waste
            score, _ = await _score_waste("t-1", _SETTINGS)
        assert 75 <= score <= 85


class TestScoringUnitEconomics:
    @pytest.mark.asyncio
    async def test_no_metrics_score_0(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            score, evidence = await _score_unit_economics("t-1", _SETTINGS)
        assert score == 0.0
        assert evidence["metric_definitions"] == 0

    @pytest.mark.asyncio
    async def test_one_metric_40pts(self):
        from datetime import date
        metric = {"type": "unit_metric", "recorded_at": date.today().isoformat()}
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = [metric]
            score, _ = await _score_unit_economics("t-1", _SETTINGS)
        assert score >= 40.0

    @pytest.mark.asyncio
    async def test_three_recent_metrics_high_score(self):
        from datetime import date
        metrics = [
            {"type": "unit_metric", "recorded_at": date.today().isoformat()}
            for _ in range(3)
        ]
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = metrics
            score, _ = await _score_unit_economics("t-1", _SETTINGS)
        assert score == 100.0


class TestScoringAnomalyResponse:
    @pytest.mark.asyncio
    async def test_no_violations_neutral_score(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            score, evidence = await _score_anomaly_response("t-1", _SETTINGS)
        assert score == 50.0
        assert evidence["resolved_violations"] == 0

    @pytest.mark.asyncio
    async def test_fast_response_high_score(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        violations = [
            {
                "triggered_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "resolved_at":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = violations
            score, evidence = await _score_anomaly_response("t-1", _SETTINGS)
        assert score == 100.0
        assert evidence["avg_response_hours"] < 4

    @pytest.mark.asyncio
    async def test_slow_response_low_score(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        violations = [
            {
                "triggered_at": (now - timedelta(hours=200)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "resolved_at":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = violations
            score, _ = await _score_anomaly_response("t-1", _SETTINGS)
        assert score <= 20.0


class TestScoringBudgetAdherence:
    @pytest.mark.asyncio
    async def test_no_budgets_walk_score(self):
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            score, evidence = await _score_budget_adherence("t-1", _SETTINGS)
        assert score == 40.0
        assert evidence["total_budgets"] == 0

    @pytest.mark.asyncio
    async def test_all_budgets_ok_score_100(self):
        budgets = [{"status": "ok"}, {"status": "ok"}, {"status": "ok"}]
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = budgets
            score, evidence = await _score_budget_adherence("t-1", _SETTINGS)
        assert score == 100.0
        assert evidence["breached"] == 0

    @pytest.mark.asyncio
    async def test_one_budget_breached(self):
        budgets = [{"status": "exceeded"}, {"status": "ok"}, {"status": "ok"}]
        with patch("app.services.maturity.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = budgets
            score, evidence = await _score_budget_adherence("t-1", _SETTINGS)
        assert evidence["breached"] == 1
        assert score == pytest.approx(66.7, abs=0.2)


# ══════════════════════════════════════════════════════════════════════════════
# compute_maturity_score (full integration, all scorers mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeMaturityScore:
    def _mock_scorers(self, score_value: float = 80.0):
        """Patch all 6 scorer functions to return a fixed score."""
        from unittest.mock import AsyncMock
        targets = [
            "app.services.maturity._score_tagging",
            "app.services.maturity._score_waste",
            "app.services.maturity._score_commitment_coverage",
            "app.services.maturity._score_unit_economics",
            "app.services.maturity._score_anomaly_response",
            "app.services.maturity._score_budget_adherence",
        ]
        return [
            patch(t, new_callable=AsyncMock, return_value=(score_value, {}))
            for t in targets
        ]

    @pytest.mark.asyncio
    async def test_returns_maturity_score(self):
        patches = self._mock_scorers(75.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        assert isinstance(result, MaturityScore)
        assert result.tenant_id == "t-1"

    @pytest.mark.asyncio
    async def test_all_dimensions_present(self):
        patches = self._mock_scorers(60.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        dim_keys = {d.dimension for d in result.dimensions}
        assert dim_keys == set(_DIM_WEIGHTS.keys())

    @pytest.mark.asyncio
    async def test_overall_score_weighted(self):
        patches = self._mock_scorers(80.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        assert result.overall_score == pytest.approx(80.0 * sum(_DIM_WEIGHTS.values()), abs=0.5)

    @pytest.mark.asyncio
    async def test_high_score_run_or_fly_label(self):
        patches = self._mock_scorers(85.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        assert result.overall_label in ("Run", "Fly")

    @pytest.mark.asyncio
    async def test_low_score_crawl_label(self):
        patches = self._mock_scorers(20.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        assert result.overall_label == "Crawl"

    @pytest.mark.asyncio
    async def test_invalid_vertical_defaults(self):
        patches = self._mock_scorers(50.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1", vertical="banking")
        assert result.vertical in _VALID_VERTICALS

    @pytest.mark.asyncio
    async def test_saas_vertical(self):
        patches = self._mock_scorers(60.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1", vertical="saas")
        assert result.vertical == "saas"

    @pytest.mark.asyncio
    async def test_top_recommendation_not_empty(self):
        patches = self._mock_scorers(50.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        assert len(result.top_recommendation) > 20

    @pytest.mark.asyncio
    async def test_percentile_range(self):
        patches = self._mock_scorers(70.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1")
        for d in result.dimensions:
            assert 0 <= d.percentile <= 100

    @pytest.mark.asyncio
    async def test_benchmark_metadata_present(self):
        patches = self._mock_scorers(70.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await compute_maturity_score("t-1", vertical="saas")
        for d in result.dimensions:
            bench = _BENCHMARKS["saas"][d.dimension]
            assert d.cohort_median == bench[0]
            assert d.cohort_p75 == bench[1]
            assert d.cohort_p90 == bench[2]
