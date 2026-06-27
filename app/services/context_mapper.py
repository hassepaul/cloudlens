"""
Business Context Auto-Mapping
==============================

Automatically maps cloud spend to product lines and features by scanning:
  * Resource tags — product, app, application, feature, team, component, etc.
  * Kubernetes namespace / deployment label patterns in resource names.

No LLM is required — pure tag-and-regex inference.  This complements the
hierarchy service (which relies on explicit admin-defined tag filters) with
zero-configuration attribution on day one.

Attribution strategy (in priority order)
-----------------------------------------
1. Explicit tag: ``product=checkout`` → product "checkout"
2. Explicit tag: ``app=api-gateway`` → product "api-gateway"
3. Resource name regex: ``checkout-api-prod`` → product "checkout"
4. K8s namespace embedded in resource_name: ``ns/checkout/pod/api-…`` → product "checkout"
5. Unattributed — counted in ``unattributed_eur``

The service is read-only and doesn't write anything to Cosmos.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)

# Tag keys scanned for product-level context (checked in order).
_PRODUCT_KEYS = ["product", "app", "application", "service", "component", "module"]
# Tag keys scanned for feature-level context.
_FEATURE_KEYS = ["feature", "feature_flag", "experiment", "variant", "capability", "epic"]
# Tag keys for team ownership.
_TEAM_KEYS = ["team", "owner", "squad", "tribe", "group", "domain", "cost_center"]

# Regex to extract the first meaningful slug from a resource name.
# Captures the leading alphabetic token before the first separator.
_NAME_RE = re.compile(r"^([a-z][a-z0-9]{2,})", re.I)
# K8s namespace embedded as "namespaces/<ns>/" or "ns/<ns>/"
_K8S_NS_RE = re.compile(r"(?:namespaces?|ns)[/:]([a-z][a-z0-9-]{1,50})", re.I)


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class FeatureContext:
    name: str
    cost_eur: float
    pct_of_product: float
    resource_count: int
    clouds: list[str] = field(default_factory=list)


@dataclass
class ProductContext:
    name: str
    cost_eur: float
    pct_of_total: float
    features: list[FeatureContext] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    top_services: list[dict] = field(default_factory=list)  # [{service, cost_eur}]
    resource_count: int = 0
    clouds: list[str] = field(default_factory=list)
    inference_method: str = "tag"   # "tag" | "name" | "k8s"


@dataclass
class ContextMapping:
    tenant_id: str
    period_start: str
    period_end: str
    total_cost_eur: float
    attributed_eur: float
    unattributed_eur: float
    attribution_pct: float
    products: list[ProductContext] = field(default_factory=list)
    inference_notes: list[str] = field(default_factory=list)


# ── Pure extraction helpers ───────────────────────────────────────────────────

def _normalize(s: str) -> str:
    return s.strip().lower()


def _extract_product_from_tags(tags: dict) -> Optional[str]:
    """Return the product name from a tags dict, or None if not found."""
    for key in _PRODUCT_KEYS:
        for tag_key, tag_val in tags.items():
            if _normalize(tag_key) == key and tag_val:
                v = str(tag_val).strip()
                if v:
                    return v
    return None


def _extract_feature_from_tags(tags: dict) -> Optional[str]:
    for key in _FEATURE_KEYS:
        for tag_key, tag_val in tags.items():
            if _normalize(tag_key) == key and tag_val:
                v = str(tag_val).strip()
                if v:
                    return v
    return None


def _extract_team_from_tags(tags: dict) -> Optional[str]:
    for key in _TEAM_KEYS:
        for tag_key, tag_val in tags.items():
            if _normalize(tag_key) == key and tag_val:
                v = str(tag_val).strip()
                if v:
                    return v
    return None


def _infer_product_from_name(resource_name: str) -> tuple[Optional[str], str]:
    """
    Attempt to infer a product name from a resource name.
    Returns (product_name | None, method).
    """
    if not resource_name:
        return None, "none"

    # K8s namespace pattern takes priority
    m = _K8S_NS_RE.search(resource_name)
    if m:
        ns = m.group(1).lower()
        if len(ns) >= 3:
            return ns, "k8s"

    # Generic slug extraction
    m2 = _NAME_RE.match(resource_name.strip())
    if m2:
        slug = m2.group(1).lower()
        # Filter out generic/meaningless slugs
        if slug not in {"api", "app", "svc", "web", "db", "prod", "dev", "stg", "test"}:
            return slug, "name"

    return None, "none"


def _build_product_context(
    product: str,
    records: list[dict],
    total_cost: float,
) -> ProductContext:
    """Build a ProductContext from a list of cost records attributed to this product."""
    cost = sum(float(r.get("effective_cost") or 0) for r in records)
    pct = round(cost / total_cost * 100, 1) if total_cost > 0 else 0.0

    # Features
    feature_buckets: dict[str, float] = defaultdict(float)
    for r in records:
        tags = r.get("tags") or {}
        feat = _extract_feature_from_tags(tags)
        if feat:
            feature_buckets[feat] += float(r.get("effective_cost") or 0)

    features = sorted(
        [
            FeatureContext(
                name=f,
                cost_eur=round(v, 2),
                pct_of_product=round(v / cost * 100, 1) if cost > 0 else 0,
                resource_count=sum(
                    1 for r in records
                    if _extract_feature_from_tags(r.get("tags") or {}) == f
                ),
                clouds=list({
                    r.get("provider_name", "unknown").lower() for r in records
                    if _extract_feature_from_tags(r.get("tags") or {}) == f
                }),
            )
            for f, v in feature_buckets.items()
        ],
        key=lambda x: x.cost_eur,
        reverse=True,
    )

    # Teams
    teams = list({
        _extract_team_from_tags(r.get("tags") or {})
        for r in records
        if _extract_team_from_tags(r.get("tags") or {})
    })

    # Top services
    svc_buckets: dict[str, float] = defaultdict(float)
    for r in records:
        svc_buckets[r.get("service_name", "Unknown")] += float(r.get("effective_cost") or 0)
    top_services = sorted(
        [{"service": s, "cost_eur": round(v, 2)} for s, v in svc_buckets.items()],
        key=lambda x: x["cost_eur"],
        reverse=True,
    )[:5]

    clouds = list({r.get("provider_name", "unknown").lower() for r in records})

    # Inference method: use the dominant method across records
    methods = [r.get("_inference_method", "tag") for r in records]
    dominant = max(set(methods), key=methods.count) if methods else "tag"

    return ProductContext(
        name=product,
        cost_eur=round(cost, 2),
        pct_of_total=pct,
        features=features,
        teams=sorted(t for t in teams if t),
        top_services=top_services,
        resource_count=len(records),
        clouds=clouds,
        inference_method=dominant,
    )


# ── Public async API ──────────────────────────────────────────────────────────

async def map_context(
    tenant_id: str,
    lookback_days: int = 30,
) -> ContextMapping:
    """
    Build a business context mapping for a tenant from FOCUS records.
    """
    settings = get_settings()
    today = date.today()
    period_start = (today - timedelta(days=lookback_days)).isoformat()
    period_end = today.isoformat()

    query = """
        SELECT c.service_name, c.provider_name, c.charge_period_start,
               c.effective_cost, c.tags, c.resource_name, c.resource_id
        FROM c
        WHERE c.tenant_id = @tenant_id
          AND c.type = 'focus_record'
          AND c.charge_period_start >= @start
    """
    try:
        records = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            query,
            parameters=[
                {"name": "@tenant_id", "value": tenant_id},
                {"name": "@start",     "value": period_start},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        records = []

    # Attribute each record to a product
    product_buckets: dict[str, list[dict]] = defaultdict(list)
    unattributed_eur = 0.0
    total_cost_eur = 0.0
    tag_hits = 0
    name_hits = 0
    k8s_hits = 0

    for r in records:
        cost = float(r.get("effective_cost") or 0.0)
        total_cost_eur += cost
        tags = r.get("tags") or {}

        product = _extract_product_from_tags(tags)
        method = "tag"
        if not product:
            product, method = _infer_product_from_name(
                r.get("resource_name") or r.get("resource_id") or ""
            )

        if product:
            r["_inference_method"] = method
            product_buckets[product].append(r)
            if method == "tag":
                tag_hits += 1
            elif method == "k8s":
                k8s_hits += 1
            else:
                name_hits += 1
        else:
            unattributed_eur += cost

    attributed_eur = total_cost_eur - unattributed_eur
    attribution_pct = (
        round(attributed_eur / total_cost_eur * 100, 1) if total_cost_eur > 0 else 0.0
    )

    products = sorted(
        [
            _build_product_context(p, recs, total_cost_eur)
            for p, recs in product_buckets.items()
        ],
        key=lambda x: x.cost_eur,
        reverse=True,
    )

    inference_notes: list[str] = []
    if tag_hits:
        inference_notes.append(f"{tag_hits} records attributed via resource tags.")
    if name_hits:
        inference_notes.append(f"{name_hits} records inferred from resource name patterns.")
    if k8s_hits:
        inference_notes.append(f"{k8s_hits} records inferred from Kubernetes namespace labels.")
    if unattributed_eur > 0:
        inference_notes.append(
            f"€{unattributed_eur:.0f} ({100 - attribution_pct:.0f}%) "
            "could not be attributed — add product/app tags to improve coverage."
        )

    return ContextMapping(
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
        total_cost_eur=round(total_cost_eur, 2),
        attributed_eur=round(attributed_eur, 2),
        unattributed_eur=round(unattributed_eur, 2),
        attribution_pct=attribution_pct,
        products=products,
        inference_notes=inference_notes,
    )
