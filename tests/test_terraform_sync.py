"""
Tests for Terraform Drift Management
======================================
Covers: tag generation, HCL templates, import cmd generation,
        resource-type inference, drift recording, query API,
        acknowledgement workflow, webhook firing, and all HTTP endpoints.
"""
from __future__ import annotations

import os

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststorage")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.terraform_sync import (
    DRIFT_STATUS_ACKNOWLEDGED,
    DRIFT_STATUS_IMPORTED,
    DRIFT_STATUS_PENDING,
    TerraformDriftRecord,
    _infer_resource_type,
    acknowledge_drift,
    build_autonomous_tags,
    dismiss_drift,
    generate_hcl,
    generate_import_cmd,
    get_drift_record,
    get_drift_summary,
    list_drift,
    record_drift,
    _build_webhook_payload,
)

TENANT = "tenant-a"


@pytest.fixture()
def client():
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def _api_headers() -> dict:
    from app.config import get_settings
    return {"X-API-Key": get_settings().internal_api_key}


def _sample_record(**overrides) -> TerraformDriftRecord:
    defaults = dict(
        id="dr-1",
        tenant_id=TENANT,
        action_id="act-abc12345",
        approval_id="act-abc12345",
        approved_by="session-xyz",
        tool_name="create_budget",
        resource_type="azurerm_consumption_budget_subscription",
        resource_name="cloudlens_auto_act-abc1",
        resource_id="my-budget",
        provider="azure",
        region="",
        hcl_snippet="resource ... {}",
        import_cmd="terraform import ...",
        tags={"cloudlens:source": "autonomous"},
        status=DRIFT_STATUS_PENDING,
        created_at="2026-06-27T00:00:00Z",
        acknowledged_at="",
        acknowledged_by="",
        notification_sent=False,
    )
    defaults.update(overrides)
    return TerraformDriftRecord(**defaults)


# ── TestTagGeneration ─────────────────────────────────────────────────────────

class TestTagGeneration:
    def test_required_tags_present(self):
        tags = build_autonomous_tags(
            action_id="act-1", approval_id="appr-1",
            tenant_id=TENANT, resource_type="aws_budgets_budget",
        )
        assert tags["cloudlens:source"] == "autonomous"
        assert tags["cloudlens:action_id"] == "act-1"
        assert tags["cloudlens:tenant_id"] == TENANT
        assert tags["cloudlens:resource_type"] == "aws_budgets_budget"

    def test_approved_by_included(self):
        tags = build_autonomous_tags(
            action_id="a", approval_id="b", tenant_id=TENANT,
            resource_type="x", approved_by="user-eng",
        )
        assert tags["cloudlens:approved_by"] == "user-eng"

    def test_created_at_iso_format(self):
        tags = build_autonomous_tags("a", "b", TENANT, "x")
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}T", tags["cloudlens:created_at"])

    def test_tag_count(self):
        # Exactly 7 standard tags
        tags = build_autonomous_tags("a", "b", TENANT, "x")
        assert len(tags) == 7


# ── TestHclGeneration ─────────────────────────────────────────────────────────

class TestHclGeneration:
    def test_aws_budget_hcl_contains_resource_type(self):
        hcl = generate_hcl("aws_budgets_budget", "my_budget", {"name": "test", "monthly_limit_usd": 100}, {})
        assert 'resource "aws_budgets_budget" "my_budget"' in hcl
        assert '"test"' in hcl
        assert "MONTHLY" in hcl

    def test_azure_budget_hcl(self):
        hcl = generate_hcl(
            "azurerm_consumption_budget_subscription", "az_budget",
            {"name": "azure-test", "monthly_limit_eur": 200}, {}
        )
        assert 'resource "azurerm_consumption_budget_subscription" "az_budget"' in hcl
        assert "200" in hcl

    def test_azure_monitor_alert_hcl(self):
        hcl = generate_hcl(
            "azurerm_monitor_metric_alert", "my_alert",
            {"name": "alert-test", "threshold_eur": 500}, {}
        )
        assert 'resource "azurerm_monitor_metric_alert" "my_alert"' in hcl
        assert "500" in hcl

    def test_aws_cloudwatch_alarm_hcl(self):
        hcl = generate_hcl(
            "aws_cloudwatch_metric_alarm", "cw_alarm",
            {"name": "cw-test", "threshold": 300}, {}
        )
        assert 'resource "aws_cloudwatch_metric_alarm" "cw_alarm"' in hcl

    def test_cloudlens_budget_fallback_hcl(self):
        hcl = generate_hcl("cloudlens_budget", "cl_budget", {"name": "cl-test", "monthly_limit_eur": 50}, {})
        assert "CloudLens internal budget" in hcl
        assert "cl-test" in hcl

    def test_generic_hcl_for_unknown_type(self):
        hcl = generate_hcl("custom_resource_type", "my_res", {"name": "custom"}, {})
        assert 'resource "custom_resource_type" "my_res"' in hcl

    def test_tags_rendered_in_hcl(self):
        tags = {"cloudlens:source": "autonomous", "cloudlens:action_id": "abc123"}
        hcl = generate_hcl("aws_budgets_budget", "b", {"name": "t"}, tags)
        assert "cloudlens:source" in hcl
        assert "autonomous" in hcl


