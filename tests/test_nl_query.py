"""
Tests for Natural Language Cost Querying.
Run: pytest tests/test_nl_query.py -v
"""
from __future__ import annotations
import json
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("INTERNAL_API_KEY",       "test-key")
os.environ.setdefault("AZURE_TENANT_ID",        "t")
os.environ.setdefault("AZURE_CLIENT_ID",        "c")
os.environ.setdefault("COSMOS_ENDPOINT",        "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME",   "s")
os.environ.setdefault("KEY_VAULT_NAME",         "k")

from app.services.nl_query import (
    _rule_based_intent,
    _fallback_narrative,
    _INTENT_PATTERNS,
    answer_question,
    NLQueryResult,
    _MAX_QUESTION_LEN,
)


# ── Rule-based intent detection ───────────────────────────────────────────────

class TestRuleBasedIntent:
    def test_top_services(self):
        assert _rule_based_intent("Which service costs the most?") == "top_services"
        assert _rule_based_intent("show top 5 services") == "top_services"

    def test_by_cloud(self):
        assert _rule_based_intent("break down spend by cloud") == "by_cloud"
        assert _rule_based_intent("multicloud breakdown") == "by_cloud"
        assert _rule_based_intent("per cloud spend") == "by_cloud"

    def test_trend(self):
        assert _rule_based_intent("show me the trend over the last 30 days") == "trend"
        assert _rule_based_intent("daily spend this week") == "trend"

    def test_compare(self):
        assert _rule_based_intent("compare this month vs last month") == "compare"
        assert _rule_based_intent("month-over-month cost change") == "compare"
        assert _rule_based_intent("MoM cost delta") == "compare"

    def test_top_resources(self):
        assert _rule_based_intent("which VM is most expensive?") == "top_resources"
        assert _rule_based_intent("top 10 instances by cost") == "top_resources"

    def test_unknown_defaults_to_top_services(self):
        assert _rule_based_intent("???") == "top_services"


# ── Fallback narrative ─────────────────────────────────────────────────────────

class TestFallbackNarrative:
    def test_empty_data(self):
        narrative = _fallback_narrative("top_services", [])
        assert "No cost data" in narrative

    def test_top_services_mentions_service(self):
        data = [{"service": "Virtual Machines", "cost_eur": 1200.0}]
        narrative = _fallback_narrative("top_services", data)
        assert "Virtual Machines" in narrative
        assert "1200" in narrative or "1,200" in narrative

    def test_by_cloud_mentions_total(self):
        data = [
            {"cloud": "azure", "cost_eur": 800.0},
            {"cloud": "aws",   "cost_eur": 300.0},
        ]
        narrative = _fallback_narrative("by_cloud", data)
        assert "2" in narrative  # 2 providers

    def test_compare_mentions_delta(self):
        data = [
            {"period": "last", "cost_eur": 1000.0},
            {"period": "curr", "cost_eur": 1200.0},
        ]
        narrative = _fallback_narrative("compare", data)
        assert "200" in narrative or "+" in narrative

    def test_trend_mentions_days(self):
        data = [{"day": f"2024-01-{i:02d}", "cost_eur": 100.0} for i in range(1, 8)]
        narrative = _fallback_narrative("trend", data)
        assert "7" in narrative

    def test_top_resources_returns_string(self):
        data = [{"resource": "vm-prod-001", "cloud": "azure", "cost_eur": 500.0}]
        narrative = _fallback_narrative("top_resources", data)
        assert isinstance(narrative, str)
        assert len(narrative) > 0


# ══════════════════════════════════════════════════════════════════════════════
# answer_question — rule-based path
# ══════════════════════════════════════════════════════════════════════════════

