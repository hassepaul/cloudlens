"""
Tests for GenAI Cost Tracking
================================
Covers: pricing table, cost calculation, record building, ingestion,
        summarisation, model comparison, budget management, and all
        HTTP router endpoints.
"""
from __future__ import annotations

import os

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststorage")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("USD_TO_EUR", "0.92")

from dataclasses import asdict
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.genai_cost import (
    GenAIBudget,
    GenAISummary,
    GenAIUsageRecord,
    ModelComparison,
    ModelStats,
    _PRICING,
    _build_comparisons,
    _build_record,
    _compute_model_stats,
    _normalize_model,
    calculate_cost,
    check_budget_alerts,
    create_budget,
    delete_budget,
    get_summary,
    ingest_batch,
    ingest_usage,
    list_budgets,
    lookup_pricing,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

TENANT = "t-genai"


def _api_headers() -> dict:
    """Return auth headers using whatever INTERNAL_API_KEY is active at runtime."""
    from app.config import get_settings
    return {"X-API-Key": get_settings().internal_api_key}


@pytest.fixture()
def client():
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def _dummy_usage_row(
    provider="openai",
    model="gpt-4o-mini",
    input_tokens=500,
    output_tokens=200,
    cost_usd=0.0001,
    period_date=None,
    app_name="",
):
    return {
        "tenant_id": TENANT,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "total_cost_usd": cost_usd,
        "total_cost_eur": cost_usd * 0.92,
        "request_type": "chat",
        "quantity": 1,
        "duration_seconds": 0.0,
        "app_name": app_name,
        "environment": "prod",
        "user_id": "",
        "tags": {},
        "latency_ms": 0,
        "period_date": period_date or date.today().isoformat(),
        "recorded_at": "2025-01-01T00:00:00Z",
        "id": "rec-1",
        "deployment_name": "",
        "type": "genai_usage",
    }


# ── TestPricingTable ──────────────────────────────────────────────────────────

class TestPricingTable:
    def test_openai_gpt4o_has_pricing(self):
        assert "openai/gpt-4o" in _PRICING
        assert _PRICING["openai/gpt-4o"]["input"] == 2.50

    def test_all_pricing_entries_have_billing_key(self):
        for key, val in _PRICING.items():
            has_key = any(k in val for k in ("input", "per_image", "per_minute", "per_1m_chars"))
            assert has_key, f"{key} has no recognised billing key"

    def test_dall_e_3_per_image(self):
        assert "per_image" in _PRICING["openai/dall-e-3"]
        assert _PRICING["openai/dall-e-3"]["per_image"] == 0.040

    def test_whisper_per_minute(self):
        assert "per_minute" in _PRICING["openai/whisper-1"]
        assert _PRICING["openai/whisper-1"]["per_minute"] == 0.006

    def test_tts_per_1m_chars(self):
        assert "per_1m_chars" in _PRICING["openai/tts-1"]

    def test_bedrock_claude_opus_more_expensive_than_haiku(self):
        opus = _PRICING["bedrock/claude-3-opus"]["input"]
        haiku = _PRICING["bedrock/claude-3-haiku"]["input"]
        assert opus > haiku

    def test_vertex_gemini_flash_cheaper_than_pro(self):
        flash = _PRICING["vertex_ai/gemini-1.5-flash"]["input"]
        pro = _PRICING["vertex_ai/gemini-1.5-pro"]["input"]
        assert flash < pro


# ── TestModelNormalization ────────────────────────────────────────────────────

class TestModelNormalization:
    def test_strip_date_suffix(self):
        assert _normalize_model("gpt-4o-2024-11-20") == "gpt-4o"

    def test_strip_8digit_suffix(self):
        assert _normalize_model("gpt-4o-20241022") == "gpt-4o"

    def test_already_clean(self):
        assert _normalize_model("gpt-4o-mini") == "gpt-4o-mini"

    def test_known_model_after_normalise(self):
        pricing = lookup_pricing("openai", "gpt-4o-2024-11-20")
        assert pricing.get("input") == 2.50


# ── TestCostCalculation ───────────────────────────────────────────────────────

class TestCostCalculation:
    def test_gpt4o_token_cost(self):
        in_c, out_c = calculate_cost("openai", "gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000)
        assert in_c == pytest.approx(2.50, rel=1e-3)
        assert out_c == pytest.approx(10.00, rel=1e-3)

    def test_gpt4o_mini_cheaper(self):
        in_c, out_c = calculate_cost("openai", "gpt-4o-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert in_c == pytest.approx(0.15, rel=1e-3)
        assert out_c == pytest.approx(0.60, rel=1e-3)

    def test_custom_price_override(self):
        in_c, out_c = calculate_cost(
            "custom", "my-model",
            input_tokens=1_000_000, output_tokens=1_000_000,
            custom_input_price=1.0, custom_output_price=2.0,
        )
        assert in_c == pytest.approx(1.0)
        assert out_c == pytest.approx(2.0)

    def test_unknown_model_returns_zero(self):
        in_c, out_c = calculate_cost("openai", "not-a-real-model-xyz", input_tokens=1000)
        assert in_c == 0.0
        assert out_c == 0.0

    def test_dall_e_per_image(self):
        in_c, out_c = calculate_cost("openai", "dall-e-3", quantity=3)
        assert in_c == pytest.approx(0.040 * 3, rel=1e-3)
        assert out_c == 0.0

    def test_whisper_per_minute(self):
        in_c, out_c = calculate_cost("openai", "whisper-1", duration_seconds=120.0)
        assert in_c == pytest.approx(0.006 * 2.0, rel=1e-3)   # 120s = 2 minutes
        assert out_c == 0.0

    def test_tts_per_1m_chars(self):
        in_c, _ = calculate_cost("openai", "tts-1", input_tokens=1_000_000)
        assert in_c == pytest.approx(15.0, rel=1e-3)


# ── TestRecordBuilding ────────────────────────────────────────────────────────

class TestRecordBuilding:
    def test_record_structure(self):
        rec = _build_record(TENANT, {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "input_tokens": 500,
            "output_tokens": 200,
        })
        assert isinstance(rec, GenAIUsageRecord)
        assert rec.tenant_id == TENANT
        assert rec.provider == "openai"
        assert rec.model == "gpt-4o-mini"
        assert rec.total_tokens == 700

    def test_tenant_scoping(self):
        rec = _build_record("my-tenant", {"provider": "openai", "model": "gpt-4o-mini"})
        assert rec.tenant_id == "my-tenant"

    def test_period_date_set(self):
        rec = _build_record(TENANT, {"provider": "openai", "model": "gpt-4o-mini"})
        # Default period_date is "today" — accept both the UTC and local date so
        # the assertion is stable across the UTC/local midnight boundary.
        from datetime import datetime, timezone
        today_local = date.today().isoformat()
        today_utc = datetime.now(timezone.utc).date().isoformat()
        assert rec.period_date in (today_local, today_utc)

    def test_eur_conversion_applied(self):
        rec = _build_record(TENANT, {
            "provider": "openai", "model": "gpt-4o",
            "input_tokens": 1_000_000, "output_tokens": 0,
        })
        assert rec.total_cost_usd > 0
        assert rec.total_cost_eur == pytest.approx(rec.total_cost_usd * 0.92, rel=1e-2)

    def test_caller_supplied_cost_honoured(self):
        rec = _build_record(TENANT, {
            "provider": "custom", "model": "unknown",
            "total_cost_usd": 99.99,
        })
        assert rec.total_cost_usd == pytest.approx(99.99)

    def test_to_cosmos_has_type(self):
        rec = _build_record(TENANT, {"provider": "openai", "model": "gpt-4o-mini"})
        doc = rec.to_cosmos()
        assert doc["type"] == "genai_usage"
        assert doc["_partitionKey"] == TENANT


# ── TestUsageIngestion ────────────────────────────────────────────────────────

class TestUsageIngestion:
    @pytest.mark.asyncio
    async def test_ingest_usage_calls_upsert(self):
        with patch("app.services.genai_cost.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert:
            rec = await ingest_usage(TENANT, {"provider": "openai", "model": "gpt-4o-mini", "input_tokens": 100, "output_tokens": 50})
            assert mock_upsert.called
            assert isinstance(rec, GenAIUsageRecord)

    @pytest.mark.asyncio
    async def test_ingest_batch_returns_counts(self):
        with patch("app.services.genai_cost.cosmos.upsert_item", new_callable=AsyncMock):
            result = await ingest_batch(TENANT, [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "openai", "model": "gpt-4o"},
            ])
        assert result["ingested"] == 2
        assert result["failed"] == 0
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_ingest_batch_records_failures(self):
        call_count = 0

        async def flaky_upsert(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("Cosmos unavailable")

        with patch("app.services.genai_cost.cosmos.upsert_item", side_effect=flaky_upsert):
            result = await ingest_batch(TENANT, [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "openai", "model": "gpt-4o"},
                {"provider": "openai", "model": "gpt-3.5-turbo"},
            ])
        assert result["total"] == 3
        assert result["ingested"] + result["failed"] == 3

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_record(self):
        with patch("app.services.genai_cost.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert:
            await ingest_usage("tenant-abc", {"provider": "openai", "model": "gpt-4o-mini"})
            _, kwargs = mock_upsert.call_args
            doc = mock_upsert.call_args.args[1]
            assert doc["tenant_id"] == "tenant-abc"


# ── TestGenAIFocusConsolidation ───────────────────────────────────────────────
# GenAI usage must also flow into the shared FOCUS cost_records container so it
# appears in the consolidated explorer / multi-cloud / allocation / forecast
# views — the "first-class GenAI cost" + "FOCUS-native consolidation" wedges.

class TestGenAIFocusConsolidation:
    @pytest.mark.asyncio
    async def test_emits_focus_record_alongside_usage(self):
        from app.services.genai_cost import _focus_container, _usage_container
        captured: list = []

        async def cap(container, doc):
            captured.append((container, doc))

        with patch("app.services.genai_cost.cosmos.upsert_item", side_effect=cap):
            await ingest_usage(TENANT, {
                "provider": "bedrock", "model": "claude-3-5-sonnet",
                "input_tokens": 1000, "output_tokens": 500,
                "app_name": "chatbot", "tags": {"cost_center": "ml-platform"},
            })

        usage = [d for c, d in captured if c == _usage_container()]
        focus = [d for c, d in captured if c == _focus_container()]
        assert len(usage) == 1
        assert len(focus) == 1
        f = focus[0]
        assert f["type"] == "focus_record"
        assert f["service_category"] == "AI and Machine Learning"
        assert f["provider_name"] == "Amazon Web Services"
        assert f["service_name"].startswith("AWS Bedrock ·")
        assert f["resource_type"] == "genai"
        assert f["tags"]["source"] == "genai_meter"
        # allocation/chargeback dimension is preserved so GenAI is charged back
        assert f["tags"]["cost_center"] == "ml-platform"
        # legacy-compat aliases for the cost summary / forecast views
        assert f["record_date"] == f["charge_period_start"]
        assert f["cost_eur"] == f["effective_cost"]

    @pytest.mark.asyncio
    async def test_provider_mapping_to_focus_providers(self):
        from app.services.genai_cost import _focus_container
        expected = {
            "openai": "OpenAI",
            "azure_openai": "Microsoft Azure",
            "bedrock": "Amazon Web Services",
            "vertex_ai": "Google Cloud",
        }
        for prov, provider_name in expected.items():
            captured: list = []

            async def cap(container, doc, _cap=captured):
                _cap.append((container, doc))

            with patch("app.services.genai_cost.cosmos.upsert_item", side_effect=cap):
                await ingest_usage(TENANT, {"provider": prov, "model": "m",
                                            "input_tokens": 10, "output_tokens": 10})
            focus = [d for c, d in captured if c == _focus_container()]
            assert focus, f"no FOCUS record for {prov}"
            assert focus[0]["provider_name"] == provider_name

    @pytest.mark.asyncio
    async def test_custom_provider_not_consolidated(self):
        # Self-hosted ("custom") infra cost is already Compute in the cloud bill;
        # emitting an AI/ML FOCUS line would double-count, so it is skipped.
        from app.services.genai_cost import _focus_container
        captured: list = []

        async def cap(container, doc):
            captured.append((container, doc))

        with patch("app.services.genai_cost.cosmos.upsert_item", side_effect=cap):
            await ingest_usage(TENANT, {"provider": "custom", "model": "llama-self",
                                        "input_tokens": 10, "output_tokens": 10,
                                        "total_cost_usd": 0.01})
        focus = [d for c, d in captured if c == _focus_container()]
        assert focus == []

    @pytest.mark.asyncio
    async def test_focus_emit_failure_does_not_drop_usage(self):
        from app.services.genai_cost import _focus_container

        async def cap(container, doc):
            if container == _focus_container():
                raise RuntimeError("cosmos down")

        with patch("app.services.genai_cost.cosmos.upsert_item", side_effect=cap):
            rec = await ingest_usage(TENANT, {"provider": "openai", "model": "gpt-4o"})
        # usage still returned; the FOCUS failure was swallowed
        assert rec is not None
        assert rec.provider == "openai"


# ── TestModelStats ────────────────────────────────────────────────────────────

class TestModelStats:
    def test_groups_by_model(self):
        rows = [
            _dummy_usage_row("openai", "gpt-4o", 500, 200, 0.002),
            _dummy_usage_row("openai", "gpt-4o", 600, 300, 0.003),
            _dummy_usage_row("openai", "gpt-4o-mini", 100, 50, 0.0001),
        ]
        stats = _compute_model_stats(rows)
        models = [s.model for s in stats]
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models

    def test_sorted_by_cost_desc(self):
        rows = [
            _dummy_usage_row("openai", "gpt-4o-mini", 100, 50, 0.0001),
            _dummy_usage_row("openai", "gpt-4o", 500, 200, 0.005),
        ]
        stats = _compute_model_stats(rows)
        assert stats[0].model == "gpt-4o"

    def test_blended_rate_calculation(self):
        # 1 request, 1000 input + 500 output = 1500 total tokens, cost $0.003
        rows = [_dummy_usage_row("openai", "gpt-4o", 1000, 500, 0.003)]
        stats = _compute_model_stats(rows)
        assert len(stats) == 1
        assert stats[0].total_tokens == 1500
        expected_blended = 0.003 / 1500 * 1_000_000
        assert stats[0].blended_cost_per_1m_tokens_usd == pytest.approx(expected_blended, rel=1e-3)

    def test_avg_tokens_per_request(self):
        rows = [
            _dummy_usage_row("openai", "gpt-4o", 400, 200, 0.001),  # 600 tokens
            _dummy_usage_row("openai", "gpt-4o", 800, 400, 0.002),  # 1200 tokens
        ]
        stats = _compute_model_stats(rows)
        assert len(stats) == 1
        assert stats[0].avg_tokens_per_request == pytest.approx(900.0, rel=1e-3)


# ── TestModelComparison ───────────────────────────────────────────────────────

class TestModelComparison:
    def _ms(self, provider, model, cost, input_tok=1_000_000, output_tok=500_000):
        tok = input_tok + output_tok
        return ModelStats(
            provider=provider, model=model,
            total_requests=1000,
            total_input_tokens=input_tok, total_output_tokens=output_tok,
            total_tokens=tok, total_cost_usd=cost, total_cost_eur=cost * 0.92,
            avg_cost_per_request_usd=cost / 1000,
            avg_tokens_per_request=tok / 1000,
            blended_cost_per_1m_tokens_usd=cost / tok * 1_000_000,
        )

    def test_gpt4o_comparison_generated(self):
        ms = self._ms("openai", "gpt-4o", 50.0)
        cmps = _build_comparisons([ms])
        assert len(cmps) >= 1
        assert cmps[0].alternative_model == "gpt-4o-mini"

    def test_saving_pct_positive(self):
        ms = self._ms("openai", "gpt-4o", 50.0)
        cmps = _build_comparisons([ms])
        assert cmps[0].saving_pct > 0

    def test_low_cost_model_skipped(self):
        # cost < $1 → no comparison generated
        ms = self._ms("openai", "gpt-4o", 0.50)
        cmps = _build_comparisons([ms])
        assert len(cmps) == 0

    def test_no_comparison_for_cheapest_model(self):
        # gpt-4o-mini has no defined alternative
        ms = self._ms("openai", "gpt-4o-mini", 10.0)
        cmps = _build_comparisons([ms])
        assert len(cmps) == 0


# ── TestSummaryComputation ────────────────────────────────────────────────────

class TestSummaryComputation:
    @pytest.mark.asyncio
    async def test_summary_structure(self):
        rows = [_dummy_usage_row("openai", "gpt-4o-mini", 500, 200, 0.0001)]
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=rows):
            summary = await get_summary(TENANT, period_days=30)
        assert isinstance(summary, GenAISummary)
        assert summary.tenant_id == TENANT
        assert summary.total_cost_usd == pytest.approx(0.0001)

    @pytest.mark.asyncio
    async def test_by_provider_grouping(self):
        rows = [
            _dummy_usage_row("openai", "gpt-4o", 500, 200, 0.005),
            _dummy_usage_row("bedrock", "claude-3-5-haiku", 500, 200, 0.002),
        ]
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=rows):
            summary = await get_summary(TENANT, period_days=30)
        providers = [p["provider"] for p in summary.by_provider]
        assert "openai" in providers
        assert "bedrock" in providers

    @pytest.mark.asyncio
    async def test_daily_trend_computation(self):
        rows = [
            _dummy_usage_row(period_date="2025-01-10", cost_usd=0.001),
            _dummy_usage_row(period_date="2025-01-11", cost_usd=0.002),
        ]
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=rows):
            summary = await get_summary(TENANT, period_days=30)
        dates = [d["date"] for d in summary.daily_trend]
        assert "2025-01-10" in dates
        assert "2025-01-11" in dates

    @pytest.mark.asyncio
    async def test_empty_data_returns_zeros(self):
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=[]):
            summary = await get_summary(TENANT, period_days=30)
        assert summary.total_cost_usd == 0.0
        assert summary.total_requests == 0
        assert summary.top_model == ""

    @pytest.mark.asyncio
    async def test_top_model_is_most_expensive(self):
        rows = [
            _dummy_usage_row("openai", "gpt-4o", 500, 200, 0.010),
            _dummy_usage_row("openai", "gpt-4o-mini", 500, 200, 0.001),
        ]
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=rows):
            summary = await get_summary(TENANT, period_days=30)
        assert summary.top_model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_cost_per_1m_tokens(self):
        # 1M tokens, $2.50 cost → $2.50/1M
        rows = [_dummy_usage_row(input_tokens=600_000, output_tokens=400_000, cost_usd=2.50)]
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=rows):
            summary = await get_summary(TENANT, period_days=30)
        assert summary.cost_per_1m_tokens_usd == pytest.approx(2.50, rel=1e-2)