# ── TestImportCmdGeneration ───────────────────────────────────────────────────

class TestImportCmdGeneration:
    def test_aws_budget_import_cmd(self):
        cmd = generate_import_cmd("aws_budgets_budget", "my_budget", "test-budget")
        assert "terraform import" in cmd
        assert "aws_budgets_budget.my_budget" in cmd
        assert "test-budget" in cmd

    def test_azure_budget_subscription_import_cmd(self):
        cmd = generate_import_cmd(
            "azurerm_consumption_budget_subscription", "az_budget", "my-budget"
        )
        assert "azurerm_consumption_budget_subscription.az_budget" in cmd
        assert "Microsoft.Consumption/budgets/my-budget" in cmd

    def test_azure_budget_rg_import_cmd(self):
        cmd = generate_import_cmd(
            "azurerm_consumption_budget_resource_group", "rg_budget", "my-budget"
        )
        assert "resourceGroups" in cmd

    def test_azure_monitor_alert_import_cmd(self):
        cmd = generate_import_cmd("azurerm_monitor_metric_alert", "my_alert", "alert-name")
        assert "metricAlerts/alert-name" in cmd

    def test_cloudwatch_import_cmd(self):
        cmd = generate_import_cmd("aws_cloudwatch_metric_alarm", "cw", "alarm-name")
        assert "aws_cloudwatch_metric_alarm.cw" in cmd
        assert "alarm-name" in cmd

    def test_generic_import_cmd(self):
        cmd = generate_import_cmd("some_resource", "my_name", "resource-id-123")
        assert "terraform import" in cmd
        assert "some_resource.my_name" in cmd
        assert "resource-id-123" in cmd

    def test_module_prefix_included(self):
        cmd = generate_import_cmd("aws_budgets_budget", "b", "id", prefix="module.budgeting")
        assert "module.budgeting.aws_budgets_budget.b" in cmd


# ── TestResourceTypeInference ─────────────────────────────────────────────────

class TestResourceTypeInference:
    def test_create_budget_aws(self):
        rt, prov = _infer_resource_type("create_budget", "aws")
        assert rt == "aws_budgets_budget"
        assert prov == "aws"

    def test_create_budget_azure(self):
        rt, prov = _infer_resource_type("create_budget", "azure")
        assert rt == "azurerm_consumption_budget_subscription"
        assert prov == "azure"

    def test_create_budget_no_hint_defaults_internal(self):
        rt, prov = _infer_resource_type("create_budget", "")
        assert rt == "cloudlens_budget"
        assert prov == "internal"

    def test_create_alert_rule_azure(self):
        rt, prov = _infer_resource_type("create_alert_rule", "azure")
        assert rt == "azurerm_monitor_metric_alert"
        assert prov == "azure"

    def test_unknown_tool_returns_internal(self):
        rt, prov = _infer_resource_type("buy_reserved_instance", "aws")
        assert prov == "internal"


# ── TestDriftRecording ────────────────────────────────────────────────────────

