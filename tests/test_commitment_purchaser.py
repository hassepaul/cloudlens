"""
Tests for automated AWS commitment purchasing.

Covers:
  - PurchaseSettings default/serialisation
  - Global and per-tenant safety gates
  - Per-advisory filtering (cloud, type, timing, confidence, budget caps)
  - SP and RI purchase paths (boto3 fully mocked)
  - Dry-run mode produces no real API calls
  - Monthly budget accumulation guard
  - Skipped/failed record generation
  - Router endpoints: GET/PUT settings, POST execute, GET history
Run: pytest tests/test_commitment_purchaser.py -v
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.commitment_purchaser import (
    PurchaseSettings,
    PurchaseRecord,
    CommitmentAutoDisabledError,
    get_purchase_settings,
    save_purchase_settings,
    run_purchase,
    get_purchase_history,
    _infer_instance_type,
    _skip,
    PurchaseRun,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _advisory(
    service="Amazon EC2",
    cloud="aws",
    recommended_type="savings-plan-1yr",
    timing="commit_now",
    confidence_score=0.80,
    on_demand_monthly_eur=3000.0,
    saving_pct=0.27,
    commitment_horizon_months=12,
    wait_months=0,
) -> dict:
    return {
        "service": service,
        "cloud": cloud,
        "recommended_type": recommended_type,
        "timing": timing,
        "confidence_score": confidence_score,
        "on_demand_monthly_eur": on_demand_monthly_eur,
        "saving_pct": saving_pct,
        "commitment_horizon_months": commitment_horizon_months,
        "wait_months": wait_months,
    }


_AWS_CREDS_JSON = json.dumps({
    "role_arn": "arn:aws:iam::123456789012:role/CloudLensTest",
    "external_id": "test-external",
    "region": "us-east-1",
})


# ══════════════════════════════════════════════════════════════════════════════
# PurchaseSettings
# ══════════════════════════════════════════════════════════════════════════════

class TestPurchaseSettings:
    def test_defaults_are_disabled(self):
        s = PurchaseSettings(tenant_id="t1")
        assert s.enabled is False
        assert s.dry_run is True

    def test_roundtrip_cosmos(self):
        s = PurchaseSettings(
            tenant_id="t1",
            enabled=True,
            dry_run=False,
            max_single_purchase_eur=2000.0,
            max_monthly_budget_eur=10000.0,
            min_confidence_score=0.75,
            allowed_commitment_types=["savings-plan-1yr"],
            allowed_services=["Amazon EC2"],
        )
        doc = s.to_cosmos()
        s2 = PurchaseSettings.from_cosmos(doc)
        assert s2.enabled is True
        assert s2.dry_run is False
        assert s2.max_single_purchase_eur == 2000.0
        assert s2.allowed_commitment_types == ["savings-plan-1yr"]

    def test_from_cosmos_missing_fields_use_defaults(self):
        s = PurchaseSettings.from_cosmos({"tenant_id": "t1"})
        assert s.enabled is False
        assert s.dry_run is True
        assert s.min_confidence_score == 0.70


# ══════════════════════════════════════════════════════════════════════════════
# Settings CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestSettingsCRUD:
    @pytest.mark.asyncio
    async def test_get_settings_no_record_returns_defaults(self):
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
            s = await get_purchase_settings("t-new")
        assert s.tenant_id == "t-new"
        assert s.enabled is False

    @pytest.mark.asyncio
    async def test_get_settings_loads_from_cosmos(self):
        doc = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=False).to_cosmos()
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[doc])):
            s = await get_purchase_settings("t1")
        assert s.enabled is True
        assert s.dry_run is False

    @pytest.mark.asyncio
    async def test_save_settings_upserts_cosmos(self):
        mock_upsert = AsyncMock()
        s = PurchaseSettings(tenant_id="t1", enabled=True)
        with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=mock_upsert):
            with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
                saved = await save_purchase_settings(s)
        mock_upsert.assert_awaited_once()
        assert saved.updated_at  # timestamp was set


# ══════════════════════════════════════════════════════════════════════════════
# Safety gates
# ══════════════════════════════════════════════════════════════════════════════

class TestSafetyGates:
    @pytest.mark.asyncio
    async def test_global_gate_closed_raises(self):
        """When COMMITMENT_AUTO_PURCHASE_ENABLED is false, run_purchase raises."""
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = False
            with pytest.raises(CommitmentAutoDisabledError, match="disabled globally"):
                await run_purchase("t1", [_advisory()])

    @pytest.mark.asyncio
    async def test_tenant_gate_closed_raises(self):
        """When tenant enabled=False, run_purchase raises even if global gate is open."""
        disabled_settings = PurchaseSettings(tenant_id="t1", enabled=False)
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch(
                "app.services.commitment_purchaser.get_purchase_settings",
                new=AsyncMock(return_value=disabled_settings),
            ):
                with pytest.raises(CommitmentAutoDisabledError, match="disabled for tenant"):
                    await run_purchase("t1", [_advisory()])

    @pytest.mark.asyncio
    async def test_both_gates_open_proceeds(self):
        """With both gates open + dry_run, run_purchase completes without error."""
        enabled_settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch(
                "app.services.commitment_purchaser.get_purchase_settings",
                new=AsyncMock(return_value=enabled_settings),
            ):
                with patch(
                    "app.services.commitment_purchaser._keyvault.get_secret",
                    new=AsyncMock(return_value=_AWS_CREDS_JSON),
                ):
                    with patch("app.services.commitment_purchaser._month_spend_eur", new=AsyncMock(return_value=0.0)):
                        with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=AsyncMock()):
                            with patch("app.services.commitment_purchaser._assume_role", return_value={"AccessKeyId": "A", "SecretAccessKey": "B", "SessionToken": "C"}):
                                with patch("app.services.commitment_purchaser._sp_client") as mock_sp_cls:
                                    mock_sp = MagicMock()
                                    mock_sp.describe_savings_plans_offerings.return_value = {
                                        "searchResults": [{"offeringId": "test-offering-id"}]
                                    }
                                    mock_sp_cls.return_value = mock_sp
                                    run = await run_purchase("t1", [_advisory()])
        # dry_run=True → should be in "purchased" with status="dry_run"
        assert len(run.purchased) == 1
        assert run.purchased[0].status == "dry_run"
        assert run.purchased[0].dry_run is True


# ══════════════════════════════════════════════════════════════════════════════
# Advisory filtering
# ══════════════════════════════════════════════════════════════════════════════

class TestAdvisoryFiltering:
    def _run(self, advisories, **settings_kwargs) -> PurchaseRun:
        """Run filtering logic synchronously using the _skip helper."""
        s = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True, **settings_kwargs)
        run = PurchaseRun(tenant_id="t1", run_at="2026-01-01T00:00:00", dry_run=True)
        allowed_types = set(s.allowed_commitment_types) or {"savings-plan-1yr", "savings-plan-3yr", "1yr-ri", "3yr-ri"}
        allowed_services = set(s.allowed_services)
        for adv in advisories:
            if adv.get("cloud", "").lower() != "aws":
                _skip(run, adv, "non-aws", "non-aws")
                continue
            if adv.get("recommended_type") not in allowed_types:
                _skip(run, adv, "type", "type not allowed")
                continue
            if allowed_services and adv.get("service") not in allowed_services:
                _skip(run, adv, "service", "service not allowed")
                continue
            if adv.get("timing") != "commit_now":
                _skip(run, adv, "timing", "wait")
                continue
            if float(adv.get("confidence_score", 0)) < s.min_confidence_score:
                _skip(run, adv, "conf", "low confidence")
                continue
        return run

    def test_non_aws_skipped(self):
        run = self._run([_advisory(cloud="azure")])
        assert len(run.skipped) == 1
        assert "non-aws" in run.skipped[0].skip_reason

    def test_wrong_timing_skipped(self):
        run = self._run([_advisory(timing="wait")])
        assert len(run.skipped) == 1
        assert "timing" in run.skipped[0].skip_reason

    def test_low_confidence_skipped(self):
        run = self._run([_advisory(confidence_score=0.50)], min_confidence_score=0.70)
        assert len(run.skipped) == 1
        assert "conf" in run.skipped[0].skip_reason

    def test_allowed_types_filter(self):
        run = self._run([_advisory(recommended_type="3yr-ri")],
                        allowed_commitment_types=["savings-plan-1yr"])
        assert len(run.skipped) == 1
        assert "type" in run.skipped[0].skip_reason

    def test_allowed_services_filter(self):
        run = self._run([_advisory(service="Amazon RDS")],
                        allowed_services=["Amazon EC2"])
        assert len(run.skipped) == 1
        assert "service" in run.skipped[0].skip_reason

    def test_all_allowed_no_skips(self):
        run = self._run([_advisory(cloud="aws", timing="commit_now",
                                   confidence_score=0.80, recommended_type="savings-plan-1yr")])
        assert len(run.skipped) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Budget caps
# ══════════════════════════════════════════════════════════════════════════════

class TestBudgetCaps:
    @pytest.mark.asyncio
    async def test_single_purchase_cap_skips_large_advisory(self):
        """Advisory whose estimated saving > max_single_purchase_eur is skipped."""
        settings = PurchaseSettings(
            tenant_id="t1", enabled=True, dry_run=True,
            max_single_purchase_eur=100.0,   # tiny cap
        )
        adv = _advisory(on_demand_monthly_eur=10_000.0, saving_pct=0.27)  # ~2700 EUR saving
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch("app.services.commitment_purchaser.get_purchase_settings", new=AsyncMock(return_value=settings)):
                with patch("app.services.commitment_purchaser._keyvault.get_secret", new=AsyncMock(return_value=_AWS_CREDS_JSON)):
                    with patch("app.services.commitment_purchaser._month_spend_eur", new=AsyncMock(return_value=0.0)):
                        with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=AsyncMock()):
                            run = await run_purchase("t1", [adv])
        assert len(run.skipped) == 1
        assert "single_cap" in run.skipped[0].skip_reason

    @pytest.mark.asyncio
    async def test_monthly_budget_exhausted_skips_advisory(self):
        """Advisory skipped when this month's spend already reached the cap."""
        settings = PurchaseSettings(
            tenant_id="t1", enabled=True, dry_run=True,
            max_monthly_budget_eur=1000.0,
        )
        adv = _advisory(on_demand_monthly_eur=3000.0, saving_pct=0.27)  # ~810 EUR saving
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch("app.services.commitment_purchaser.get_purchase_settings", new=AsyncMock(return_value=settings)):
                with patch("app.services.commitment_purchaser._keyvault.get_secret", new=AsyncMock(return_value=_AWS_CREDS_JSON)):
                    # Already spent 950 EUR this month
                    with patch("app.services.commitment_purchaser._month_spend_eur", new=AsyncMock(return_value=950.0)):
                        with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=AsyncMock()):
                            run = await run_purchase("t1", [adv])
        assert len(run.skipped) == 1
        assert "monthly_budget" in run.skipped[0].skip_reason


