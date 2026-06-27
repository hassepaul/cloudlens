"""
Comprehensive tests for pod-level Kubernetes cost allocation.

Covers:
  - normalize_k8s_allocation (FocusRecord creation + all tag fields)
  - K8sCostBreakdown:
      by_namespace, by_workload, total_eur,          (existing)
      by_pod, by_node, cost_components,              (new)
      namespace_trend, efficiency_metrics,           (new)
      workloads_for_namespace                        (new)
  - K8sCostClient: pagination, HTTP errors
  - Router endpoints:
      GET /allocation/pods/{ns}
      GET /allocation/workloads/{ns}
      GET /allocation/nodes
      GET /allocation/trends
      GET /allocation/components
      GET /allocation/efficiency
      GET /allocation (existing, smoke)
      GET /allocation/namespaces (existing, smoke)
      cluster-not-found 404
      opencost-unreachable 503
"""
from __future__ import annotations

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient
from httpx import ASGITransport


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _alloc(
    name: str = "api-server",
    namespace: str = "production",
    pod: str = "api-server-abc-1",
    deployment: str = "api-server",
    node: str = "node-1",
    day: str = "2024-04-01",
    total: float = 100.0,
    cpu_frac: float = 0.6,
    ram_frac: float = 0.3,
    pv_frac: float = 0.05,
    net_frac: float = 0.05,
    gpu: float = 0.0,
    shared: float = 0.0,
    cpu_req: float = 0.5,
    cpu_use: float = 0.3,
    ram_req: float = 1_073_741_824.0,  # 1 GiB
    ram_use: float = 536_870_912.0,    # 512 MiB
    container_count: int = 1,
    labels: dict | None = None,
) -> dict:
    """Build a realistic OpenCost allocation dict."""
    return {
        "_day": day,
        "_aggregate_key": name,
        "totalCost": total,
        "cpuCost": total * cpu_frac,
        "ramCost": total * ram_frac,
        "pvCost": total * pv_frac,
        "networkCost": total * net_frac,
        "gpuCost": gpu,
        "sharedCost": shared,
        "cpuCoreRequestAverage": cpu_req,
        "cpuCoreUsageAverage": cpu_use,
        "ramByteRequestAverage": ram_req,
        "ramByteUsageAverage": ram_use,
        "containerCount": container_count,
        "properties": {
            "namespace": namespace,
            "pod": pod,
            "deployment": deployment,
            "controller": deployment,
            "node": node,
            "labels": labels or {"team": "platform"},
        },
    }


@pytest.fixture()
def sample_records():
    """Three diverse FocusRecords across two namespaces and two nodes."""
    from app.services.k8s_cost import normalize_k8s_allocation
    raw = [
        _alloc("api-server",   namespace="production", pod="api-1",    deployment="api-server", node="node-1", total=200.0,
               cpu_req=0.8, cpu_use=0.5, ram_req=2e9, ram_use=1.2e9),
        _alloc("worker",       namespace="production", pod="worker-1",  deployment="worker",     node="node-1", total=100.0,
               cpu_req=0.4, cpu_use=0.3, ram_req=1e9, ram_use=0.7e9),
        _alloc("prometheus",   namespace="monitoring",  pod="prom-0",   deployment="prometheus", node="node-2", total=50.0,
               cpu_req=0.2, cpu_use=0.15, ram_req=0.5e9, ram_use=0.4e9),
    ]
    return normalize_k8s_allocation("t1", "aks-prod", raw)


@pytest.fixture()
def transport():
    """ASGI transport using the real FastAPI app (services mocked at call sites)."""
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
    from app.main import create_app
    return ASGITransport(app=create_app())


