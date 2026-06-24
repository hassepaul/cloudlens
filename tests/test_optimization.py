"""
Tests for the optimization layer: rightsizing (CPU+memory, cross-family),
scheduling, utilization scoring, and the realized-savings ledger.
Run: pytest tests/test_optimization.py -v
"""
from __future__ import annotations
import os
from unittest.mock import patch

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.rightsizing import recommend as rightsize, recommend_one
from app.services.scheduling import recommend as schedule
from app.services.utilization import analyze
from app.services.instance_catalog import lookup, candidates_for

TID = "t-1"


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


# ══════════════════════════════════════════════════════════════════════════════
# INSTANCE CATALOG
# ══════════════════════════════════════════════════════════════════════════════

class TestCatalog:
    def test_lookup_normalizes_provider(self):
        assert lookup("Amazon Web Services", "m5.large") is not None
        assert lookup("aws", "M5.LARGE") is not None        # case-insensitive
        assert lookup("Microsoft Azure", "D4s_v5") is not None

    def test_candidates_sorted_cheapest_first(self):
        cands = candidates_for("aws")
        prices = [c.hourly_usd for c in cands]
        assert prices == sorted(prices)


# ══════════════════════════════════════════════════════════════════════════════
# RIGHTSIZING — the core differentiator (uses memory, cross-family)
# ══════════════════════════════════════════════════════════════════════════════

class TestRightsizing:
    def test_downsizes_overprovisioned(self):
        rec = recommend_one("/i1", "web", "aws", "m5.2xlarge", 280, cpu_peak_pct=18, mem_peak_pct=22)
        assert rec.action == "downsize"
        assert rec.monthly_saving_eur > 0
        assert rec.recommended_type != "m5.2xlarge"

    def test_memory_bound_not_wrongly_downsized(self):
        """12% CPU but 85% memory: a CPU-only tool would downsize and cause OOM.
        CloudLens must keep enough memory."""
        rec = recommend_one("/i2", "analytics", "aws", "m5.4xlarge", 560,
                            cpu_peak_pct=12, mem_peak_pct=85)
        # required memory = 64 * 0.85 * 1.3 ≈ 70 GiB → nothing cheaper fits → no_change
        if rec.action == "downsize":
            target = lookup("aws", rec.recommended_type)
            assert target.memory_gib >= rec.required_mem_gib   # never under-provision memory
        else:
            assert rec.action == "no_change"

    def test_idle_terminate(self):
        rec = recommend_one("/i3", "ghost", "aws", "m5.large", 70, cpu_peak_pct=0.4, mem_peak_pct=1.0)
        assert rec.action == "terminate"
        assert rec.recommended_type is None
        assert rec.monthly_saving_eur == 70

    def test_well_sized_no_change(self):
        rec = recommend_one("/i4", "busy", "aws", "c5.large", 62, cpu_peak_pct=78, mem_peak_pct=60)
        assert rec.action == "no_change"

    def test_cross_family_move(self):
        # low CPU, high memory on general-purpose → memory-optimized family
        rec = recommend_one("/i5", "cache", "azure", "D8s_v5", 280, cpu_peak_pct=15, mem_peak_pct=70)
        assert rec.action == "downsize"
        assert rec.cross_family is True

    def test_headroom_respected(self):
        # tight headroom vs generous headroom → generous may pick a bigger (or equal) target
        tight = recommend_one("/i", "x", "aws", "m5.2xlarge", 280, cpu_peak_pct=40, mem_peak_pct=40, headroom=0.1)
        wide = recommend_one("/i", "x", "aws", "m5.2xlarge", 280, cpu_peak_pct=40, mem_peak_pct=40, headroom=0.6)
        assert tight.required_vcpu < wide.required_vcpu

    def test_unknown_instance_skipped(self):
        res = rightsize([{"resource_id": "/x", "provider": "aws", "instance_type": "totally-made-up",
                          "monthly_eur": 100, "cpu_peak_pct": 10, "mem_peak_pct": 10}])
        assert res.scanned == 1
        assert len(res.recommendations) == 0
        assert any("catalog" in n for n in res.notes)

    def test_confidence_from_window(self):
        hi = recommend_one("/i", "x", "aws", "m5.2xlarge", 280, cpu_peak_pct=15, mem_peak_pct=15,
                           observation_days=30, samples=30)
        lo = recommend_one("/i", "x", "aws", "m5.2xlarge", 280, cpu_peak_pct=15, mem_peak_pct=15,
                           observation_days=7, samples=5)
        assert hi.confidence == "high"
        assert lo.confidence == "low"


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

