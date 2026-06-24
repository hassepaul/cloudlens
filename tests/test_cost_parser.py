"""
Tests for AzureCostClient._parse_cost_response — the column-mapping logic that
turns a Cost Management query response into normalized cost rows.

These guard the highest-risk integration point: the real API returns columns in
an order that varies by query, so the parser must be strictly name-based and
must never pull a value from the wrong position.

Run: pytest tests/test_cost_parser.py -v
"""
from __future__ import annotations
import os
import pytest

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.azure_cost import AzureCostClient
from app.exceptions import AzureAPIError


def _client():
    return AzureCostClient(
        subscription_id="sub-1", client_id="cid", client_secret="sec", tenant_id="tid"
    )


def _response(columns, rows):
    return {"properties": {"columns": [{"name": c} for c in columns], "rows": rows}}


# A realistic daily-grouped response (note: Cost first, UsageDate last — a
# different order than the old positional code assumed).
CANONICAL_COLUMNS = [
    "Cost", "UsageQuantity", "ResourceId", "ResourceGroupName", "ServiceName",
    "ResourceType", "ResourceLocation", "MeterCategory", "MeterSubCategory",
    "UnitOfMeasure", "Currency", "UsageDate",
]


class TestParseCostResponse:
    def test_canonical_row_maps_correctly(self):
        c = _client()
        row = [
            12.50, 3.0,
            "/subscriptions/sub-1/resourceGroups/RG-Prod/providers/Microsoft.Compute/virtualMachines/VM01",
            "RG-Prod", "Virtual Machines", "Microsoft.Compute/virtualMachines",
            "westeurope", "Compute", "D-series", "Hours", "EUR", 20260601,
        ]
        out = c._parse_cost_response(_response(CANONICAL_COLUMNS, [row]))
        assert len(out) == 1
        r = out[0]
        assert r["cost"] == 12.50
        assert r["quantity"] == 3.0
        assert r["resource_id"].endswith("/vm01")            # lower-cased
        assert r["resource_group"] == "rg-prod"              # lower-cased
        assert r["service_name"] == "Virtual Machines"
        assert r["location"] == "westeurope"
        assert r["currency"] == "EUR"
        assert r["date"] == "20260601"

    def test_reordered_columns_still_map_by_name(self):
        """The whole point: shuffle the column order, values must follow names."""
        c = _client()
        cols = [
            "UsageDate", "ServiceName", "Cost", "Currency", "ResourceId",
            "ResourceGroupName", "UsageQuantity", "ResourceType",
            "ResourceLocation", "MeterCategory", "MeterSubCategory", "UnitOfMeasure",
        ]
        row = [
            20260601, "Storage", 4.20, "USD",
            "/subscriptions/sub-1/resourceGroups/RG-Data/providers/Microsoft.Storage/storageAccounts/sa1",
            "RG-Data", 100.0, "Microsoft.Storage/storageAccounts",
            "northeurope", "Storage", "Blob", "GB-Month",
        ]
        out = c._parse_cost_response(_response(cols, [row]))[0]
        assert out["cost"] == 4.20
        assert out["currency"] == "USD"
        assert out["service_name"] == "Storage"
        assert out["quantity"] == 100.0
        assert out["date"] == "20260601"
        assert out["resource_group"] == "rg-data"

    def test_alias_pretax_cost_recognised(self):
        c = _client()
        cols = ["PreTaxCost", "UsageDate", "ResourceId"]
        out = c._parse_cost_response(_response(cols, [[9.99, 20260601, "/x/y/z"]]))[0]
        assert out["cost"] == 9.99

    def test_missing_optional_columns_default_safely(self):
        """A minimal response (only Cost + date) must not crash or misalign."""
        c = _client()
        cols = ["Cost", "UsageDate"]
        out = c._parse_cost_response(_response(cols, [[5.0, 20260601]]))[0]
        assert out["cost"] == 5.0
        assert out["date"] == "20260601"
        assert out["resource_id"] == ""        # absent → empty, not a wrong value
        assert out["service_name"] == ""
        assert out["currency"] == "EUR"        # sensible default

    def test_missing_cost_column_raises(self):
        c = _client()
        cols = ["UsageDate", "ResourceId"]
        with pytest.raises(AzureAPIError):
            c._parse_cost_response(_response(cols, [[20260601, "/x/y/z"]]))

    def test_missing_date_column_raises(self):
        c = _client()
        cols = ["Cost", "ResourceId"]
        with pytest.raises(AzureAPIError):
            c._parse_cost_response(_response(cols, [[5.0, "/x/y/z"]]))

    def test_null_and_short_rows_are_tolerated(self):
        c = _client()
        cols = CANONICAL_COLUMNS
        # row shorter than columns + a null cost value
        row = [None, None, "/x/y/z", "rg", "Svc"]
        out = c._parse_cost_response(_response(cols, [row]))[0]
        assert out["cost"] == 0.0
        assert out["quantity"] == 0.0
        assert out["resource_id"] == "/x/y/z"

    def test_empty_rows_returns_empty(self):
        c = _client()
        out = c._parse_cost_response(_response(CANONICAL_COLUMNS, []))
        assert out == []

    def test_non_numeric_cost_coerces_to_zero(self):
        c = _client()
        cols = ["Cost", "UsageDate"]
        out = c._parse_cost_response(_response(cols, [["not-a-number", 20260601]]))[0]
        assert out["cost"] == 0.0
