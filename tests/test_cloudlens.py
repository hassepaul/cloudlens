"""
CloudLens Test Suite
Covers: waste engine rules, Pydantic model validation, router contracts.
Run: pytest tests/ -v --tb=short
"""
from __future__ import annotations
from datetime import date
import pytest

from app.models.tenant import TenantConfig, PlanTier
from app.models.waste import WasteItem, WasteType, Priority
from app.models.report import ReportMeta, ReportStatus
from app.services.waste_engine import (
    rule_idle_vm,
    rule_unattached_disk,
    rule_orphan_public_ip,
    rule_dev_test_eligible,
    rule_reserved_instance,
    run_all_rules,
)
from app.exceptions import CloudLensError, NotFoundError, AzureAPIError


# ── fixtures ─────────────────────────────────────────────────────────────────

TENANT_ID = "00000000-0000-0000-0000-000000000001"
SUB_ID = "00000000-0000-0000-0000-000000000002"

SAMPLE_COST_RECORDS = [
    {
        "resource_id": f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm-idle-01",
        "service_name": "Virtual Machines",
        "cost_eur": 150.0,
        "record_date": "2026-06-01",
        "resource_group": "rg-prod",
        "meter_sub_category": "",
    },
    {
        "resource_id": f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/disks/disk-orphan-01",
        "service_name": "Storage",
        "cost_eur": 45.0,
        "record_date": "2026-06-01",
        "resource_group": "rg-prod",
        "meter_sub_category": "",
    },
    {
        "resource_id": f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Network/publicIPAddresses/pip-orphan-01",
        "service_name": "Virtual Network",
        "cost_eur": 12.0,
        "record_date": "2026-06-01",
        "resource_group": "rg-prod",
        "meter_sub_category": "",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# MODEL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTenantConfig:
    def test_valid_subscription_id_normalised(self):
        config = TenantConfig(
            id=TENANT_ID,
            tenant_name="Acme Corp",
            subscription_ids=["AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"],
            plan_tier=PlanTier.GROWTH,
            alert_email="ops@acme.com",
            sp_secret_ref="sp-creds-acme",
        )
        assert config.subscription_ids[0] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_invalid_subscription_id_raises(self):
        with pytest.raises(Exception):
            TenantConfig(
                id=TENANT_ID,
                tenant_name="Bad Corp",
                subscription_ids=["not-a-uuid"],
                plan_tier=PlanTier.STARTER,
                alert_email="ops@bad.com",
                sp_secret_ref="sp-creds-bad",
            )

    def test_invalid_email_raises(self):
        with pytest.raises(Exception):
            TenantConfig(
                id=TENANT_ID,
                tenant_name="Bad Corp",
                subscription_ids=[SUB_ID],
                plan_tier=PlanTier.STARTER,
                alert_email="not-an-email",
                sp_secret_ref="sp-creds",
            )

    def test_cosmos_roundtrip(self):
        config = TenantConfig(
            id=TENANT_ID,
            tenant_name="Roundtrip Corp",
            subscription_ids=[SUB_ID],
            plan_tier=PlanTier.ENTERPRISE,
            alert_email="ops@roundtrip.com",
            sp_secret_ref="sp-creds-rt",
        )
        cosmos_doc = config.to_cosmos()
        assert cosmos_doc["_partitionKey"] == TENANT_ID
        restored = TenantConfig.from_cosmos(cosmos_doc)
        assert restored.tenant_name == config.tenant_name
        assert restored.plan_tier == config.plan_tier

    def test_plan_tier_enum_values(self):
        assert PlanTier.STARTER.value == "starter"
        assert PlanTier.GROWTH.value == "growth"
        assert PlanTier.ENTERPRISE.value == "enterprise"


class TestWasteItem:
    def test_is_resolved_false_when_no_resolved_at(self):
        item = WasteItem(
            tenant_id=TENANT_ID,
            subscription_id=SUB_ID,
            resource_id="/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-01",
            resource_name="vm-01",
            resource_group="rg",
            waste_type=WasteType.IDLE_VM,
            monthly_cost_eur=100.0,
            saving_eur=80.0,
            priority=Priority.HIGH,
            recommendation="Resize the VM",
            recommendation_it="Ridimensiona la VM",
        )
        assert item.is_resolved is False
        assert item.saving_pct == 0.0  # set by waste engine, not bare constructor

    def test_cosmos_serialization(self):
        item = WasteItem(
            tenant_id=TENANT_ID,
            subscription_id=SUB_ID,
            resource_id="/x/y/z",
            resource_name="z",
            resource_group="rg",
            waste_type=WasteType.UNATTACHED_DISK,
            monthly_cost_eur=50.0,
            saving_eur=50.0,
            priority=Priority.CRITICAL,
            recommendation="Delete disk",
            recommendation_it="Elimina disco",
        )
        doc = item.to_cosmos()
        assert doc["_partitionKey"] == TENANT_ID
        assert doc["waste_type"] == "unattached_disk"


class TestReportMeta:
    def test_status_transitions(self):
        meta = ReportMeta(
            tenant_id=TENANT_ID,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
        )
        assert meta.status == ReportStatus.PENDING
        generating = meta.model_copy(update={"status": ReportStatus.GENERATING})
        assert generating.status == ReportStatus.GENERATING


# ══════════════════════════════════════════════════════════════════════════════
# WASTE ENGINE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestWasteEngineIdleVM:
    @pytest.mark.asyncio
    async def test_detects_idle_vm(self):
        async def mock_metrics(resource_id: str) -> dict:
            return {"cpu_avg_pct": 1.2, "samples": 14}

        items = await rule_idle_vm(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, mock_metrics)
        assert len(items) == 1
        assert items[0].waste_type == WasteType.IDLE_VM
        assert items[0].priority == Priority.CRITICAL
        assert items[0].saving_eur > 0

    @pytest.mark.asyncio
    async def test_skips_active_vm(self):
        async def mock_metrics(resource_id: str) -> dict:
            return {"cpu_avg_pct": 72.5, "samples": 14}

        items = await rule_idle_vm(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, mock_metrics)
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_handles_metrics_fetch_error_gracefully(self):
        async def failing_metrics(resource_id: str) -> dict:
            raise Exception("Metrics API unavailable")

        # Should NOT raise — just log and skip
        items = await rule_idle_vm(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, failing_metrics)
        assert items == []


class TestWasteEngineUnattachedDisk:
    @pytest.mark.asyncio
    async def test_detects_unattached_disk(self):
        disk_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/disks/disk-orphan-01"
        disk_states = {disk_rid: "Unattached"}
        items = await rule_unattached_disk(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, disk_states)
        assert len(items) == 1
        assert items[0].waste_type == WasteType.UNATTACHED_DISK
        assert items[0].priority == Priority.CRITICAL

    @pytest.mark.asyncio
    async def test_skips_attached_disk(self):
        disk_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/disks/disk-orphan-01"
        disk_states = {disk_rid: "Attached"}
        items = await rule_unattached_disk(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, disk_states)
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_empty_disk_states_returns_empty(self):
        items = await rule_unattached_disk(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, {})
        assert items == []


class TestWasteEngineOrphanIP:
    @pytest.mark.asyncio
    async def test_detects_orphan_ip(self):
        ip_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Network/publicIPAddresses/pip-orphan-01"
        items = await rule_orphan_public_ip(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, {ip_rid: False})
        assert len(items) == 1
        assert items[0].waste_type == WasteType.ORPHAN_PUBLIC_IP

    @pytest.mark.asyncio
    async def test_skips_associated_ip(self):
        ip_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Network/publicIPAddresses/pip-orphan-01"
        items = await rule_orphan_public_ip(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, {ip_rid: True})
        assert items == []


class TestWasteEngineDevTest:
    @pytest.mark.asyncio
    async def test_detects_dev_test_eligible(self):
        records = [
            {"resource_id": "/sub/rg/vm1", "cost_eur": 200.0, "meter_sub_category": "Windows Server", "service_name": "Virtual Machines"},
        ]
        items = await rule_dev_test_eligible(TENANT_ID, SUB_ID, records, "MS-AZR-0003P", "staging")
        assert len(items) == 1
        assert items[0].waste_type == WasteType.DEV_TEST_ELIGIBLE

    @pytest.mark.asyncio
    async def test_skips_production_environment(self):
        records = [
            {"resource_id": "/sub/rg/vm1", "cost_eur": 200.0, "meter_sub_category": "Windows Server", "service_name": "Virtual Machines"},
        ]
        items = await rule_dev_test_eligible(TENANT_ID, SUB_ID, records, "MS-AZR-0003P", "production")
        assert items == []


class TestWasteEngineReservedInstance:
    @pytest.mark.asyncio
    async def test_detects_ri_candidate(self):
        vm_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm-idle-01"
        items = await rule_reserved_instance(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, {vm_rid: 45})
        assert len(items) == 1
        assert items[0].waste_type == WasteType.RESERVED_INSTANCE
        assert items[0].priority == Priority.MEDIUM

    @pytest.mark.asyncio
    async def test_skips_short_running_vm(self):
        vm_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/vm-idle-01"
        items = await rule_reserved_instance(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, {vm_rid: 10})
        assert items == []


class TestRunAllRules:
    @pytest.mark.asyncio
    async def test_run_all_returns_sorted_by_saving(self):
        async def mock_metrics(rid: str) -> dict:
            return {"cpu_avg_pct": 2.0, "samples": 14}

        disk_rid = f"/subscriptions/{SUB_ID}/resourceGroups/rg-prod/providers/Microsoft.Compute/disks/disk-orphan-01"

        context = {
            "metrics_fetcher": mock_metrics,
            "disk_states": {disk_rid: "Unattached"},
            "ip_associations": {},
            "advisor_recommendations": [],
            "subscription_offer_type": "",
            "env_tag_value": "",
            "vm_uptime_days": {},
            "app_service_metrics": {},
            "lb_backend_counts": {},
            "snapshot_ages": {},
        }
        items = await run_all_rules(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, context)
        assert len(items) >= 2
        # Sorted by saving descending
        savings = [i.saving_eur for i in items]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_run_all_tolerates_rule_failures(self):
        """If one rule fails, others still complete."""
        async def crashing_metrics(rid: str) -> dict:
            raise RuntimeError("Boom")

        context = {
            "metrics_fetcher": crashing_metrics,
            "disk_states": {},
            "ip_associations": {},
            "advisor_recommendations": [],
            "subscription_offer_type": "",
            "env_tag_value": "",
            "vm_uptime_days": {},
            "app_service_metrics": {},
            "lb_backend_counts": {},
            "snapshot_ages": {},
        }
        # Should not raise
        items = await run_all_rules(TENANT_ID, SUB_ID, SAMPLE_COST_RECORDS, context)
        assert isinstance(items, list)


# ══════════════════════════════════════════════════════════════════════════════
# EXCEPTION HIERARCHY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_not_found_inherits_cloudlens(self):
        exc = NotFoundError("Item not found")
        assert isinstance(exc, CloudLensError)
        assert exc.status_code == 404
        assert exc.error_code == "NOT_FOUND"

    def test_azure_api_error(self):
        exc = AzureAPIError("Rate limited", detail="429 from Cost Management")
        assert exc.status_code == 502
        d = exc.to_dict()
        assert d["error"] == "AZURE_API_ERROR"
        assert d["detail"] == "429 from Cost Management"

    def test_to_dict_format(self):
        exc = NotFoundError("Tenant XYZ not found")
        d = exc.to_dict()
        assert "error" in d
        assert "message" in d
        assert d["message"] == "Tenant XYZ not found"
