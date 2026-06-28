"""
Tests for the CloudLens AI Agent layer.

Covers: tool registry, individual tool handlers, session management,
LLM path (mocked httpx), fallback (no API key), action approval,
briefing generation, and router endpoints.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# ── Test environment setup ────────────────────────────────────────────────────
os.environ.setdefault("INTERNAL_API_KEY",       "test-key")
os.environ.setdefault("AZURE_TENANT_ID",        "test-tenant")
os.environ.setdefault("AZURE_CLIENT_ID",        "test-client")
os.environ.setdefault("COSMOS_ENDPOINT",        "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME",   "teststorage")
os.environ.setdefault("KEY_VAULT_NAME",         "test-kv")

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _settings(**overrides):
    from app.config import get_settings
    s = get_settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def _api_headers():
    from app.config import get_settings
    return {"X-API-Key": get_settings().internal_api_key}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════════════
# Tool registry
# ══════════════════════════════════════════════════════════════════════════════

class TestToolRegistry:
    def test_all_expected_tools_registered(self):
        from app.services.ai_agent import _TOOL_REGISTRY
        expected = {
            "get_cost_summary", "get_anomalies", "get_waste_items", "get_forecast",
            "get_top_services", "get_commitment_advice", "get_maturity_score",
            "get_budget_status", "get_k8s_costs", "get_unit_economics",
            "get_policies", "explain_anomaly", "create_budget", "create_alert_rule",
        }
        assert expected.issubset(set(_TOOL_REGISTRY.keys()))

    def test_write_tools_defined(self):
        from app.services.ai_agent import _WRITE_TOOLS
        assert "create_budget" in _WRITE_TOOLS
        assert "create_alert_rule" in _WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        from app.services.ai_agent import _TOOL_REGISTRY, _WRITE_TOOLS
        read_tools = set(_TOOL_REGISTRY.keys()) - _WRITE_TOOLS
        assert "get_cost_summary" in read_tools
        assert "get_anomalies" in read_tools

    def test_tool_defs_have_required_openai_fields(self):
        from app.services.ai_agent import _TOOL_DEFS
        for td in _TOOL_DEFS:
            assert td["type"] == "function"
            fn = td["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn

    def test_all_registry_tools_have_defs(self):
        from app.services.ai_agent import _TOOL_DEFS, _TOOL_REGISTRY
        def_names = {td["function"]["name"] for td in _TOOL_DEFS}
        for name in _TOOL_REGISTRY:
            assert name in def_names, f"{name} has no _TOOL_DEF entry"


# ══════════════════════════════════════════════════════════════════════════════
# Tool handlers — individual unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestToolHandlers:
    @pytest.mark.asyncio
    async def test_cost_summary_returns_expected_structure(self):
        from app.services.ai_agent import _tool_cost_summary
        rows = [
            {"name": "Compute", "eur": 12000},
            {"name": "Storage", "eur": 5000},
        ]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=rows)):
            result = await _tool_cost_summary("t1", period_days=30, group_by="service")
        assert result["total_eur"] == 17000
        assert len(result["top_items"]) == 2
        assert result["top_items"][0]["name"] == "Compute"
        assert "_chart" in result

    @pytest.mark.asyncio
    async def test_cost_summary_handles_cosmos_error(self):
        from app.services.ai_agent import _tool_cost_summary
        from app.exceptions import CosmosError
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(side_effect=CosmosError("fail"))):
            result = await _tool_cost_summary("t1")
        assert result["total_eur"] == 0
        assert result["top_items"] == []

    @pytest.mark.asyncio
    async def test_waste_items_returns_structure(self):
        from app.services.ai_agent import _tool_waste
        docs = [
            {"id": "w1", "title": "Idle VM", "category": "compute", "monthly_saving_eur": 450, "resource_id": "/r/1"},
            {"id": "w2", "title": "Orphaned Disk", "category": "storage", "monthly_saving_eur": 120, "resource_id": "/r/2"},
        ]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=docs)):
            result = await _tool_waste("t1")
        assert result["total_waste_eur"] == 570
        assert result["item_count"] == 2
        assert result["items"][0]["title"] == "Idle VM"

    @pytest.mark.asyncio
    async def test_anomalies_insufficient_history(self):
        from app.services.ai_agent import _tool_anomalies
        # Only 5 days of data → not enough
        rows = [{"day": f"2026-06-{22+i}", "eur": 1000} for i in range(5)]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=rows)):
            result = await _tool_anomalies("t1")
        assert result["anomaly_count"] == 0
        assert "Insufficient" in result.get("note", "")

    @pytest.mark.asyncio
    async def test_budget_status_calculates_pct(self):
        from app.services.ai_agent import _tool_budget_status
        docs = [
            {"name": "Eng", "monthly_limit_eur": 10000, "current_spend_eur": 9500},
            {"name": "ML", "monthly_limit_eur": 5000, "current_spend_eur": 2000},
        ]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=docs)):
            result = await _tool_budget_status("t1")
        assert result["budget_count"] == 2
        eng = next(b for b in result["budgets"] if b["name"] == "Eng")
        assert eng["status"] == "warning"
        ml = next(b for b in result["budgets"] if b["name"] == "ML")
        assert ml["status"] == "ok"

    @pytest.mark.asyncio
    async def test_budget_status_detects_breach(self):
        from app.services.ai_agent import _tool_budget_status
        docs = [{"name": "Ops", "monthly_limit_eur": 1000, "current_spend_eur": 1200}]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=docs)):
            result = await _tool_budget_status("t1")
        assert result["breached"] == 1
        assert result["budgets"][0]["status"] == "breach"

    @pytest.mark.asyncio
    async def test_policies_returns_structure(self):
        from app.services.ai_agent import _tool_policies
        docs = [
            {"id": "p1", "name": "Tag enforcement", "action_type": "notify", "condition_type": "tag_missing"},
        ]
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=docs)):
            result = await _tool_policies("t1")
        assert result["policy_count"] == 1
        assert result["policies"][0]["name"] == "Tag enforcement"

    @pytest.mark.asyncio
    async def test_unit_economics_no_config(self):
        from app.services.ai_agent import _tool_unit_economics
        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=[])):
            result = await _tool_unit_economics("t1")
        assert "note" in result
        assert result["metrics"] == []

    @pytest.mark.asyncio
    async def test_tool_cost_summary_scoped_to_tenant(self):
        """Tool must pass the correct tenant_id to Cosmos — not another tenant's."""
        from app.services.ai_agent import _tool_cost_summary
        captured = {}
        async def fake_query(container, sql, params, partition_key=None):
            captured["partition_key"] = partition_key
            return []
        with patch("app.services.ai_agent.cosmos.query_items", new=fake_query):
            await _tool_cost_summary("my-tenant")
        assert captured["partition_key"] == "my-tenant"


