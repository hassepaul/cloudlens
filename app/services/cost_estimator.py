"""
Infrastructure cost estimator — parses Terraform plan JSON and returns
a per-resource monthly cost estimate using a built-in pricing catalog.

Usage
─────
  POST /api/v1/estimate/terraform
  Body: output of `terraform show -json <plan-file>`

Supported providers & resource types
────────────────────────────────────
  AWS  : aws_instance, aws_db_instance, aws_rds_cluster, aws_lambda_function,
         aws_nat_gateway, aws_elasticache_cluster, aws_elasticsearch_domain,
         aws_eks_node_group, aws_sqs_queue, aws_kinesis_firehose_delivery_stream
  Azure: azurerm_linux_virtual_machine, azurerm_windows_virtual_machine,
         azurerm_sql_database, azurerm_mysql_flexible_server,
         azurerm_postgresql_flexible_server, azurerm_kubernetes_cluster,
         azurerm_app_service_plan, azurerm_redis_cache
  GCP  : google_compute_instance, google_sql_database_instance,
         google_container_cluster, google_cloud_run_service,
         google_pubsub_topic

Pricing notes
─────────────
  All prices are approximate on-demand EUR/month for a 730-hour month
  (us-east-1 / eastus / us-central1) as of mid-2026.  They are
  conservative estimates — actual costs may vary by region, commitment,
  and usage pattern.  Set confidence="approximate" always.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.config import get_settings
from app.services import cosmos


# ── Pricing catalog ───────────────────────────────────────────────────────────
# Structure: {resource_type: {"default": eur/month, "by_size": {key: eur/month}}}

_HOURS_PER_MONTH = 730.0

_AWS_EC2: dict[str, float] = {
    # General purpose — t3
    "t3.nano": 3.80, "t3.micro": 7.59, "t3.small": 15.18, "t3.medium": 30.37,
    "t3.large": 60.74, "t3.xlarge": 121.48, "t3.2xlarge": 242.96,
    # Graviton — t4g
    "t4g.nano": 2.92, "t4g.micro": 5.84, "t4g.small": 11.68, "t4g.medium": 23.36,
    "t4g.large": 46.72, "t4g.xlarge": 93.44, "t4g.2xlarge": 186.88,
    # General purpose — m5 / m6i
    "m5.large": 70.08, "m5.xlarge": 140.16, "m5.2xlarge": 280.32,
    "m5.4xlarge": 560.64, "m5.8xlarge": 1121.28,
    "m6i.large": 72.16, "m6i.xlarge": 144.32, "m6i.2xlarge": 288.64,
    "m6i.4xlarge": 577.28, "m6i.8xlarge": 1154.56,
    # Compute optimised — c5 / c6i
    "c5.large": 62.05, "c5.xlarge": 124.10, "c5.2xlarge": 248.20,
    "c5.4xlarge": 496.40, "c5.9xlarge": 1116.90,
    "c6i.large": 62.05, "c6i.xlarge": 124.10, "c6i.2xlarge": 248.20,
    # Memory optimised — r5 / r6i
    "r5.large": 91.98, "r5.xlarge": 183.96, "r5.2xlarge": 367.92,
    "r5.4xlarge": 735.84, "r5.8xlarge": 1471.68,
    "r6i.large": 95.26, "r6i.xlarge": 190.52, "r6i.2xlarge": 381.04,
}

_AZURE_VM: dict[str, float] = {
    # Burstable — B series
    "Standard_B1ls": 3.65, "Standard_B1ms": 11.83, "Standard_B1s": 7.30,
    "Standard_B2ms": 47.30, "Standard_B2s": 23.65, "Standard_B4ms": 94.60,
    "Standard_B8ms": 189.20, "Standard_B16ms": 378.40,
    # General purpose — D v3/v5
    "Standard_D2s_v3": 70.08, "Standard_D4s_v3": 140.16, "Standard_D8s_v3": 280.32,
    "Standard_D2s_v5": 72.26, "Standard_D4s_v5": 144.52, "Standard_D8s_v5": 289.04,
    "Standard_D16s_v5": 578.08, "Standard_D32s_v5": 1156.16,
    "Standard_D2as_v5": 65.70, "Standard_D4as_v5": 131.40, "Standard_D8as_v5": 262.80,
    # Memory optimised — E v5
    "Standard_E2s_v5": 99.28, "Standard_E4s_v5": 198.56, "Standard_E8s_v5": 397.12,
    "Standard_E16s_v5": 794.24, "Standard_E32s_v5": 1588.48,
    # Compute optimised — F v2
    "Standard_F2s_v2": 61.98, "Standard_F4s_v2": 123.96, "Standard_F8s_v2": 247.92,
    "Standard_F16s_v2": 495.84, "Standard_F32s_v2": 991.68,
    # High memory — M series
    "Standard_M8ms": 672.60, "Standard_M16ms": 1345.20,
}

_GCP_COMPUTE: dict[str, float] = {
    # e2 (cost-optimised)
    "e2-micro": 6.11, "e2-small": 12.23, "e2-medium": 24.46,
    "e2-standard-2": 48.92, "e2-standard-4": 97.84, "e2-standard-8": 195.68,
    "e2-standard-16": 391.36, "e2-standard-32": 782.72,
    "e2-highmem-2": 61.15, "e2-highmem-4": 122.30, "e2-highmem-8": 244.60,
    "e2-highcpu-2": 36.69, "e2-highcpu-4": 73.38, "e2-highcpu-8": 146.76,
    # n2 (balanced)
    "n2-standard-2": 70.89, "n2-standard-4": 141.78, "n2-standard-8": 283.56,
    "n2-standard-16": 567.12, "n2-standard-32": 1134.24,
    "n2-highmem-2": 88.61, "n2-highmem-4": 177.22, "n2-highmem-8": 354.44,
    # n1 (previous generation)
    "n1-standard-1": 34.68, "n1-standard-2": 69.36, "n1-standard-4": 138.72,
    "n1-standard-8": 277.44, "n1-standard-16": 554.88,
    # c2 (compute-optimised)
    "c2-standard-4": 140.58, "c2-standard-8": 281.16, "c2-standard-16": 562.32,
}

_CATALOG: dict[str, dict] = {
    # ── AWS ──────────────────────────────────────────────────────────────────
    "aws_instance": {
        "size_attr": "instance_type",
        "by_size": _AWS_EC2,
        "default": 70.08,  # m5.large fallback
    },
    "aws_db_instance": {
        "size_attr": "instance_class",
        "by_size": {
            "db.t3.micro": 18.98, "db.t3.small": 37.96, "db.t3.medium": 75.92,
            "db.t3.large": 151.84, "db.t3.xlarge": 303.68, "db.t3.2xlarge": 607.36,
            "db.m5.large": 130.14, "db.m5.xlarge": 260.28, "db.m5.2xlarge": 520.56,
            "db.m5.4xlarge": 1041.12, "db.m5.8xlarge": 2082.24,
            "db.r5.large": 171.54, "db.r5.xlarge": 343.08, "db.r5.2xlarge": 686.16,
        },
        "default": 130.14,
        "extra_storage_per_gb": 0.115,
        "storage_attr": "allocated_storage",
    },
    "aws_rds_cluster": {
        "size_attr": "db_cluster_instance_class",
        "by_size": {},  # Aurora Serverless v2 ACU-based, use default
        "default": 260.28,  # ~2x db.m5.large for HA cluster
    },
    "aws_lambda_function": {
        "default": 2.00,  # ~2M invocations/month at 128 MB — minimal
    },
    "aws_nat_gateway": {
        "default": 32.85,  # $0.045/h + data transfer
    },
    "aws_elasticache_cluster": {
        "size_attr": "node_type",
        "by_size": {
            "cache.t3.micro": 11.68, "cache.t3.small": 23.36, "cache.t3.medium": 46.72,
            "cache.r6g.large": 91.25, "cache.r6g.xlarge": 182.50,
            "cache.m6g.large": 73.00, "cache.m6g.xlarge": 146.00,
        },
        "default": 73.00,
    },
    "aws_eks_node_group": {
        "size_attr": "instance_types",  # list — take first
        "by_size": _AWS_EC2,
        "default": 140.16,  # m5.xlarge fallback
        "is_list_attr": True,
    },
    "aws_kinesis_firehose_delivery_stream": {
        "default": 14.60,  # ~5 GB/day at $0.029/GB
    },

    # ── Azure ─────────────────────────────────────────────────────────────────
    "azurerm_linux_virtual_machine": {
        "size_attr": "size",
        "by_size": _AZURE_VM,
        "default": 72.26,  # Standard_D2s_v5 fallback
    },
    "azurerm_windows_virtual_machine": {
        "size_attr": "size",
        "by_size": {k: v * 1.28 for k, v in _AZURE_VM.items()},  # ~28% Windows surcharge
        "default": 92.49,
    },
    "azurerm_sql_database": {
        "size_attr": "sku_name",
        "by_size": {
            "Basic": 4.38, "S0": 14.60, "S1": 29.20, "S2": 73.00, "S3": 146.00,
            "S4": 292.00, "S6": 584.00, "S7": 876.00, "S9": 1460.00,
            "P1": 438.00, "P2": 730.00, "P4": 1460.00,
            "GP_Gen5_2": 243.72, "GP_Gen5_4": 487.44, "GP_Gen5_8": 974.88,
            "BC_Gen5_2": 731.16, "BC_Gen5_4": 1462.32,
        },
        "default": 73.00,
    },
    "azurerm_mysql_flexible_server": {
        "size_attr": "sku_name",
        "by_size": {
            "B_Standard_B1ms": 14.60, "B_Standard_B2s": 29.20,
            "GP_Standard_D2ds_v4": 109.50, "GP_Standard_D4ds_v4": 219.00,
            "MO_Standard_E4ds_v4": 438.00,
        },
        "default": 109.50,
    },
    "azurerm_postgresql_flexible_server": {
        "size_attr": "sku_name",
        "by_size": {
            "B_Standard_B1ms": 14.60, "B_Standard_B2ms": 29.20,
            "GP_Standard_D2ds_v4": 109.50, "GP_Standard_D4ds_v4": 219.00,
        },
        "default": 109.50,
    },
    "azurerm_kubernetes_cluster": {
        "default": 146.00,  # Management fee + default node pool (Standard_D2s_v5 x2)
    },
    "azurerm_app_service_plan": {
        "size_attr": "sku_name",
        "by_size": {
            "F1": 0.0, "D1": 7.30, "B1": 12.41, "B2": 24.82, "B3": 49.64,
            "S1": 56.21, "S2": 112.42, "S3": 224.84,
            "P1v3": 91.25, "P2v3": 182.50, "P3v3": 365.00,
            "P1mv3": 109.50, "P2mv3": 219.00, "P3mv3": 438.00,
        },
        "default": 91.25,  # P1v3
    },
    "azurerm_redis_cache": {
        "size_attr": "sku_name",
        "by_size": {
            "Basic_C0": 16.79, "Basic_C1": 33.58, "Basic_C2": 103.95,
            "Standard_C0": 33.58, "Standard_C1": 67.16, "Standard_C2": 207.90,
            "Premium_P1": 219.00, "Premium_P2": 438.00,
        },
        "default": 67.16,
    },

    # ── GCP ───────────────────────────────────────────────────────────────────
    "google_compute_instance": {
        "size_attr": "machine_type",
        "by_size": _GCP_COMPUTE,
        "default": 70.89,  # n2-standard-2 fallback
    },
    "google_sql_database_instance": {
        "size_attr": "database_version",
        "by_size": {},
        "default": 130.00,  # db-n1-standard-2
    },
    "google_container_cluster": {
        "default": 73.00,  # management fee + default node pool
    },
    "google_cloud_run_service": {
        "default": 5.00,  # minimal — pay-per-use
    },
    "google_pubsub_topic": {
        "default": 0.50,  # negligible unless high throughput
    },
}

# Resource types that carry no direct compute cost
_FREE_TYPES: frozenset[str] = frozenset({
    "aws_s3_bucket", "aws_iam_role", "aws_iam_policy", "aws_security_group",
    "aws_vpc", "aws_subnet", "aws_route_table", "aws_internet_gateway",
    "aws_key_pair", "aws_eip",
    "azurerm_resource_group", "azurerm_virtual_network", "azurerm_subnet",
    "azurerm_network_security_group", "azurerm_public_ip",
    "google_project", "google_project_iam_member", "google_storage_bucket",
})


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ResourceEstimate:
    address: str
    resource_type: str
    action: str           # "create" | "update" | "delete" | "no-op"
    provider: str         # "aws" | "azure" | "gcp" | "unknown"
    size: str
    monthly_delta_eur: float   # positive = added cost, negative = removed cost
    confidence: str       # "catalog" | "default" | "free" | "unsupported"
    notes: str


@dataclass
class CostEstimate:
    resources: list[ResourceEstimate] = field(default_factory=list)
    total_monthly_delta_eur: float = 0.0
    breakdown_by_action: dict[str, float] = field(default_factory=dict)
    unsupported_resource_types: list[str] = field(default_factory=list)
    total_resources_analyzed: int = 0
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "total_monthly_delta_eur": round(self.total_monthly_delta_eur, 2),
            "total_annual_delta_eur": round(self.total_monthly_delta_eur * 12, 2),
            "breakdown_by_action": {k: round(v, 2) for k, v in self.breakdown_by_action.items()},
            "total_resources_analyzed": self.total_resources_analyzed,
            "unsupported_resource_types": sorted(set(self.unsupported_resource_types)),
            "generated_at": self.generated_at,
            "resources": [
                {
                    "address": r.address,
                    "resource_type": r.resource_type,
                    "action": r.action,
                    "provider": r.provider,
                    "size": r.size,
                    "monthly_delta_eur": round(r.monthly_delta_eur, 2),
                    "confidence": r.confidence,
                    "notes": r.notes,
                }
                for r in self.resources
                if r.action != "no-op"
            ],
        }


# ── Provider detection ────────────────────────────────────────────────────────

def _detect_provider(resource_type: str) -> str:
    if resource_type.startswith("aws_"):
        return "aws"
    if resource_type.startswith("azurerm_") or resource_type.startswith("azure"):
        return "azure"
    if resource_type.startswith("google_"):
        return "gcp"
    return "unknown"


# ── Per-resource pricing ──────────────────────────────────────────────────────

def _lookup_price(resource_type: str, config: dict[str, Any]) -> tuple[float, str, str]:
    """
    Return (monthly_eur, size_key, confidence).
    confidence is "catalog", "default", "free", or "unsupported".
    """
    if resource_type in _FREE_TYPES:
        return 0.0, "—", "free"

    entry = _CATALOG.get(resource_type)
    if not entry:
        return 0.0, "?", "unsupported"

    size_attr = entry.get("size_attr")
    size_key = "—"
    price = 0.0
    confidence = "default"

    if size_attr:
        raw = config.get(size_attr)
        # Some attrs are lists (e.g. instance_types in aws_eks_node_group)
        if entry.get("is_list_attr") and isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw:
            size_key = str(raw)
            by_size = entry.get("by_size", {})
            if size_key in by_size:
                price = by_size[size_key]
                confidence = "catalog"
            else:
                price = entry.get("default", 0.0)
        else:
            price = entry.get("default", 0.0)
    else:
        price = entry.get("default", 0.0)
        if price:
            confidence = "catalog"

    # Add storage cost if applicable
    storage_attr = entry.get("storage_attr")
    if storage_attr:
        storage_gb = config.get(storage_attr, 20)
        try:
            storage_gb = float(storage_gb)
        except (TypeError, ValueError):
            storage_gb = 20.0
        price += storage_gb * entry.get("extra_storage_per_gb", 0.0)

    return price, size_key, confidence


# ── Plan parser ───────────────────────────────────────────────────────────────

def _normalize_actions(actions: list[str]) -> str:
    """Collapse Terraform action tuples to a single canonical action."""
    action_set = set(actions)
    if "create" in action_set:
        return "create"
    if "delete" in action_set:
        return "delete"
    if "update" in action_set:
        return "update"
    return "no-op"


def estimate_plan(plan_json: str | dict) -> CostEstimate:
    """
    Parse a `terraform show -json` plan and return a CostEstimate.

    Raises ValueError if the input is not a valid Terraform plan JSON.
    """
    if isinstance(plan_json, str):
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
    else:
        plan = plan_json

    resource_changes = plan.get("resource_changes")
    if resource_changes is None:
        raise ValueError("Plan JSON missing 'resource_changes' array — ensure input is from `terraform show -json`")
    if not isinstance(resource_changes, list):
        raise ValueError("Plan JSON 'resource_changes' must be an array")

    estimate = CostEstimate(generated_at=datetime.now(timezone.utc).isoformat())
    estimate.total_resources_analyzed = len(resource_changes)
    breakdown: dict[str, float] = {}

    for change in resource_changes:
        address = change.get("address", "?")
        resource_type = change.get("type", "?")
        change_detail = change.get("change", {})
        actions = change_detail.get("actions", ["no-op"])
        action = _normalize_actions(actions)

        # Config is "after" for creates/updates; "before" for deletes
        if action == "delete":
            config = change_detail.get("before") or {}
        else:
            config = change_detail.get("after") or {}

        provider = _detect_provider(resource_type)
        monthly_price, size_key, confidence = _lookup_price(resource_type, config)

        if confidence == "unsupported":
            estimate.unsupported_resource_types.append(resource_type)

        # Sign: creates add cost, deletes reduce it, updates are net-zero
        if action == "delete":
            delta = -monthly_price
        elif action in ("create",):
            delta = monthly_price
        else:
            delta = 0.0  # update: old cost ≈ new cost; flag as change but no net delta

        notes_parts: list[str] = []
        if action == "update":
            notes_parts.append("in-place update — delta not computed")
        if confidence == "default":
            notes_parts.append("size not recognised — using category default")
        if confidence == "free":
            notes_parts.append("no direct compute cost")
        if confidence == "unsupported":
            notes_parts.append("resource type not in pricing catalog")

        estimate.resources.append(ResourceEstimate(
            address=address,
            resource_type=resource_type,
            action=action,
            provider=provider,
            size=size_key,
            monthly_delta_eur=delta,
            confidence=confidence,
            notes="; ".join(notes_parts),
        ))

        estimate.total_monthly_delta_eur += delta
        breakdown[action] = breakdown.get(action, 0.0) + delta

    estimate.breakdown_by_action = breakdown
    return estimate


# ── Pipeline run persistence ──────────────────────────────────────────────────

@dataclass
class PipelineRun:
    """A persisted CI/CD cost estimate run."""
    id: str
    type: str
    tenant_id: str
    label: str
    ci_system: str          # "github_actions" | "gitlab_ci" | "azure_devops" | "other"
    repo: str
    branch: str
    commit_sha: str
    pr_number: int | None
    total_monthly_delta_eur: float
    total_annual_delta_eur: float
    total_resources_analyzed: int
    budget_gate_eur: float | None
    gate_passed: bool | None
    breakdown_by_action: dict
    resources: list[dict]
    unsupported_resource_types: list[str]
    recorded_at: str
    ttl: int = 7_776_000    # 90 days


def compute_gate(monthly_delta: float, gate_eur: float) -> bool:
    """Return True (pass) if the net monthly increase is within the budget gate."""
    return monthly_delta <= gate_eur


def compute_drift(current: float, previous: float) -> float | None:
    """
    Return the percentage change from a previous run to the current one.
    Returns None if previous is zero (division undefined).
    """
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 2)


async def record_run(
    tenant_id: str,
    estimate: CostEstimate,
    *,
    label: str = "",
    ci_system: str = "other",
    repo: str = "",
    branch: str = "",
    commit_sha: str = "",
    pr_number: int | None = None,
    budget_gate_eur: float | None = None,
) -> PipelineRun:
    """Persist a pipeline estimate run to Cosmos."""
    gate_passed: bool | None = None
    if budget_gate_eur is not None:
        gate_passed = compute_gate(estimate.total_monthly_delta_eur, budget_gate_eur)

    run = PipelineRun(
        id=f"prun-{uuid4()}",
        type="pipeline_run",
        tenant_id=tenant_id,
        label=label,
        ci_system=ci_system,
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        pr_number=pr_number,
        total_monthly_delta_eur=round(estimate.total_monthly_delta_eur, 2),
        total_annual_delta_eur=round(estimate.total_monthly_delta_eur * 12, 2),
        total_resources_analyzed=estimate.total_resources_analyzed,
        budget_gate_eur=budget_gate_eur,
        gate_passed=gate_passed,
        breakdown_by_action={k: round(v, 2) for k, v in estimate.breakdown_by_action.items()},
        resources=estimate.to_dict().get("resources", []),
        unsupported_resource_types=sorted(set(estimate.unsupported_resource_types)),
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )

    doc = {k: v for k, v in run.__dict__.items()}
    doc["_partitionKey"] = tenant_id
    settings = get_settings()
    await cosmos.upsert_item(settings.cosmos_container_pipeline_runs, doc)
    return run


async def list_runs(tenant_id: str, limit: int = 50) -> list[dict]:
    """Return the most recent pipeline runs for a tenant."""
    settings = get_settings()
    docs = await cosmos.query_items(
        settings.cosmos_container_pipeline_runs,
        ("SELECT * FROM c WHERE c.tenant_id=@t AND c.type='pipeline_run' "
         "ORDER BY c.recorded_at DESC OFFSET 0 LIMIT @lim"),
        parameters=[
            {"name": "@t", "value": tenant_id},
            {"name": "@lim", "value": limit},
        ],
        partition_key=tenant_id,
    )
    for d in docs:
        d.pop("_partitionKey", None)
    return docs


def catalog_summary() -> dict:
    """Return a summary of all supported resource types and their prices."""
    entries = []
    for rtype, entry in _CATALOG.items():
        provider = _detect_provider(rtype)
        default = entry.get("default", 0.0)
        sizes = list(entry.get("by_size", {}).keys())
        entries.append({
            "resource_type": rtype,
            "provider": provider,
            "default_monthly_eur": default,
            "supported_sizes": sizes[:10],  # truncate for readability
            "total_sizes_in_catalog": len(sizes),
        })
    entries.sort(key=lambda e: (e["provider"], e["resource_type"]))
    return {
        "total_resource_types": len(entries),
        "providers": ["aws", "azure", "gcp"],
        "pricing_note": (
            "Approximate on-demand EUR/month for a 730-hour month. "
            "Region: us-east-1 / eastus / us-central1. "
            "Actual costs vary by region, commitment tier, and usage."
        ),
        "entries": entries,
    }
