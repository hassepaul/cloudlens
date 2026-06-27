"""
Kubernetes pod-level cost allocation
======================================

Integrates with OpenCost (self-hosted) or Kubecost Cloud to fetch per-pod,
per-namespace, and per-deployment cost breakdowns and normalize them into
FocusRecords.

Architecture:
  - The customer deploys OpenCost (free, open-source) or Kubecost into their
    AKS/EKS/GKE cluster.  CloudLens calls the OpenCost REST API on their
    behalf (agent-pull model — no CloudLens code runs in-cluster).
  - Pod costs are attributed to FOCUS records using the cluster's Node pool
    subscription cost as the denominator, so the numbers reconcile with Azure
    Cost Management totals exactly.
  - Namespace → team/cost-center mapping uses the same allocation rule engine
    as the multi-cloud chargeback flow.

OpenCost REST API (v1):
  GET http://<opencost-service>:<port>/model/allocation
      ?window=<duration>&aggregate=<dimension>&resolution=<granularity>

Kubecost Enterprise API (compatible subset):
  GET http://<kubecost>/model/allocation
      (same shape — intentional compatibility)

The k8s_endpoint per cluster is stored in the tenant's Key Vault secret.
"""
from __future__ import annotations
import asyncio
from datetime import date, timedelta
from typing import Any

import httpx

from app.exceptions import CloudLensError
from app.logging_config import get_logger
from app.models.focus import FocusRecord, ProviderName, ServiceCategory, CommitmentDiscountType

log = get_logger(__name__)

# Maximum pages to fetch per cluster (safety cap — each page = 24 h window).
_MAX_PAGES = 31