# ══════════════════════════════════════════════════════════════════════════════
# Session management
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionManagement:
    @pytest.mark.asyncio
    async def test_new_session_has_empty_turns(self):
        from app.services.ai_agent import _new_session
        s = _new_session("t1", "My question")
        assert s.turns == []
        assert s.tenant_id == "t1"
        assert s.title == "My question"

    def test_session_title_truncation(self):
        from app.services.ai_agent import _session_title
        long = "a" * 100
        title = _session_title(long)
        assert len(title) <= 61  # 60 + ellipsis

    def test_session_to_cosmos_includes_ttl(self):
        from app.services.ai_agent import _new_session
        s = _new_session("t1", "Test")
        doc = s.to_cosmos()
        assert doc["ttl"] > 0
        assert doc["_partitionKey"] == "t1"
        assert doc["type"] == "agent_session"

    def test_session_roundtrip(self):
        from app.services.ai_agent import _new_session, AgentSession
        s = _new_session("t1", "Roundtrip test")
        doc = s.to_cosmos()
        s2 = AgentSession.from_cosmos(doc)
        assert s2.session_id == s.session_id
        assert s2.tenant_id == "t1"

    @pytest.mark.asyncio
    async def test_load_session_returns_none_on_missing(self):
        from app.services.ai_agent import _load_session
        with patch("app.services.ai_agent.cosmos.get_item", new=AsyncMock(side_effect=Exception("not found"))):
            result = await _load_session("missing-id", "t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_session_returns_true(self):
        from app.services.ai_agent import delete_session
        with patch("app.services.ai_agent.cosmos.delete_item", new=AsyncMock()):
            result = await delete_session("s1", "t1")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_session_returns_false_on_error(self):
        from app.services.ai_agent import delete_session
        with patch("app.services.ai_agent.cosmos.delete_item", new=AsyncMock(side_effect=Exception("gone"))):
            result = await delete_session("s1", "t1")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Fallback path (no API key)
# ══════════════════════════════════════════════════════════════════════════════

class TestFallbackPath:
    def test_classify_anomaly_intent(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Are there any cost anomalies?")
        assert tool == "get_anomalies"

    def test_classify_waste_intent(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Show me idle and unused resources we can cut")
        assert tool == "get_waste_items"

    def test_classify_forecast_intent(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Predict our spend next month")
        assert tool == "get_forecast"

    def test_classify_budget_intent(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Are we over budget?")
        assert tool == "get_budget_status"

    def test_classify_commitment_intent(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Should we buy reserved instances?")
        assert tool == "get_commitment_advice"

    def test_classify_default_is_cost_summary(self):
        from app.services.ai_agent import _classify_intent
        tool, _ = _classify_intent("Hello there")
        assert tool == "get_cost_summary"

    @pytest.mark.asyncio
    async def test_fallback_chat_returns_response(self):
        from app.services.ai_agent import chat
        cost_rows = [{"name": "Compute", "eur": 15000}]
        with patch("app.config.get_settings") as mock_settings:
            s = MagicMock()
            s.openai_api_key = ""
            s.agent_model = ""
            s.openai_model = "gpt-4o"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_cost_records = "cost_records"
            s.cosmos_container_waste_items = "waste_items"
            s.cosmos_container_policies = "policies"
            s.cosmos_container_agent_sessions = "agent_sessions"
            mock_settings.return_value = s
            with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=cost_rows)):
                with patch("app.services.ai_agent.cosmos.upsert_item", new=AsyncMock()):
                    with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                        resp = await chat("t1", "How much did we spend?")
        assert resp.fallback is True
        assert resp.reply
        assert resp.session_id


# ══════════════════════════════════════════════════════════════════════════════
# LLM path (mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestLLMPath:
    def _make_llm_text_resp(self, text: str) -> dict:
        return {"choices": [{"message": {"content": text, "tool_calls": None}, "finish_reason": "stop"}]}

    def _make_llm_tool_resp(self, tool_name: str, args: dict) -> dict:
        return {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}]
        }, "finish_reason": "tool_calls"}]}

    @pytest.mark.asyncio
    async def test_llm_text_response_returned(self):
        from app.services.ai_agent import chat
        llm_resp = self._make_llm_text_resp("Your total spend is **€45,000** this month.")
        with patch("app.services.ai_agent.get_settings") as mock_cfg:
            s = MagicMock()
            s.openai_api_key = "test-key"
            s.agent_model = "gpt-4o"
            s.openai_model = "gpt-4o"
            s.openai_base_url = "https://api.openai.com/v1"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_agent_sessions = "agent_sessions"
            mock_cfg.return_value = s
            with patch("app.services.ai_agent._call_llm", new=AsyncMock(return_value=llm_resp)):
                with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                    with patch("app.services.ai_agent._save_session", new=AsyncMock()):
                        resp = await chat("t1", "How much are we spending?")
        assert "€45,000" in resp.reply
        assert resp.fallback is False

    @pytest.mark.asyncio
    async def test_tool_call_executed_and_fed_to_llm(self):
        from app.services.ai_agent import chat
        tool_resp = self._make_llm_tool_resp("get_cost_summary", {"period_days": 30})
        text_resp = self._make_llm_text_resp("Based on the data: **€50,000** this month.")
        call_count = {"n": 0}

        async def mock_llm(messages, tools, model):
            call_count["n"] += 1
            return tool_resp if call_count["n"] == 1 else text_resp

        with patch("app.services.ai_agent.get_settings") as mock_cfg:
            s = MagicMock()
            s.openai_api_key = "test-key"
            s.agent_model = "gpt-4o"
            s.openai_model = "gpt-4o"
            s.openai_base_url = "https://api.openai.com/v1"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_agent_sessions = "agent_sessions"
            s.cosmos_container_cost_records = "cost_records"
            mock_cfg.return_value = s
            with patch("app.services.ai_agent._call_llm", new=mock_llm):
                with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                    with patch("app.services.ai_agent._save_session", new=AsyncMock()):
                        with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=[])):
                            resp = await chat("t1", "How much did we spend?")
        assert call_count["n"] == 2  # tool call + final answer
        assert "get_cost_summary" in resp.tools_used

    @pytest.mark.asyncio
    async def test_write_tool_becomes_pending_action(self):
        from app.services.ai_agent import chat
        tool_resp = self._make_llm_tool_resp("create_budget", {"name": "ML budget", "monthly_limit_eur": 5000})
        text_resp = self._make_llm_text_resp("I've proposed a budget of **€5,000/month** for ML. Please approve.")
        call_count = {"n": 0}

        async def mock_llm(messages, tools, model):
            call_count["n"] += 1
            return tool_resp if call_count["n"] == 1 else text_resp

        with patch("app.services.ai_agent.get_settings") as mock_cfg:
            s = MagicMock()
            s.openai_api_key = "test-key"
            s.agent_model = "gpt-4o"
            s.openai_model = "gpt-4o"
            s.openai_base_url = "https://api.openai.com/v1"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_agent_sessions = "agent_sessions"
            mock_cfg.return_value = s
            with patch("app.services.ai_agent._call_llm", new=mock_llm):
                with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                    with patch("app.services.ai_agent._save_session", new=AsyncMock()):
                        resp = await chat("t1", "Create a €5k ML budget")
        assert len(resp.pending_actions) == 1
        pa = resp.pending_actions[0]
        assert pa["tool_name"] == "create_budget"
        assert pa["parameters"]["monthly_limit_eur"] == 5000

    @pytest.mark.asyncio
    async def test_session_saved_after_chat(self):
        from app.services.ai_agent import chat
        llm_resp = self._make_llm_text_resp("Your spend is fine.")
        saved = {"called": False}

        async def mock_save(session):
            saved["called"] = True

        with patch("app.services.ai_agent.get_settings") as mock_cfg:
            s = MagicMock()
            s.openai_api_key = "test-key"
            s.agent_model = "gpt-4o"
            s.openai_model = "gpt-4o"
            s.openai_base_url = "https://api.openai.com/v1"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_agent_sessions = "agent_sessions"
            mock_cfg.return_value = s
            with patch("app.services.ai_agent._call_llm", new=AsyncMock(return_value=llm_resp)):
                with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                    with patch("app.services.ai_agent._save_session", new=mock_save):
                        await chat("t1", "Status check")
        assert saved["called"]


