"""Tests for the Terraform plan cost estimator."""
from __future__ import annotations

import json
import pytest

from app.services.cost_estimator import (
    CostEstimate,
    ResourceEstimate,
    _detect_provider,
    _lookup_price,
    _normalize_actions,
    catalog_summary,
    estimate_plan,
)


# ── Provider detection ────────────────────────────────────────────────────────

class TestDetectProvider:
    def test_aws(self):
        assert _detect_provider("aws_instance") == "aws"
        assert _detect_provider("aws_db_instance") == "aws"
        assert _detect_provider("aws_eks_node_group") == "aws"

    def test_azure(self):
        assert _detect_provider("azurerm_linux_virtual_machine") == "azure"
        assert _detect_provider("azurerm_sql_database") == "azure"
        assert _detect_provider("azurerm_kubernetes_cluster") == "azure"

    def test_gcp(self):
        assert _detect_provider("google_compute_instance") == "gcp"
        assert _detect_provider("google_sql_database_instance") == "gcp"
        assert _detect_provider("google_container_cluster") == "gcp"

    def test_unknown(self):
        assert _detect_provider("random_resource") == "unknown"


# ── Action normalisation ──────────────────────────────────────────────────────

class TestNormalizeActions:
    def test_create(self):
        assert _normalize_actions(["create"]) == "create"

    def test_delete(self):
        assert _normalize_actions(["delete"]) == "delete"

    def test_update(self):
        assert _normalize_actions(["update"]) == "update"

    def test_replace_is_delete_create(self):
        # Terraform replace = delete + create; create takes priority
        assert _normalize_actions(["delete", "create"]) == "create"

    def test_no_op(self):
        assert _normalize_actions(["no-op"]) == "no-op"


# ── Pricing lookup ────────────────────────────────────────────────────────────

class TestLookupPrice:
    def test_aws_t3_medium_exact(self):
        price, size, confidence = _lookup_price("aws_instance", {"instance_type": "t3.medium"})
        assert price == pytest.approx(30.37, abs=0.01)
        assert size == "t3.medium"
        assert confidence == "catalog"

    def test_aws_unknown_instance_type_uses_default(self):
        price, size, confidence = _lookup_price("aws_instance", {"instance_type": "t99.superlarge"})
        assert price > 0
        assert confidence == "default"

    def test_azure_vm_standard_d2sv5(self):
        price, size, confidence = _lookup_price(
            "azurerm_linux_virtual_machine", {"size": "Standard_D2s_v5"}
        )
        assert price == pytest.approx(72.26, abs=0.01)
        assert confidence == "catalog"

    def test_azure_windows_vm_has_surcharge(self):
        linux_price, _, _ = _lookup_price("azurerm_linux_virtual_machine", {"size": "Standard_D2s_v5"})
        win_price, _, _ = _lookup_price("azurerm_windows_virtual_machine", {"size": "Standard_D2s_v5"})
        assert win_price > linux_price

    def test_gcp_e2_standard_4(self):
        price, size, confidence = _lookup_price("google_compute_instance", {"machine_type": "e2-standard-4"})
        assert price == pytest.approx(97.84, abs=0.01)
        assert confidence == "catalog"

    def test_free_resource_returns_zero(self):
        price, _, confidence = _lookup_price("aws_s3_bucket", {})
        assert price == 0.0
        assert confidence == "free"

    def test_unsupported_type_returns_zero(self):
        price, _, confidence = _lookup_price("random_custom_resource", {})
        assert price == 0.0
        assert confidence == "unsupported"

    def test_rds_includes_storage(self):
        price_no_storage, _, _ = _lookup_price("aws_db_instance", {"instance_class": "db.t3.medium"})
        price_with_storage, _, _ = _lookup_price("aws_db_instance", {"instance_class": "db.t3.medium", "allocated_storage": 100})
        assert price_with_storage > price_no_storage

    def test_lambda_has_minimal_default_cost(self):
        price, _, confidence = _lookup_price("aws_lambda_function", {})
        assert price > 0
        assert confidence == "catalog"


# ── Plan estimation ───────────────────────────────────────────────────────────

def _make_plan(resource_changes: list[dict]) -> str:
    return json.dumps({"resource_changes": resource_changes})


def _resource_change(address, rtype, actions, after=None, before=None):
    return {
        "address": address,
        "type": rtype,
        "change": {
            "actions": actions,
            "after": after or {},
            "before": before or {},
        },
    }


