"""
Tests for the multi-cloud layer: provider normalization (FOCUS), 100% allocation,
commitment management, AI/LLM tracking, and i18n.
Run: pytest tests/test_multicloud.py -v
"""
from __future__ import annotations
import os

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

import app.providers as P
from app.providers.aws import AWSProvider
from app.providers.clouds import GCPProvider, AlibabaProvider, OCIProvider
from app.providers.ai_llm import AnthropicProvider, OpenAIProvider, split_bedrock_from_aws
from app.models.focus import ProviderName, ServiceCategory, CommitmentDiscountType
from app.services.allocation import (
    allocate_full, AllocationRuleSet, AllocationRule, RuleKind,
)
from app.services.commitments import analyze_commitments
from app.i18n import t, labels_for, normalize_lang, SUPPORTED


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER REGISTRATION + NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════

class TestProviderRegistry:
    def test_all_providers_registered(self):
        regs = P.registered_providers()
        for key in ("aws", "gcp", "alibaba", "oci", "anthropic", "openai"):
            assert key in regs

    def test_get_provider_class(self):
        assert P.get_provider_class("aws") is AWSProvider


class TestAWSNormalization:
    def _ce(self):
        return [{"TimePeriod": {"Start": "2026-06-01"}, "Groups": [
            {"Keys": ["Amazon EC2", "111122223333"],
             "Metrics": {"UnblendedCost": {"Amount": "120.50"},
                         "AmortizedCost": {"Amount": "90.10"},
                         "UsageQuantity": {"Amount": "744"}}},
        ]}]

    def test_maps_to_focus(self):
        recs = AWSProvider().normalize("t-1", self._ce())
        r = recs[0]
        assert r.provider_name == ProviderName.AWS
        assert r.service_name == "Amazon EC2"
        assert r.service_category == ServiceCategory.COMPUTE
        assert r.billed_cost == 120.50
        assert r.effective_cost == 90.10
        assert r.sub_account_id == "111122223333"

    def test_detects_commitment_discount(self):
        # amortized < unblended ⇒ a Savings Plan/RI discount was applied
        r = AWSProvider().normalize("t-1", self._ce())[0]
        assert r.commitment_discount_type == CommitmentDiscountType.SAVINGS_PLAN


class TestOtherCloudNormalization:
    def test_gcp_cud_credit(self):
        rows = [{"service_description": "Compute Engine", "project_id": "proj-1",
                 "cost": 100.0, "credits": -30.0, "usage_amount": 744, "usage_unit": "hour",
                 "day": "2026-06-01", "sku_description": "N2 vCPU"}]
        r = GCPProvider().normalize("t-1", rows)[0]
        assert r.provider_name == ProviderName.GCP
        assert r.effective_cost == 70.0           # cost + negative credit
        assert r.commitment_discount_type == CommitmentDiscountType.CUD

    def test_alibaba_subscription_is_reserved(self):
        rows = [{"ProductName": "ECS", "PretaxAmount": 50.0, "SubscriptionType": "Subscription",
                 "InstanceID": "i-abc", "BillingDate": "2026-06-01", "Region": "eu-central-1"}]
        r = AlibabaProvider().normalize("t-1", rows)[0]
        assert r.provider_name == ProviderName.ALIBABA
        assert r.commitment_discount_type == CommitmentDiscountType.RESERVED
        assert r.service_category == ServiceCategory.COMPUTE

    def test_oci_usage(self):
        rows = [{"service": "Compute", "compartmentName": "prod", "computedAmount": 75.0,
                 "timeUsageStarted": "2026-06-01T00:00:00Z", "unit": "Hours", "currency": "EUR"}]
        r = OCIProvider().normalize("t-1", rows)[0]
        assert r.provider_name == ProviderName.OCI
        assert r.billed_cost == 75.0
        assert r.charge_period_start.isoformat() == "2026-06-01"