# ══════════════════════════════════════════════════════════════════════════════
# Action approval
# ══════════════════════════════════════════════════════════════════════════════

class TestActionApproval:
    @pytest.mark.asyncio
    async def test_approve_executes_write_tool(self):
        from app.services.ai_agent import approve_action, AgentSession, PendingAction, _new_session
        session = _new_session("t1", "Test")
        pa = PendingAction(
            action_id="act-1",
            session_id=session.session_id,
            tenant_id="t1",
            tool_name="create_budget",
            parameters={"name": "Ops budget", "monthly_limit_eur": 2000},
            description="Create budget",
            impact="Creates a budget",
        )
        session.pending_actions.append(pa)

        with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=session)):
            with patch("app.services.ai_agent._save_session", new=AsyncMock()):
                with patch("app.services.ai_agent.cosmos.upsert_item", new=AsyncMock()):
                    result = await approve_action("t1", session.session_id, "act-1")

        assert result["status"] == "executed"
        assert result["result"]["created"] is True

    @pytest.mark.asyncio
    async def test_approve_unknown_action_returns_error(self):
        from app.services.ai_agent import approve_action, _new_session
        session = _new_session("t1", "Test")
        with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=session)):
            result = await approve_action("t1", session.session_id, "no-such-action")
        assert "not_found" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_missing_session_returns_error(self):
        from app.services.ai_agent import approve_action
        with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
            result = await approve_action("t1", "fake-session", "act-1")
        assert result["error"] == "session_not_found"

    def test_action_description_create_budget(self):
        from app.services.ai_agent import _action_description
        desc, impact = _action_description("create_budget", {"name": "Test", "monthly_limit_eur": 3000, "service_filter": "Compute"})
        assert "Test" in desc
        assert "€3,000" in desc
        assert "Compute" in desc
        assert "does not modify any cloud resources" in impact.lower()

    def test_action_description_create_alert(self):
        from app.services.ai_agent import _action_description
        desc, impact = _action_description("create_alert_rule", {"name": "Spike alert", "condition": "spend_spike", "threshold_eur": 1000, "channels": ["teams"]})
        assert "Spike alert" in desc
        assert "spend_spike" in desc


