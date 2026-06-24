"""
Lightweight in-process per-tenant rate limiter.

Deliberately dependency-free (no Redis) to keep infrastructure cost at zero.
Uses a token-bucket per tenant held in module memory. Because Container Apps
can run multiple replicas, this is a per-replica limit; with min_replicas=0 and
low traffic the effective ceiling is close enough for fair-use protection.
For strict global limits later, swap the bucket store for Azure Cache for Redis.

Plan limits (requests/minute) come from settings:
  starter=60, growth=200, enterprise=600
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, status

from app.config import get_settings
from app.logging_config import get_logger
from app.models.tenant import PlanTier

log = get_logger(__name__)


@dataclass
class _Bucket:
    tokens: float
    capacity: float
    refill_per_sec: float
    last: float = field(default_factory=time.monotonic)

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


_buckets: dict[str, _Bucket] = {}


def _limit_for_plan(plan: PlanTier) -> int:
    settings = get_settings()
    return {
        PlanTier.STARTER: settings.rate_limit_starter,
        PlanTier.GROWTH: settings.rate_limit_growth,
        PlanTier.ENTERPRISE: settings.rate_limit_enterprise,
    }.get(plan, settings.rate_limit_growth)


def check_rate_limit(tenant_id: str, plan: PlanTier) -> None:
    """
    Consume one token for this tenant. Raise HTTP 429 if the bucket is empty.
    Call at the top of per-tenant read endpoints.
    """
    per_min = _limit_for_plan(plan)
    capacity = float(per_min)
    refill = per_min / 60.0

    bucket = _buckets.get(tenant_id)
    if bucket is None:
        bucket = _Bucket(tokens=capacity, capacity=capacity, refill_per_sec=refill)
        _buckets[tenant_id] = bucket
    else:
        # Keep capacity/refill in sync if the plan changed.
        bucket.capacity = capacity
        bucket.refill_per_sec = refill

    if not bucket.allow():
        log.warning("rate_limit.exceeded", tenant_id=tenant_id, limit_per_min=per_min)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "RATE_LIMITED",
                "message": f"Rate limit of {per_min} requests/minute exceeded",
            },
            headers={"Retry-After": "5"},
        )


def reset() -> None:
    """Clear all buckets — used in tests."""
    _buckets.clear()
    _plan_cache.clear()


# ── Plan-tier cache + FastAPI dependency ────────────────────────────────────
# Cache tenant -> plan for a short TTL so we don't read Cosmos on every request.
_plan_cache: dict[str, tuple[PlanTier, float]] = {}
_PLAN_TTL_SECONDS = 300


async def _resolve_plan(tenant_id: str) -> PlanTier:
    now = time.monotonic()
    cached = _plan_cache.get(tenant_id)
    if cached and now - cached[1] < _PLAN_TTL_SECONDS:
        return cached[0]

    # Lazy import to avoid a circular import at module load.
    from app.config import get_settings
    from app.services import cosmos

    plan = PlanTier.GROWTH
    try:
        doc = await cosmos.get_item(
            get_settings().cosmos_container_tenants, tenant_id, tenant_id
        )
        plan = PlanTier(doc.get("plan_tier", "growth"))
    except Exception:
        # On any lookup failure, fall back to the default plan limit rather than
        # failing the request — rate limiting must never take the API down.
        pass

    _plan_cache[tenant_id] = (plan, now)
    return plan


async def rate_limit_tenant(tenant_id: str) -> None:
    """
    FastAPI path-dependency: enforce the per-tenant rate limit using the
    `tenant_id` path parameter and the tenant's plan tier.
    """
    plan = await _resolve_plan(tenant_id)
    check_rate_limit(tenant_id, plan)