class TestAnswerQuestionFallback:
    """Tests for the rule-based (no LLM) path."""

    def _mock_cosmos_rows(self) -> list[dict]:
        return [
            {"service_name": "Virtual Machines", "cost_eur": 1200.0},
            {"service_name": "App Service",      "cost_eur": 450.0},
        ]

    @pytest.mark.asyncio
    async def test_returns_nl_query_result(self):
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._mock_cosmos_rows()
            result = await answer_question("Which service costs the most?", "t-1")
        assert isinstance(result, NLQueryResult)

    @pytest.mark.asyncio
    async def test_fallback_flag_set(self):
        """When no OpenAI key is set, fallback should be True."""
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock):
            with patch("app.services.nl_query.get_settings") as mock_settings:
                mock_settings.return_value = MagicMock(
                    openai_api_key="",
                    cosmos_container_cost_records="cost_records",
                )
                result = await answer_question("top services", "t-1")
        assert result.fallback is True

    @pytest.mark.asyncio
    async def test_empty_question_returns_prompt(self):
        result = await answer_question("", "t-1")
        assert result.intent == "none"
        assert "Please provide" in result.narrative

    @pytest.mark.asyncio
    async def test_question_truncated(self):
        """Overly long questions are truncated to _MAX_QUESTION_LEN."""
        long_q = "x" * (_MAX_QUESTION_LEN + 200)
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock):
            result = await answer_question(long_q, "t-1")
        assert len(result.question) <= _MAX_QUESTION_LEN

    @pytest.mark.asyncio
    async def test_suggestions_not_empty(self):
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._mock_cosmos_rows()
            result = await answer_question("top services?", "t-1")
        assert isinstance(result.suggestions, list)
        assert len(result.suggestions) >= 1

    @pytest.mark.asyncio
    async def test_chart_type_bar_for_services(self):
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._mock_cosmos_rows()
            result = await answer_question("top services?", "t-1")
        assert result.chart_type == "bar"

    @pytest.mark.asyncio
    async def test_chart_type_line_for_trend(self):
        trend_rows = [
            {"day": f"2024-01-{i:02d}", "cost_eur": 100.0} for i in range(1, 8)
        ]
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = trend_rows
            result = await answer_question("show me the daily cost trend", "t-1")
        assert result.chart_type == "line"

    @pytest.mark.asyncio
    async def test_cosmos_error_graceful(self):
        from app.exceptions import CosmosError
        with patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = CosmosError("test")
            result = await answer_question("top services", "t-1")
        # Should still return a result (empty data, fallback narrative)
        assert isinstance(result, NLQueryResult)


# ══════════════════════════════════════════════════════════════════════════════
# answer_question — LLM path (mocked httpx)
# ══════════════════════════════════════════════════════════════════════════════

class TestAnswerQuestionLLM:
    """Smoke tests for the LLM function-calling path."""

    def _llm_tool_response(self, func_name: str, args: dict) -> dict:
        """Build a mock LLM response that calls a function."""
        return {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc123",
                        "function": {
                            "name": func_name,
                            "arguments": json.dumps(args),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }

    def _llm_narrative_response(self, text: str) -> dict:
        return {
            "choices": [{
                "message": {"content": text},
                "finish_reason": "stop",
            }]
        }

    @pytest.mark.asyncio
    async def test_llm_path_invoked_when_api_key_set(self):
        today = date.today()
        args = {"start": (today - timedelta(days=30)).isoformat(), "end": today.isoformat()}
        tool_resp = self._llm_tool_response("query_top_services", args)
        narrative_resp = self._llm_narrative_response(
            "Virtual Machines accounted for €1,200 (60%) of total spend last month."
        )

        mock_response_1 = MagicMock()
        mock_response_1.raise_for_status = MagicMock()
        mock_response_1.json = MagicMock(return_value=tool_resp)

        mock_response_2 = MagicMock()
        mock_response_2.raise_for_status = MagicMock()
        mock_response_2.json = MagicMock(return_value=narrative_resp)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=[mock_response_1, mock_response_2])

        cosmos_data = [{"service_name": "Virtual Machines", "cost_eur": 1200.0}]

        with patch("app.services.nl_query.get_settings") as mock_settings, \
             patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock, return_value=cosmos_data), \
             patch("app.services.nl_query.httpx.AsyncClient", return_value=mock_client):

            mock_settings.return_value = MagicMock(
                openai_api_key="sk-test",
                openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-4o",
                ai_analyst_max_tokens=700,
                cosmos_container_cost_records="cost_records",
            )
            result = await answer_question("Which service costs the most?", "t-1")

        assert result.fallback is False
        assert "Virtual Machines" in result.narrative
        assert result.intent == "top_services"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rule_based(self):
        """If the LLM call raises, we should fall back to rule-based without error."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("LLM timeout"))

        cosmos_data = [{"service_name": "VM", "cost_eur": 100.0}]

        with patch("app.services.nl_query.get_settings") as mock_settings, \
             patch("app.services.nl_query.cosmos.query_items", new_callable=AsyncMock, return_value=cosmos_data), \
             patch("app.services.nl_query.httpx.AsyncClient", return_value=mock_client):

            mock_settings.return_value = MagicMock(
                openai_api_key="sk-test",
                openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-4o",
                ai_analyst_max_tokens=700,
                cosmos_container_cost_records="cost_records",
            )
            result = await answer_question("top services", "t-1")

        # Fell back gracefully
        assert isinstance(result, NLQueryResult)
        assert result.fallback is True