class TestAILLM:
    def test_anthropic_normalize(self):
        rows = [{"amount": 42.0, "model": "claude-opus-4", "workspace_id": "ws-1",
                 "date": "2026-06-01", "input_tokens": 1000, "output_tokens": 500}]
        r = AnthropicProvider().normalize("t-1", rows)[0]
        assert r.provider_name == ProviderName.ANTHROPIC
        assert r.service_category == ServiceCategory.AI_ML
        assert "Claude" in r.service_name
        assert r.consumed_quantity == 1500

    def test_openai_normalize(self):
        rows = [{"amount": 30.0, "line_item": "gpt-4o", "project_id": "proj-x", "date": "2026-06-01"}]
        r = OpenAIProvider().normalize("t-1", rows)[0]
        assert r.provider_name == ProviderName.OPENAI
        assert r.service_category == ServiceCategory.AI_ML

    def test_bedrock_recategorized(self):
        recs = AWSProvider().normalize("t-1", [{"TimePeriod": {"Start": "2026-06-01"}, "Groups": [
            {"Keys": ["Amazon Bedrock", "111"], "Metrics": {"UnblendedCost": {"Amount": "45"},
             "AmortizedCost": {"Amount": "45"}, "UsageQuantity": {"Amount": "1"}}}]}])
        split_bedrock_from_aws(recs)
        assert recs[0].service_category == ServiceCategory.AI_ML


# ══════════════════════════════════════════════════════════════════════════════
# ALLOCATION — 100% without perfect tags
# ══════════════════════════════════════════════════════════════════════════════

class TestAllocation:
    def _records(self):
        return [
            {"effective_cost": 2000, "tags": {"cost_center": "engineering"}},
            {"effective_cost": 1500, "tags": {"team": "payments"}},
            {"effective_cost": 1200, "sub_account_id": "111122223333"},
            {"effective_cost": 800, "resource_name": "prod-erp-db-01", "tags": {}},
            {"effective_cost": 900, "tags": {}},
            {"effective_cost": 600, "tags": {}},
        ]

    def _ruleset(self, strategy="proportional"):
        return AllocationRuleSet(dimension="cost_center", shared_strategy=strategy, rules=[
            AllocationRule(kind=RuleKind.TAG_MAP, cost_center="", source_key="team",
                           value_map={"payments": "engineering"}),
            AllocationRule(kind=RuleKind.ACCOUNT, cost_center="data-platform",
                           accounts=("111122223333",)),
            AllocationRule(kind=RuleKind.NAME_PATTERN, cost_center="erp", pattern=r"^prod-erp-"),
        ])

    def test_reaches_100_pct(self):
        res = allocate_full(self._records(), self._ruleset("proportional"))
        assert res.allocated_pct == 100.0
        assert res.unallocated_eur == 0.0

    def test_rule_chain_attribution(self):
        res = allocate_full(self._records(), self._ruleset())
        eng = next(g for g in res.groups if g.name == "engineering")
        assert "tag" in eng.rule_breakdown and "tag_map" in eng.rule_breakdown
        dp = next(g for g in res.groups if g.name == "data-platform")
        assert "account" in dp.rule_breakdown
        erp = next(g for g in res.groups if g.name == "erp")
        assert "name_pattern" in erp.rule_breakdown

    def test_direct_coverage_below_100(self):
        res = allocate_full(self._records(), self._ruleset())
        # direct rules cover 5500/7000 ≈ 78.6%, shared split lifts to 100
        assert 75 < res.coverage_before_shared_pct < 85

    def test_no_shared_leaves_unallocated(self):
        res = allocate_full(self._records(), self._ruleset("none"))
        assert res.unallocated_eur > 0
        assert any(g.name == "Unallocated" for g in res.groups)


# ══════════════════════════════════════════════════════════════════════════════
# COMMITMENTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCommitments:
    def _focus(self):
        return [
            {"service_category": "Compute", "provider_name": "Amazon Web Services",
             "effective_cost": 5000, "commitment_discount_type": ""},
            {"service_category": "Compute", "provider_name": "Amazon Web Services",
             "effective_cost": 3000, "commitment_discount_type": "Savings Plan"},
            {"service_category": "Compute", "provider_name": "Google Cloud",
             "effective_cost": 2000, "commitment_discount_type": ""},
            {"service_category": "Storage", "provider_name": "Amazon Web Services",
             "effective_cost": 1000, "commitment_discount_type": ""},
        ]

    def test_coverage_and_eligibility(self):
        rep = analyze_commitments(self._focus(), [], days=30)
        # storage excluded from eligibility; compute eligible = 10000, covered = 3000
        assert rep.total_eligible_eur == 10000
        assert rep.total_covered_eur == 3000
        assert rep.blended_coverage_pct == 30.0

    def test_recommendations_generated(self):
        rep = analyze_commitments(self._focus(), [], days=30)
        assert rep.monthly_opportunity_eur > 0
        provs = {r.provider for r in rep.recommendations}
        assert "Amazon Web Services" in provs

    def test_idle_commitment_flagged(self):
        held = [{"provider_name": "Amazon Web Services", "hourly_commitment_eur": 5.0,
                 "utilization_pct": 60}]
        rep = analyze_commitments(self._focus(), held, days=30)
        assert rep.total_idle_commitment_eur > 0
        assert any("idle" in n.lower() for n in rep.notes)