class TestEstimatePlan:
    def test_create_aws_instance_adds_cost(self):
        plan = _make_plan([
            _resource_change("aws_instance.web", "aws_instance", ["create"],
                             after={"instance_type": "t3.medium"})
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur == pytest.approx(30.37, abs=0.01)
        assert result.total_resources_analyzed == 1

    def test_delete_reduces_cost(self):
        plan = _make_plan([
            _resource_change("aws_instance.old", "aws_instance", ["delete"],
                             before={"instance_type": "m5.large"})
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur < 0
        assert result.total_monthly_delta_eur == pytest.approx(-70.08, abs=0.01)

    def test_no_op_is_zero_delta(self):
        plan = _make_plan([
            _resource_change("aws_instance.x", "aws_instance", ["no-op"])
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur == 0.0

    def test_update_is_zero_delta(self):
        plan = _make_plan([
            _resource_change("azurerm_linux_virtual_machine.app", "azurerm_linux_virtual_machine",
                             ["update"], after={"size": "Standard_D4s_v5"})
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur == 0.0

    def test_mixed_creates_and_deletes(self):
        plan = _make_plan([
            _resource_change("aws_instance.new", "aws_instance", ["create"],
                             after={"instance_type": "t3.large"}),
            _resource_change("aws_instance.old", "aws_instance", ["delete"],
                             before={"instance_type": "t3.small"}),
        ])
        result = estimate_plan(plan)
        expected = 60.74 - 15.18
        assert result.total_monthly_delta_eur == pytest.approx(expected, abs=0.01)

    def test_breakdown_by_action(self):
        plan = _make_plan([
            _resource_change("aws_instance.a", "aws_instance", ["create"],
                             after={"instance_type": "t3.medium"}),
            _resource_change("aws_instance.b", "aws_instance", ["delete"],
                             before={"instance_type": "t3.medium"}),
        ])
        result = estimate_plan(plan)
        assert result.breakdown_by_action["create"] == pytest.approx(30.37, abs=0.01)
        assert result.breakdown_by_action["delete"] == pytest.approx(-30.37, abs=0.01)

    def test_free_resource_not_in_unsupported(self):
        plan = _make_plan([
            _resource_change("aws_s3_bucket.data", "aws_s3_bucket", ["create"])
        ])
        result = estimate_plan(plan)
        assert "aws_s3_bucket" not in result.unsupported_resource_types

    def test_unsupported_type_collected(self):
        plan = _make_plan([
            _resource_change("custom_thing.x", "custom_thing", ["create"])
        ])
        result = estimate_plan(plan)
        assert "custom_thing" in result.unsupported_resource_types

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            estimate_plan("not-json{{{")

    def test_missing_resource_changes_raises(self):
        with pytest.raises(ValueError, match="resource_changes"):
            estimate_plan(json.dumps({"other": "field"}))

    def test_empty_plan_is_zero(self):
        result = estimate_plan(_make_plan([]))
        assert result.total_monthly_delta_eur == 0.0
        assert result.total_resources_analyzed == 0

    def test_annual_delta_is_twelve_times_monthly(self):
        plan = _make_plan([
            _resource_change("aws_instance.x", "aws_instance", ["create"],
                             after={"instance_type": "m5.xlarge"})
        ])
        result = estimate_plan(plan)
        out = result.to_dict()
        assert out["total_annual_delta_eur"] == pytest.approx(
            out["total_monthly_delta_eur"] * 12, abs=0.01
        )

    def test_to_dict_excludes_no_ops(self):
        plan = _make_plan([
            _resource_change("aws_instance.a", "aws_instance", ["create"],
                             after={"instance_type": "t3.micro"}),
            _resource_change("aws_instance.b", "aws_instance", ["no-op"]),
        ])
        result = estimate_plan(plan)
        out = result.to_dict()
        addresses = [r["address"] for r in out["resources"]]
        assert "aws_instance.a" in addresses
        assert "aws_instance.b" not in addresses

    def test_azure_sql_sku_lookup(self):
        plan = _make_plan([
            _resource_change("azurerm_sql_database.prod", "azurerm_sql_database",
                             ["create"], after={"sku_name": "GP_Gen5_4"})
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur == pytest.approx(487.44, abs=0.01)

    def test_gcp_container_cluster_default(self):
        plan = _make_plan([
            _resource_change("google_container_cluster.main", "google_container_cluster",
                             ["create"])
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur > 0

    def test_multicloud_plan(self):
        plan = _make_plan([
            _resource_change("aws_instance.api", "aws_instance", ["create"],
                             after={"instance_type": "t3.medium"}),
            _resource_change("azurerm_linux_virtual_machine.worker", "azurerm_linux_virtual_machine",
                             ["create"], after={"size": "Standard_D2s_v5"}),
            _resource_change("google_compute_instance.job", "google_compute_instance",
                             ["create"], after={"machine_type": "e2-standard-2"}),
        ])
        result = estimate_plan(plan)
        assert result.total_monthly_delta_eur > 0
        providers = {r.provider for r in result.resources if r.action == "create"}
        assert providers == {"aws", "azure", "gcp"}


# ── Catalog summary ───────────────────────────────────────────────────────────

class TestCatalogSummary:
    def test_has_all_three_providers(self):
        summary = catalog_summary()
        entries = summary["entries"]
        providers = {e["provider"] for e in entries}
        assert "aws" in providers
        assert "azure" in providers
        assert "gcp" in providers

    def test_total_resource_types_count(self):
        summary = catalog_summary()
        assert summary["total_resource_types"] >= 20

    def test_pricing_note_present(self):
        summary = catalog_summary()
        assert "approximate" in summary["pricing_note"].lower()

    def test_aws_instance_in_catalog(self):
        summary = catalog_summary()
        types = [e["resource_type"] for e in summary["entries"]]
        assert "aws_instance" in types
        assert "azurerm_linux_virtual_machine" in types
        assert "google_compute_instance" in types