class TestDriftRecording:
    @pytest.mark.asyncio
    async def test_record_drift_creates_cosmos_record(self):
        with patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert:
            record = await record_drift(
                tenant_id=TENANT,
                action_id="act-111",
                approval_id="act-111",
                tool_name="create_budget",
                tool_params={"name": "Test Budget", "monthly_limit_eur": 100.0},
                tool_result={"budget_id": "bdg-999", "created": True},
                approved_by="user-eng",
            )
        assert mock_upsert.called
        assert isinstance(record, TerraformDriftRecord)
        assert record.tenant_id == TENANT
        assert record.action_id == "act-111"
        assert record.status == DRIFT_STATUS_PENDING
        assert record.resource_id == "bdg-999"

    @pytest.mark.asyncio
    async def test_record_drift_generates_hcl(self):
        with patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock):
            record = await record_drift(
                tenant_id=TENANT,
                action_id="act-222",
                approval_id="act-222",
                tool_name="create_budget",
                tool_params={"name": "My Budget", "monthly_limit_eur": 50},
                tool_result={"budget_id": "bdg-001"},
            )
        assert len(record.hcl_snippet) > 10
        assert "terraform import" in record.import_cmd

    @pytest.mark.asyncio
    async def test_record_drift_cosmos_failure_does_not_raise(self):
        """Drift recording must be best-effort — CosmosError should not propagate."""
        from app.exceptions import CosmosError
        with patch("app.services.terraform_sync.cosmos.upsert_item",
                   new_callable=AsyncMock, side_effect=CosmosError("fail")):
            # Should not raise
            record = await record_drift(
                tenant_id=TENANT,
                action_id="act-err",
                approval_id="act-err",
                tool_name="create_budget",
                tool_params={"name": "Err Budget"},
                tool_result={"budget_id": "bdg-err"},
            )
        # Returns a record even if persistence failed
        assert isinstance(record, TerraformDriftRecord)

    @pytest.mark.asyncio
    async def test_record_drift_with_provider_hint(self):
        with patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock):
            record = await record_drift(
                tenant_id=TENANT,
                action_id="act-aws",
                approval_id="act-aws",
                tool_name="create_budget",
                tool_params={"name": "AWS Budget"},
                tool_result={"budget_id": "bdg-aws"},
                provider_hint="aws",
            )
        assert record.provider == "aws"
        assert record.resource_type == "aws_budgets_budget"

    @pytest.mark.asyncio
    async def test_record_drift_has_autonomous_tags(self):
        with patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock):
            record = await record_drift(
                tenant_id=TENANT,
                action_id="act-tags",
                approval_id="act-tags",
                tool_name="create_alert_rule",
                tool_params={"name": "Alert", "threshold_eur": 100, "channels": []},
                tool_result={"rule_id": "rule-001"},
                approved_by="eng-alice",
            )
        assert record.tags.get("cloudlens:source") == "autonomous"
        assert record.tags.get("cloudlens:action_id") == "act-tags"
        assert record.tags.get("cloudlens:approved_by") == "eng-alice"


# ── TestDriftQuery ────────────────────────────────────────────────────────────

