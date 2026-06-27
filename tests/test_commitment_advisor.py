"""
Tests for Smart Commitment Advisor.
Run: pytest tests/test_commitment_advisor.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

os.environ.setdefault("INTERNAL_API_KEY",       "test-key")
os.environ.setdefault("AZURE_TENANT_ID",        "t")
os.environ.setdefault("AZURE_CLIENT_ID",        "c")
os.environ.setdefault("COSMOS_ENDPOINT",        "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME",   "s")
os.environ.setdefault("KEY_VAULT_NAME",         "k")

from app.services.commitment_advisor import (
    _series_stats,
    _choose_commitment,
    _calendar_notes,
    build_advisory,
    generate_advisories,
    CommitmentAdvisory,
    CommitmentAdvisoryReport,
    _MIN_MONTHLY_EUR,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _flat_series(n: int = 60, daily_cost: float = 100.0) -> list[float]:
    """Perfectly flat daily cost series."""
    return [daily_cost] * n


def _growing_series(n: int = 60, start: float = 100.0, pct_growth: float = 0.5) -> list[float]:
    """Series growing at pct_growth % per day."""
    return [start * (1 + pct_growth / 100) ** i for i in range(n)]


def _declining_series(n: int = 60, start: float = 300.0, pct_decline: float = 0.4) -> list[float]:
    return [max(10.0, start * (1 - pct_decline / 100) ** i) for i in range(n)]


def _noisy_series(n: int = 60, base: float = 150.0, noise_std: float = 80.0) -> list[float]:
    np.random.seed(42)
    return [max(0.0, base + np.random.normal(0, noise_std)) for _ in range(n)]


# ══════════════════════════════════════════════════════════════════════════════
# _series_stats
# ══════════════════════════════════════════════════════════════════════════════

class TestSeriesStats:
    def test_flat_series_high_stability(self):
        stability, direction, trend_pct, mape = _series_stats(_flat_series())
        assert stability >= 0.7, f"expected high stability, got {stability}"
        assert direction == "stable"
        assert abs(trend_pct) < 5

    def test_growing_series_direction(self):
        _, direction, trend_pct, _ = _series_stats(_growing_series(pct_growth=1.0))
        assert direction == "growing"
        assert trend_pct > 10

    def test_declining_series_direction(self):
        _, direction, trend_pct, _ = _series_stats(_declining_series(pct_decline=1.0))
        assert direction == "declining"
        assert trend_pct < -10

    def test_noisy_series_low_stability(self):
        stability, _, _, _ = _series_stats(_noisy_series(noise_std=100.0))
        assert stability < 0.7, f"expected low stability for noisy series, got {stability}"

    def test_short_series_returns_defaults(self):
        stability, direction, trend_pct, mape = _series_stats([100.0, 200.0])
        assert stability == 0.3
        assert direction == "unknown"
        assert mape is None

    def test_stability_score_range(self):
        for series in [_flat_series(), _growing_series(), _noisy_series()]:
            stability, _, _, _ = _series_stats(series)
            assert 0.0 <= stability <= 1.0

    def test_mape_returned_for_long_series(self):
        _, _, _, mape = _series_stats(_flat_series(n=60))
        # 60 days ≥ MIN_SEASONAL_POINTS (14) and holdout is feasible
        assert mape is not None


# ══════════════════════════════════════════════════════════════════════════════
# _choose_commitment
# ══════════════════════════════════════════════════════════════════════════════

class TestChooseCommitment:
    def test_azure_1yr(self):
        rec_type, rate = _choose_commitment("azure", 12)
        assert "1yr" in rec_type
        assert 0 < rate < 1

    def test_aws_3yr(self):
        rec_type, rate = _choose_commitment("aws", 36)
        assert "3yr" in rec_type
        assert rate >= 0.50

    def test_unknown_cloud_uses_default(self):
        rec_type, rate = _choose_commitment("alibaba", 12)
        assert rec_type != ""
        assert 0 < rate < 1

    def test_gcp_3yr(self):
        rec_type, rate = _choose_commitment("gcp", 36)
        assert "3yr" in rec_type
        assert rate >= 0.50


# ══════════════════════════════════════════════════════════════════════════════
# _calendar_notes
# ══════════════════════════════════════════════════════════════════════════════

class TestCalendarNotes:
    def test_flat_no_notes(self):
        notes = _calendar_notes(_flat_series())
        assert notes == []

    def test_weekday_pattern_detected(self):
        # Artificially high Monday spend
        series = []
        for i in range(28):
            series.append(500.0 if i % 7 == 0 else 50.0)
        notes = _calendar_notes(series)
        assert len(notes) >= 1
        assert "Mon" in notes[0]

    def test_too_short_no_notes(self):
        notes = _calendar_notes([100.0] * 10)
        assert notes == []


# ══════════════════════════════════════════════════════════════════════════════
# build_advisory
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildAdvisory:
    def _today(self) -> date:
        return date.today()

    def test_stable_series_commit_now(self):
        adv = build_advisory("Virtual Machines", "azure", _flat_series(), [], self._today())
        assert adv is not None
        assert adv.timing == "commit_now"
        assert adv.confidence_label in ("high", "medium")
        assert adv.estimated_monthly_saving_eur > 0

    def test_declining_series_wait(self):
        # Use 1.5%/day decline so the 30-day window shows >10% drop
        adv = build_advisory("VMs", "azure", _declining_series(pct_decline=1.5), [], self._today())
        assert adv is not None
        assert adv.timing == "wait"
        assert adv.wait_months >= 2

    def test_fast_growing_wait(self):
        # >20% monthly growth → wait
        adv = build_advisory("GPU Nodes", "aws", _growing_series(pct_growth=1.5), [], self._today())
        assert adv is not None
        assert adv.timing == "wait"

    def test_planned_event_delays_commit(self):
        today = self._today()
        event_date = today + timedelta(days=30)
        events = [{"date": event_date.isoformat(), "description": "Migration to GCP"}]
        adv = build_advisory("Storage", "azure", _flat_series(), events, today)
        assert adv is not None
        assert adv.timing == "wait"
        assert adv.wait_months >= 1
        assert any("Migration" in n for n in adv.calendar_notes)

    def test_below_threshold_returns_none(self):
        # Daily cost averaging €1 → monthly €30 < _MIN_MONTHLY_EUR
        adv = build_advisory("TinyService", "azure", [1.0] * 60, [], self._today())
        assert adv is None

    def test_empty_series_returns_none(self):
        adv = build_advisory("Service", "azure", [], [], self._today())
        assert adv is None

    def test_confidence_score_range(self):
        adv = build_advisory("VMs", "azure", _flat_series(), [], self._today())
        assert adv is not None
        assert 0.0 <= adv.confidence_score <= 1.0

    def test_stability_score_range(self):
        adv = build_advisory("VMs", "azure", _flat_series(), [], self._today())
        assert adv is not None
        assert 0.0 <= adv.stability_score <= 1.0

    def test_3yr_horizon_for_high_confidence_stable(self):
        # Very flat series → high confidence → should recommend 3yr
        series = [200.0] * 90
        adv = build_advisory("BigCompute", "azure", series, [], self._today())
        assert adv is not None
        if adv.confidence_score >= 0.75:
            assert adv.commitment_horizon_months == 36

    def test_rationale_not_empty(self):
        adv = build_advisory("VMs", "azure", _flat_series(), [], self._today())
        assert adv is not None
        assert len(adv.rationale) > 20

    def test_earliest_commit_date_format(self):
        adv = build_advisory("VMs", "azure", _flat_series(), [], self._today())
        assert adv is not None
        # Should be first-of-month
        d = date.fromisoformat(adv.earliest_commit_date)
        assert d.day == 1


# ══════════════════════════════════════════════════════════════════════════════
# generate_advisories (integration, mocked Cosmos)
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateAdvisories:
    def _focus_records(self) -> list[dict]:
        """90 days of daily Compute records for two services."""
        today = date.today()
        records = []
        for i in range(90):
            day = (today - timedelta(days=i)).isoformat()
            records.append({
                "service_name": "Virtual Machines",
                "provider_name": "azure",
                "charge_period_start": day,
                "effective_cost": 250.0,
                "commitment_discount_type": "None",
                "service_category": "Compute",
            })
            records.append({
                "service_name": "EC2",
                "provider_name": "aws",
                "charge_period_start": day,
                "effective_cost": 180.0,
                "commitment_discount_type": "None",
                "service_category": "Compute",
            })
        return records

    @pytest.mark.asyncio
    async def test_returns_report(self):
        with patch("app.services.commitment_advisor.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._focus_records()
            report = await generate_advisories("t-1")
        assert isinstance(report, CommitmentAdvisoryReport)
        assert report.tenant_id == "t-1"
        assert len(report.advisories) >= 1

    @pytest.mark.asyncio
    async def test_advisories_sorted_by_saving(self):
        with patch("app.services.commitment_advisor.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._focus_records()
            report = await generate_advisories("t-1")
        savings = [a.estimated_monthly_saving_eur for a in report.advisories]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_committed_records_excluded(self):
        """Records with a commitment_discount_type already set are excluded."""
        today = date.today()
        records = [
            {
                "service_name": "Virtual Machines",
                "provider_name": "azure",
                "charge_period_start": (today - timedelta(days=i)).isoformat(),
                "effective_cost": 300.0,
                "commitment_discount_type": "AzureSavingsPlan",
                "service_category": "Compute",
            }
            for i in range(90)
        ]
        with patch("app.services.commitment_advisor.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = records
            report = await generate_advisories("t-1")
        # All records are already committed — no advisories
        assert report.total_on_demand_eligible_eur == 0.0
        assert len(report.advisories) == 0

    @pytest.mark.asyncio
    async def test_empty_cosmos_returns_empty_report(self):
        with patch("app.services.commitment_advisor.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            report = await generate_advisories("t-1")
        assert report.total_estimated_saving_eur == 0.0
        assert "No on-demand" in report.notes[0]

    @pytest.mark.asyncio
    async def test_cosmos_error_returns_empty_report(self):
        from app.exceptions import CosmosError
        with patch("app.services.commitment_advisor.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = CosmosError("test")
            report = await generate_advisories("t-1")
        assert report.total_on_demand_eligible_eur == 0.0