# ══════════════════════════════════════════════════════════════════════════════
# Dry-run and live purchase paths
# ══════════════════════════════════════════════════════════════════════════════

def _mock_globals(settings, sp_mock=None, ri_mock=None):
    """Context manager stack for a full run_purchase call with mocked AWS."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _ctx():
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch("app.services.commitment_purchaser.get_purchase_settings", new=AsyncMock(return_value=settings)):
                with patch("app.services.commitment_purchaser._keyvault.get_secret", new=AsyncMock(return_value=_AWS_CREDS_JSON)):
                    with patch("app.services.commitment_purchaser._month_spend_eur", new=AsyncMock(return_value=0.0)):
                        with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=AsyncMock()):
                            with patch("app.services.commitment_purchaser._assume_role", return_value={"AccessKeyId": "A", "SecretAccessKey": "B", "SessionToken": "C"}):
                                if sp_mock is not None:
                                    with patch("app.services.commitment_purchaser._sp_client", return_value=sp_mock):
                                        if ri_mock is not None:
                                            with patch("app.services.commitment_purchaser._ec2_client", return_value=ri_mock):
                                                yield
                                        else:
                                            yield
                                elif ri_mock is not None:
                                    with patch("app.services.commitment_purchaser._ec2_client", return_value=ri_mock):
                                        yield
                                else:
                                    yield

    return _ctx()


class TestDryRunMode:
    @pytest.mark.asyncio
    async def test_dry_run_returns_dry_run_status(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {
            "searchResults": [{"offeringId": "o-1"}]
        }
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        async with _mock_globals(settings, sp_mock=sp_mock):
            run = await run_purchase("t1", [_advisory()])
        assert run.dry_run is True
        assert len(run.purchased) == 1
        assert run.purchased[0].status == "dry_run"
        # Verify boto3 purchase was NOT called
        sp_mock.create_savings_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_no_sp_create_called(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {
            "searchResults": [{"offeringId": "o-1"}]
        }
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        async with _mock_globals(settings, sp_mock=sp_mock):
            await run_purchase("t1", [_advisory(recommended_type="savings-plan-1yr")])
        sp_mock.create_savings_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_mode_calls_sp_create(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {
            "searchResults": [{"offeringId": "o-123"}]
        }
        sp_mock.create_savings_plan.return_value = {"savingsPlanId": "sp-arn-abc"}
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=False)
        async with _mock_globals(settings, sp_mock=sp_mock):
            run = await run_purchase("t1", [_advisory(recommended_type="savings-plan-1yr")])
        sp_mock.create_savings_plan.assert_called_once()
        assert run.purchased[0].status == "purchased"
        assert run.purchased[0].aws_commitment_id == "sp-arn-abc"

    @pytest.mark.asyncio
    async def test_live_mode_calls_ri_purchase(self):
        ec2_mock = MagicMock()
        ec2_mock.describe_reserved_instances_offerings.return_value = {
            "ReservedInstancesOfferings": [{"ReservedInstancesOfferingId": "ri-offering-xyz"}]
        }
        ec2_mock.purchase_reserved_instances_offering.return_value = {
            "ReservedInstancesId": "ri-id-123"
        }
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=False)
        async with _mock_globals(settings, ri_mock=ec2_mock):
            run = await run_purchase("t1", [_advisory(recommended_type="1yr-ri")])
        ec2_mock.purchase_reserved_instances_offering.assert_called_once()
        assert run.purchased[0].status == "purchased"
        assert run.purchased[0].aws_commitment_id == "ri-id-123"

    @pytest.mark.asyncio
    async def test_sp_offering_not_found_fails_gracefully(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {"searchResults": []}
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=False)
        async with _mock_globals(settings, sp_mock=sp_mock):
            run = await run_purchase("t1", [_advisory(recommended_type="savings-plan-1yr")])
        assert len(run.failed) == 1
        assert "No Savings Plan offering" in run.failed[0].error

    @pytest.mark.asyncio
    async def test_unknown_commitment_type_skipped(self):
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        async with _mock_globals(settings):
            run = await run_purchase("t1", [_advisory(recommended_type="unknown-type")])
        # filtered at allowed_types check (default set excludes unknown-type)
        assert len(run.skipped) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Multiple advisories in one run
# ══════════════════════════════════════════════════════════════════════════════

class TestMultipleAdvisories:
    @pytest.mark.asyncio
    async def test_mixed_advisories_partitioned_correctly(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {
            "searchResults": [{"offeringId": "o-1"}]
        }
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        advisories = [
            _advisory(service="Amazon EC2", timing="commit_now", confidence_score=0.85),
            _advisory(service="Amazon RDS", timing="wait", confidence_score=0.90),   # skipped
            _advisory(service="Lambda", cloud="gcp", timing="commit_now"),            # skipped
        ]
        async with _mock_globals(settings, sp_mock=sp_mock):
            run = await run_purchase("t1", advisories)
        assert len(run.purchased) == 1
        assert len(run.skipped) == 2

    @pytest.mark.asyncio
    async def test_run_notes_summary_present(self):
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        async with _mock_globals(settings):
            run = await run_purchase("t1", [])
        assert any("Processed" in n for n in run.notes)

    @pytest.mark.asyncio
    async def test_total_committed_eur_sums_purchases(self):
        sp_mock = MagicMock()
        sp_mock.describe_savings_plans_offerings.return_value = {
            "searchResults": [{"offeringId": "o-1"}]
        }
        settings = PurchaseSettings(tenant_id="t1", enabled=True, dry_run=True)
        adv1 = _advisory(on_demand_monthly_eur=2000.0, saving_pct=0.27)  # 540 EUR
        adv2 = _advisory(service="Amazon RDS", on_demand_monthly_eur=1000.0, saving_pct=0.27)  # 270 EUR
        async with _mock_globals(settings, sp_mock=sp_mock):
            run = await run_purchase("t1", [adv1, adv2])
        assert run.total_committed_eur == pytest.approx(540 + 270, rel=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_infer_instance_type_ec2_default(self):
        assert _infer_instance_type("Amazon EC2") == "m5.large"

    def test_infer_instance_type_rds(self):
        assert _infer_instance_type("Amazon RDS") == "db.m5.large"

    def test_infer_instance_type_elasticache(self):
        assert _infer_instance_type("Amazon ElastiCache") == "cache.r6g.large"

    def test_skip_appends_to_skipped(self):
        run = PurchaseRun(tenant_id="t1", run_at="2026-01-01", dry_run=True)
        _skip(run, _advisory(), "test_code", "test reason")
        assert len(run.skipped) == 1
        assert "test_code" in run.skipped[0].skip_reason


# ══════════════════════════════════════════════════════════════════════════════
# Purchase history
# ══════════════════════════════════════════════════════════════════════════════

class TestPurchaseHistory:
    @pytest.mark.asyncio
    async def test_get_history_returns_cosmos_rows(self):
        rows = [{"id": "1", "service": "EC2", "status": "dry_run"}]
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=rows)):
            result = await get_purchase_history("t1", limit=10)
        assert result == rows

    @pytest.mark.asyncio
    async def test_get_history_cosmos_error_returns_empty(self):
        from app.exceptions import CosmosError
        with patch("app.services.commitment_purchaser.cosmos.query_items",
                   new=AsyncMock(side_effect=CosmosError("boom"))):
            result = await get_purchase_history("t1")
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# Router endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def _headers(self):
        from app.config import get_settings
        return {"X-API-Key": get_settings().internal_api_key}

    def test_get_settings_returns_defaults(self):
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().get("/api/v1/commitment-purchaser/t1/settings", headers=self._headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["dry_run"] is True
        assert "global_auto_purchase_enabled" in body
        assert "effective_enabled" in body

    def test_put_settings_updates(self):
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
            with patch("app.services.commitment_purchaser.cosmos.upsert_item", new=AsyncMock()):
                resp = self._client().put(
                    "/api/v1/commitment-purchaser/t1/settings",
                    json={"enabled": True, "dry_run": True, "max_single_purchase_eur": 3000.0,
                          "max_monthly_budget_eur": 15000.0, "min_confidence_score": 0.75},
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["max_single_purchase_eur"] == 3000.0

    def test_put_settings_invalid_type_rejects(self):
        resp = self._client().put(
            "/api/v1/commitment-purchaser/t1/settings",
            json={"allowed_commitment_types": ["banana-plan"]},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_execute_global_disabled_returns_403(self):
        """Global gate closed → 403."""
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().post(
                "/api/v1/commitment-purchaser/t1/execute",
                json={},
                headers=self._headers(),
            )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "DISABLED"

    def test_execute_tenant_disabled_returns_403(self):
        """Tenant gate closed → 403 even if global is enabled."""
        disabled_settings = PurchaseSettings(tenant_id="t1", enabled=False)
        with patch("app.services.commitment_purchaser.get_settings") as mock_cfg:
            mock_cfg.return_value.commitment_auto_purchase_enabled = True
            with patch("app.services.commitment_purchaser.get_purchase_settings", new=AsyncMock(return_value=disabled_settings)):
                from app.services.commitment_advisor import CommitmentAdvisoryReport
                empty_report = CommitmentAdvisoryReport(
                    tenant_id="t1", period_start="", period_end="",
                    total_on_demand_eligible_eur=0, total_estimated_saving_eur=0,
                )
                with patch("app.routers.commitment_purchaser.generate_advisories", new=AsyncMock(return_value=empty_report)):
                    resp = self._client().post(
                        "/api/v1/commitment-purchaser/t1/execute",
                        json={},
                        headers=self._headers(),
                    )
        assert resp.status_code == 403

    def test_get_history_returns_records(self):
        rows = [{"id": "abc", "status": "dry_run", "service": "EC2"}]
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=rows)):
            resp = self._client().get(
                "/api/v1/commitment-purchaser/t1/history",
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["records"] == rows

    def test_get_history_status_filter_valid(self):
        with patch("app.services.commitment_purchaser.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().get(
                "/api/v1/commitment-purchaser/t1/history?status=purchased",
                headers=self._headers(),
            )
        assert resp.status_code == 200

    def test_get_history_status_filter_invalid_rejects(self):
        resp = self._client().get(
            "/api/v1/commitment-purchaser/t1/history?status=banana",
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_requires_api_key(self):
        resp = self._client().get("/api/v1/commitment-purchaser/t1/settings")
        assert resp.status_code == 401
