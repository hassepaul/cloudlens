"""
Tests for Business Context Auto-Mapping.
Run: pytest tests/test_context_mapper.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("INTERNAL_API_KEY",       "test-key")
os.environ.setdefault("AZURE_TENANT_ID",        "t")
os.environ.setdefault("AZURE_CLIENT_ID",        "c")
os.environ.setdefault("COSMOS_ENDPOINT",        "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME",   "s")
os.environ.setdefault("KEY_VAULT_NAME",         "k")

from app.services.context_mapper import (
    _extract_product_from_tags,
    _extract_feature_from_tags,
    _extract_team_from_tags,
    _infer_product_from_name,
    map_context,
    ContextMapping,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _record(
    service: str = "Virtual Machines",
    cloud: str = "azure",
    cost: float = 100.0,
    tags: dict | None = None,
    resource_name: str = "",
    day_offset: int = 0,
) -> dict:
    return {
        "service_name": service,
        "provider_name": cloud,
        "charge_period_start": (date.today() - timedelta(days=day_offset)).isoformat(),
        "effective_cost": cost,
        "tags": tags or {},
        "resource_name": resource_name,
        "resource_id": resource_name,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Tag extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestTagExtraction:
    def test_product_tag_exact(self):
        assert _extract_product_from_tags({"product": "checkout"}) == "checkout"

    def test_product_tag_case_insensitive(self):
        assert _extract_product_from_tags({"Product": "Search"}) == "Search"

    def test_app_tag_fallback(self):
        assert _extract_product_from_tags({"app": "api-gateway"}) == "api-gateway"

    def test_application_tag(self):
        assert _extract_product_from_tags({"Application": "payments"}) == "payments"

    def test_no_product_tag_returns_none(self):
        assert _extract_product_from_tags({"env": "prod", "team": "platform"}) is None

    def test_empty_tags_returns_none(self):
        assert _extract_product_from_tags({}) is None

    def test_feature_tag(self):
        assert _extract_feature_from_tags({"feature": "dark-mode"}) == "dark-mode"

    def test_experiment_tag(self):
        assert _extract_feature_from_tags({"Experiment": "checkout-v2"}) == "checkout-v2"

    def test_no_feature_returns_none(self):
        assert _extract_feature_from_tags({"product": "search"}) is None

    def test_team_tag(self):
        assert _extract_team_from_tags({"team": "platform"}) == "platform"

    def test_owner_tag(self):
        assert _extract_team_from_tags({"Owner": "ml-team"}) == "ml-team"

    def test_squad_tag(self):
        assert _extract_team_from_tags({"squad": "growth"}) == "growth"


# ══════════════════════════════════════════════════════════════════════════════
# Name-based inference
# ══════════════════════════════════════════════════════════════════════════════

class TestNameInference:
    def test_k8s_namespace_pattern(self):
        product, method = _infer_product_from_name("namespaces/checkout/pod/api-1")
        assert product == "checkout"
        assert method == "k8s"

    def test_k8s_ns_shorthand(self):
        product, method = _infer_product_from_name("ns/payments/deployment/worker")
        assert product == "payments"
        assert method == "k8s"

    def test_generic_slug_extraction(self):
        product, method = _infer_product_from_name("recommendations-api-prod")
        assert product == "recommendations"  # first meaningful slug before dash
        assert method == "name"

    def test_generic_slug_filtered_out(self):
        # "api" alone is filtered (too generic)
        product, _ = _infer_product_from_name("api-prod-001")
        # Should either get a non-generic name or None
        if product:
            assert product not in {"api", "app", "svc", "web", "db"}

    def test_empty_name_returns_none(self):
        product, method = _infer_product_from_name("")
        assert product is None
        assert method == "none"


# ══════════════════════════════════════════════════════════════════════════════
# map_context (integration, mocked Cosmos)
# ══════════════════════════════════════════════════════════════════════════════

class TestMapContext:
    def _tagged_records(self) -> list[dict]:
        return [
            _record(cost=500.0, tags={"product": "checkout", "feature": "cart", "team": "payments"}),
            _record(cost=300.0, tags={"product": "checkout", "feature": "shipping"}),
            _record(cost=200.0, tags={"product": "search", "team": "discovery"}),
            _record(cost=100.0, tags={}),  # unattributed
        ]

    @pytest.mark.asyncio
    async def test_returns_context_mapping(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._tagged_records()
            result = await map_context("t-1")
        assert isinstance(result, ContextMapping)
        assert result.tenant_id == "t-1"

    @pytest.mark.asyncio
    async def test_attribution_pct_correct(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._tagged_records()
            result = await map_context("t-1")
        # 500+300+200=1000 attributed, 100 unattributed → 90.9%
        assert result.attribution_pct == pytest.approx(90.9, abs=0.1)
        assert result.unattributed_eur == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_products_sorted_by_cost(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._tagged_records()
            result = await map_context("t-1")
        costs = [p.cost_eur for p in result.products]
        assert costs == sorted(costs, reverse=True)

    @pytest.mark.asyncio
    async def test_features_grouped_under_product(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._tagged_records()
            result = await map_context("t-1")
        checkout = next((p for p in result.products if p.name == "checkout"), None)
        assert checkout is not None
        feature_names = [f.name for f in checkout.features]
        assert "cart" in feature_names

    @pytest.mark.asyncio
    async def test_k8s_inference(self):
        records = [
            _record(cost=400.0, resource_name="namespaces/recommendations/pod/api-1"),
            _record(cost=200.0, resource_name="namespaces/recommendations/pod/worker-2"),
        ]
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = records
            result = await map_context("t-1")
        assert any(p.name == "recommendations" for p in result.products)
        reco = next(p for p in result.products if p.name == "recommendations")
        assert reco.inference_method == "k8s"

    @pytest.mark.asyncio
    async def test_empty_records_zero_attribution(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            result = await map_context("t-1")
        assert result.total_cost_eur == 0.0
        assert result.attribution_pct == 0.0

    @pytest.mark.asyncio
    async def test_cosmos_error_graceful(self):
        from app.exceptions import CosmosError
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.side_effect = CosmosError("test")
            result = await map_context("t-1")
        assert result.total_cost_eur == 0.0

    @pytest.mark.asyncio
    async def test_inference_notes_contain_tag_count(self):
        with patch("app.services.context_mapper.cosmos.query_items", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = self._tagged_records()
            result = await map_context("t-1")
        notes = " ".join(result.inference_notes)
        assert "tag" in notes.lower()