# ══════════════════════════════════════════════════════════════════════════════
# I18N
# ══════════════════════════════════════════════════════════════════════════════

class TestI18n:
    def test_all_supported_languages_have_core_keys(self):
        for lang in SUPPORTED:
            labels = labels_for(lang)
            for key in ("cost_of_inaction", "forecast", "commitments", "chargeback"):
                assert key in labels and labels[key]

    def test_translation_differs_by_language(self):
        assert t("forecast", "de") != t("forecast", "fr")
        assert t("commitments", "it") == "Impegni"

    def test_fallback_to_english(self):
        assert t("forecast", "xx") == t("forecast", "en")  # unknown lang → EN

    def test_normalize_lang(self):
        assert normalize_lang("de-DE") == "de"
        assert normalize_lang("fr,en;q=0.9") == "fr"
        assert normalize_lang(None) == "en"


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════

from unittest.mock import patch


class TestMulticloudRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        return TestClient(create_app())

    def _focus_rows(self):
        return [
            {"provider_name": "Amazon Web Services", "service_name": "Amazon EC2",
             "service_category": "Compute", "effective_cost": 5000, "billed_cost": 5200,
             "commitment_discount_type": "", "tags": {"cost_center": "engineering"},
             "sub_account_id": "111", "resource_name": "ec2-1"},
            {"provider_name": "Microsoft Azure", "service_name": "Virtual Machines",
             "service_category": "Compute", "effective_cost": 3000, "billed_cost": 3000,
             "commitment_discount_type": "Reserved", "tags": {}, "sub_account_id": "sub-a",
             "resource_name": "vm-1"},
            {"provider_name": "Anthropic", "service_name": "Claude · opus",
             "service_category": "AI and Machine Learning", "effective_cost": 800,
             "billed_cost": 800, "commitment_discount_type": "", "tags": {}, "resource_name": ""},
        ]

    def test_spend_endpoint_groups_by_provider(self):
        tenant_doc = {
            "id": "t-1", "type": "tenant", "tenant_name": "Test",
            "subscription_ids": ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
            "plan_tier": "growth", "alert_email": "ops@test.com", "active": True,
            "sp_secret_ref": "sp-creds-t-1",
            "enabled_clouds": ["azure", "aws"],
            "cloud_accounts": {}, "cloud_credential_refs": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return self._focus_rows()

        async def fake_get(container, item_id, pk):
            return dict(tenant_doc)

        with patch("app.services.cosmos.query_items", new=fake_query), \
             patch("app.services.cosmos.get_item", new=fake_get):
            r = self._client().get("/api/v1/multicloud/t-1/spend?lang=de")
        assert r.status_code == 200
        body = r.json()
        assert body["lang"] == "de"
        assert len(body["providers"]) == 3
        assert body["ai_llm"]["total_eur"] == 800
        assert "by_provider" in body["labels"]
        assert "enabled_clouds" in body
        assert "locked_clouds" in body

    def test_commitments_endpoint(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            if "type='commitment'" in q:
                return [{"provider_name": "Amazon Web Services",
                         "hourly_commitment_eur": 2.0, "utilization_pct": 75}]
            return self._focus_rows()
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = self._client().get("/api/v1/multicloud/t-1/commitments")
        assert r.status_code == 200
        body = r.json()
        assert "blended_coverage_pct" in body
        assert "recommendations" in body

    def test_allocate_endpoint(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return self._focus_rows()
        ruleset = {"dimension": "cost_center", "shared_strategy": "proportional",
                   "rules": [{"kind": "account", "cost_center": "platform", "accounts": ["sub-a"]}]}
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = self._client().post("/api/v1/multicloud/t-1/allocate", json=ruleset)
        assert r.status_code == 200
        assert r.json()["allocated_pct"] == 100.0

    def test_labels_endpoint_no_tenant(self):
        r = self._client().get("/api/v1/multicloud/labels?lang=fr")
        assert r.status_code == 200
        assert r.json()["lang"] == "fr"
        assert r.json()["labels"]["forecast"] == "Prévision"