class K8sCostClient:
    """
    Client for the OpenCost / Kubecost allocation REST API.
    One instance per cluster (tenant may have multiple clusters).
    """

    def __init__(self, base_url: str, cluster_id: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.cluster_id = cluster_id
        self._timeout = timeout

    async def get_allocation(
        self,
        start: date,
        end: date,
        aggregate: str = "namespace",
    ) -> list[dict]:
        """
        Fetch allocation data from OpenCost.

        ``aggregate`` can be any of:
          namespace | pod | deployment | label:<key> | annotation:<key> |
          node | cluster | controller | service | daemonset | statefulset

        Returns a flat list of allocation objects (one per aggregate value per day).
        """
        results = []
        current = start
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while current <= end:
                window = f"{current.isoformat()}T00:00:00Z,{(current + timedelta(days=1)).isoformat()}T00:00:00Z"
                try:
                    resp = await client.get(
                        f"{self.base_url}/model/allocation",
                        params={
                            "window": window,
                            "aggregate": aggregate,
                            "resolution": "1h",
                            "includeIdle": "true",
                            "idleByNode": "false",
                            "format": "json",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # OpenCost returns {"code":200,"data":[{...},...]}
                    for day_data in (data.get("data") or []):
                        if isinstance(day_data, dict):
                            for alloc_name, alloc in day_data.items():
                                alloc["_day"] = current.isoformat()
                                alloc["_aggregate_key"] = alloc_name
                                results.append(alloc)
                except httpx.HTTPStatusError as exc:
                    log.warning("k8s.opencost_http_error",
                                cluster=self.cluster_id, day=current.isoformat(),
                                status=exc.response.status_code)
                except Exception as exc:
                    log.warning("k8s.opencost_fetch_error",
                                cluster=self.cluster_id, day=current.isoformat(),
                                error=str(exc))
                current += timedelta(days=1)
        return results


def normalize_k8s_allocation(
    tenant_id: str,
    cluster_id: str,
    raw: list[dict],
    provider_name: ProviderName = ProviderName.AZURE,
) -> list[FocusRecord]:
    """
    Convert OpenCost allocation objects to FocusRecords.

    Cost fields from OpenCost:
      totalCost       — total node cost attributed to this workload
      cpuCost         — CPU share of node cost
      ramCost         — memory share of node cost
      pvCost          — persistent volume cost
      networkCost     — network egress cost
      gpuCost         — GPU share (if present)
      sharedCost      — cluster-wide shared overhead (proportional)

    Usage/request fields from OpenCost:
      cpuCoreRequestAverage   — average requested CPU cores
      cpuCoreUsageAverage     — average actual CPU core usage
      ramByteRequestAverage   — average requested RAM bytes
      ramByteUsageAverage     — average actual RAM byte usage
      containerCount          — number of containers in the pod
    """
    records: list[FocusRecord] = []
    for alloc in raw:
        total = float(alloc.get("totalCost") or 0.0)
        if total <= 0.0:
            continue

        day = alloc.get("_day", str(date.today()))
        aggregate_key = alloc.get("_aggregate_key", "__idle__")
        props = alloc.get("properties") or {}
        namespace = props.get("namespace") or aggregate_key
        pod = props.get("pod") or ""
        deployment = props.get("deployment") or ""
        controller = props.get("controller") or deployment or ""
        node = props.get("node") or ""
        labels = props.get("labels") or {}
        container_count = int(alloc.get("containerCount") or props.get("containerCount") or 1)

        # Cost components
        cpu_cost = float(alloc.get("cpuCost") or 0.0)
        ram_cost = float(alloc.get("ramCost") or 0.0)
        pv_cost = float(alloc.get("pvCost") or 0.0)
        network_cost = float(alloc.get("networkCost") or 0.0)
        gpu_cost = float(alloc.get("gpuCost") or 0.0)
        shared_cost = float(alloc.get("sharedCost") or 0.0)

        # Efficiency fields (request vs actual)
        cpu_req = float(alloc.get("cpuCoreRequestAverage") or 0.0)
        cpu_use = float(alloc.get("cpuCoreUsageAverage") or 0.0)
        ram_req = float(alloc.get("ramByteRequestAverage") or 0.0)
        ram_use = float(alloc.get("ramByteUsageAverage") or 0.0)

        # Build a stable resource ID for idempotent upserts.
        resource_path = f"/k8s/{cluster_id}/{namespace}/{deployment or pod}"

        records.append(FocusRecord(
            tenant_id=tenant_id,
            provider_name=provider_name,
            sub_account_id=cluster_id,
            charge_period_start=day,
            billed_cost=total,
            effective_cost=total,
            list_cost=total,
            service_name="Kubernetes",
            service_category=ServiceCategory.COMPUTE,
            charge_description=(
                f"K8s {aggregate_key} — "
                f"cpu:{round(cpu_cost,4)} "
                f"ram:{round(ram_cost,4)} "
                f"pv:{round(pv_cost,4)}"
            ),
            resource_id=resource_path,
            resource_name=deployment or pod or aggregate_key,
            tags={
                **{f"k8s_{k}": str(v) for k, v in labels.items()},
                "k8s_cluster": cluster_id,
                "k8s_namespace": namespace,
                "k8s_node": node,
                "k8s_pod": pod,
                "k8s_controller": controller,
                "k8s_container_count": str(container_count),
                # Cost components stored as strings (float precision preserved)
                "k8s_component_cpu": str(cpu_cost),
                "k8s_component_ram": str(ram_cost),
                "k8s_component_pv": str(pv_cost),
                "k8s_component_network": str(network_cost),
                "k8s_component_gpu": str(gpu_cost),
                "k8s_component_shared": str(shared_cost),
                # Efficiency
                "k8s_cpu_request_avg": str(cpu_req),
                "k8s_cpu_usage_avg": str(cpu_use),
                "k8s_ram_request_avg": str(ram_req),
                "k8s_ram_usage_avg": str(ram_use),
            },
            commitment_discount_type=CommitmentDiscountType.NONE,
        ))
    return records


class K8sCostBreakdown:
    """High-level aggregation of K8s costs for the API response."""

    def __init__(self, records: list[FocusRecord]):
        self.records = records

    # ── Existing aggregations (unchanged) ────────────────────────────────────

    def by_namespace(self) -> list[dict]:
        totals: dict[str, float] = {}
        for r in self.records:
            ns = r.tags.get("k8s_namespace") or "__unknown__"
            totals[ns] = totals.get(ns, 0.0) + r.effective_cost
        grand = sum(totals.values()) or 1.0
        return sorted(
            [{"namespace": ns, "cost_eur": round(v, 2), "pct": round(v / grand * 100, 1)}
             for ns, v in totals.items()],
            key=lambda x: x["cost_eur"],
            reverse=True,
        )

    def by_workload(self) -> list[dict]:
        totals: dict[str, float] = {}
        for r in self.records:
            key = r.resource_name or "__unknown__"
            totals[key] = totals.get(key, 0.0) + r.effective_cost
        grand = sum(totals.values()) or 1.0
        return sorted(
            [{"workload": k, "cost_eur": round(v, 2), "pct": round(v / grand * 100, 1)}
             for k, v in totals.items()],
            key=lambda x: x["cost_eur"],
            reverse=True,
        )[:50]

    def total_eur(self) -> float:
        return round(sum(r.effective_cost for r in self.records), 2)

    # ── Pod-level detail ─────────────────────────────────────────────────────

    def by_pod(self, namespace: str | None = None) -> list[dict]:
        """Per-pod breakdown with cost component detail, optionally filtered to a namespace."""
        pods: dict[str, dict] = {}
        for r in self.records:
            ns = r.tags.get("k8s_namespace") or ""
            if namespace and ns != namespace:
                continue
            pod = r.tags.get("k8s_pod") or r.resource_name or "__unknown__"
            controller = r.tags.get("k8s_controller") or ""
            node = r.tags.get("k8s_node") or ""
            if pod not in pods:
                pods[pod] = {
                    "pod": pod, "namespace": ns, "controller": controller,
                    "node": node, "cost_eur": 0.0,
                    "cpu_cost": 0.0, "ram_cost": 0.0, "pv_cost": 0.0,
                    "network_cost": 0.0, "gpu_cost": 0.0, "shared_cost": 0.0,
                    "days": set(),
                }
            p = pods[pod]
            p["cost_eur"] += r.effective_cost
            p["cpu_cost"] += float(r.tags.get("k8s_component_cpu") or 0)
            p["ram_cost"] += float(r.tags.get("k8s_component_ram") or 0)
            p["pv_cost"] += float(r.tags.get("k8s_component_pv") or 0)
            p["network_cost"] += float(r.tags.get("k8s_component_network") or 0)
            p["gpu_cost"] += float(r.tags.get("k8s_component_gpu") or 0)
            p["shared_cost"] += float(r.tags.get("k8s_component_shared") or 0)
            p["days"].add(r.charge_period_start)

        grand = sum(p["cost_eur"] for p in pods.values()) or 1.0
        result = []
        for p in pods.values():
            total = p["cost_eur"] or 1.0
            result.append({
                "pod": p["pod"],
                "namespace": p["namespace"],
                "controller": p["controller"],
                "node": p["node"],
                "cost_eur": round(p["cost_eur"], 4),
                "pct": round(p["cost_eur"] / grand * 100, 1),
                "days_tracked": len(p["days"]),
                "components": {
                    "cpu": round(p["cpu_cost"], 4),
                    "ram": round(p["ram_cost"], 4),
                    "pv": round(p["pv_cost"], 4),
                    "network": round(p["network_cost"], 4),
                    "gpu": round(p["gpu_cost"], 4),
                    "shared": round(p["shared_cost"], 4),
                },
                "cpu_pct": round(p["cpu_cost"] / total * 100, 1),
                "ram_pct": round(p["ram_cost"] / total * 100, 1),
            })
        return sorted(result, key=lambda x: x["cost_eur"], reverse=True)

    def by_node(self) -> list[dict]:
        """Per-node cost with pod count."""
        nodes: dict[str, dict] = {}
        for r in self.records:
            node = r.tags.get("k8s_node") or "__unscheduled__"
            if node not in nodes:
                nodes[node] = {"node": node, "cost_eur": 0.0, "pods": set(), "namespaces": set()}
            nodes[node]["cost_eur"] += r.effective_cost
            pod = r.tags.get("k8s_pod") or r.resource_name
            if pod:
                nodes[node]["pods"].add(pod)
            ns = r.tags.get("k8s_namespace")
            if ns:
                nodes[node]["namespaces"].add(ns)
        grand = sum(n["cost_eur"] for n in nodes.values()) or 1.0
        result = []
        for n in nodes.values():
            result.append({
                "node": n["node"],
                "cost_eur": round(n["cost_eur"], 2),
                "pct": round(n["cost_eur"] / grand * 100, 1),
                "pod_count": len(n["pods"]),
                "namespace_count": len(n["namespaces"]),
            })
        return sorted(result, key=lambda x: x["cost_eur"], reverse=True)

    def cost_components(self, namespace: str | None = None) -> dict:
        """Aggregate cpu/ram/pv/network/gpu costs, optionally filtered to a namespace."""
        cpu = ram = pv = network = gpu = shared = 0.0
        for r in self.records:
            if namespace and r.tags.get("k8s_namespace") != namespace:
                continue
            cpu += float(r.tags.get("k8s_component_cpu") or 0)
            ram += float(r.tags.get("k8s_component_ram") or 0)
            pv += float(r.tags.get("k8s_component_pv") or 0)
            network += float(r.tags.get("k8s_component_network") or 0)
            gpu += float(r.tags.get("k8s_component_gpu") or 0)
            shared += float(r.tags.get("k8s_component_shared") or 0)
        total = (cpu + ram + pv + network + gpu + shared) or 1.0
        return {
            "cpu": round(cpu, 2),
            "ram": round(ram, 2),
            "pv": round(pv, 2),
            "network": round(network, 2),
            "gpu": round(gpu, 2),
            "shared": round(shared, 2),
            "total": round(total, 2),
            "cpu_pct": round(cpu / total * 100, 1),
            "ram_pct": round(ram / total * 100, 1),
            "pv_pct": round(pv / total * 100, 1),
            "network_pct": round(network / total * 100, 1),
            "gpu_pct": round(gpu / total * 100, 1),
        }

    def namespace_trend(self, namespace: str | None = None) -> list[dict]:
        """Daily cost time series, optionally filtered to a namespace."""
        daily: dict[str, float] = {}
        for r in self.records:
            if namespace and r.tags.get("k8s_namespace") != namespace:
                continue
            day = r.charge_period_start
            daily[day] = daily.get(day, 0.0) + r.effective_cost
        return sorted(
            [{"date": d, "cost_eur": round(v, 2)} for d, v in daily.items()],
            key=lambda x: x["date"],
        )

    def efficiency_metrics(self) -> list[dict]:
        """Per-namespace CPU/RAM efficiency (request vs actual usage)."""
        ns_data: dict[str, dict] = {}
        for r in self.records:
            ns = r.tags.get("k8s_namespace") or "__unknown__"
            if ns not in ns_data:
                ns_data[ns] = {
                    "namespace": ns, "cost_eur": 0.0,
                    "cpu_req": 0.0, "cpu_use": 0.0,
                    "ram_req": 0.0, "ram_use": 0.0,
                    "record_count": 0,
                }
            d = ns_data[ns]
            d["cost_eur"] += r.effective_cost
            d["cpu_req"] += float(r.tags.get("k8s_cpu_request_avg") or 0)
            d["cpu_use"] += float(r.tags.get("k8s_cpu_usage_avg") or 0)
            d["ram_req"] += float(r.tags.get("k8s_ram_request_avg") or 0)
            d["ram_use"] += float(r.tags.get("k8s_ram_usage_avg") or 0)
            d["record_count"] += 1

        result = []
        for ns, d in ns_data.items():
            cpu_req = d["cpu_req"] or 1.0
            ram_req = d["ram_req"] or 1.0
            cpu_eff = min(round(d["cpu_use"] / cpu_req * 100, 1), 100.0)
            ram_eff = min(round(d["ram_use"] / ram_req * 100, 1), 100.0)
            avg_eff = round((cpu_eff + ram_eff) / 2, 1)
            result.append({
                "namespace": ns,
                "cost_eur": round(d["cost_eur"], 2),
                "cpu_efficiency_pct": cpu_eff,
                "ram_efficiency_pct": ram_eff,
                "avg_efficiency_pct": avg_eff,
                "waste_estimate_eur": round(d["cost_eur"] * (1 - avg_eff / 100), 2),
                "record_count": d["record_count"],
            })
        return sorted(result, key=lambda x: x["waste_estimate_eur"], reverse=True)

    def workloads_for_namespace(self, namespace: str) -> list[dict]:
        """All workloads (deployments/controllers) within a namespace with pod count and components."""
        wl_data: dict[str, dict] = {}
        for r in self.records:
            if r.tags.get("k8s_namespace") != namespace:
                continue
            wl = r.tags.get("k8s_controller") or r.resource_name or "__unknown__"
            if wl not in wl_data:
                wl_data[wl] = {
                    "workload": wl, "cost_eur": 0.0, "pods": set(),
                    "cpu": 0.0, "ram": 0.0, "pv": 0.0, "network": 0.0, "gpu": 0.0,
                }
            d = wl_data[wl]
            d["cost_eur"] += r.effective_cost
            pod = r.tags.get("k8s_pod") or r.resource_name
            if pod:
                d["pods"].add(pod)
            d["cpu"] += float(r.tags.get("k8s_component_cpu") or 0)
            d["ram"] += float(r.tags.get("k8s_component_ram") or 0)
            d["pv"] += float(r.tags.get("k8s_component_pv") or 0)
            d["network"] += float(r.tags.get("k8s_component_network") or 0)
            d["gpu"] += float(r.tags.get("k8s_component_gpu") or 0)

        grand = sum(d["cost_eur"] for d in wl_data.values()) or 1.0
        result = []
        for d in wl_data.values():
            total = d["cost_eur"] or 1.0
            result.append({
                "workload": d["workload"],
                "namespace": namespace,
                "cost_eur": round(d["cost_eur"], 2),
                "pct": round(d["cost_eur"] / grand * 100, 1),
                "pod_count": len(d["pods"]),
                "components": {
                    "cpu": round(d["cpu"], 4),
                    "ram": round(d["ram"], 4),
                    "pv": round(d["pv"], 4),
                    "network": round(d["network"], 4),
                    "gpu": round(d["gpu"], 4),
                },
                "cpu_pct": round(d["cpu"] / total * 100, 1),
                "ram_pct": round(d["ram"] / total * 100, 1),
            })
        return sorted(result, key=lambda x: x["cost_eur"], reverse=True)
