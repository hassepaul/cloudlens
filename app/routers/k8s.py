"""Kubernetes cost allocation router — /api/v1/k8s"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.focus import ProviderName
from app.models.tenant import TenantConfig
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.k8s_cost import K8sCostClient, normalize_k8s_allocation, K8sCostBreakdown

# ── Shared helper ─────────────────────────────────────────────────────────────

async def _resolve_cluster(
    tenant_id: str,
    cluster_id: str,
) -> tuple[str, ProviderName]:
    """
    Look up the cluster config for a tenant and return (opencost_url, provider).
    Raises HTTPException on 404/503/422.
    """
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    clusters = doc.get("k8s_clusters", [])
    cluster_cfg = next((c for c in clusters if c["cluster_id"] == cluster_id), None)
    if not cluster_cfg:
        raise HTTPException(
            status_code=404,
            detail={"error": "CLUSTER_NOT_FOUND",
                    "message": f"Cluster '{cluster_id}' not registered."},
        )
    if not cluster_cfg.get("enabled", True):
        raise HTTPException(
            status_code=422,
            detail={"error": "CLUSTER_DISABLED",
                    "message": f"Cluster '{cluster_id}' is disabled."},
        )
    cloud = cluster_cfg.get("cloud", "azure")
    provider = ProviderName.AWS if cloud == "aws" else (
        ProviderName.GCP if cloud == "gcp" else ProviderName.AZURE
    )
    return cluster_cfg["opencost_url"], provider

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/k8s", tags=["kubernetes"],
    dependencies=[Depends(rate_limit_tenant)],
)
admin_router = APIRouter(
    prefix="/api/v1/k8s", tags=["kubernetes"],
    dependencies=[Depends(require_api_key)],
)


class K8sClusterConfig(BaseModel):
    """Configuration for one Kubernetes cluster."""
    cluster_id: str = Field(..., description="Stable cluster identifier (e.g. AKS cluster name)")
    opencost_url: str = Field(
        ...,
        description=(
            "Base URL of the OpenCost / Kubecost service reachable from CloudLens "
            "(e.g. https://opencost.internal.example.com). "
            "For private clusters, expose via Azure Application Gateway or NGINX ingress."
        ),
    )
    cloud: str = Field(default="azure", description="Cloud provider of this cluster")
    enabled: bool = Field(default=True)


# ── Cluster registration (admin) ─────────────────────────────────────────────

@admin_router.post("/{tenant_id}/clusters", status_code=201)
async def register_cluster(tenant_id: str, config: K8sClusterConfig) -> dict:
    """Register an OpenCost endpoint for a tenant's Kubernetes cluster."""
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        tenant = TenantConfig.from_cosmos(doc)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    clusters: list[dict] = tenant.extra.get("k8s_clusters", [])  # type: ignore[attr-defined]
    if any(c["cluster_id"] == config.cluster_id for c in clusters):
        raise HTTPException(
            status_code=409,
            detail={"error": "CONFLICT",
                    "message": f"Cluster '{config.cluster_id}' already registered."},
        )
    clusters.append(config.model_dump())
    doc_update = {**doc, "k8s_clusters": clusters}
    try:
        await cosmos.upsert_item(settings.cosmos_container_tenants, doc_update)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    log.info("k8s.cluster_registered", tenant_id=tenant_id, cluster_id=config.cluster_id)
    return {"tenant_id": tenant_id, "cluster_id": config.cluster_id, "status": "registered"}


@admin_router.get("/{tenant_id}/clusters")
async def list_clusters(tenant_id: str) -> dict:
    """List registered Kubernetes clusters for a tenant."""
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {
        "tenant_id": tenant_id,
        "clusters": doc.get("k8s_clusters", []),
    }


# ── Cost breakdown — shared fetch helper ─────────────────────────────────────

async def _fetch_breakdown(
    tenant_id: str,
    cluster_id: str,
    days: int,
    aggregate: str,
) -> tuple[K8sCostBreakdown, date, date]:
    """Fetch allocation from OpenCost and return (breakdown, start, end)."""
    opencost_url, provider = await _resolve_cluster(tenant_id, cluster_id)
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    client = K8sCostClient(opencost_url, cluster_id)
    try:
        raw = await client.get_allocation(start_date, end_date, aggregate=aggregate)
    except Exception as exc:
        log.warning("k8s.opencost_unreachable", cluster=cluster_id, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"error": "OPENCOST_UNREACHABLE",
                    "message": f"Cannot reach OpenCost at {opencost_url}. "
                               "Ensure OpenCost is running and the URL is reachable."},
        )
    records = normalize_k8s_allocation(tenant_id, cluster_id, raw, provider_name=provider)
    return K8sCostBreakdown(records), start_date, end_date


# ── Cost breakdown endpoints ──────────────────────────────────────────────────