# ══════════════════════════════════════════════════════════════════════════════
# Briefing generation
# ══════════════════════════════════════════════════════════════════════════════

class TestBriefingGeneration:
    @pytest.mark.asyncio
    async def test_briefing_returns_expected_structure(self):
        from app.services.ai_briefing import generate_briefing
        signals = {
            "anomalies": {"anomaly_count": 0, "anomalies": []},
            "waste": {"total_waste_eur": 1200, "item_count": 3, "items": [{"monthly_saving_eur": 450}]},
            "budgets": {"budget_count": 2, "breached": 0, "warning": 1, "budgets": [{"name": "Eng", "pct": 85, "status": "warning"}]},
            "commitments": {"commit_now_count": 1, "total_potential_saving_eur": 2400, "opportunities": []},
            "maturity": {"overall_score": 72, "grade": "B", "top_recommendations": ["Reduce untagged spend"]},
            "spend": {"total_eur": 45000, "top_items": [{"name": "Compute", "eur": 23000, "pct": 51}]},
        }
        with patch("app.services.ai_briefing._gather_signals", new=AsyncMock(return_value=signals)):
            with patch("app.config.get_settings") as mock_cfg:
                s = MagicMock()
                s.openai_api_key = ""  # use deterministic
                s.agent_model = ""
                s.openai_model = "gpt-4o"
                mock_cfg.return_value = s
                result = await generate_briefing("t1")

        assert result.tenant_id == "t1"
        assert result.generated_at
        assert result.narrative
        assert result.top_action
        assert result.generated_by == "deterministic"

    @pytest.mark.asyncio
    async def test_briefing_cards_include_waste_card(self):
        from app.services.ai_briefing import _build_cards
        signals = {
            "waste": {"total_waste_eur": 2000, "item_count": 5, "items": [{"monthly_saving_eur": 600}]},
            "anomalies": {}, "budgets": {}, "commitments": {}, "maturity": {}, "spend": {},
        }
        cards = _build_cards(signals)
        categories = [c.category for c in cards]
        assert "waste" in categories

    @pytest.mark.asyncio
    async def test_briefing_anomaly_critical_severity(self):
        from app.services.ai_briefing import _build_cards
        signals = {
            "anomalies": {"anomaly_count": 1, "anomalies": [{"date": "2026-06-27", "severity": "high", "excess_eur": 3000}]},
            "waste": {}, "budgets": {}, "commitments": {}, "maturity": {}, "spend": {},
        }
        cards = _build_cards(signals)
        anomaly_card = next((c for c in cards if c.category == "anomaly"), None)
        assert anomaly_card is not None
        assert anomaly_card.severity == "critical"

    def test_top_action_prioritises_anomalies(self):
        from app.services.ai_briefing import _top_action
        signals = {
            "anomalies": {"anomaly_count": 2, "anomalies": [{"date": "2026-06-27"}]},
            "budgets": {"breached": 1},
            "commitments": {"commit_now_count": 1, "total_potential_saving_eur": 2000},
            "waste": {"total_waste_eur": 5000, "items": [{"monthly_saving_eur": 500}]},
        }
        action = _top_action(signals)
        assert "anomaly" in action.lower()

    def test_deterministic_narrative_includes_waste(self):
        from app.services.ai_briefing import _deterministic_narrative
        signals = {
            "anomalies": {"anomaly_count": 0, "anomalies": []},
            "waste": {"total_waste_eur": 1500, "item_count": 4, "items": []},
            "budgets": {"budget_count": 0, "breached": 0, "warning": 0, "budgets": []},
            "commitments": {"commit_now_count": 0, "total_potential_saving_eur": 0},
            "maturity": {"overall_score": 65, "grade": "C"},
            "spend": {},
        }
        narrative = _deterministic_narrative(signals)
        assert "€1,500" in narrative

    def test_deterministic_narrative_shows_no_anomaly(self):
        from app.services.ai_briefing import _deterministic_narrative
        signals = {"anomalies": {"anomaly_count": 0, "anomalies": []}, "waste": {}, "budgets": {}, "commitments": {}, "maturity": {}, "spend": {}}
        narrative = _deterministic_narrative(signals)
        assert "No anomalies" in narrative


