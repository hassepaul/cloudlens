"""
Budget CRUD + status tests.
Run: pytest tests/test_budgets.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

TID = "t-1"


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


def _budget_doc(bid="b-1", name="Engineering", amount=10000, dim=None, val=None):
    return {
        "id": bid, "type": "budget", "tenant_id": TID, "name": name,
        "amount_eur": amount, "period": "monthly",
        "scope_dimension": dim, "scope_value": val, "warning_threshold_pct": 85,
        "created_at": "2026-06-01T00:00:00+00:00",
    }


class TestBudgetCRUD:
    def test_create_budget(self):
        captured = {}

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        payload = {"tenant_id": TID, "name": "Engineering", "amount_eur": 12000,
                   "scope_dimension": "cost_center", "scope_value": "engineering"}
        with patch("app.services.cosmos.upsert_item", new=fake_upsert):
            r = _client().post(f"/api/v1/budgets/{TID}", json=payload)
        assert r.status_code == 201
        assert captured["item"]["type"] == "budget"
        assert captured["item"]["amount_eur"] == 12000

    def test_create_rejects_tenant_mismatch(self):
        payload = {"tenant_id": "other", "name": "X", "amount_eur": 100}
        r = _client().post(f"/api/v1/budgets/{TID}", json=payload)
        assert r.status_code == 422

    def test_create_rejects_half_scope(self):
        payload = {"tenant_id": TID, "name": "X", "amount_eur": 100,
                   "scope_dimension": "cost_center"}  # value missing
        r = _client().post(f"/api/v1/budgets/{TID}", json=payload)
        assert r.status_code == 422

    def test_create_rejects_negative_amount(self):
        payload = {"tenant_id": TID, "name": "X", "amount_eur": -5}
        r = _client().post(f"/api/v1/budgets/{TID}", json=payload)
        assert r.status_code == 422

    def test_list_budgets(self):
        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return [_budget_doc(), _budget_doc("b-2", "Data", 5000)]

        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/budgets/{TID}")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_get_budget(self):
        async def fake_get(container, item_id, pk):
            return _budget_doc()

        with patch("app.services.cosmos.get_item", new=fake_get):
            r = _client().get(f"/api/v1/budgets/{TID}/b-1")
        assert r.status_code == 200
        assert r.json()["name"] == "Engineering"

    def test_get_budget_404(self):
        from app.exceptions import NotFoundError

        async def fake_get(container, item_id, pk):
            raise NotFoundError("nope")

        with patch("app.services.cosmos.get_item", new=fake_get):
            r = _client().get(f"/api/v1/budgets/{TID}/missing")
        assert r.status_code == 404

    def test_update_budget(self):
        captured = {}

        async def fake_get(container, item_id, pk):
            return _budget_doc()

        async def fake_upsert(container, item):
            captured["item"] = item
            return item

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            r = _client().patch(f"/api/v1/budgets/{TID}/b-1", json={"amount_eur": 15000})
        assert r.status_code == 200
        assert captured["item"]["amount_eur"] == 15000
        assert captured["item"]["name"] == "Engineering"  # unchanged

    def test_delete_budget(self):
        deleted = {}

        async def fake_get(container, item_id, pk):
            return _budget_doc()

        async def fake_delete(container, item_id, pk):
            deleted["id"] = item_id

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.delete_item", new=fake_delete):
            r = _client().delete(f"/api/v1/budgets/{TID}/b-1")
        assert r.status_code == 204
        assert deleted["id"] == "b-1"


def _daily(n=40, base=600):
    np.random.seed(3)
    start = date.today() - timedelta(days=n - 1)
    return [{"record_date": (start + timedelta(days=i)).isoformat(),
             "daily_cost": round(base + np.random.normal(0, 20), 2)} for i in range(n)]


class TestBudgetStatus:
    def test_tenant_budget_status(self):
        async def fake_get(container, item_id, pk):
            return _budget_doc(amount=25000)

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            return _daily()

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/budgets/{TID}/b-1/status")
        assert r.status_code == 200
        body = r.json()
        assert body["amount_eur"] == 25000
        assert body["spend_to_date_eur"] >= 0
        assert body["status"] in ("ok", "warning", "breach", "projected_breach")
        assert body["scope"] == "tenant"

    def test_scoped_budget_status(self):
        async def fake_get(container, item_id, pk):
            return _budget_doc(amount=5000, dim="cost_center", val="engineering")

        async def fake_query(container, query, parameters=None, partition_key=None, **kw):
            if "c.tags" in query:
                # scoped MTD query → tagged records
                return [{"cost_eur": 1000, "tags": {"cost_center": "engineering"}},
                        {"cost_eur": 800, "tags": {"cost_center": "engineering"}},
                        {"cost_eur": 500, "tags": {"cost_center": "marketing"}}]
            return _daily()

        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/budgets/{TID}/b-1/status")
        assert r.status_code == 200
        body = r.json()
        # only engineering records counted: 1000 + 800 = 1800
        assert body["spend_to_date_eur"] == 1800
        assert body["scope"] == "cost_center=engineering"
