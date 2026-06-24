"""
Instance catalog
================

A representative catalog of compute instance types across AWS, Azure, and GCP —
vCPU, memory (GiB), and an indicative on-demand hourly price — used by the
rightsizing engine to pick the cheapest target SKU that satisfies a workload's
observed CPU *and* memory requirements, including cross-family moves.

In production this is refreshed from each provider's pricing API (AWS Price List,
Azure Retail Prices, GCP Cloud Billing Catalog). The shape here is the contract
the rightsizing engine consumes; prices are indicative eu-region on-demand rates
and should be treated as representative, not billing-grade, until wired to the
live pricing feeds.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class InstanceType:
    provider: str
    name: str
    family: str            # "general" | "compute" | "memory" | "burstable"
    vcpu: int
    memory_gib: float
    hourly_usd: float


# Curated, cross-family subset (enough to demonstrate real downgrade paths).
_CATALOG: list[InstanceType] = [
    # ── AWS ──
    InstanceType("aws", "t3.medium",   "burstable", 2,   4.0,  0.0416),
    InstanceType("aws", "t3.large",    "burstable", 2,   8.0,  0.0832),
    InstanceType("aws", "t3.xlarge",   "burstable", 4,  16.0,  0.1664),
    InstanceType("aws", "m5.large",    "general",   2,   8.0,  0.096),
    InstanceType("aws", "m5.xlarge",   "general",   4,  16.0,  0.192),
    InstanceType("aws", "m5.2xlarge",  "general",   8,  32.0,  0.384),
    InstanceType("aws", "m5.4xlarge",  "general",  16,  64.0,  0.768),
    InstanceType("aws", "c5.large",    "compute",   2,   4.0,  0.085),
    InstanceType("aws", "c5.xlarge",   "compute",   4,   8.0,  0.17),
    InstanceType("aws", "c5.2xlarge",  "compute",   8,  16.0,  0.34),
    InstanceType("aws", "r5.large",    "memory",    2,  16.0,  0.126),
    InstanceType("aws", "r5.xlarge",   "memory",    4,  32.0,  0.252),
    InstanceType("aws", "r5.2xlarge",  "memory",    8,  64.0,  0.504),
    # ── Azure ──
    InstanceType("azure", "B2s",        "burstable", 2,   4.0,  0.0416),
    InstanceType("azure", "B2ms",       "burstable", 2,   8.0,  0.0832),
    InstanceType("azure", "D2s_v5",     "general",   2,   8.0,  0.096),
    InstanceType("azure", "D4s_v5",     "general",   4,  16.0,  0.192),
    InstanceType("azure", "D8s_v5",     "general",   8,  32.0,  0.384),
    InstanceType("azure", "D16s_v5",    "general",  16,  64.0,  0.768),
    InstanceType("azure", "F2s_v2",     "compute",   2,   4.0,  0.0846),
    InstanceType("azure", "F4s_v2",     "compute",   4,   8.0,  0.169),
    InstanceType("azure", "F8s_v2",     "compute",   8,  16.0,  0.338),
    InstanceType("azure", "E2s_v5",     "memory",    2,  16.0,  0.126),
    InstanceType("azure", "E4s_v5",     "memory",    4,  32.0,  0.252),
    InstanceType("azure", "E8s_v5",     "memory",    8,  64.0,  0.504),
    # ── GCP ──
    InstanceType("gcp", "e2-medium",      "general",   2,   4.0,  0.0335),
    InstanceType("gcp", "e2-standard-2",  "general",   2,   8.0,  0.0671),
    InstanceType("gcp", "e2-standard-4",  "general",   4,  16.0,  0.1342),
    InstanceType("gcp", "e2-standard-8",  "general",   8,  32.0,  0.2684),
    InstanceType("gcp", "n2-standard-2",  "general",   2,   8.0,  0.0971),
    InstanceType("gcp", "n2-standard-4",  "general",   4,  16.0,  0.1942),
    InstanceType("gcp", "c2-standard-4",  "compute",   4,  16.0,  0.2088),
    InstanceType("gcp", "n2-highmem-2",   "memory",    2,  16.0,  0.1310),
    InstanceType("gcp", "n2-highmem-4",   "memory",    4,  32.0,  0.2620),
]

# normalized lookup by provider+name
_BY_NAME = {(i.provider, i.name.lower()): i for i in _CATALOG}


def _norm_provider(p: str) -> str:
    p = (p or "").lower()
    if "amazon" in p or "aws" in p:
        return "aws"
    if "azure" in p or "microsoft" in p:
        return "azure"
    if "google" in p or "gcp" in p:
        return "gcp"
    return p


def lookup(provider: str, name: str) -> InstanceType | None:
    return _BY_NAME.get((_norm_provider(provider), (name or "").lower()))


def candidates_for(provider: str) -> list[InstanceType]:
    """All instance types for a provider, cheapest first."""
    p = _norm_provider(provider)
    return sorted([i for i in _CATALOG if i.provider == p], key=lambda i: i.hourly_usd)