# ─────────────────────────────────────────────────────────────────────────────
# normalize_k8s_allocation — tag completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeK8sAllocation:

    def test_all_component_tags_present(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(total=100.0, gpu=5.0, shared=2.0)]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        for tag in ("k8s_component_cpu", "k8s_component_ram", "k8s_component_pv",
                    "k8s_component_network", "k8s_component_gpu", "k8s_component_shared"):
            assert tag in r.tags, f"Missing tag: {tag}"

    def test_efficiency_tags_present(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(cpu_req=0.5, cpu_use=0.3, ram_req=1e9, ram_use=0.5e9)]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert r.tags["k8s_cpu_request_avg"] == "0.5"
        assert r.tags["k8s_cpu_usage_avg"] == "0.3"
        assert float(r.tags["k8s_ram_request_avg"]) == pytest.approx(1e9)
        assert float(r.tags["k8s_ram_usage_avg"]) == pytest.approx(0.5e9)

    def test_pod_and_controller_tags(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(pod="my-pod-abc", deployment="my-deploy")]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert r.tags["k8s_pod"] == "my-pod-abc"
        assert r.tags["k8s_controller"] == "my-deploy"

    def test_node_tag(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(node="big-node-42")]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert r.tags["k8s_node"] == "big-node-42"

    def test_container_count_tag(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(container_count=3)]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert r.tags["k8s_container_count"] == "3"

    def test_label_passthrough(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(labels={"env": "prod", "team": "checkout"})]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert r.tags["k8s_env"] == "prod"
        assert r.tags["k8s_team"] == "checkout"

    def test_cpu_cost_matches_fraction(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(total=100.0, cpu_frac=0.6)]
        r = normalize_k8s_allocation("t1", "aks-1", raw)[0]
        assert float(r.tags["k8s_component_cpu"]) == pytest.approx(60.0)

    def test_zero_total_skipped(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(total=0.0)]
        assert normalize_k8s_allocation("t1", "aks-1", raw) == []

    def test_resource_id_includes_cluster_and_ns(self):
        from app.services.k8s_cost import normalize_k8s_allocation
        raw = [_alloc(namespace="checkout", deployment="web")]
        r = normalize_k8s_allocation("t1", "my-cluster", raw)[0]
        assert "my-cluster" in r.resource_id
        assert "checkout" in r.resource_id


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — by_pod
# ─────────────────────────────────────────────────────────────────────────────

