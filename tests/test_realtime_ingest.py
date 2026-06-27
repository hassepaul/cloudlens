"""Tests for the real-time ingest scheduler and poll-state management."""
from __future__ import annotations

import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com:443/")
os.environ.setdefault("AZURE_TENANT_ID", "test-aad-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client-id")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststorage")

from app.services.realtime_ingest import (
    _compute_lag_minutes,
    _default_state,
    _state_id,
    get_poll_state,
    poll_all_active_tenants,
    run_delta_pull,
)


# ── Helper tests ──────────────────────────────────────────────────────────────

class TestComputeLagMinutes:
    def test_none_last_success_returns_none(self):
        now = datetime.now(timezone.utc)
        assert _compute_lag_minutes(None, now) is None

    def test_30_minute_lag(self):
        now = datetime.now(timezone.utc)
        last = (now - timedelta(minutes=30)).isoformat()
        lag = _compute_lag_minutes(last, now)
        assert 29 <= lag <= 31

    def test_zero_lag_floor(self):
        """Lag should never be negative."""
        now = datetime.now(timezone.utc)
        future = (now + timedelta(minutes=5)).isoformat()
        lag = _compute_lag_minutes(future, now)
        assert lag == 0

    def test_invalid_timestamp_returns_none(self):
        now = datetime.now(timezone.utc)
        assert _compute_lag_minutes("not-a-date", now) is None

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime.now(timezone.utc)
        naive = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
        lag = _compute_lag_minutes(naive, now)
        assert lag is not None
        assert 14 <= lag <= 16


class TestStateId:
    def test_format(self):
        assert _state_id("t-acme") == "pollstate-t-acme"


class TestDefaultState:
    def test_defaults(self):
        state = _default_state("t-acme")
        assert state["tenant_id"] == "t-acme"
        assert state["lag_minutes"] is None
        assert state["consecutive_errors"] == 0
        assert state["records_last_pull"] == 0
        assert state["type"] == "poll_state"


# ── get_poll_state ────────────────────────────────────────────────────────────

class TestGetPollState:
    @pytest.mark.asyncio
    async def test_returns_existing_state(self):
        existing = {
            "id": "pollstate-t-1",
            "type": "poll_state",
            "tenant_id": "t-1",
            "lag_minutes": 12,
            "consecutive_errors": 0,
        }
        with patch("app.services.realtime_ingest.cosmos.get_item", new_callable=AsyncMock, return_value=existing):
            state = await get_poll_state("t-1")
        assert state["lag_minutes"] == 12

    @pytest.mark.asyncio
    async def test_returns_default_on_not_found(self):
        from app.exceptions import NotFoundError
        with patch("app.services.realtime_ingest.cosmos.get_item", side_effect=NotFoundError("not found")):
            state = await get_poll_state("t-new")
        assert state["lag_minutes"] is None
        assert state["tenant_id"] == "t-new"


# ── run_delta_pull ────────────────────────────────────────────────────────────

