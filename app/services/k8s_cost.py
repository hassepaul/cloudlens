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
    """
    records: list[FocusRecord] = []
    for alloc in raw:
        total = float(alloc.get("totalCost") or 0.0)
        if total <= 0.0:
            continue

        day = alloc.get("_day", str(date.today()))
        aggregate_key = alloc.get("_aggregate_key", "__idle__")
        namespace = alloc.get("properties", {}).get("namespace", aggregate_key)
        pod = alloc.get("properties", {}).get("pod", "")
        deployment = alloc.get("properties", {}).get("deployment", "")
        node = alloc.get("properties", {}).get("node", "")
        labels = alloc.get("properties", {}).get("labels", {})

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
                f"cpu:{round(float(alloc.get('cpuCost') or 0),4)} "
                f"ram:{round(float(alloc.get('ramCost') or 0),4)} "
                f"pv:{round(float(alloc.get('pvCost') or 0),4)}"
            ),
            resource_id=resource_path,
            resource_name=deployment or pod or aggregate_key,
            tags={
                **{f"k8s_{k}": str(v) for k, v in labels.items()},
                "k8s_cluster": cluster_id,
                "k8s_namespace": namespace,
                "k8s_node": node,
            },
            commitment_discount_type=CommitmentDiscountType.NONE,
        ))
    return records


class K8sCostBreakdown:
    """High-level aggregation of K8s costs for the API response."""

    def __init__(self, records: list[FocusRecord]):
        self.records = records

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