class TestDriftQuery:
    @pytest.mark.asyncio
    async def test_list_drift_returns_records(self):
        doc = _sample_record().to_cosmos()
        with patch("app.services.terraform_sync.cosmos.query_items", new_callable=AsyncMock, return_value=[doc]):
            records = await list_drift(TENANT)
        assert len(records) == 1
        assert records[0].id == "dr-1"

    @pytest.mark.asyncio
    async def test_list_drift_empty_on_cosmos_error(self):
        from app.exceptions import CosmosError
        with patch("app.services.terraform_sync.cosmos.query_items",
                   new_callable=AsyncMock, side_effect=CosmosError("fail")):
            records = await list_drift(TENANT)
        assert records == []

    @pytest.mark.asyncio
    async def test_get_drift_record_returns_record(self):
        doc = _sample_record().to_cosmos()
        with patch("app.services.terraform_sync.cosmos.get_item", new_callable=AsyncMock, return_value=doc):
            r = await get_drift_record(TENANT, "dr-1")
        assert r is not None
        assert r.id == "dr-1"

    @pytest.mark.asyncio
    async def test_get_drift_record_returns_none_on_miss(self):
        with patch("app.services.terraform_sync.cosmos.get_item",
                   new_callable=AsyncMock, side_effect=Exception("not found")):
            r = await get_drift_record(TENANT, "dr-missing")
        assert r is None

    @pytest.mark.asyncio
    async def test_drift_summary_counts(self):
        docs = [
            _sample_record(id="1", status=DRIFT_STATUS_PENDING).to_cosmos(),
            _sample_record(id="2", status=DRIFT_STATUS_PENDING).to_cosmos(),
            _sample_record(id="3", status=DRIFT_STATUS_ACKNOWLEDGED).to_cosmos(),
        ]
        with patch("app.services.terraform_sync.cosmos.query_items", new_callable=AsyncMock, return_value=docs):
            summary = await get_drift_summary(TENANT)
        assert summary["pending"] == 2
        assert summary["acknowledged"] == 1
        assert summary["imported"] == 0
        assert summary["total"] == 3
        assert summary["all_reconciled"] is False

    @pytest.mark.asyncio
    async def test_drift_summary_all_reconciled(self):
        docs = [
            _sample_record(id="1", status=DRIFT_STATUS_IMPORTED).to_cosmos(),
        ]
        with patch("app.services.terraform_sync.cosmos.query_items", new_callable=AsyncMock, return_value=docs):
            summary = await get_drift_summary(TENANT)
        assert summary["all_reconciled"] is True


# ── TestDriftAcknowledgement ──────────────────────────────────────────────────

