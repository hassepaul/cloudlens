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
    # Store in Cosmos under the tenant doc extra field.
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


# ── Cost breakdown ────────────────────────────────────────────────────────────

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
                    "message": f"Cluster '{cluster_id}' not registered for this tenant. "
                               "Register it via POST /api/v1/k8s/{tenant_id}/clusters."},
        )
    if not cluster_cfg.get("enabled", True):
        raise HTTPException(
            status_code=422,
            detail={"error": "CLUSTER_DISABLED",
                    "message": f"Cluster '{cluster_id}' is disabled."},
        )

    opencost_url = cluster_cfg["opencost_url"]
    cloud = cluster_cfg.get("cloud", "azure")
    provider = ProviderName.AZURE
    if cloud == "aws":
        provider = ProviderName.AWS
    elif cloud == "gcp":
        provider = ProviderName.GCP

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
                               "Ensure OpenCost is running and the URL is accessible."},
        )

    records = normalize_k8s_allocation(tenant_id, cluster_id, raw, provider_name=provider)
    breakdown = K8sCostBreakdown(records)

    return {
        "tenant_id": tenant_id,
        "cluster_id": cluster_id,
        "aggregate": aggregate,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_cost_eur": breakdown.total_eur(),
        "by_namespace": breakdown.by_namespace(),
        "by_workload": breakdown.by_workload(),
        "record_count": len(records),
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
    """Cost per namespace for the period — most useful for chargeback."""
    return await k8s_allocation(tenant_id, cluster_id=cluster_id, days=days, aggregate="namespace")


@router.get("/{tenant_id}/allocation/pods")
async def k8s_pod_breakdown(
    tenant_id: str,
    cluster_id: str = Query(...),
    namespace: str = Query(..., description="Namespace to drill into"),
    days: int = Query(default=7, ge=1, le=30),
) -> dict:
    """Per-pod cost breakdown within a namespace."""
    aggregate = f"label:kubernetes.io/namespace={namespace}"
    return await k8s_allocation(tenant_id, cluster_id=cluster_id, days=days, aggregate="pod")
