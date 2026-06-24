"""
Cloud provider abstraction
==========================

Every cloud (and AI vendor) implements CloudProvider. Each adapter knows how to
authenticate, pull native billing data, and normalize it into FocusRecord. The
ingest job is provider-agnostic: it iterates a tenant's configured providers and
calls the same three methods on each.

`normalize()` is deliberately separated from `fetch_*` so it can be unit-tested
against captured/mock native payloads without live credentials — the same honest
boundary as the Azure cost parser.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date

from app.models.focus import FocusRecord, Commitment, ProviderName, ServiceCategory


# ── Shared service-category mapping helpers ──────────────────────────────────
# Each provider names services differently; we map to FOCUS ServiceCategory.
_CATEGORY_KEYWORDS = {
    ServiceCategory.COMPUTE: ("compute", "ec2", "vm", "virtual machine", "instance",
                              "kubernetes", "container", "function", "lambda", "app service",
                              "gke", "aks", "eks", "ecs", "fargate"),
    ServiceCategory.STORAGE: ("storage", "s3", "blob", "disk", "ebs", "bucket", "filestore"),
    ServiceCategory.DATABASES: ("sql", "database", "cosmos", "dynamodb", "rds", "bigtable",
                                "spanner", "redis", "cache", "mongo", "aurora"),
    ServiceCategory.NETWORKING: ("network", "vpc", "load balancer", "cdn", "bandwidth",
                                 "egress", "gateway", "dns", "route"),
    ServiceCategory.AI_ML: ("sagemaker", "bedrock", "vertex", "openai", "anthropic",
                            "machine learning", "cognitive", "ai ", "gpt", "claude",
                            "inference", "gpu"),
    ServiceCategory.ANALYTICS: ("analytics", "bigquery", "athena", "synapse", "databricks",
                                "redshift", "emr", "dataflow", "kinesis"),
    ServiceCategory.SECURITY: ("security", "kms", "key vault", "waf", "guardduty", "sentinel"),
    ServiceCategory.MANAGEMENT: ("monitor", "log", "cloudwatch", "governance", "management"),
}


def classify_service(service_name: str) -> ServiceCategory:
    s = (service_name or "").lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in s for k in keywords):
            return cat
    return ServiceCategory.OTHER


class CloudProvider(ABC):
    """Interface every cloud / AI billing adapter implements."""

    provider_name: ProviderName

    @abstractmethod
    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """Return raw, provider-native billing rows for the period."""
        ...

    @abstractmethod
    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        """Map native rows to FOCUS records. Pure function — unit-testable."""
        ...

    async def fetch_commitments(self) -> list[dict]:
        """Return raw reservation / savings-plan / CUD inventory. Optional."""
        return []

    def normalize_commitments(self, tenant_id: str, raw: list[dict]) -> list[Commitment]:
        """Map native commitment inventory to Commitment models. Optional."""
        return []


# ── Registry ─────────────────────────────────────────────────────────────────
_PROVIDERS: dict[str, type[CloudProvider]] = {}


def register(key: str):
    def deco(cls):
        _PROVIDERS[key] = cls
        return cls
    return deco


def get_provider_class(key: str) -> type[CloudProvider]:
    if key not in _PROVIDERS:
        raise KeyError(f"Unknown provider '{key}'. Registered: {list(_PROVIDERS)}")
    return _PROVIDERS[key]


def registered_providers() -> list[str]:
    return sorted(_PROVIDERS)