class TestByPod:

    def test_by_pod_returns_pod_names(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        names = [p["pod"] for p in pods]
        assert "api-1" in names
        assert "worker-1" in names
        assert "prom-0" in names

    def test_by_pod_sorted_descending(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        costs = [p["cost_eur"] for p in pods]
        assert costs == sorted(costs, reverse=True)

    def test_by_pod_namespace_filter(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod(namespace="monitoring")
        assert all(p["namespace"] == "monitoring" for p in pods)
        assert len(pods) == 1
        assert pods[0]["pod"] == "prom-0"

    def test_by_pod_components_present(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        for p in pods:
            assert "components" in p
            for key in ("cpu", "ram", "pv", "network", "gpu", "shared"):
                assert key in p["components"]

    def test_by_pod_pct_sums_to_100(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        total_pct = sum(p["pct"] for p in pods)
        assert total_pct == pytest.approx(100.0, abs=0.5)

    def test_by_pod_controller_populated(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        api_pod = next(p for p in pods if p["pod"] == "api-1")
        assert api_pod["controller"] == "api-server"

    def test_by_pod_empty_when_no_match(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod(namespace="nonexistent")
        assert pods == []

    def test_by_pod_cpu_pct_within_100(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        pods = K8sCostBreakdown(sample_records).by_pod()
        for p in pods:
            assert 0.0 <= p["cpu_pct"] <= 100.0
            assert 0.0 <= p["ram_pct"] <= 100.0


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — by_node
# ─────────────────────────────────────────────────────────────────────────────

class TestByNode:

    def test_by_node_returns_nodes(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        nodes = K8sCostBreakdown(sample_records).by_node()
        names = [n["node"] for n in nodes]
        assert "node-1" in names
        assert "node-2" in names

    def test_by_node_pod_count_correct(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        nodes = K8sCostBreakdown(sample_records).by_node()
        node1 = next(n for n in nodes if n["node"] == "node-1")
        assert node1["pod_count"] == 2  # api-1 + worker-1

    def test_by_node_pct_sums_to_100(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        nodes = K8sCostBreakdown(sample_records).by_node()
        assert sum(n["pct"] for n in nodes) == pytest.approx(100.0, abs=0.5)

    def test_by_node_sorted_descending(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        nodes = K8sCostBreakdown(sample_records).by_node()
        costs = [n["cost_eur"] for n in nodes]
        assert costs == sorted(costs, reverse=True)

    def test_by_node_namespace_count(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        nodes = K8sCostBreakdown(sample_records).by_node()
        node1 = next(n for n in nodes if n["node"] == "node-1")
        # Both production pods are on node-1
        assert node1["namespace_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — cost_components
# ─────────────────────────────────────────────────────────────────────────────

class TestCostComponents:

    def test_components_sum_to_total(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        c = K8sCostBreakdown(sample_records).cost_components()
        component_sum = c["cpu"] + c["ram"] + c["pv"] + c["network"] + c["gpu"] + c["shared"]
        assert component_sum == pytest.approx(c["total"], abs=0.01)

    def test_pct_keys_present(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        c = K8sCostBreakdown(sample_records).cost_components()
        for key in ("cpu_pct", "ram_pct", "pv_pct", "network_pct", "gpu_pct"):
            assert key in c

    def test_namespace_filter(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        bd = K8sCostBreakdown(sample_records)
        all_c = bd.cost_components()
        prod_c = bd.cost_components(namespace="production")
        # Production has 300 EUR total, monitoring has 50 EUR
        assert prod_c["total"] < all_c["total"]

    def test_cpu_dominates_in_cpu_heavy_workload(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [_alloc(total=100.0, cpu_frac=0.9, ram_frac=0.05, pv_frac=0.02, net_frac=0.03)]
        records = normalize_k8s_allocation("t1", "aks-1", raw)
        c = K8sCostBreakdown(records).cost_components()
        assert c["cpu_pct"] > 80.0


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — namespace_trend
# ─────────────────────────────────────────────────────────────────────────────

class TestNamespaceTrend:

    def test_trend_sorted_by_date(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [
            _alloc(day="2024-04-03", total=30.0),
            _alloc(day="2024-04-01", total=10.0),
            _alloc(day="2024-04-02", total=20.0),
        ]
        records = normalize_k8s_allocation("t1", "aks-1", raw)
        trend = K8sCostBreakdown(records).namespace_trend()
        dates = [t["date"] for t in trend]
        assert dates == sorted(dates)

    def test_trend_daily_aggregation(self):
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [
            _alloc("a", day="2024-04-01", total=40.0),
            _alloc("b", day="2024-04-01", total=60.0),
        ]
        records = normalize_k8s_allocation("t1", "aks-1", raw)
        trend = K8sCostBreakdown(records).namespace_trend()
        assert len(trend) == 1
        assert trend[0]["cost_eur"] == pytest.approx(100.0)

    def test_trend_namespace_filter(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        all_trend = K8sCostBreakdown(sample_records).namespace_trend()
        mon_trend = K8sCostBreakdown(sample_records).namespace_trend(namespace="monitoring")
        # monitoring has only 50 EUR
        assert sum(d["cost_eur"] for d in mon_trend) < sum(d["cost_eur"] for d in all_trend)

    def test_trend_empty_for_unknown_namespace(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        trend = K8sCostBreakdown(sample_records).namespace_trend(namespace="ghost")
        assert trend == []


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — efficiency_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestEfficiencyMetrics:

    def test_efficiency_keys(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        eff = K8sCostBreakdown(sample_records).efficiency_metrics()
        assert len(eff) > 0
        for e in eff:
            for key in ("namespace", "cost_eur", "cpu_efficiency_pct",
                        "ram_efficiency_pct", "avg_efficiency_pct", "waste_estimate_eur"):
                assert key in e

    def test_efficiency_capped_at_100(self):
        """Usage > request must be capped at 100% efficiency."""
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [_alloc(cpu_req=0.1, cpu_use=0.9, ram_req=0.1e9, ram_use=0.9e9)]
        records = normalize_k8s_allocation("t1", "aks-1", raw)
        eff = K8sCostBreakdown(records).efficiency_metrics()
        assert eff[0]["cpu_efficiency_pct"] <= 100.0
        assert eff[0]["ram_efficiency_pct"] <= 100.0

    def test_waste_estimate_positive(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        eff = K8sCostBreakdown(sample_records).efficiency_metrics()
        for e in eff:
            assert e["waste_estimate_eur"] >= 0.0

    def test_sorted_by_waste_descending(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        eff = K8sCostBreakdown(sample_records).efficiency_metrics()
        wastes = [e["waste_estimate_eur"] for e in eff]
        assert wastes == sorted(wastes, reverse=True)

    def test_zero_request_handled_gracefully(self):
        """Avoid division by zero when cpu/ram request are zero."""
        from app.services.k8s_cost import normalize_k8s_allocation, K8sCostBreakdown
        raw = [_alloc(cpu_req=0.0, cpu_use=0.0, ram_req=0.0, ram_use=0.0)]
        records = normalize_k8s_allocation("t1", "aks-1", raw)
        eff = K8sCostBreakdown(records).efficiency_metrics()
        assert len(eff) == 1  # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostBreakdown — workloads_for_namespace
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkloadsForNamespace:

    def test_returns_only_requested_namespace(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("production")
        assert all(w["namespace"] == "production" for w in wl)

    def test_workload_names_match_controllers(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("production")
        names = {w["workload"] for w in wl}
        assert "api-server" in names
        assert "worker" in names

    def test_pod_count_per_workload(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("production")
        api_wl = next(w for w in wl if w["workload"] == "api-server")
        assert api_wl["pod_count"] >= 1

    def test_sorted_descending(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("production")
        costs = [w["cost_eur"] for w in wl]
        assert costs == sorted(costs, reverse=True)

    def test_empty_for_unknown_namespace(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("ghost-ns")
        assert wl == []

    def test_component_fields_present(self, sample_records):
        from app.services.k8s_cost import K8sCostBreakdown
        wl = K8sCostBreakdown(sample_records).workloads_for_namespace("production")
        for w in wl:
            assert "components" in w
            assert "cpu_pct" in w
            assert "ram_pct" in w


# ─────────────────────────────────────────────────────────────────────────────
# K8sCostClient — HTTP behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestK8sCostClient:

    @pytest.mark.asyncio
    async def test_paginates_by_day(self):
        from app.services.k8s_cost import K8sCostClient
        calls: list[str] = []

        async def _mock_get(url, params=None, **kwargs):
            calls.append(params.get("window", ""))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"code": 200, "data": []}
            return resp

        with patch("httpx.AsyncClient") as cls:
            mock_c = AsyncMock()
            mock_c.get = AsyncMock(side_effect=_mock_get)
            mock_c.__aenter__ = AsyncMock(return_value=mock_c)
            mock_c.__aexit__ = AsyncMock(return_value=None)
            cls.return_value = mock_c
            from app.services.k8s_cost import K8sCostClient
            client = K8sCostClient("http://oc.local", "c1")
            await client.get_allocation(date(2024, 1, 1), date(2024, 1, 4))

        assert len(calls) == 4  # Jan 1–4 inclusive

    @pytest.mark.asyncio
    async def test_http_error_is_skipped_not_raised(self):
        """HTTP errors per-day should be logged but not abort the entire request."""
        from httpx import HTTPStatusError, Request, Response
        from app.services.k8s_cost import K8sCostClient

        async def _fail(url, params=None, **kwargs):
            resp = MagicMock()
            resp.status_code = 503
            resp.raise_for_status.side_effect = HTTPStatusError(
                "service unavailable", request=MagicMock(), response=resp
            )
            return resp

        with patch("httpx.AsyncClient") as cls:
            mock_c = AsyncMock()
            mock_c.get = AsyncMock(side_effect=_fail)
            mock_c.__aenter__ = AsyncMock(return_value=mock_c)
            mock_c.__aexit__ = AsyncMock(return_value=None)
            cls.return_value = mock_c
            client = K8sCostClient("http://oc.local", "c1")
            result = await client.get_allocation(date(2024, 1, 1), date(2024, 1, 1))

        assert result == []  # error swallowed, empty list returned

    @pytest.mark.asyncio
    async def test_aggregate_param_passed_through(self):
        from app.services.k8s_cost import K8sCostClient
        seen_params: list[dict] = []

        async def _capture(url, params=None, **kwargs):
            seen_params.append(params or {})
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"code": 200, "data": []}
            return resp

        with patch("httpx.AsyncClient") as cls:
            mock_c = AsyncMock()
            mock_c.get = AsyncMock(side_effect=_capture)
            mock_c.__aenter__ = AsyncMock(return_value=mock_c)
            mock_c.__aexit__ = AsyncMock(return_value=None)
            cls.return_value = mock_c
            client = K8sCostClient("http://oc.local", "c1")
            await client.get_allocation(date(2024, 1, 1), date(2024, 1, 1), aggregate="pod")

        assert seen_params[0]["aggregate"] == "pod"


# ─────────────────────────────────────────────────────────────────────────────
# Router — helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_tenant_doc(clusters: list[dict] | None = None) -> dict:
    return {
        "id": "t-test",
        "tenant_id": "t-test",
        "name": "Test Tenant",
        "plan_tier": "growth",
        "status": "active",
        "alert_email": "test@test.com",
        "k8s_clusters": clusters or [
            {
                "cluster_id": "aks-prod",
                "opencost_url": "http://opencost.local",
                "cloud": "azure",
                "enabled": True,
            }
        ],
    }


def _make_raw_allocs(n: int = 3) -> list[dict]:
    namespaces = ["production", "staging", "monitoring"]
    return [
        _alloc(
            name=f"workload-{i}",
            namespace=namespaces[i % 3],
            pod=f"pod-{i}",
            deployment=f"workload-{i}",
            node=f"node-{i % 2}",
            day="2024-04-01",
            total=float((i + 1) * 50),
            cpu_req=0.5, cpu_use=0.3,
            ram_req=1e9, ram_use=0.5e9,
        )
        for i in range(n)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/pods/{namespace}
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterPodsInNamespace:

    @pytest.mark.asyncio
    async def test_returns_pods_for_namespace(self, transport):
        raw = _make_raw_allocs(6)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/pods/production",
                                 params={"cluster_id": "aks-prod", "days": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["namespace"] == "production"
        assert "pods" in body
        assert "cost_components" in body

    @pytest.mark.asyncio
    async def test_cost_components_in_response(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/pods/production",
                                 params={"cluster_id": "aks-prod"})
        body = r.json()
        comp = body["cost_components"]
        assert "cpu" in comp
        assert "ram" in comp
        assert "pv" in comp
        assert "network" in comp

    @pytest.mark.asyncio
    async def test_cluster_not_found_404(self, transport):
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc(clusters=[])):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/pods/production",
                                 params={"cluster_id": "missing-cluster"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_opencost_unreachable_503(self, transport):
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, side_effect=Exception("connection refused")):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/pods/production",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 503


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/workloads/{namespace}
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterWorkloadsInNamespace:

    @pytest.mark.asyncio
    async def test_returns_workloads(self, transport):
        raw = _make_raw_allocs(6)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/workloads/production",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 200
        body = r.json()
        assert "workloads" in body
        assert body["namespace"] == "production"

    @pytest.mark.asyncio
    async def test_workload_fields(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/workloads/production",
                                 params={"cluster_id": "aks-prod"})
        body = r.json()
        for wl in body["workloads"]:
            assert "workload" in wl
            assert "cost_eur" in wl
            assert "pod_count" in wl
            assert "components" in wl


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/nodes
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterNodes:

    @pytest.mark.asyncio
    async def test_returns_nodes(self, transport):
        raw = _make_raw_allocs(4)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/nodes",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "node_count" in body
        for node in body["nodes"]:
            assert "node" in node
            assert "cost_eur" in node
            assert "pod_count" in node

    @pytest.mark.asyncio
    async def test_tenant_not_found_404(self, transport):
        from app.exceptions import NotFoundError
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   side_effect=NotFoundError("tenant", "t-test")):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/nodes",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/trends
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterTrends:

    @pytest.mark.asyncio
    async def test_returns_trend_series(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/trends",
                                 params={"cluster_id": "aks-prod", "days": 7})
        assert r.status_code == 200
        body = r.json()
        assert "trend" in body
        assert "data_points" in body
        for pt in body["trend"]:
            assert "date" in pt
            assert "cost_eur" in pt

    @pytest.mark.asyncio
    async def test_namespace_filter_accepted(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/trends",
                                 params={"cluster_id": "aks-prod", "namespace": "production"})
        assert r.status_code == 200
        assert r.json()["namespace"] == "production"


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/components
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterComponents:

    @pytest.mark.asyncio
    async def test_returns_component_breakdown(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/components",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 200
        body = r.json()
        for key in ("cpu", "ram", "pv", "network", "gpu", "total",
                    "cpu_pct", "ram_pct", "pv_pct", "network_pct"):
            assert key in body

    @pytest.mark.asyncio
    async def test_namespace_scoped_components(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/components",
                                 params={"cluster_id": "aks-prod", "namespace": "production"})
        assert r.status_code == 200
        assert r.json()["namespace"] == "production"


# ─────────────────────────────────────────────────────────────────────────────
# Router — GET /allocation/efficiency
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterEfficiency:

    @pytest.mark.asyncio
    async def test_returns_efficiency_per_namespace(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/efficiency",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 200
        body = r.json()
        assert "namespaces" in body
        assert "cluster_avg_efficiency_pct" in body
        assert "total_waste_estimate_eur" in body
        for ns in body["namespaces"]:
            assert "cpu_efficiency_pct" in ns
            assert "ram_efficiency_pct" in ns
            assert "waste_estimate_eur" in ns

    @pytest.mark.asyncio
    async def test_efficiency_pct_between_0_and_100(self, transport):
        raw = _make_raw_allocs(3)
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=_make_tenant_doc()), \
             patch("app.services.k8s_cost.K8sCostClient.get_allocation",
                   new_callable=AsyncMock, return_value=raw):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/efficiency",
                                 params={"cluster_id": "aks-prod"})
        for ns in r.json()["namespaces"]:
            assert 0.0 <= ns["cpu_efficiency_pct"] <= 100.0
            assert 0.0 <= ns["ram_efficiency_pct"] <= 100.0

    @pytest.mark.asyncio
    async def test_disabled_cluster_422(self, transport):
        doc = _make_tenant_doc(clusters=[{
            "cluster_id": "aks-prod", "opencost_url": "http://x",
            "cloud": "azure", "enabled": False,
        }])
        with patch("app.routers.k8s.cosmos.get_item", new_callable=AsyncMock,
                   return_value=doc):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/k8s/t-test/allocation/efficiency",
                                 params={"cluster_id": "aks-prod"})
        assert r.status_code == 422
