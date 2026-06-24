"""
Tests for all five P0 ship-blocker features:
  P0-1  Multi-currency FX
  P0-2  Hourly near-realtime ingestion
  P0-3  Kubernetes pod-level cost allocation
  P0-4  Unit economics (cost per unit)
  P0-5  Push notifications (Slack / Teams / generic webhook)
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
from datetime import date, timedelta, timezone, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# P0-1  Multi-currency FX
# ══════════════════════════════════════════════════════════════════════════════

class TestFXService:
    """Unit tests for app.services.fx — no network calls."""

    def test_convert_identity(self):
        from app.services.fx import convert
        assert convert(100.0, 1.0) == pytest.approx(100.0)

    def test_convert_scales(self):
        from app.services.fx import convert
        assert convert(100.0, 1.1) == pytest.approx(110.0)

    @pytest.mark.asyncio
    async def test_get_rate_returns_1_for_eur(self):
        """EUR→EUR is always 1.0."""
        from app.services import fx
        rate = await fx.get_rate("EUR")
        assert rate == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_get_rate_caches_result(self):
        """Second call must reuse cache and not hit ECB."""
        from app.services import fx
        fx._cache.clear()
        fetch_calls: list[str] = []

        async def _fake_fetch(ccy: str) -> float:
            fetch_calls.append(ccy)
            return 1.25

        with patch.object(fx, "_fetch_rate_from_ecb", side_effect=_fake_fetch):
            r1 = await fx.get_rate("USD")
            r2 = await fx.get_rate("USD")

        assert r1 == pytest.approx(1.25)
        assert r2 == pytest.approx(1.25)
        assert len(fetch_calls) == 1, "Should only call ECB once — second call uses cache"

    @pytest.mark.asyncio
    async def test_get_rate_cache_expires(self):
        """Rate is re-fetched after TTL expires."""
        import time
        from app.services import fx
        fx._cache["GBP"] = (0.85, time.monotonic() - 7200)  # 2 h ago — expired
        fetch_calls: list[str] = []

        async def _fake_fetch(ccy: str) -> float:
            fetch_calls.append(ccy)
            return 0.87

        with patch.object(fx, "_fetch_rate_from_ecb", side_effect=_fake_fetch):
            rate = await fx.get_rate("GBP")

        assert rate == pytest.approx(0.87)
        assert len(fetch_calls) == 1

    @pytest.mark.asyncio
    async def test_get_rate_fallback_on_network_error(self):
        """On ECB failure, last known rate is returned; no exception raised."""
        import time
        from app.services import fx
        fx._cache["JPY"] = (160.0, time.monotonic() - 7200)  # stale but present

        # Patch the httpx client so the actual network call fails.
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=ConnectionError("ECB unreachable"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http
            rate = await fx.get_rate("JPY")

        assert rate == pytest.approx(160.0)

    @pytest.mark.asyncio
    async def test_get_rate_fallback_to_1_when_no_cached(self):
        """No cached value + ECB failure → returns 1.0 (safe default)."""
        from app.services import fx
        fx._cache.pop("SEK", None)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=ConnectionError("ECB unreachable"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http
            rate = await fx.get_rate("SEK")

        assert rate == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_prefetch_warms_cache(self):
        """prefetch() populates the cache for multiple currencies."""
        from app.services import fx
        fx._cache.clear()

        async def _fake_fetch(ccy: str) -> float:
            return {"USD": 1.1, "GBP": 0.85, "CHF": 0.95}.get(ccy, 1.0)

        with patch.object(fx, "_fetch_rate_from_ecb", side_effect=_fake_fetch):
            await fx.prefetch(["USD", "GBP", "CHF"])

        assert "USD" in fx._cache
        assert "GBP" in fx._cache


class TestFXRouter:
    """Integration tests against the FX API endpoints."""

    @pytest.fixture
    def client(self):
        import os
        os.environ.setdefault("AZURE_TENANT_ID", "test")
        os.environ.setdefault("AZURE_CLIENT_ID", "test")
        os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com:443/")
        os.environ.setdefault("KEY_VAULT_URL", "https://test.vault.azure.net/")
        os.environ.setdefault("INTERNAL_API_KEY", "test-key")
        os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
        os.environ.setdefault("KEY_VAULT_NAME", "testvault")
        from app.config import get_settings
        get_settings.cache_clear()
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def test_rates_endpoint_returns_rates(self, client):
        with patch("app.services.fx.get_rate", new_callable=AsyncMock, return_value=1.1):
            resp = client.get("/api/v1/fx/rates?currencies=USD")
        assert resp.status_code == 200
        data = resp.json()
        assert "rates" in data
        assert "USD" in data["rates"]

    def test_rates_rejects_unsupported_currency(self, client):
        resp = client.get("/api/v1/fx/rates?currencies=BADCCY")
        assert resp.status_code == 422

    def test_convert_endpoint(self, client):
        with patch("app.services.fx.get_rate", new_callable=AsyncMock, return_value=1.1):
            resp = client.get("/api/v1/fx/convert?amount=1000&currency=USD")
        assert resp.status_code == 200
        data = resp.json()
        assert data["amount_eur"] == pytest.approx(1000.0)
        assert data["currency"] == "USD"
        assert data["converted"] == pytest.approx(1100.0)


# ══════════════════════════════════════════════════════════════════════════════
# P0-2  CostRecord — estimated field + hourly ingest model
# ══════════════════════════════════════════════════════════════════════════════

class TestCostRecordModel:
    def test_estimated_defaults_to_false(self):
        from app.models.cost import CostRecord
        r = CostRecord(
            tenant_id="t1", subscription_id="s1",
            record_date=date.today(), service_name="VM",
            resource_id="/r", resource_group="rg", resource_name="myvm",
            cost_eur=10.0,
        )
        assert r.estimated is False
        assert r.extra == {}

    def test_estimated_can_be_true(self):
        from app.models.cost import CostRecord
        r = CostRecord(
            tenant_id="t1", subscription_id="s1",
            record_date=date.today(), service_name="VM",
            resource_id="/r", resource_group="rg", resource_name="myvm",
            cost_eur=10.0,
            estimated=True,
            extra={"source": "usage_aggregates"},
        )
        assert r.estimated is True
        assert r.extra["source"] == "usage_aggregates"

    def test_to_cosmos_includes_estimated(self):
        from app.models.cost import CostRecord
        r = CostRecord(
            tenant_id="t1", subscription_id="s1",
            record_date=date.today(), service_name="VM",
            resource_id="/r", resource_group="rg", resource_name="myvm",
            cost_eur=10.0,
            estimated=True,
        )
        doc = r.to_cosmos()
        assert doc["estimated"] is True
        assert doc["_partitionKey"] == "t1"


# ══════════════════════════════════════════════════════════════════════════════
# P0-3  Kubernetes pod-level cost allocation
# ══════════════════════════════════════════════════════════════════════════════

class TestK8sCostService:
    """Unit tests for the OpenCost normalisation logic (no network calls)."""

    def _raw_alloc(self, name: str, day: str = "2024-03-01", cost: float = 50.0) -> dict:
        return {
            "_day": day,
            "_aggregate_key": name,
            "totalCost": cost,
            "cpuCost": cost * 0.6,
            "ramCost": cost * 0.3,
            "pvCost": cost * 0.05,
            "networkCost": cost * 0.05,
            "properties": {
                "namespace": "production",
                "pod": f"pod-{name}",
                "deployment": f"deploy-{name}",
                "node": "node-1",
                "labels": {"team": "platform"},
            },
        }

    def test_normalise_creates_focus_records(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [self._raw_alloc("api-server", cost=100.0)]
        records = normalize_k8s_allocation("t1", "aks-prod", raw)
        assert len(records) == 1
        r = records[0]
        assert r.effective_cost == pytest.approx(100.0)
        assert r.tags["k8s_namespace"] == "production"
        assert r.tags["k8s_cluster"] == "aks-prod"

    def test_normalise_skips_zero_cost(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [self._raw_alloc("idle", cost=0.0)]
        records = normalize_k8s_allocation("t1", "aks-prod", raw)
        assert records == []

    def test_breakdown_by_namespace(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [
            self._raw_alloc("api", cost=100.0),
            self._raw_alloc("worker", cost=50.0),
        ]
        records = normalize_k8s_allocation("t1", "aks-prod", raw)
        bd = K8sCostBreakdown(records)
        ns_summary = bd.by_namespace()
        assert len(ns_summary) == 1
        assert ns_summary[0]["namespace"] == "production"
        assert ns_summary[0]["cost_eur"] == pytest.approx(150.0)
        assert ns_summary[0]["pct"] == pytest.approx(100.0)

    def test_breakdown_total(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [self._raw_alloc("x", cost=33.33), self._raw_alloc("y", cost=66.67)]
        records = normalize_k8s_allocation("t1", "aks-prod", raw)
        assert K8sCostBreakdown(records).total_eur() == pytest.approx(100.0, abs=0.01)

    def test_by_workload_sorted_descending(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [
            self._raw_alloc("cheap-svc", cost=10.0),
            self._raw_alloc("expensive-svc", cost=200.0),
        ]
        records = normalize_k8s_allocation("t1", "aks-prod", raw)
        wl = K8sCostBreakdown(records).by_workload()
        assert wl[0]["cost_eur"] >= wl[-1]["cost_eur"]

    @pytest.mark.asyncio
    async def test_opencost_client_paginates_by_day(self):
        """Client must issue one request per day in the requested window."""
        from app.services.k8s_cost import K8sCostClient
        call_count = 0

        async def _mock_get(url, params=None, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "code": 200,
                "data": [{"default/api-server": {
                    "_aggregate_key": "api-server",
                    "totalCost": 10.0, "cpuCost": 6.0, "ramCost": 3.0,
                    "pvCost": 0.5, "networkCost": 0.5,
                    "properties": {"namespace": "default", "pod": "p", "deployment": "d", "node": "n", "labels": {}},
                }}],
            }
            return resp

        client = K8sCostClient("http://opencost.local", "aks-1")
        start = date(2024, 3, 1)
        end = date(2024, 3, 3)  # 3 days

        with patch("httpx.AsyncClient") as mock_cls:
            mock_httpx = AsyncMock()
            mock_httpx.get = AsyncMock(side_effect=_mock_get)
            mock_httpx.__aenter__ = AsyncMock(return_value=mock_httpx)
            mock_httpx.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_httpx
            await client.get_allocation(start, end)

        assert call_count == 3


# ══════════════════════════════════════════════════════════════════════════════
# P0-4  Unit economics
# ══════════════════════════════════════════════════════════════════════════════

class TestUnitEconomicsService:
    """Unit tests for cost-per-unit computation (pure logic, no Cosmos)."""

    def _make_series(
        self,
        costs: dict[str, float],
        counts: dict[str, float],
        start: date,
        end: date,
    ) -> dict:
        """Run compute_cost_per_unit with mocked Cosmos returns."""
        import os
        os.environ.setdefault("AZURE_TENANT_ID", "test")
        os.environ.setdefault("AZURE_CLIENT_ID", "test")
        os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com:443/")
        os.environ.setdefault("KEY_VAULT_URL", "https://test.vault.azure.net/")
        os.environ.setdefault("INTERNAL_API_KEY", "test-key")
        os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
        os.environ.setdefault("KEY_VAULT_NAME", "testvault")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.services.unit_economics import compute_cost_per_unit

        metric_doc = {
            "id": "m1", "type": "unit_metric", "tenant_id": "t1",
            "name": "Active users", "unit_label": "user", "scope": {},
        }
        cost_rows = [{"record_date": d, "daily_cost": v} for d, v in costs.items()]
        datapoints = [{"date": d, "count": v} for d, v in counts.items()]

        with (
            patch("app.services.unit_economics.get_metric",
                  new_callable=AsyncMock, return_value=metric_doc),
            patch("app.services.unit_economics.list_datapoints",
                  new_callable=AsyncMock, return_value=datapoints),
            patch("app.services.unit_economics.cosmos.query_items",
                  new_callable=AsyncMock, return_value=cost_rows),
        ):
            return asyncio.get_event_loop().run_until_complete(
                compute_cost_per_unit("t1", "m1", start, end)
            )

    def test_cost_per_unit_basic(self):
        today = date.today()
        start = today - timedelta(days=6)
        costs = {(start + timedelta(i)).isoformat(): 100.0 for i in range(7)}
        counts = {(start + timedelta(i)).isoformat(): 1000.0 for i in range(7)}
        result = self._make_series(costs, counts, start, today)
        assert result["average_cost_per_unit_eur"] == pytest.approx(0.1, rel=1e-4)
        assert len(result["series"]) == 7

    def test_cost_per_unit_missing_count_becomes_null(self):
        today = date.today()
        start = today - timedelta(days=2)
        day0 = start.isoformat()
        day1 = (start + timedelta(1)).isoformat()
        day2 = (start + timedelta(2)).isoformat()
        costs = {day0: 100.0, day1: 100.0, day2: 100.0}
        counts = {day0: 500.0}  # only one day has counts
        result = self._make_series(costs, counts, start, today)
        pt0 = next(p for p in result["series"] if p["date"] == day0)
        pt1 = next(p for p in result["series"] if p["date"] == day1)
        assert pt0["cost_per_unit_eur"] == pytest.approx(0.2, rel=1e-4)
        assert pt1["cost_per_unit_eur"] is None

    def test_trend_increasing(self):
        today = date.today()
        start = today - timedelta(days=13)
        costs = {(start + timedelta(i)).isoformat(): float(100 + i * 10) for i in range(14)}
        counts = {(start + timedelta(i)).isoformat(): 1000.0 for i in range(14)}
        result = self._make_series(costs, counts, start, today)
        assert result["trend"] == "increasing"

    def test_trend_decreasing(self):
        today = date.today()
        start = today - timedelta(days=13)
        costs = {(start + timedelta(i)).isoformat(): float(200 - i * 10) for i in range(14)}
        counts = {(start + timedelta(i)).isoformat(): 1000.0 for i in range(14)}
        result = self._make_series(costs, counts, start, today)
        assert result["trend"] == "decreasing"


class TestDatapointValidation:
    """Tests for the Pydantic DatapointBatch model validation."""

    def test_valid_batch_passes(self):
        from app.routers.unit_economics import DatapointBatch
        batch = DatapointBatch(data=[
            {"date": "2024-03-01", "count": 1000},
            {"date": "2024-03-02", "count": 2000},
        ])
        assert len(batch.data) == 2

    def test_missing_date_raises(self):
        from pydantic import ValidationError
        from app.routers.unit_economics import DatapointBatch
        with pytest.raises(ValidationError):
            DatapointBatch(data=[{"count": 100}])

    def test_negative_count_raises(self):
        from pydantic import ValidationError
        from app.routers.unit_economics import DatapointBatch
        with pytest.raises(ValidationError):
            DatapointBatch(data=[{"date": "2024-03-01", "count": -1}])

    def test_bad_date_format_raises(self):
        from pydantic import ValidationError
        from app.routers.unit_economics import DatapointBatch
        with pytest.raises(ValidationError):
            DatapointBatch(data=[{"date": "01/03/2024", "count": 100}])


# ══════════════════════════════════════════════════════════════════════════════
# P0-5  Push notification delivery
# ══════════════════════════════════════════════════════════════════════════════

def _make_alert_event(**kwargs) -> Any:
    from app.models.alert import AlertEvent, AlertType, AlertSeverity
    defaults = dict(
        tenant_id="t1",
        rule_id="r1",
        rule_name="Budget 80%",
        alert_type=AlertType.BUDGET_BREACH,
        severity=AlertSeverity.HIGH,
        title="Budget 'prod' at 85%",
        title_it="Budget 'prod' all'85%",
        detail={"budget_id": "b1", "consumed_pct": 85},
        impact_eur=1000.0,
    )
    defaults.update(kwargs)
    return AlertEvent(**defaults)


def _make_alert_rule(**kwargs) -> Any:
    from app.models.alert import AlertRule, AlertType, AlertSeverity, AlertChannel
    defaults = dict(
        tenant_id="t1",
        name="Budget 80%",
        alert_type=AlertType.BUDGET_BREACH,
        threshold=80.0,
        severity=AlertSeverity.HIGH,
        channels=[AlertChannel.WEBHOOK],
        webhook_url="https://hooks.slack.com/services/T000/B000/xxxxxxxx",
    )
    defaults.update(kwargs)
    return AlertRule(**defaults)


class TestDeliveryURLDetection:
    def test_slack_url_detected(self):
        from app.services.alerts import _is_slack_url
        assert _is_slack_url("https://hooks.slack.com/services/T000/B000/xxx")
        assert not _is_slack_url("https://hooks.generic.io/incoming")

    def test_teams_url_detected(self):
        from app.services.alerts import _is_teams_url
        assert _is_teams_url("https://myorg.webhook.office.com/webhookb2/xxx")
        assert _is_teams_url("https://outlook.office.com/webhook/xxx")
        assert not _is_teams_url("https://hooks.slack.com/xxx")


class TestSlackPayload:
    def test_has_required_keys(self):
        from app.services.alerts import _build_slack_payload
        evt = _make_alert_event()
        payload = _build_slack_payload(evt)
        assert "text" in payload
        assert "attachments" in payload
        attachment = payload["attachments"][0]
        assert "color" in attachment
        assert "blocks" in attachment

    def test_critical_is_red(self):
        from app.models.alert import AlertSeverity
        from app.services.alerts import _build_slack_payload
        evt = _make_alert_event(severity=AlertSeverity.CRITICAL)
        payload = _build_slack_payload(evt)
        assert payload["attachments"][0]["color"] == "#FF0000"

    def test_title_in_fallback_text(self):
        from app.services.alerts import _build_slack_payload
        evt = _make_alert_event()
        payload = _build_slack_payload(evt)
        assert "Budget" in payload["text"]


class TestTeamsPayload:
    def test_has_required_keys(self):
        from app.services.alerts import _build_teams_payload
        evt = _make_alert_event()
        payload = _build_teams_payload(evt)
        assert payload["@type"] == "MessageCard"
        assert payload["@context"] == "http://schema.org/extensions"
        assert "sections" in payload
        assert len(payload["sections"]) > 0

    def test_facts_include_severity(self):
        from app.services.alerts import _build_teams_payload
        evt = _make_alert_event()
        payload = _build_teams_payload(evt)
        facts = payload["sections"][0]["facts"]
        names = [f["name"] for f in facts]
        assert "Severity" in names

    def test_colour_matches_severity(self):
        from app.models.alert import AlertSeverity
        from app.services.alerts import _build_teams_payload
        evt = _make_alert_event(severity=AlertSeverity.CRITICAL)
        assert _build_teams_payload(evt)["themeColor"] == "FF0000"


class TestGenericWebhookPayload:
    def test_fields_present(self):
        from app.services.alerts import _build_generic_payload
        evt = _make_alert_event()
        payload = _build_generic_payload(evt)
        for key in ("event_id", "tenant_id", "rule_id", "rule_name",
                    "alert_type", "severity", "title", "impact_eur"):
            assert key in payload, f"Missing key: {key}"

    def test_hmac_signature_correct(self):
        from app.services.alerts import _hmac_signature
        body = b'{"test": "payload"}'
        secret = "my-webhook-secret"
        sig = _hmac_signature(body, secret)
        assert sig.startswith("sha256=")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert sig == expected


class TestDeliveryRetry:
    """Test the retry logic in _post_with_retry."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        from app.services.alerts import _post_with_retry

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http

            result = await _post_with_retry("https://example.com/hook", {"key": "val"})

        assert result is True

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self):
        from app.services.alerts import _post_with_retry

        call_count = 0

        async def _flaky_post(url, content=None, headers=None):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200 if call_count >= 2 else 500
            return resp

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=_flaky_post)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await _post_with_retry(
                    "https://example.com/hook", {"k": "v"}, attempts=3
                )

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_false(self):
        from app.services.alerts import _post_with_retry

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await _post_with_retry(
                    "https://example.com/hook", {"k": "v"}, attempts=3
                )

        assert result is False