class TestDriftAcknowledgement:
    @pytest.mark.asyncio
    async def test_acknowledge_moves_to_acknowledged(self):
        doc = _sample_record(status=DRIFT_STATUS_PENDING).to_cosmos()
        with patch("app.services.terraform_sync.cosmos.get_item", new_callable=AsyncMock, return_value=doc), \
             patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert:
            r = await acknowledge_drift(TENANT, "dr-1", "eng-bob")
        assert r is not None
        assert r.status == DRIFT_STATUS_ACKNOWLEDGED
        assert r.acknowledged_by == "eng-bob"
        assert mock_upsert.called

    @pytest.mark.asyncio
    async def test_mark_imported(self):
        doc = _sample_record(status=DRIFT_STATUS_ACKNOWLEDGED).to_cosmos()
        with patch("app.services.terraform_sync.cosmos.get_item", new_callable=AsyncMock, return_value=doc), \
             patch("app.services.terraform_sync.cosmos.upsert_item", new_callable=AsyncMock):
            r = await acknowledge_drift(TENANT, "dr-1", "eng-alice", new_status=DRIFT_STATUS_IMPORTED)
        assert r.status == DRIFT_STATUS_IMPORTED

    @pytest.mark.asyncio
    async def test_acknowledge_returns_none_on_miss(self):
        with patch("app.services.terraform_sync.cosmos.get_item",
                   new_callable=AsyncMock, side_effect=Exception("not found")):
            r = await acknowledge_drift(TENANT, "nonexistent", "eng")
        assert r is None

    def test_acknowledge_raises_on_invalid_status(self):
        with pytest.raises(ValueError, match="Invalid status"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                acknowledge_drift(TENANT, "dr-1", "eng", new_status="random_status")
            )

    @pytest.mark.asyncio
    async def test_dismiss_drift_returns_true(self):
        with patch("app.services.terraform_sync.cosmos.delete_item", new_callable=AsyncMock):
            result = await dismiss_drift(TENANT, "dr-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_dismiss_drift_returns_false_on_miss(self):
        with patch("app.services.terraform_sync.cosmos.delete_item",
                   new_callable=AsyncMock, side_effect=Exception("not found")):
            result = await dismiss_drift(TENANT, "nonexistent")
        assert result is False


# ── TestWebhookPayload ────────────────────────────────────────────────────────

class TestWebhookPayload:
    def test_payload_has_text(self):
        record = _sample_record()
        payload = _build_webhook_payload(record)
        assert "text" in payload
        assert "autonomous" in payload["text"].lower() or "drift" in payload["text"].lower()

    def test_payload_has_attachments(self):
        record = _sample_record()
        payload = _build_webhook_payload(record)
        assert "attachments" in payload
        assert len(payload["attachments"]) >= 1

    def test_payload_includes_import_cmd(self):
        record = _sample_record(import_cmd="terraform import azurerm_x.y /subscriptions/abc/...")
        payload = _build_webhook_payload(record)
        text = str(payload)
        assert "terraform import" in text

    def test_payload_includes_action_id(self):
        record = _sample_record(action_id="act-webhook-test")
        payload = _build_webhook_payload(record)
        assert "act-webhook-test" in str(payload)


# ── TestRouter ────────────────────────────────────────────────────────────────

class TestRouter:
    def test_get_drift_summary_200(self, client):
        summary = {"pending": 2, "acknowledged": 1, "imported": 3, "total": 6, "all_reconciled": False}
        with patch("app.routers.terraform_sync.get_drift_summary", new_callable=AsyncMock, return_value=summary):
            r = client.get(f"/api/v1/terraform/{TENANT}/drift/summary", headers=_api_headers())
        assert r.status_code == 200
        assert r.json()["pending"] == 2

    def test_list_drift_200(self, client):
        with patch("app.routers.terraform_sync.list_drift", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/terraform/{TENANT}/drift", headers=_api_headers())
        assert r.status_code == 200
        assert r.json() == []

    def test_list_drift_with_status_filter(self, client):
        with patch("app.routers.terraform_sync.list_drift", new_callable=AsyncMock, return_value=[]) as mock_list:
            r = client.get(
                f"/api/v1/terraform/{TENANT}/drift?status=pending", headers=_api_headers()
            )
        assert r.status_code == 200
        mock_list.assert_awaited_once_with(TENANT, status_filter="pending")

    def test_list_drift_invalid_status_422(self, client):
        r = client.get(
            f"/api/v1/terraform/{TENANT}/drift?status=invalid_status", headers=_api_headers()
        )
        assert r.status_code == 422

    def test_get_single_drift_record_200(self, client):
        rec = _sample_record()
        with patch("app.routers.terraform_sync.get_drift_record", new_callable=AsyncMock, return_value=rec):
            r = client.get(f"/api/v1/terraform/{TENANT}/drift/dr-1", headers=_api_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "dr-1"
        assert "hcl_snippet" in body
        assert "import_cmd" in body

    def test_get_drift_record_404(self, client):
        with patch("app.routers.terraform_sync.get_drift_record", new_callable=AsyncMock, return_value=None):
            r = client.get(f"/api/v1/terraform/{TENANT}/drift/nonexistent", headers=_api_headers())
        assert r.status_code == 404

    def test_acknowledge_drift_200(self, client):
        rec = _sample_record(status=DRIFT_STATUS_ACKNOWLEDGED, acknowledged_by="eng-bob")
        with patch("app.routers.terraform_sync.acknowledge_drift", new_callable=AsyncMock, return_value=rec):
            r = client.post(
                f"/api/v1/terraform/{TENANT}/drift/dr-1/acknowledge",
                json={"acknowledged_by": "eng-bob"},
                headers=_api_headers(),
            )
        assert r.status_code == 200
        assert r.json()["status"] == DRIFT_STATUS_ACKNOWLEDGED

    def test_acknowledge_drift_404(self, client):
        with patch("app.routers.terraform_sync.acknowledge_drift", new_callable=AsyncMock, return_value=None):
            r = client.post(
                f"/api/v1/terraform/{TENANT}/drift/nonexistent/acknowledge",
                json={},
                headers=_api_headers(),
            )
        assert r.status_code == 404

    def test_mark_imported_200(self, client):
        rec = _sample_record(status=DRIFT_STATUS_IMPORTED)
        with patch("app.routers.terraform_sync.acknowledge_drift", new_callable=AsyncMock, return_value=rec):
            r = client.post(
                f"/api/v1/terraform/{TENANT}/drift/dr-1/imported",
                json={"acknowledged_by": "eng-alice"},
                headers=_api_headers(),
            )
        assert r.status_code == 200
        assert r.json()["status"] == DRIFT_STATUS_IMPORTED

    def test_dismiss_drift_204(self, client):
        with patch("app.routers.terraform_sync.dismiss_drift", new_callable=AsyncMock, return_value=True):
            r = client.delete(f"/api/v1/terraform/{TENANT}/drift/dr-1", headers=_api_headers())
        assert r.status_code == 204

    def test_dismiss_drift_404(self, client):
        with patch("app.routers.terraform_sync.dismiss_drift", new_callable=AsyncMock, return_value=False):
            r = client.delete(f"/api/v1/terraform/{TENANT}/drift/nonexistent", headers=_api_headers())
        assert r.status_code == 404

    def test_tag_policy_200(self, client):
        r = client.get(f"/api/v1/terraform/{TENANT}/tag-policy", headers=_api_headers())
        assert r.status_code == 200
        body = r.json()
        assert "required_tags" in body
        assert body["required_tags"]["cloudlens:source"] == "autonomous"
        assert "terraform_import_workflow" in body

    def test_missing_api_key_401(self, client):
        r = client.get(f"/api/v1/terraform/{TENANT}/drift")
        assert r.status_code == 401


# ── TestApproveActionDriftIntegration ─────────────────────────────────────────

class TestApproveActionDriftIntegration:
    """Verify that approve_action() creates a drift record for write tools."""

    @pytest.mark.asyncio
    async def test_approve_budget_creates_drift_record(self):
        from app.services.ai_agent import approve_action, PendingAction, AgentSession, _TOOL_REGISTRY
        from datetime import datetime, timezone

        mock_session = AgentSession(
            session_id="sess-drift",
            tenant_id=TENANT,
            title="Test",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        pa = PendingAction(
            action_id="act-drift-1",
            session_id="sess-drift",
            tenant_id=TENANT,
            tool_name="create_budget",
            parameters={"name": "Drift Budget", "monthly_limit_eur": 100.0},
            description="Create budget",
            impact="medium",
        )
        mock_session.pending_actions = [pa]

        tool_result = {"budget_id": "bdg-drift", "name": "Drift Budget", "monthly_limit_eur": 100.0, "created": True}
        drift_called = False

        async def mock_record_drift(**kwargs):
            nonlocal drift_called
            drift_called = True
            return _sample_record()

        mock_handler = AsyncMock(return_value=tool_result)
        patched_registry = {**_TOOL_REGISTRY, "create_budget": mock_handler}

        with patch("app.services.ai_agent._load_session", new_callable=AsyncMock, return_value=mock_session), \
             patch("app.services.ai_agent._save_session", new_callable=AsyncMock), \
             patch("app.services.ai_agent._TOOL_REGISTRY", patched_registry), \
             patch("app.services.terraform_sync.record_drift", mock_record_drift):
            result = await approve_action(TENANT, "sess-drift", "act-drift-1")

        assert result["status"] == "executed"
        assert drift_called, "record_drift should have been called for write tool"

    @pytest.mark.asyncio
    async def test_drift_failure_does_not_block_approval(self):
        """If drift recording fails, approve_action must still return success."""
        from app.services.ai_agent import approve_action, PendingAction, AgentSession, _TOOL_REGISTRY
        from datetime import datetime, timezone

        mock_session = AgentSession(
            session_id="sess-drift2",
            tenant_id=TENANT,
            title="Test",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        pa = PendingAction(
            action_id="act-drift-2",
            session_id="sess-drift2",
            tenant_id=TENANT,
            tool_name="create_budget",
            parameters={"name": "Budget2", "monthly_limit_eur": 50.0},
            description="Create budget",
            impact="low",
        )
        mock_session.pending_actions = [pa]

        mock_handler = AsyncMock(return_value={"budget_id": "bdg-2", "created": True})
        patched_registry = {**_TOOL_REGISTRY, "create_budget": mock_handler}

        with patch("app.services.ai_agent._load_session", new_callable=AsyncMock, return_value=mock_session), \
             patch("app.services.ai_agent._save_session", new_callable=AsyncMock), \
             patch("app.services.ai_agent._TOOL_REGISTRY", patched_registry), \
             patch("app.services.terraform_sync.record_drift", side_effect=RuntimeError("Drift system down")):
            result = await approve_action(TENANT, "sess-drift2", "act-drift-2")

        # Approval still succeeds despite drift recording failure
        assert result["status"] == "executed"