class TestScheduling:
    def test_nonprod_247_scheduled(self):
        res = schedule([{"resource_id": "/d", "resource_name": "dev-app", "provider": "aws",
                         "environment": "dev", "monthly_eur": 180, "currently_runs_247": True}])
        assert len(res.recommendations) == 1
        r = res.recommendations[0]
        assert r.recommended_hours_week == 60
        assert 60 < r.saving_pct < 70           # ~64%

    def test_prod_not_scheduled(self):
        res = schedule([{"resource_id": "/p", "resource_name": "prod-api", "provider": "aws",
                         "environment": "production", "monthly_eur": 300, "currently_runs_247": True}])
        assert len(res.recommendations) == 0

    def test_detects_nonprod_from_name_or_tags(self):
        res = schedule([{"resource_id": "/x", "resource_name": "myapp-staging-01", "provider": "aws",
                         "environment": "", "monthly_eur": 100, "currently_runs_247": True}])
        assert len(res.recommendations) == 1

    def test_activity_profile_high_confidence(self):
        profile = [0] * 168
        for i in range(40):
            profile[i] = 1     # ~40 active hours
        res = schedule([{"resource_id": "/a", "resource_name": "test-batch", "provider": "aws",
                         "environment": "test", "monthly_eur": 200, "currently_runs_247": True,
                         "activity_profile": profile}])
        assert res.recommendations[0].confidence == "high"

    def test_extended_style(self):
        res = schedule([{"resource_id": "/d", "resource_name": "dev-x", "provider": "aws",
                         "environment": "dev", "monthly_eur": 100, "currently_runs_247": True}],
                       schedule_style="extended")
        assert res.recommendations[0].recommended_hours_week == 70


# ══════════════════════════════════════════════════════════════════════════════
# UTILIZATION
# ══════════════════════════════════════════════════════════════════════════════

class TestUtilization:
    def _resources(self):
        return [
            {"resource_id": "/1", "resource_name": "web", "provider": "aws", "service": "EC2",
             "cpu_peak_pct": 18, "mem_peak_pct": 22, "monthly_eur": 280},
            {"resource_id": "/2", "resource_name": "analytics", "provider": "aws", "service": "EC2",
             "cpu_peak_pct": 12, "mem_peak_pct": 85, "monthly_eur": 560},
            {"resource_id": "/3", "resource_name": "idle", "provider": "aws", "service": "EC2",
             "cpu_peak_pct": 1, "mem_peak_pct": 2, "monthly_eur": 70},
            {"resource_id": "/4", "resource_name": "busy", "provider": "aws", "service": "RDS",
             "cpu_peak_pct": 88, "mem_peak_pct": 79, "monthly_eur": 400},
        ]

    def test_bands_and_scores(self):
        rows, summary = analyze(self._resources())
        by_name = {r.resource_name: r for r in rows}
        assert by_name["idle"].band == "idle"
        assert by_name["web"].band == "over"
        assert by_name["analytics"].band == "hot"     # memory is binding → not over
        assert by_name["busy"].band == "hot"

    def test_memory_bound_not_counted_reclaimable(self):
        rows, summary = analyze(self._resources())
        # only idle + web are reclaimable; analytics (high mem) is not
        assert summary.idle_count == 1
        assert summary.over_provisioned_count == 1
        assert summary.hot_count == 2

    def test_reclaimable_positive(self):
        rows, summary = analyze(self._resources())
        assert summary.reclaimable_monthly_eur > 0


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestOptimizationRouter:
    def _focus_rows(self):
        return [
            {"resource_id": "/i1", "resource_name": "web-prod", "provider_name": "aws",
             "service_name": "EC2", "instance_type": "m5.2xlarge", "environment": "prod",
             "cpu_peak_pct": 18, "mem_peak_pct": 22, "monthly_eur": 280},
            {"resource_id": "/d1", "resource_name": "dev-app", "provider_name": "aws",
             "service_name": "EC2", "instance_type": "m5.large", "environment": "dev",
             "cpu_peak_pct": 25, "mem_peak_pct": 30, "monthly_eur": 70},
        ]

    def test_rightsizing_endpoint(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return self._focus_rows()
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/optimization/{TID}/rightsizing")
        assert r.status_code == 200
        assert "total_monthly_saving_eur" in r.json()

    def test_scheduling_endpoint(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return self._focus_rows()
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/optimization/{TID}/scheduling")
        assert r.status_code == 200
        # dev-app is non-prod → at least one scheduling rec
        assert len(r.json()["recommendations"]) >= 1

    def test_utilization_endpoint(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return self._focus_rows()
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/optimization/{TID}/utilization")
        assert r.status_code == 200
        assert "summary" in r.json() and "by_band" in r.json()["summary"]

    def test_savings_ledger_lifecycle(self):
        store = {}

        async def fake_upsert(c, item):
            store[item["id"]] = item
            return item

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return list(store.values())

        with patch("app.services.cosmos.upsert_item", new=fake_upsert), \
             patch("app.services.cosmos.query_items", new=fake_query):
            cli = _client()
            # create an identified saving
            r = cli.post(f"/api/v1/optimization/{TID}/savings", json={
                "tenant_id": TID, "category": "rightsize", "resource_name": "web",
                "estimated_monthly_eur": 188})
            assert r.status_code == 201
            # ledger reflects it as identified
            led = cli.get(f"/api/v1/optimization/{TID}/savings/ledger").json()
            assert led["identified_monthly_eur"] == 188
            assert led["realized_monthly_eur"] == 0

    def test_create_savings_tenant_mismatch(self):
        r = _client().post(f"/api/v1/optimization/{TID}/savings", json={
            "tenant_id": "other", "category": "waste", "estimated_monthly_eur": 10})
        assert r.status_code == 422