# ══════════════════════════════════════════════════════════════════════════════
# Streaming
# ══════════════════════════════════════════════════════════════════════════════

class TestStreaming:
    @pytest.mark.asyncio
    async def test_chat_stream_is_async_iterator(self):
        import inspect
        from app.services.ai_agent import chat_stream
        # chat_stream should be an async generator function
        gen = chat_stream("t1", "Hello")
        assert inspect.isasyncgen(gen)
        # Clean up without iterating fully
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_stream_fallback_yields_done(self):
        from app.services.ai_agent import chat_stream
        chunks = []
        llm_text_resp = {"choices": [{"message": {"content": "Answer.", "tool_calls": None}}]}
        with patch("app.services.ai_agent.get_settings") as mock_cfg:
            s = MagicMock()
            s.openai_api_key = ""
            s.agent_model = ""
            s.openai_model = "gpt-4o"
            s.agent_max_history_turns = 20
            s.agent_session_ttl_days = 30
            s.cosmos_container_agent_sessions = "agent_sessions"
            s.cosmos_container_cost_records = "cost_records"
            s.cosmos_container_waste_items = "waste_items"
            s.cosmos_container_policies = "policies"
            mock_cfg.return_value = s
            with patch("app.services.ai_agent._load_session", new=AsyncMock(return_value=None)):
                with patch("app.services.ai_agent._save_session", new=AsyncMock()):
                    with patch("app.services.ai_agent.cosmos.query_items", new=AsyncMock(return_value=[])):
                        with patch("app.services.ai_agent.cosmos.upsert_item", new=AsyncMock()):
                            async for chunk in chat_stream("t1", "test"):
                                chunks.append(chunk)
        types = [c.get("type") for c in chunks]
        assert "done" in types