@router.get("/{tenant_id}/allocation")
async def k8s_allocation(
    tenant_id: str,
    cluster_id: str = Query(..., description="Cluster ID to query"),
    days: int = Query(default=7, ge=1, le=90),
    aggregate: str = Query(
        default="namespace",
        description="Aggregation dimension: namespace | pod | deployment | label:<key>",
    ),
) -> dict:
    """
    Return Kubernetes pod/namespace/deployment cost breakdown from OpenCost.

    Costs are expressed in EUR and reconcile with the Azure Cost Management
    totals for the same period (node pool compute cost is the denominator).
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, aggregate
    )
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "aggregate": aggregate,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": breakdown.total_eur(),
        "by_namespace": breakdown.by_namespace(),
        "by_workload": breakdown.by_workload(),
        "record_count": len(breakdown.records),
        "note": (
            "Costs are derived from OpenCost/Kubecost node-cost attribution and "
            "reconcile with cloud billing totals for the same period."
        ),
    }


@router.get("/{tenant_id}/allocation/namespaces")
async def k8s_namespace_summary(
    tenant_id: str,
    cluster_id: str = Query(...),
    days: int = Query(default=30, ge=1, le=90),
) -> dict:
    """Cost per namespace — most useful for chargeback reporting."""
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "namespace"
    )
    ns_list = breakdown.by_namespace()
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": breakdown.total_eur(),
        "namespaces": ns_list,
        "namespace_count": len(ns_list),
    }


@router.get("/{tenant_id}/allocation/pods/{namespace}")
async def k8s_pods_in_namespace(
    tenant_id: str,
    namespace: str,
    cluster_id: str = Query(...),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """
    Per-pod cost breakdown within a namespace, with detailed cost components
    (cpu / ram / pv / network / gpu) for each pod.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "pod"
    )
    pods = breakdown.by_pod(namespace=namespace)
    components = breakdown.cost_components(namespace=namespace)
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": sum(p["cost_eur"] for p in pods),
        "pod_count": len(pods),
        "pods": pods,
        "cost_components": components,
    }


@router.get("/{tenant_id}/allocation/pods")
async def k8s_pod_breakdown(
    tenant_id: str,
    cluster_id: str = Query(...),
    namespace: str = Query(..., description="Namespace to drill into"),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """Per-pod cost breakdown within a namespace (query-param variant)."""
    return await k8s_pods_in_namespace(
        tenant_id, namespace=namespace, cluster_id=cluster_id, days=days
    )


@router.get("/{tenant_id}/allocation/workloads/{namespace}")
async def k8s_workloads_in_namespace(
    tenant_id: str,
    namespace: str,
    cluster_id: str = Query(...),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """
    All deployments/controllers in a namespace with pod count,
    cost components, and CPU/RAM share breakdown.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "deployment"
    )
    workloads = breakdown.workloads_for_namespace(namespace)
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": sum(w["cost_eur"] for w in workloads),
        "workload_count": len(workloads),
        "workloads": workloads,
    }


@router.get("/{tenant_id}/allocation/nodes")
async def k8s_node_breakdown(
    tenant_id: str,
    cluster_id: str = Query(...),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """
    Per-node cost breakdown showing total spend, pod density,
    and namespace spread per node.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "node"
    )
    nodes = breakdown.by_node()
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": breakdown.total_eur(),
        "node_count": len(nodes),
        "nodes": nodes,
    }


@router.get("/{tenant_id}/allocation/trends")
async def k8s_cost_trends(
    tenant_id: str,
    cluster_id: str = Query(...),
    namespace: Optional[str] = Query(default=None, description="Filter to a specific namespace"),
    days: int = Query(default=14, ge=1, le=90),
) -> dict:
    """
    Daily cost trend for the cluster (or a specific namespace).
    Returns a sorted time series suitable for sparkline/chart rendering.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "namespace"
    )
    trend = breakdown.namespace_trend(namespace=namespace)
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": sum(d["cost_eur"] for d in trend),
        "data_points": len(trend),
        "trend": trend,
    }


@router.get("/{tenant_id}/allocation/components")
async def k8s_cost_components(
    tenant_id: str,
    cluster_id: str = Query(...),
    namespace: Optional[str] = Query(default=None, description="Filter to a namespace"),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """
    Aggregate CPU / RAM / PV / Network / GPU cost components,
    with percentage share of total — ideal for donut/bar charts.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "pod"
    )
    components = breakdown.cost_components(namespace=namespace)
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        **components,
    }


@router.get("/{tenant_id}/allocation/efficiency")
async def k8s_efficiency(
    tenant_id: str,
    cluster_id: str = Query(...),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """
    Per-namespace CPU and RAM efficiency (request vs actual usage).
    Returns waste estimate in EUR, sorted by highest waste first.

    Requires OpenCost to be configured with in-cluster Prometheus so that
    cpuCoreUsageAverage and ramByteUsageAverage are populated.
    """
    breakdown, start_date, end_date = await _fetch_breakdown(
        tenant_id, cluster_id, days, "pod"
    )
    efficiency = breakdown.efficiency_metrics()
    total_waste = sum(e["waste_estimate_eur"] for e in efficiency)
    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": breakdown.total_eur(),
        "total_waste_estimate_eur": round(total_waste, 2),
        "cluster_avg_efficiency_pct": round(
            sum(e["avg_efficiency_pct"] for e in efficiency) / len(efficiency)
            if efficiency else 0.0, 1
        ),
        "namespaces": efficiency,
    }
