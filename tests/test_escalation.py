"""
Tests for PagerDuty / Jira / ADO / Teams escalation integrations.

Covers:
  - EscalationConfig model (serialisation, from_cosmos defaults)
  - Config CRUD (get, list, save, delete)
  - Payload builders for all four channels
  - Delivery functions (KV + HTTP fully mocked)
  - deliver_escalation() dispatcher
  - AlertChannel enum has new values
  - alerts.deliver() routes to escalation for new channels
  - Router: GET/PUT/DELETE integrations, POST test
Run: pytest tests/test_escalation.py -v
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.models.alert import AlertChannel, AlertEvent, AlertType, AlertSeverity
from app.services.escalation import (
    EscalationConfig,
    CHANNEL_TYPES,
    get_escalation_config,
    list_escalation_configs,
    save_escalation_config,
    delete_escalation_config,
    deliver_escalation,
    _build_pagerduty_payload,
    _build_jira_payload,
    _build_ado_payload,
    _build_teams_adaptive_card,
    escalate_pagerduty,
    escalate_jira,
    escalate_ado,
    escalate_teams,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(
    tenant_id="t1",
    rule_id="rule-1",
    rule_name="Budget Rule",
    alert_type=AlertType.BUDGET_BREACH,
    severity=AlertSeverity.HIGH,
    title="Budget exceeded €10k",
    impact_eur=10_000.0,
    detail=None,
) -> AlertEvent:
    return AlertEvent(
        tenant_id=tenant_id,
        rule_id=rule_id,
        rule_name=rule_name,
        alert_type=alert_type,
        severity=severity,
        title=title,
        impact_eur=impact_eur,
        detail=detail or {"budget_id": "b-1", "consumed_pct": 105},
    )


def _pd_cfg(tenant_id="t1") -> EscalationConfig:
    return EscalationConfig(
        id=EscalationConfig.make_id(tenant_id, "pagerduty"),
        tenant_id=tenant_id,
        channel_type="pagerduty",
        enabled=True,
        pagerduty_service_name="CloudLens Test",
    )


def _jira_cfg(tenant_id="t1") -> EscalationConfig:
    return EscalationConfig(
        id=EscalationConfig.make_id(tenant_id, "jira"),
        tenant_id=tenant_id,
        channel_type="jira",
        enabled=True,
        jira_base_url="https://acme.atlassian.net",
        jira_email="ops@acme.com",
        jira_project_key="OPS",
        jira_issue_type="Bug",
        jira_labels=["cloudlens"],
    )


def _ado_cfg(tenant_id="t1") -> EscalationConfig:
    return EscalationConfig(
        id=EscalationConfig.make_id(tenant_id, "ado"),
        tenant_id=tenant_id,
        channel_type="ado",
        enabled=True,
        ado_org="mycompany",
        ado_project="Operations",
        ado_work_item_type="Bug",
        ado_tags="CloudLens; FinOps",
    )


def _teams_cfg(tenant_id="t1") -> EscalationConfig:
    return EscalationConfig(
        id=EscalationConfig.make_id(tenant_id, "teams"),
        tenant_id=tenant_id,
        channel_type="teams",
        enabled=True,
        teams_webhook_url="https://outlook.office.com/webhook/test",
        teams_action_url="https://cloudlens.io/t1/alerts",
    )


# ══════════════════════════════════════════════════════════════════════════════
# AlertChannel enum
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertChannelEnum:
    def test_pagerduty_channel_exists(self):
        assert AlertChannel.PAGERDUTY == "pagerduty"

    def test_jira_channel_exists(self):
        assert AlertChannel.JIRA == "jira"

    def test_ado_channel_exists(self):
        assert AlertChannel.ADO == "ado"

    def test_teams_channel_exists(self):
        assert AlertChannel.TEAMS == "teams"

    def test_all_original_channels_still_present(self):
        assert AlertChannel.IN_APP == "in_app"
        assert AlertChannel.WEBHOOK == "webhook"
        assert AlertChannel.EMAIL == "email"


# ══════════════════════════════════════════════════════════════════════════════
# EscalationConfig model
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationConfig:
    def test_make_id(self):
        assert EscalationConfig.make_id("tenant1", "jira") == "tenant1_jira"

    def test_roundtrip_cosmos(self):
        cfg = _jira_cfg()
        doc = cfg.to_cosmos()
        cfg2 = EscalationConfig.from_cosmos(doc)
        assert cfg2.jira_base_url == "https://acme.atlassian.net"
        assert cfg2.jira_project_key == "OPS"
        assert cfg2.jira_labels == ["cloudlens"]

    def test_from_cosmos_defaults(self):
        cfg = EscalationConfig.from_cosmos({"id": "t1_pagerduty",
                                             "tenant_id": "t1",
                                             "channel_type": "pagerduty"})
        assert cfg.enabled is True
        assert cfg.pagerduty_service_name == "CloudLens"
        assert cfg.jira_issue_type == "Bug"
        assert cfg.ado_work_item_type == "Bug"

    def test_channel_types_set_covers_all(self):
        assert {"pagerduty", "jira", "ado", "teams"} == CHANNEL_TYPES


# ══════════════════════════════════════════════════════════════════════════════
# Config CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigCRUD:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            result = await get_escalation_config("t1", "jira")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_config_when_present(self):
        doc = _jira_cfg().to_cosmos()
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[doc])):
            result = await get_escalation_config("t1", "jira")
        assert result is not None
        assert result.jira_project_key == "OPS"

    @pytest.mark.asyncio
    async def test_list_returns_all_configs(self):
        docs = [_pd_cfg().to_cosmos(), _jira_cfg().to_cosmos()]
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=docs)):
            result = await list_escalation_configs("t1")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_save_sets_updated_at(self):
        mock_upsert = AsyncMock()
        with patch("app.services.escalation.cosmos.upsert_item", new=mock_upsert):
            saved = await save_escalation_config(_jira_cfg())
        mock_upsert.assert_awaited_once()
        assert saved.updated_at  # timestamp was populated

    @pytest.mark.asyncio
    async def test_delete_calls_cosmos_delete(self):
        mock_delete = AsyncMock()
        with patch("app.services.escalation.cosmos.delete_item", new=mock_delete):
            result = await delete_escalation_config("t1", "jira")
        mock_delete.assert_awaited_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_on_error(self):
        with patch("app.services.escalation.cosmos.delete_item",
                   new=AsyncMock(side_effect=Exception("not found"))):
            result = await delete_escalation_config("t1", "jira")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# PagerDuty payload builder
# ══════════════════════════════════════════════════════════════════════════════

class TestPagerDutyPayload:
    def test_routing_key_present(self):
        p = _build_pagerduty_payload(_event(), "rk-123", "CloudLens Test")
        assert p["routing_key"] == "rk-123"

    def test_event_action_trigger(self):
        p = _build_pagerduty_payload(_event(), "rk-123")
        assert p["event_action"] == "trigger"

    def test_dedup_key_from_rule_id(self):
        p = _build_pagerduty_payload(_event(rule_id="rule-abc"), "rk-123")
        assert p["dedup_key"] == "cloudlens-rule-abc"

    def test_severity_mapping_critical(self):
        p = _build_pagerduty_payload(_event(severity=AlertSeverity.CRITICAL), "rk")
        assert p["payload"]["severity"] == "critical"

    def test_severity_mapping_high_to_error(self):
        p = _build_pagerduty_payload(_event(severity=AlertSeverity.HIGH), "rk")
        assert p["payload"]["severity"] == "error"

    def test_severity_mapping_medium_to_warning(self):
        p = _build_pagerduty_payload(_event(severity=AlertSeverity.MEDIUM), "rk")
        assert p["payload"]["severity"] == "warning"

    def test_custom_details_includes_tenant(self):
        p = _build_pagerduty_payload(_event(tenant_id="acme"), "rk")
        assert p["payload"]["custom_details"]["tenant_id"] == "acme"

    def test_custom_details_includes_detail_dict(self):
        p = _build_pagerduty_payload(_event(detail={"foo": "bar"}), "rk")
        assert p["payload"]["custom_details"]["foo"] == "bar"

    def test_summary_is_event_title(self):
        p = _build_pagerduty_payload(_event(title="My Alert"), "rk")
        assert p["payload"]["summary"] == "My Alert"


# ══════════════════════════════════════════════════════════════════════════════
# Jira payload builder
# ══════════════════════════════════════════════════════════════════════════════

class TestJiraPayload:
    def test_summary_prefixed(self):
        p = _build_jira_payload(_event(title="Budget breach"), _jira_cfg())
        assert p["fields"]["summary"] == "[CloudLens] Budget breach"

    def test_project_key_set(self):
        p = _build_jira_payload(_event(), _jira_cfg())
        assert p["fields"]["project"]["key"] == "OPS"

    def test_issue_type_set(self):
        p = _build_jira_payload(_event(), _jira_cfg())
        assert p["fields"]["issuetype"]["name"] == "Bug"

    def test_priority_critical_maps_to_highest(self):
        p = _build_jira_payload(_event(severity=AlertSeverity.CRITICAL), _jira_cfg())
        assert p["fields"]["priority"]["name"] == "Highest"

    def test_priority_high_maps_to_high(self):
        p = _build_jira_payload(_event(severity=AlertSeverity.HIGH), _jira_cfg())
        assert p["fields"]["priority"]["name"] == "High"

    def test_labels_included(self):
        p = _build_jira_payload(_event(), _jira_cfg())
        assert "cloudlens" in p["fields"]["labels"]

    def test_description_is_adf(self):
        p = _build_jira_payload(_event(), _jira_cfg())
        desc = p["fields"]["description"]
        assert desc["type"] == "doc"
        assert desc["version"] == 1
        assert isinstance(desc["content"], list)

    def test_description_contains_title(self):
        p = _build_jira_payload(_event(title="Spend spike"), _jira_cfg())
        desc_text = str(p["fields"]["description"])
        assert "Spend spike" in desc_text


# ══════════════════════════════════════════════════════════════════════════════
# ADO payload builder
# ══════════════════════════════════════════════════════════════════════════════

class TestADOPayload:
    def test_returns_list_of_ops(self):
        ops = _build_ado_payload(_event(), _ado_cfg())
        assert isinstance(ops, list)
        assert len(ops) >= 3

    def test_title_op_present(self):
        ops = _build_ado_payload(_event(title="Budget breach"), _ado_cfg())
        title_op = next(o for o in ops if o["path"] == "/fields/System.Title")
        assert "[CloudLens] Budget breach" in title_op["value"]

    def test_description_op_present(self):
        ops = _build_ado_payload(_event(), _ado_cfg())
        assert any(o["path"] == "/fields/System.Description" for o in ops)

    def test_priority_op_critical(self):
        ops = _build_ado_payload(_event(severity=AlertSeverity.CRITICAL), _ado_cfg())
        prio_op = next(o for o in ops if "Priority" in o["path"])
        assert prio_op["value"] == 1

    def test_priority_op_high(self):
        ops = _build_ado_payload(_event(severity=AlertSeverity.HIGH), _ado_cfg())
        prio_op = next(o for o in ops if "Priority" in o["path"])
        assert prio_op["value"] == 2

    def test_tags_op_present(self):
        ops = _build_ado_payload(_event(), _ado_cfg())
        assert any(o["path"] == "/fields/System.Tags" for o in ops)

    def test_area_path_added_when_set(self):
        cfg = _ado_cfg()
        cfg.ado_area_path = "Operations\\FinOps"
        ops = _build_ado_payload(_event(), cfg)
        assert any(o["path"] == "/fields/System.AreaPath" for o in ops)

    def test_area_path_omitted_when_empty(self):
        cfg = _ado_cfg()
        cfg.ado_area_path = ""
        ops = _build_ado_payload(_event(), cfg)
        assert not any(o["path"] == "/fields/System.AreaPath" for o in ops)


# ══════════════════════════════════════════════════════════════════════════════
# Teams Adaptive Card builder
# ══════════════════════════════════════════════════════════════════════════════

class TestTeamsAdaptiveCard:
    def test_card_type_is_message(self):
        card = _build_teams_adaptive_card(_event(), _teams_cfg())
        assert card["type"] == "message"

    def test_adaptive_card_schema_present(self):
        card = _build_teams_adaptive_card(_event(), _teams_cfg())
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert content["version"] == "1.5"

    def test_facts_include_severity(self):
        card = _build_teams_adaptive_card(_event(severity=AlertSeverity.CRITICAL), _teams_cfg())
        content = card["attachments"][0]["content"]
        fact_set = next(b for b in content["body"] if b["type"] == "FactSet")
        severities = [f["value"] for f in fact_set["facts"] if f["title"] == "Severity"]
        assert severities and "CRITICAL" in severities[0]

    def test_action_url_adds_button(self):
        cfg = _teams_cfg()
        cfg.teams_action_url = "https://cloudlens.io/alerts"
        card = _build_teams_adaptive_card(_event(), cfg)
        content = card["attachments"][0]["content"]
        assert "actions" in content
        assert any(a["type"] == "Action.OpenUrl" for a in content["actions"])

    def test_no_action_url_no_actions(self):
        cfg = _teams_cfg()
        cfg.teams_action_url = ""
        card = _build_teams_adaptive_card(_event(), cfg)
        content = card["attachments"][0]["content"]
        assert "actions" not in content


# ══════════════════════════════════════════════════════════════════════════════
# Delivery functions
# ══════════════════════════════════════════════════════════════════════════════

class TestPagerDutyDelivery:
    @pytest.mark.asyncio
    async def test_delivers_and_returns_pagerduty_label(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_pd_cfg())):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(return_value="test-routing-key")):
                with patch("app.services.escalation._post_retry",
                           new=AsyncMock(return_value=(True, 202, "ok"))):
                    label = await escalate_pagerduty(_event(), "t1")
        assert label == "pagerduty"

    @pytest.mark.asyncio
    async def test_http_failure_returns_failed_label(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_pd_cfg())):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(return_value="rk")):
                with patch("app.services.escalation._post_retry",
                           new=AsyncMock(return_value=(False, 400, "bad request"))):
                    label = await escalate_pagerduty(_event(), "t1")
        assert "failed" in label

    @pytest.mark.asyncio
    async def test_not_configured_returns_unconfigured(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=None)):
            label = await escalate_pagerduty(_event(), "t1")
        assert label == "pagerduty_unconfigured"

    @pytest.mark.asyncio
    async def test_kv_error_returns_auth_failed(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_pd_cfg())):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(side_effect=Exception("kv error"))):
                label = await escalate_pagerduty(_event(), "t1")
        assert label == "pagerduty_auth_failed"

    @pytest.mark.asyncio
    async def test_disabled_config_returns_unconfigured(self):
        cfg = _pd_cfg()
        cfg.enabled = False
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=cfg)):
            label = await escalate_pagerduty(_event(), "t1")
        assert label == "pagerduty_unconfigured"


class TestJiraDelivery:
    @pytest.mark.asyncio
    async def test_delivers_and_returns_jira_label(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_jira_cfg())):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(return_value="token-abc")):
                with patch("app.services.escalation._post_retry",
                           new=AsyncMock(return_value=(True, 201, '{"id":"JRA-1"}'))):
                    label = await escalate_jira(_event(), "t1")
        assert label == "jira"

    @pytest.mark.asyncio
    async def test_incomplete_config_returns_incomplete(self):
        cfg = _jira_cfg()
        cfg.jira_project_key = ""
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=cfg)):
            label = await escalate_jira(_event(), "t1")
        assert label == "jira_config_incomplete"

    @pytest.mark.asyncio
    async def test_not_configured_returns_unconfigured(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=None)):
            label = await escalate_jira(_event(), "t1")
        assert label == "jira_unconfigured"


class TestADODelivery:
    @pytest.mark.asyncio
    async def test_delivers_and_returns_ado_label(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_ado_cfg())):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(return_value="pat-xyz")):
                with patch("httpx.AsyncClient") as mock_client_cls:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
                    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                    mock_client_cls.return_value.post = AsyncMock(return_value=mock_resp)
                    label = await escalate_ado(_event(), "t1")
        assert label == "ado"

    @pytest.mark.asyncio
    async def test_incomplete_config_returns_incomplete(self):
        cfg = _ado_cfg()
        cfg.ado_org = ""
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=cfg)):
            label = await escalate_ado(_event(), "t1")
        assert label == "ado_config_incomplete"

    @pytest.mark.asyncio
    async def test_not_configured_returns_unconfigured(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=None)):
            label = await escalate_ado(_event(), "t1")
        assert label == "ado_unconfigured"


class TestTeamsDelivery:
    @pytest.mark.asyncio
    async def test_delivers_using_direct_webhook_url(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=_teams_cfg())):
            with patch("app.services.escalation._post_retry",
                       new=AsyncMock(return_value=(True, 200, "ok"))):
                label = await escalate_teams(_event(), "t1")
        assert label == "teams_adaptive"

    @pytest.mark.asyncio
    async def test_falls_back_to_kv_when_no_direct_url(self):
        cfg = _teams_cfg()
        cfg.teams_webhook_url = ""
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=cfg)):
            with patch("app.services.escalation._keyvault.get_secret",
                       new=AsyncMock(return_value="https://webhook.office.com/test")):
                with patch("app.services.escalation._post_retry",
                           new=AsyncMock(return_value=(True, 200, "ok"))):
                    label = await escalate_teams(_event(), "t1")
        assert label == "teams_adaptive"

    @pytest.mark.asyncio
    async def test_not_configured_returns_unconfigured(self):
        with patch("app.services.escalation.get_escalation_config",
                   new=AsyncMock(return_value=None)):
            label = await escalate_teams(_event(), "t1")
        assert label == "teams_unconfigured"


# ══════════════════════════════════════════════════════════════════════════════
# Unified dispatcher
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliverEscalation:
    @pytest.mark.asyncio
    async def test_routes_pagerduty(self):
        with patch("app.services.escalation.escalate_pagerduty",
                   new=AsyncMock(return_value="pagerduty")) as mock_pd:
            label = await deliver_escalation(_event(), "t1", "pagerduty")
        mock_pd.assert_awaited_once()
        assert label == "pagerduty"

    @pytest.mark.asyncio
    async def test_routes_jira(self):
        with patch("app.services.escalation.escalate_jira",
                   new=AsyncMock(return_value="jira")) as mock_j:
            label = await deliver_escalation(_event(), "t1", "jira")
        mock_j.assert_awaited_once()
        assert label == "jira"

    @pytest.mark.asyncio
    async def test_routes_ado(self):
        with patch("app.services.escalation.escalate_ado",
                   new=AsyncMock(return_value="ado")) as mock_a:
            label = await deliver_escalation(_event(), "t1", "ado")
        mock_a.assert_awaited_once()
        assert label == "ado"

    @pytest.mark.asyncio
    async def test_routes_teams(self):
        with patch("app.services.escalation.escalate_teams",
                   new=AsyncMock(return_value="teams_adaptive")) as mock_t:
            label = await deliver_escalation(_event(), "t1", "teams")
        mock_t.assert_awaited_once()
        assert label == "teams_adaptive"

    @pytest.mark.asyncio
    async def test_unknown_channel_returns_label(self):
        label = await deliver_escalation(_event(), "t1", "fax")
        assert "unknown_channel" in label

    @pytest.mark.asyncio
    async def test_never_raises_on_exception(self):
        with patch("app.services.escalation.escalate_pagerduty",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            label = await deliver_escalation(_event(), "t1", "pagerduty")
        assert "error" in label


# ══════════════════════════════════════════════════════════════════════════════
# alerts.deliver() integration with new channels
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertsDeliverIntegration:
    @pytest.mark.asyncio
    async def test_pagerduty_channel_calls_escalation(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertRule, AlertRuleCreate, AlertChannel, AlertType

        rule = AlertRule(
            tenant_id="t1",
            name="Test rule",
            alert_type=AlertType.BUDGET_BREACH,
            channels=[AlertChannel.PAGERDUTY],
        )
        with patch("app.services.escalation.deliver_escalation",
                   new=AsyncMock(return_value="pagerduty")) as mock_esc:
            event = await deliver(_event(), rule)
        mock_esc.assert_awaited_once()
        _, call_tenant, call_channel = mock_esc.call_args.args
        assert call_tenant == "t1"
        assert call_channel == "pagerduty"
        assert "pagerduty" in event.delivered_channels

    @pytest.mark.asyncio
    async def test_jira_channel_calls_escalation(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertRule, AlertChannel, AlertType

        rule = AlertRule(
            tenant_id="t1",
            name="Test rule",
            alert_type=AlertType.BUDGET_BREACH,
            channels=[AlertChannel.JIRA],
        )
        with patch("app.services.escalation.deliver_escalation",
                   new=AsyncMock(return_value="jira")) as mock_esc:
            event = await deliver(_event(), rule)
        mock_esc.assert_awaited_once()
        _, call_tenant, call_channel = mock_esc.call_args.args
        assert call_tenant == "t1"
        assert call_channel == "jira"
        assert "jira" in event.delivered_channels

    @pytest.mark.asyncio
    async def test_in_app_always_delivered(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertRule, AlertChannel, AlertType

        rule = AlertRule(
            tenant_id="t1",
            name="Test",
            alert_type=AlertType.WASTE_THRESHOLD,
            channels=[AlertChannel.IN_APP, AlertChannel.ADO],
        )
        with patch("app.services.escalation.deliver_escalation",
                   new=AsyncMock(return_value="ado")):
            event = await deliver(_event(), rule)
        assert "in_app" in event.delivered_channels
        assert "ado" in event.delivered_channels


# ══════════════════════════════════════════════════════════════════════════════
# Router endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def _headers(self):
        from app.config import get_settings
        return {"X-API-Key": get_settings().internal_api_key}

    def test_list_integrations_empty(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().get("/api/v1/escalation/t1/integrations", headers=self._headers())
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_get_integration_not_found(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().get("/api/v1/escalation/t1/integrations/jira", headers=self._headers())
        assert resp.status_code == 404

    def test_get_integration_invalid_channel_rejects(self):
        resp = self._client().get("/api/v1/escalation/t1/integrations/banana", headers=self._headers())
        assert resp.status_code == 422

    def test_upsert_jira_integration(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            with patch("app.services.escalation.cosmos.upsert_item", new=AsyncMock()):
                resp = self._client().put(
                    "/api/v1/escalation/t1/integrations/jira",
                    json={
                        "jira_base_url": "https://acme.atlassian.net",
                        "jira_email": "ops@acme.com",
                        "jira_project_key": "OPS",
                    },
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["jira_project_key"] == "OPS"
        assert "kv_secret_names" in body
        assert body["kv_secret_names"]["required_secret"] == "jira-api-token-t1"

    def test_upsert_bad_jira_url_rejected(self):
        resp = self._client().put(
            "/api/v1/escalation/t1/integrations/jira",
            json={"jira_base_url": "http://not-https.example.com"},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_upsert_pagerduty_integration(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            with patch("app.services.escalation.cosmos.upsert_item", new=AsyncMock()):
                resp = self._client().put(
                    "/api/v1/escalation/t1/integrations/pagerduty",
                    json={"pagerduty_service_name": "Prod"},
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["kv_secret_names"]["required_secret"] == "pd-routing-key-t1"

    def test_upsert_ado_integration(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            with patch("app.services.escalation.cosmos.upsert_item", new=AsyncMock()):
                resp = self._client().put(
                    "/api/v1/escalation/t1/integrations/ado",
                    json={"ado_org": "myco", "ado_project": "Ops"},
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["kv_secret_names"]["required_secret"] == "ado-pat-t1"

    def test_upsert_teams_integration(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            with patch("app.services.escalation.cosmos.upsert_item", new=AsyncMock()):
                resp = self._client().put(
                    "/api/v1/escalation/t1/integrations/teams",
                    json={"teams_webhook_url": "https://outlook.office.com/webhook/test"},
                    headers=self._headers(),
                )
        assert resp.status_code == 200

    def test_delete_not_found_returns_404(self):
        with patch("app.services.escalation.cosmos.delete_item",
                   new=AsyncMock(side_effect=Exception("not found"))):
            resp = self._client().delete(
                "/api/v1/escalation/t1/integrations/jira",
                headers=self._headers(),
            )
        assert resp.status_code == 404

    def test_delete_invalid_channel_rejects(self):
        resp = self._client().delete(
            "/api/v1/escalation/t1/integrations/slack",
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_test_endpoint_not_configured_returns_404(self):
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = self._client().post(
                "/api/v1/escalation/t1/integrations/pagerduty/test",
                headers=self._headers(),
            )
        assert resp.status_code == 404

    def test_test_endpoint_delivers_and_reports_success(self):
        doc = _pd_cfg().to_cosmos()
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[doc])):
            with patch("app.routers.escalation.deliver_escalation",
                       new=AsyncMock(return_value="pagerduty")):
                resp = self._client().post(
                    "/api/v1/escalation/t1/integrations/pagerduty/test",
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["status"] == "pagerduty"

    def test_test_endpoint_reports_failure(self):
        doc = _pd_cfg().to_cosmos()
        with patch("app.services.escalation.cosmos.query_items", new=AsyncMock(return_value=[doc])):
            with patch("app.routers.escalation.deliver_escalation",
                       new=AsyncMock(return_value="pagerduty_failed(400)")):
                resp = self._client().post(
                    "/api/v1/escalation/t1/integrations/pagerduty/test",
                    headers=self._headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_requires_api_key(self):
        resp = self._client().get("/api/v1/escalation/t1/integrations")
        assert resp.status_code == 401
