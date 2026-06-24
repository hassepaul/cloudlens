"""
CloudLens Router Tests
Exercises the FastAPI routers end-to-end with a mocked Cosmos DB layer.
These tests guard the query-construction logic (the layer where the
waste-filter precedence bug and the ORDER-BY-on-aggregate bugs lived).

Run: pytest tests/test_routers.py -v
"""
from __future__ import annotations
import os
from unittest.mock import AsyncMock, patch

import pytest

# ── Required settings must exist before importing the app ────────────────────
os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
os.environ.setdefault("SERVICE_BUS_NAMESPACE", "test.servicebus.windows.net")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("ENVIRONMENT", "development")

from fastapi.testclient import TestClient
from app.main import create_app

TENANT_ID = "00000000-0000-0000-0000-000000000001"
SUB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Matches INTERNAL_API_KEY set in the environment for the test run.
ADMIN_HEADERS = {"X-API-Key": os.environ["INTERNAL_API_KEY"]}


@pytest.fixture
def client():
    return TestClient(create_app())


# ══════════════════════════════════════════════════════════════════════════════
# TENANTS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestTenantsRouter:
    def test_list_tenants_empty(self, client):
        with patch("app.services.cosmos.query_items", new=AsyncMock(return_value=[])):
            resp = client.get("/api/v1/tenants/", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_tenants_returns_configs(self, client):
        doc = {
            "id": TENANT_ID, "type": "tenant", "tenant_name": "Acme",
            "subscription_ids": [SUB_ID], "plan_tier": "growth",
            "alert_email": "ops@acme.com", "active": True,
            "sp_secret_ref": f"sp-creds-{TENANT_ID}",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        with patch("app.services.cosmos.query_items", new=AsyncMock(return_value=[doc])):
            resp = client.get("/api/v1/tenants/", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["tenant_name"] == "Acme"

    def test_create_tenant_stores_secret_and_config(self, client):
        captured = {}

        async def fake_query(container, query, parameters=None, **kw):
            return []  # no duplicate name

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        async def fake_store(tenant_id, client_id, client_secret, azure_tenant_id):
            captured["secret_tenant_id"] = tenant_id
            return f"sp-creds-{tenant_id}"

        payload = {
            "tenant_name": "New Corp",
            "subscription_ids": [SUB_ID],
            "plan_tier": "starter",
            "alert_email": "ops@newcorp.com",
            "active": True,
            "sp_client_id": "client-123",
            "sp_client_secret": "secret-456",
            "sp_tenant_id": "tenant-789",
        }
        with patch("app.services.cosmos.query_items", new=fake_query), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert), \
             patch("app.services.keyvault.store_sp_credentials", new=fake_store):
            resp = client.post("/api/v1/tenants/", json=payload, headers=ADMIN_HEADERS)

        assert resp.status_code == 201
        # The secret name and the stored document id must be the same UUID
        assert captured["item"]["id"] == captured["secret_tenant_id"]
        assert captured["item"]["sp_secret_ref"] == f"sp-creds-{captured['item']['id']}"

    def test_create_tenant_rejects_duplicate_name(self, client):
        async def fake_query(container, query, parameters=None, **kw):
            return [{"id": "existing"}]  # duplicate found

        payload = {
            "tenant_name": "Dup Corp",
            "subscription_ids": [SUB_ID],
            "plan_tier": "starter",
            "alert_email": "ops@dup.com",
            "active": True,
            "sp_client_id": "x", "sp_client_secret": "y", "sp_tenant_id": "z",
        }
        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.post("/api/v1/tenants/", json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 409

    def test_create_tenant_rejects_bad_subscription_id(self, client):
        payload = {
            "tenant_name": "Bad Corp",
            "subscription_ids": ["not-a-uuid"],
            "plan_tier": "starter",
            "alert_email": "ops@bad.com",
            "active": True,
            "sp_client_id": "x", "sp_client_secret": "y", "sp_tenant_id": "z",
        }
        resp = client.post("/api/v1/tenants/", json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 422

    def test_get_tenant_not_found(self, client):
        from app.exceptions import NotFoundError
        with patch("app.services.cosmos.get_item", new=AsyncMock(side_effect=NotFoundError("nope"))):
            resp = client.get(f"/api/v1/tenants/{TENANT_ID}", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    def test_delete_tenant_is_soft_delete(self, client):
        doc = {
            "id": TENANT_ID, "type": "tenant", "tenant_name": "Acme",
            "subscription_ids": [SUB_ID], "plan_tier": "growth",
            "alert_email": "ops@acme.com", "active": True,
            "sp_secret_ref": "sp-creds-x",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        captured = {}

        async def fake_get(container, item_id, pk):
            return dict(doc)

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            resp = client.delete(f"/api/v1/tenants/{TENANT_ID}", headers=ADMIN_HEADERS)

        assert resp.status_code == 204
        # Soft-delete must set active=false, not remove the document
        assert captured["item"]["active"] is False


# ══════════════════════════════════════════════════════════════════════════════
# WASTE ROUTER — guards the precedence / tenant-isolation bug
# ══════════════════════════════════════════════════════════════════════════════

class TestWasteRouter:
    def _capture_query(self):
        captured = {}

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            captured["query"] = query
            captured["parameters"] = parameters
            captured["partition_key"] = partition_key
            return []

        return captured, fake_query

    def test_list_waste_scopes_to_partition_key(self, client):
        captured, fake_query = self._capture_query()
        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/waste/{TENANT_ID}")
        assert resp.status_code == 200
        # Tenant isolation: the query must be scoped by partition key
        assert captured["partition_key"] == TENANT_ID

    def test_unresolved_filter_is_parenthesised(self, client):
        """
        Regression guard: the resolved-at OR clause must be wrapped in parentheses,
        otherwise AND/OR precedence breaks tenant isolation.
        """
        captured, fake_query = self._capture_query()
        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/waste/{TENANT_ID}?priority=critical")
        assert resp.status_code == 200
        q = captured["query"]
        # The OR sub-clause must be parenthesised
        assert "(NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null)" in q
        # And the dangerous unparenthesised form must NOT appear
        assert "waste_item' AND NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null AND" not in q

    def test_resolve_waste_item_sets_resolved_fields(self, client):
        doc = {
            "id": "waste-1", "type": "waste_item", "tenant_id": TENANT_ID,
            "subscription_id": SUB_ID,
            "resource_id": "/sub/rg/vm", "resource_name": "vm", "resource_group": "rg",
            "waste_type": "idle_vm", "monthly_cost_eur": 100.0, "saving_eur": 80.0,
            "priority": "high", "recommendation": "Resize", "recommendation_it": "Ridimensiona",
        }
        captured = {}

        async def fake_get(container, item_id, pk):
            return dict(doc)

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            resp = client.patch(
                f"/api/v1/waste/{TENANT_ID}/waste-1/resolve",
                json={"resolved_by": "pablito@cloudlens.io"},
            )
        assert resp.status_code == 200
        assert captured["item"]["resolved_by"] == "pablito@cloudlens.io"
        assert captured["item"]["resolved_at"] is not None


# ══════════════════════════════════════════════════════════════════════════════
# COSTS ROUTER — guards the ORDER-BY-on-aggregate bug
# ══════════════════════════════════════════════════════════════════════════════

class TestCostsRouter:
    def test_summary_no_order_by_on_aggregate(self, client):
        queries = []

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            queries.append(query)
            if "GROUP BY c.service_name" in query:
                return [
                    {"service_name": "Virtual Machines", "total": 300.0},
                    {"service_name": "Storage", "total": 100.0},
                ]
            return [400.0]  # previous-period scalar

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/costs/{TENANT_ID}")
        assert resp.status_code == 200
        # Cosmos cannot ORDER BY an aggregate alias — query must not contain it
        group_query = next(q for q in queries if "GROUP BY c.service_name" in q)
        assert "ORDER BY total" not in group_query
        body = resp.json()
        assert body["total_cost_eur"] == 400.0
        # Results sorted client-side, descending
        assert body["top_services"][0]["service"] == "Virtual Machines"

    def test_summary_computes_change_pct(self, client):
        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "GROUP BY c.service_name" in query:
                return [{"service_name": "VMs", "total": 110.0}]
            return [100.0]  # previous period

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/costs/{TENANT_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["change_pct"] == 10.0

    def test_breakdown_rejects_invalid_dimension(self, client):
        resp = client.get(f"/api/v1/costs/{TENANT_ID}/breakdown?dimension=banana")
        assert resp.status_code == 422

    def test_breakdown_sorts_client_side(self, client):
        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            assert "ORDER BY total" not in query
            return [
                {"dim_value": "small", "total": 10.0},
                {"dim_value": "big", "total": 90.0},
            ]

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/costs/{TENANT_ID}/breakdown?dimension=service")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["label"] == "big"  # highest cost first

    def test_trend_returns_sorted_points(self, client):
        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return [
                {"record_date": "2026-06-03", "daily_cost": 30.0},
                {"record_date": "2026-06-01", "daily_cost": 10.0},
                {"record_date": "2026-06-02", "daily_cost": 20.0},
            ]

        with patch("app.services.cosmos.query_items", new=fake_query):
            resp = client.get(f"/api/v1/costs/{TENANT_ID}/trend?days=30")
        assert resp.status_code == 200
        body = resp.json()
        dates = [p["date"] for p in body["data_points"]]
        assert dates == sorted(dates)  # chronological
        assert body["peak_cost_eur"] == 30.0


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthRouter:
    def test_health_ok_when_cosmos_reachable(self, client):
        with patch("app.services.cosmos.query_items", new=AsyncMock(return_value=[0])):
            resp = client.get("/api/v1/health/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_health_degraded_when_cosmos_down(self, client):
        with patch("app.services.cosmos.query_items", new=AsyncMock(side_effect=Exception("boom"))):
            resp = client.get("/api/v1/health/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestReportsRouter:
    def test_generate_returns_202_and_pending(self, client):
        async def fake_upsert(container, item):
            return item

        with patch("app.services.cosmos.upsert_item", new=fake_upsert):
            resp = client.post(f"/api/v1/reports/{TENANT_ID}/generate")
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"

    def test_download_conflict_when_not_ready(self, client):
        doc = {
            "id": "report-1", "type": "report", "tenant_id": TENANT_ID,
            "period_start": "2026-05-01", "period_end": "2026-05-31",
            "status": "generating",
        }
        with patch("app.services.cosmos.get_item", new=AsyncMock(return_value=doc)):
            resp = client.get(f"/api/v1/reports/report-1/download?tenant_id={TENANT_ID}")
        assert resp.status_code == 409


# ══════════════════════════════════════════════════════════════════════════════
# CLOUD ENTITLEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCloudEntitlementEndpoints:
    """Verify the cloud add-on subscription flow: list, enable, disable."""

    def _azure_only_doc(self) -> dict:
        return {
            "id": TENANT_ID, "type": "tenant", "tenant_name": "Acme",
            "subscription_ids": [SUB_ID], "plan_tier": "growth",
            "alert_email": "ops@acme.com", "active": True,
            "sp_secret_ref": "sp-creds-x",
            "enabled_clouds": ["azure"],
            "cloud_accounts": {},
            "cloud_credential_refs": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    def test_list_clouds_returns_enabled_and_available_addons(self, client):
        with patch("app.services.cosmos.get_item",
                   new=AsyncMock(return_value=self._azure_only_doc())):
            resp = client.get(f"/api/v1/tenants/{TENANT_ID}/clouds", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled_clouds"] == ["azure"]
        assert body["is_multicloud"] is False
        assert "aws" in body["available_addons"]
        assert "azure" not in body["available_addons"]

    def test_enable_cloud_adds_to_enabled_list(self, client):
        captured = {}

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        payload = {
            "cloud": "aws",
            "account_ids": ["123456789012"],
            "credential_secret_ref": "aws-creds-acme",
        }
        with patch("app.services.cosmos.get_item",
                   new=AsyncMock(return_value=self._azure_only_doc())), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            resp = client.post(f"/api/v1/tenants/{TENANT_ID}/clouds",
                               json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 201
        body = resp.json()
        assert "aws" in body["enabled_clouds"]
        assert body["cloud_accounts"]["aws"] == ["123456789012"]
        assert body["cloud_credential_refs"]["aws"] == "aws-creds-acme"

    def test_enable_cloud_conflict_if_already_enabled(self, client):
        doc = self._azure_only_doc()
        doc["enabled_clouds"] = ["azure", "aws"]
        doc["cloud_accounts"] = {"aws": ["123456789012"]}
        doc["cloud_credential_refs"] = {"aws": "aws-creds-acme"}

        payload = {"cloud": "aws", "account_ids": ["123456789012"],
                   "credential_secret_ref": "aws-creds-acme"}
        with patch("app.services.cosmos.get_item", new=AsyncMock(return_value=doc)):
            resp = client.post(f"/api/v1/tenants/{TENANT_ID}/clouds",
                               json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 409

    def test_enable_cloud_rejects_azure_as_addon(self, client):
        """Azure is included by default and must not be passed as an add-on."""
        payload = {"cloud": "azure", "account_ids": ["sub-1"],
                   "credential_secret_ref": "some-ref"}
        resp = client.post(f"/api/v1/tenants/{TENANT_ID}/clouds",
                           json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 422

    def test_enable_cloud_rejects_unknown_cloud(self, client):
        payload = {"cloud": "notacloud", "account_ids": ["x"],
                   "credential_secret_ref": "ref"}
        resp = client.post(f"/api/v1/tenants/{TENANT_ID}/clouds",
                           json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 422

    def test_disable_cloud_removes_from_enabled_list(self, client):
        doc = self._azure_only_doc()
        doc["enabled_clouds"] = ["azure", "gcp"]
        doc["cloud_accounts"] = {"gcp": ["my-proj"]}
        doc["cloud_credential_refs"] = {"gcp": "gcp-creds"}
        captured = {}

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        with patch("app.services.cosmos.get_item", new=AsyncMock(return_value=doc)), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            resp = client.delete(f"/api/v1/tenants/{TENANT_ID}/clouds/gcp",
                                 headers=ADMIN_HEADERS)
        assert resp.status_code == 204
        assert "gcp" not in captured["item"]["enabled_clouds"]
        assert "gcp" not in captured["item"].get("cloud_accounts", {})

    def test_disable_azure_is_rejected(self, client):
        """Azure is the default cloud and cannot be disabled."""
        with patch("app.services.cosmos.get_item",
                   new=AsyncMock(return_value=self._azure_only_doc())):
            resp = client.delete(f"/api/v1/tenants/{TENANT_ID}/clouds/azure",
                                 headers=ADMIN_HEADERS)
        assert resp.status_code == 422

    def test_disable_cloud_not_enabled_returns_404(self, client):
        with patch("app.services.cosmos.get_item",
                   new=AsyncMock(return_value=self._azure_only_doc())):
            resp = client.delete(f"/api/v1/tenants/{TENANT_ID}/clouds/aws",
                                 headers=ADMIN_HEADERS)
        assert resp.status_code == 404