# ══════════════════════════════════════════════════════════════════════════════
# Router endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentRouter:
    def test_chat_endpoint_returns_200(self):
        from app.services.ai_agent import AgentResponse
        mock_resp = AgentResponse(
            session_id="sess-1", turn_id="turn-1",
            reply="Your total spend is €45,000.",
            suggestions=["Show waste", "Check anomalies"],
            tools_used=["get_cost_summary"],
        )
        with patch("app.routers.agent.chat", new=AsyncMock(return_value=mock_resp)):
            resp = _client().post(
                "/api/v1/agent/t1/chat",
                json={"message": "How much did we spend?"},
                headers=_api_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "sess-1"
        assert "€45,000" in body["reply"]
        assert "get_cost_summary" in body["tools_used"]

    def test_chat_endpoint_requires_api_key(self):
        resp = _client().post("/api/v1/agent/t1/chat", json={"message": "test"})
        assert resp.status_code in (401, 403)

    def test_chat_rejects_empty_message(self):
        resp = _client().post(
            "/api/v1/agent/t1/chat",
            json={"message": ""},
            headers=_api_headers(),
        )
        assert resp.status_code == 422

    def test_stream_endpoint_returns_200(self):
        async def mock_stream(*args, **kwargs):
            yield {"type": "token", "content": "Hello "}
            yield {"type": "token", "content": "world"}
            yield {"type": "done", "session_id": "s1", "suggestions": [], "chart_data": [], "pending_actions": []}
        with patch("app.routers.agent.chat_stream", new=mock_stream):
            resp = _client().post(
                "/api/v1/agent/t1/stream",
                json={"message": "test"},
                headers=_api_headers(),
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_history_endpoint_returns_list(self):
        sessions = [
            {"session_id": "s1", "title": "Cost analysis", "created_at": "2026-06-27T10:00:00Z", "updated_at": "2026-06-27T10:05:00Z"},
        ]
        with patch("app.routers.agent.get_sessions", new=AsyncMock(return_value=sessions)):
            resp = _client().get("/api/v1/agent/t1/history", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.json()[0]["session_id"] == "s1"

    def test_history_detail_returns_session(self):
        from app.services.ai_agent import _new_session
        session = _new_session("t1", "Test session")
        with patch("app.routers.agent.get_sessions", new=AsyncMock(return_value=[])):
            with patch("app.routers.agent._load_session", new=AsyncMock(return_value=session)):
                resp = _client().get(f"/api/v1/agent/t1/history/{session.session_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session.session_id

    def test_history_detail_404_on_missing(self):
        with patch("app.routers.agent._load_session", new=AsyncMock(return_value=None)):
            resp = _client().get("/api/v1/agent/t1/history/no-such-session", headers=_api_headers())
        assert resp.status_code == 404

    def test_delete_session_returns_204(self):
        with patch("app.routers.agent.delete_session", new=AsyncMock(return_value=True)):
            resp = _client().delete("/api/v1/agent/t1/history/s1", headers=_api_headers())
        assert resp.status_code == 204

    def test_delete_session_404_when_not_found(self):
        with patch("app.routers.agent.delete_session", new=AsyncMock(return_value=False)):
            resp = _client().delete("/api/v1/agent/t1/history/no-such", headers=_api_headers())
        assert resp.status_code == 404

    def test_briefing_endpoint_returns_200(self):
        from app.services.ai_briefing import BriefingResponse, BriefingCard
        mock_briefing = BriefingResponse(
            tenant_id="t1",
            generated_at="2026-06-27T09:00:00Z",
            narrative="Good morning! No anomalies overnight.",
            cards=[BriefingCard(category="spend", title="30d Total", body="Top: Compute", metric="€45,000", severity="info")],
            top_action="Review waste items",
            generated_by="deterministic",
        )
        with patch("app.routers.agent.generate_briefing", new=AsyncMock(return_value=mock_briefing)):
            resp = _client().get("/api/v1/agent/t1/briefing", headers=_api_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["narrative"] == "Good morning! No anomalies overnight."
        assert body["top_action"] == "Review waste items"
        assert len(body["cards"]) == 1

    def test_approve_action_404_on_missing(self):
        with patch("app.routers.agent.approve_action", new=AsyncMock(return_value={"error": "action_not_found_or_already_executed"})):
            resp = _client().post("/api/v1/agent/t1/approve/no-such-action", headers=_api_headers())
        assert resp.status_code == 404