class TestDeliverRouting:
    """Verify deliver() routes to the right formatter."""

    @pytest.mark.asyncio
    async def test_deliver_slack_uses_slack_format(self):
        from app.services.alerts import deliver, _build_slack_payload
        evt = _make_alert_event()
        rule = _make_alert_rule(webhook_url="https://hooks.slack.com/services/X/Y/Z")
        captured: list[dict] = []

        async def _fake_post(url, payload, headers=None, *, attempts=3):
            captured.append(payload)
            return True

        with patch("app.services.alerts._post_with_retry", side_effect=_fake_post):
            result = await deliver(evt, rule)

        assert "slack" in result.delivered_channels
        # Slack payload has "attachments" key
        assert "attachments" in captured[0]

    @pytest.mark.asyncio
    async def test_deliver_teams_uses_message_card(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertChannel
        evt = _make_alert_event()
        rule = _make_alert_rule(
            webhook_url="https://myorg.webhook.office.com/webhookb2/xxxx",
        )
        captured: list[dict] = []

        async def _fake_post(url, payload, headers=None, *, attempts=3):
            captured.append(payload)
            return True

        with patch("app.services.alerts._post_with_retry", side_effect=_fake_post):
            result = await deliver(evt, rule)

        assert "teams" in result.delivered_channels
        assert captured[0]["@type"] == "MessageCard"

    @pytest.mark.asyncio
    async def test_deliver_generic_webhook_adds_hmac_header(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertRule, AlertType, AlertSeverity, AlertChannel
        evt = _make_alert_event()
        rule = AlertRule(
            tenant_id="t1", name="r", alert_type=AlertType.BUDGET_BREACH,
            threshold=80, severity=AlertSeverity.HIGH,
            channels=[AlertChannel.WEBHOOK],
            webhook_url="https://custom-webhook.example.com/inbound",
            webhook_secret="s3cr3t",
        )
        captured_headers: list[dict] = []

        async def _fake_post(url, payload, headers=None, *, attempts=3):
            captured_headers.append(headers or {})
            return True

        with patch("app.services.alerts._post_with_retry", side_effect=_fake_post):
            await deliver(evt, rule)

        assert "X-CloudLens-Signature" in captured_headers[0]
        assert captured_headers[0]["X-CloudLens-Signature"].startswith("sha256=")

    @pytest.mark.asyncio
    async def test_deliver_always_includes_in_app(self):
        from app.services.alerts import deliver
        from app.models.alert import AlertRule, AlertType, AlertSeverity, AlertChannel
        evt = _make_alert_event()
        # Rule with no webhook or email — in_app only
        rule = AlertRule(
            tenant_id="t1", name="r", alert_type=AlertType.BUDGET_BREACH,
            threshold=80, severity=AlertSeverity.HIGH,
            channels=[],
        )
        result = await deliver(evt, rule)
        assert "in_app" in result.delivered_channels

    @pytest.mark.asyncio
    async def test_deliver_records_failed_channel(self):
        from app.services.alerts import deliver
        evt = _make_alert_event()
        rule = _make_alert_rule(webhook_url="https://hooks.slack.com/services/X/Y/Z")

        async def _always_fails(url, payload, headers=None, *, attempts=3):
            return False

        with patch("app.services.alerts._post_with_retry", side_effect=_always_fails):
            result = await deliver(evt, rule)

        assert any("failed" in ch for ch in result.delivered_channels)