# ── TestBudgetManagement ──────────────────────────────────────────────────────

class TestBudgetManagement:
    @pytest.mark.asyncio
    async def test_create_budget_stores_to_cosmos(self):
        with patch("app.services.genai_cost.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert:
            budget = await create_budget(TENANT, {"name": "Test Budget", "monthly_limit_usd": 100.0})
        assert isinstance(budget, GenAIBudget)
        assert budget.name == "Test Budget"
        assert mock_upsert.called

    @pytest.mark.asyncio
    async def test_list_budgets_returns_list(self):
        doc = {"id": "b-1", "tenant_id": TENANT, "name": "B1", "monthly_limit_usd": 50.0,
               "model_filter": "", "provider_filter": "", "app_filter": "", "alert_threshold_pct": 80.0,
               "created_at": "", "updated_at": "", "type": "genai_budget"}
        with patch("app.services.genai_cost.cosmos.query_items", new_callable=AsyncMock, return_value=[doc]):
            budgets = await list_budgets(TENANT)
        assert len(budgets) == 1
        assert budgets[0].name == "B1"

    @pytest.mark.asyncio
    async def test_delete_budget_returns_true(self):
        with patch("app.services.genai_cost.cosmos.delete_item", new_callable=AsyncMock):
            result = await delete_budget(TENANT, "b-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self):
        with patch("app.services.genai_cost.cosmos.delete_item", new_callable=AsyncMock, side_effect=Exception("Not found")):
            result = await delete_budget(TENANT, "nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_budget_alert_warning(self):
        budget_doc = {"id": "b-warn", "tenant_id": TENANT, "name": "Warn", "monthly_limit_usd": 100.0,
                      "model_filter": "", "provider_filter": "", "app_filter": "", "alert_threshold_pct": 80.0,
                      "created_at": "", "updated_at": "", "type": "genai_budget"}
        # 85 usage records × $1 = $85 → 85% of $100 → warning
        usage_rows = [_dummy_usage_row(cost_usd=1.0) for _ in range(85)]

        async def query_side_effect(container, sql, params, **kwargs):
            if "genai_budget" in container:
                return [budget_doc]
            return usage_rows

        with patch("app.services.genai_cost.cosmos.query_items", side_effect=query_side_effect):
            alerts = await check_budget_alerts(TENANT)
        active = [a for a in alerts if a.status != "ok"]
        assert len(active) == 1
        assert active[0].status == "warning"

    @pytest.mark.asyncio
    async def test_budget_alert_breach(self):
        budget_doc = {"id": "b-breach", "tenant_id": TENANT, "name": "Breach", "monthly_limit_usd": 50.0,
                      "model_filter": "", "provider_filter": "", "app_filter": "", "alert_threshold_pct": 80.0,
                      "created_at": "", "updated_at": "", "type": "genai_budget"}
        usage_rows = [_dummy_usage_row(cost_usd=1.0) for _ in range(55)]

        async def query_side_effect(container, sql, params, **kwargs):
            if "genai_budget" in container:
                return [budget_doc]
            return usage_rows

        with patch("app.services.genai_cost.cosmos.query_items", side_effect=query_side_effect):
            alerts = await check_budget_alerts(TENANT)
        breached = [a for a in alerts if a.status == "breach"]
        assert len(breached) == 1


# ── TestRouter ────────────────────────────────────────────────────────────────

class TestRouter:
    def test_post_usage_201(self, client):
        with patch("app.routers.genai_cost.ingest_usage", new_callable=AsyncMock) as mock_ingest:
            from app.services.genai_cost import _build_record
            mock_ingest.return_value = _build_record(TENANT, {
                "provider": "openai", "model": "gpt-4o-mini",
                "input_tokens": 500, "output_tokens": 200,
            })
            r = client.post(
                f"/api/v1/genai/{TENANT}/usage",
                json={"provider": "openai", "model": "gpt-4o-mini", "input_tokens": 500, "output_tokens": 200},
                headers=_api_headers(),
            )
        assert r.status_code == 201
        body = r.json()
        assert "id" in body
        assert "total_cost_usd" in body

    def test_post_usage_unknown_provider_422(self, client):
        r = client.post(
            f"/api/v1/genai/{TENANT}/usage",
            json={"provider": "fake_provider", "model": "gpt-4o-mini"},
            headers=_api_headers(),
        )
        assert r.status_code == 422

    def test_post_usage_batch_201(self, client):
        with patch("app.routers.genai_cost.ingest_batch", new_callable=AsyncMock,
                   return_value={"ingested": 2, "failed": 0, "total": 2}):
            r = client.post(
                f"/api/v1/genai/{TENANT}/usage/batch",
                json={"records": [
                    {"provider": "openai", "model": "gpt-4o-mini"},
                    {"provider": "openai", "model": "gpt-4o"},
                ]},
                headers=_api_headers(),
            )
        assert r.status_code == 201
        body = r.json()
        assert body["ingested"] == 2

    def test_get_summary_200(self, client):
        empty_summary = GenAISummary(
            tenant_id=TENANT, period_days=30, total_cost_usd=0.0,
            total_cost_eur=0.0, total_requests=0, total_tokens=0,
            by_provider=[], by_model=[], daily_trend=[],
            top_model="", cost_per_1m_tokens_usd=0.0, comparisons=[],
        )
        with patch("app.routers.genai_cost.get_summary", new_callable=AsyncMock, return_value=empty_summary):
            r = client.get(f"/api/v1/genai/{TENANT}/summary", headers=_api_headers())
        assert r.status_code == 200
        body = r.json()
        assert "total_cost_usd" in body

    def test_get_models_200(self, client):
        with patch("app.routers.genai_cost.get_model_breakdown", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/genai/{TENANT}/models", headers=_api_headers())
        assert r.status_code == 200
        assert r.json() == []

    def test_get_trends_200(self, client):
        with patch("app.routers.genai_cost.get_daily_trends", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/genai/{TENANT}/trends", headers=_api_headers())
        assert r.status_code == 200

    def test_get_apps_200(self, client):
        with patch("app.routers.genai_cost.get_top_apps", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/genai/{TENANT}/apps", headers=_api_headers())
        assert r.status_code == 200

    def test_post_budgets_201(self, client):
        mock_budget = GenAIBudget(
            id="b-test", tenant_id=TENANT, name="My Budget",
            monthly_limit_usd=100.0, created_at="2025-01-01T00:00:00Z",
        )
        with patch("app.routers.genai_cost.create_budget", new_callable=AsyncMock, return_value=mock_budget):
            r = client.post(
                f"/api/v1/genai/{TENANT}/budgets",
                json={"name": "My Budget", "monthly_limit_usd": 100.0},
                headers=_api_headers(),
            )
        assert r.status_code == 201
        assert r.json()["name"] == "My Budget"

    def test_get_budgets_200(self, client):
        with patch("app.routers.genai_cost.list_budgets", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/genai/{TENANT}/budgets", headers=_api_headers())
        assert r.status_code == 200
        assert r.json() == []

    def test_delete_budget_204(self, client):
        with patch("app.routers.genai_cost.delete_budget", new_callable=AsyncMock, return_value=True):
            r = client.delete(f"/api/v1/genai/{TENANT}/budgets/b-test", headers=_api_headers())
        assert r.status_code == 204

    def test_delete_missing_budget_404(self, client):
        with patch("app.routers.genai_cost.delete_budget", new_callable=AsyncMock, return_value=False):
            r = client.delete(f"/api/v1/genai/{TENANT}/budgets/nonexistent", headers=_api_headers())
        assert r.status_code == 404

    def test_get_alerts_200(self, client):
        with patch("app.routers.genai_cost.check_budget_alerts", new_callable=AsyncMock, return_value=[]):
            r = client.get(f"/api/v1/genai/{TENANT}/alerts", headers=_api_headers())
        assert r.status_code == 200
        assert r.json() == []

    def test_get_pricing_200(self, client):
        r = client.get(f"/api/v1/genai/{TENANT}/pricing", headers=_api_headers())
        assert r.status_code == 200
        body = r.json()
        assert "prices" in body
        assert "openai/gpt-4o" in body["prices"]

    def test_missing_api_key_401(self, client):
        r = client.get(f"/api/v1/genai/{TENANT}/summary")
        assert r.status_code == 401

    def test_summary_period_days_param(self, client):
        empty_summary = GenAISummary(
            tenant_id=TENANT, period_days=7, total_cost_usd=0.0,
            total_cost_eur=0.0, total_requests=0, total_tokens=0,
            by_provider=[], by_model=[], daily_trend=[],
            top_model="", cost_per_1m_tokens_usd=0.0, comparisons=[],
        )
        with patch("app.routers.genai_cost.get_summary", new_callable=AsyncMock, return_value=empty_summary) as mock_s:
            r = client.get(f"/api/v1/genai/{TENANT}/summary?period_days=7", headers=_api_headers())
        assert r.status_code == 200
        mock_s.assert_awaited_once_with(TENANT, period_days=7)