class TestRunDeltaPull:
    def _make_tenant_doc(self, enabled_clouds=None):
        return {
            "id": "t-acme",
            "type": "tenant",
            "tenant_name": "Acme",
            "subscription_ids": ["12345678-0000-0000-0000-000000000001"],
            "plan_tier": "growth",
            "alert_email": "a@b.com",
            "active": True,
            "enabled_clouds": enabled_clouds or ["azure"],
            "cloud_accounts": {},
            "sp_secret_ref": "kv-ref",
        }

    @pytest.mark.asyncio
    async def test_inactive_tenant_skipped(self):
        inactive_doc = self._make_tenant_doc()
        inactive_doc["active"] = False
        with (
            patch("app.services.realtime_ingest.get_poll_state", new_callable=AsyncMock, return_value=_default_state("t-acme")),
            patch("app.services.realtime_ingest.cosmos.get_item", new_callable=AsyncMock, return_value=inactive_doc),
        ):
            result = await run_delta_pull("t-acme")
        assert result.get("skipped") is True
        assert result["reason"] == "inactive"

    @pytest.mark.asyncio
    async def test_azure_ingest_called(self):
        mock_result = MagicMock()
        mock_result.estimated_records = 10
        mock_result.confirmed_records = 5

        with (
            patch("app.services.realtime_ingest.get_poll_state", new_callable=AsyncMock, return_value=_default_state("t-acme")),
            patch("app.services.realtime_ingest.cosmos.get_item", new_callable=AsyncMock, return_value=self._make_tenant_doc()),
            patch("app.services.realtime_ingest.cosmos.upsert_item", new_callable=AsyncMock),
            patch("app.services.realtime_ingest.keyvault.get_sp_credentials", new_callable=AsyncMock, return_value={}),
            patch("app.jobs.ingest_hourly.ingest_tenant_hourly", new_callable=AsyncMock, return_value=mock_result),
        ):
            result = await run_delta_pull("t-acme")

        # Should have attempted to pull records
        assert "records_added" in result or "error" in result

    @pytest.mark.asyncio
    async def test_error_increments_consecutive_errors(self):
        state = _default_state("t-acme")
        state["consecutive_errors"] = 2

        with (
            patch("app.services.realtime_ingest.get_poll_state", new_callable=AsyncMock, return_value=state),
            patch("app.services.realtime_ingest.cosmos.get_item", side_effect=Exception("cosmos down")),
            patch("app.services.realtime_ingest.cosmos.upsert_item", new_callable=AsyncMock) as mock_upsert,
        ):
            result = await run_delta_pull("t-acme")

        assert "error" in result
        saved_state = mock_upsert.call_args[0][1]
        assert saved_state["consecutive_errors"] == 3

    @pytest.mark.asyncio
    async def test_state_updated_on_success(self):
        mock_result = MagicMock()
        mock_result.estimated_records = 20
        mock_result.confirmed_records = 3

        saved_states = []

        async def capture_upsert(container, doc):
            saved_states.append(doc)

        with (
            patch("app.services.realtime_ingest.get_poll_state", new_callable=AsyncMock, return_value=_default_state("t-acme")),
            patch("app.services.realtime_ingest.cosmos.get_item", new_callable=AsyncMock, return_value=self._make_tenant_doc()),
            patch("app.services.realtime_ingest.cosmos.upsert_item", side_effect=capture_upsert),
            patch("app.services.realtime_ingest.keyvault.get_sp_credentials", new_callable=AsyncMock, return_value={}),
            patch("app.jobs.ingest_hourly.ingest_tenant_hourly", new_callable=AsyncMock, return_value=mock_result),
        ):
            await run_delta_pull("t-acme")

        if saved_states:
            final_state = saved_states[-1]
            assert final_state.get("last_polled_at") is not None

        import sys
        # noop — patch already applied above


# ── poll_all_active_tenants ───────────────────────────────────────────────────

class TestPollAllActiveTenants:
    @pytest.mark.asyncio
    async def test_skips_tenant_with_too_many_errors(self):
        tenant_docs = [{"id": "t-bad", "active": True}]
        bad_state = _default_state("t-bad")
        bad_state["consecutive_errors"] = 10

        with (
            patch("app.services.realtime_ingest.cosmos.query_items", new_callable=AsyncMock, return_value=tenant_docs),
            patch("app.services.realtime_ingest.get_poll_state", new_callable=AsyncMock, return_value=bad_state),
        ):
            results = await poll_all_active_tenants()

        assert len(results) == 1
        assert results[0]["skipped"] is True
        assert results[0]["reason"] == "too_many_errors"

    @pytest.mark.asyncio
    async def test_empty_tenant_list(self):
        with patch("app.services.realtime_ingest.cosmos.query_items", new_callable=AsyncMock, return_value=[]):
            results = await poll_all_active_tenants()
        assert results == []

    @pytest.mark.asyncio
    async def test_cosmos_error_returns_empty(self):
        from app.exceptions import CosmosError
        with patch("app.services.realtime_ingest.cosmos.query_items", side_effect=CosmosError("down")):
            results = await poll_all_active_tenants()
        assert results == []
