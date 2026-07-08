"""
Per-tenant / per-IP rate limiter with a pluggable backend.

Two backends, selected automatically:

  1. **Redis (distributed, default when REDIS_URL is set)** — enforces a single
     GLOBAL limit across every API replica using an atomic token-bucket Lua
     script (``_TOKEN_BUCKET_LUA``). Refill + consume happen in one round-trip
     so concurrent replicas cannot race. This is the production path and fixes
     the historical per-replica limitation (formerly BUG-011).

  2. **In-process (fallback)** — a dependency-free token bucket in module memory.
     Used when REDIS_URL is unset or Redis is unreachable. On a multi-replica
     deployment this is per-replica (effective ceiling ~ N_replicas × limit),
     which is fine for single-replica/dev and as a fail-open safety net —
     rate limiting must never take the API down.

Plan limits (requests/minute) come from settings:
  starter=60, growth=200, enterprise=600
"""
from __future__ import annotations
import ipaddress
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

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


# ── Distributed (Redis) atomic token-bucket backend ─────────────────────────
# When settings.redis_url is configured, per-tenant and per-IP limits are
# enforced GLOBALLY across all replicas using this atomic Lua script (fixes the
# per-replica ceiling of the in-process fallback, BUG-011). The script refills
# and consumes a token in a single round-trip so concurrent replicas cannot
# race. If Redis is unset or unreachable, callers fall back to the in-process
# limiter transparently — rate limiting must never take the API down.
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return allowed
"""

_redis_client = None
_redis_unavailable = False


def _get_redis():
    """Lazily create a shared async Redis client. Returns None when no
    redis_url is configured or the client cannot be built (→ in-process fallback)."""
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    url = get_settings().redis_url
    if not url:
        _redis_unavailable = True
        return None
    try:
        import redis.asyncio as aioredis  # lazy — optional dependency
        _redis_client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        log.info("rate_limit.redis_backend_enabled")
        return _redis_client
    except Exception as exc:  # ImportError or bad URL
        log.warning("rate_limit.redis_init_failed", error=str(exc))
        _redis_unavailable = True
        return None


async def _redis_allow(key: str, capacity: float, refill_per_sec: float,
                       ttl_ms: int = 120_000) -> bool | None:
    """Atomically consume one token from the Redis bucket.
    Returns True (allowed) / False (limited) / None (Redis unavailable → fallback)."""
    client = _get_redis()
    if client is None:
        return None
    try:
        now = time.time()
        res = await client.eval(_TOKEN_BUCKET_LUA, 1, key,
                                capacity, refill_per_sec, now, ttl_ms)
        return bool(int(res))
    except Exception as exc:
        # Redis blipped — degrade to the in-process limiter rather than 500.
        log.warning("rate_limit.redis_error_fallback", error=str(exc))
        return None


def _too_many(per_min: int, retry_after: str, backend: str, **fields) -> HTTPException:
    log.warning("rate_limit.exceeded", limit_per_min=per_min, backend=backend, **fields)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "RATE_LIMITED",
            "message": f"Rate limit of {per_min} requests/minute exceeded",
        },
        headers={"Retry-After": retry_after},
    )


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
    _ip_buckets.clear()
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

    Uses the Redis atomic backend when configured (global across replicas),
    otherwise falls back to the in-process token bucket.
    """
    plan = await _resolve_plan(tenant_id)
    per_min = _limit_for_plan(plan)
    key = f"{get_settings().rate_limit_redis_prefix}:t:{tenant_id}"
    allowed = await _redis_allow(key, float(per_min), per_min / 60.0)
    if allowed is None:
        # Redis not configured / unreachable — in-process fallback.
        check_rate_limit(tenant_id, plan)
        return
    if not allowed:
        raise _too_many(per_min, "5", "redis", tenant_id=tenant_id)


# ── IP-based rate limiter for public (unauthenticated) endpoints ─────────────
# Simple token-bucket keyed on client IP.  Prevents abuse of public endpoints
# (onboarding wizard, credential validation) that do not carry a tenant context.
# Default: 10 requests/minute per IP.  Per-replica in-process limit; same
# multi-replica caveat as check_rate_limit applies.

_IP_BUCKET_LIMIT = 10        # requests per minute
_IP_BUCKET_MAX = 10_000      # max tracked IPs before eviction
_IP_BUCKET_STALE_SEC = 600   # evict entries idle for 10 min
_ip_buckets: dict[str, _Bucket] = {}

_TRUSTED_PROXY_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def _is_trusted_proxy(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _TRUSTED_PROXY_NETS)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """Extract the real client IP.  Only trust X-Forwarded-For when the
    direct connection originates from a private/loopback address (i.e. a
    reverse proxy).  External clients cannot spoof their IP this way."""
    direct_ip = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(direct_ip):
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return direct_ip


def _evict_stale_ip_buckets() -> None:
    if len(_ip_buckets) <= _IP_BUCKET_MAX:
        return
    now = time.monotonic()
    stale = [k for k, b in _ip_buckets.items() if now - b.last > _IP_BUCKET_STALE_SEC]
    for k in stale:
        del _ip_buckets[k]


def check_ip_rate_limit(ip: str, limit_per_min: int = _IP_BUCKET_LIMIT) -> None:
    """
    Consume one token for this IP.  Raises HTTP 429 when the bucket is empty.
    Call at the top of public endpoints that lack a tenant context.
    """
    _evict_stale_ip_buckets()
    capacity = float(limit_per_min)
    refill = limit_per_min / 60.0
    bucket = _ip_buckets.get(ip)
    if bucket is None:
        bucket = _Bucket(tokens=capacity, capacity=capacity, refill_per_sec=refill)
        _ip_buckets[ip] = bucket
    else:
        bucket.capacity = capacity
        bucket.refill_per_sec = refill

    if not bucket.allow():
        log.warning("rate_limit.ip_exceeded", ip=ip, limit_per_min=limit_per_min)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "RATE_LIMITED",
                "message": f"Rate limit of {limit_per_min} requests/minute exceeded",
            },
            headers={"Retry-After": "10"},
        )


async def enforce_ip_rate_limit(ip: str, limit_per_min: int = _IP_BUCKET_LIMIT) -> None:
    """
    Async per-IP limiter for public endpoints. Uses the Redis atomic backend
    when configured (global across replicas), otherwise falls back to the
    in-process IP bucket. Prefer this over ``check_ip_rate_limit`` in async
    request handlers so multi-replica deployments enforce a true global limit.
    """
    key = f"{get_settings().rate_limit_redis_prefix}:ip:{ip}"
    allowed = await _redis_allow(key, float(limit_per_min), limit_per_min / 60.0)
    if allowed is None:
        check_ip_rate_limit(ip, limit_per_min)
        return
    if not allowed:
        raise _too_many(limit_per_min, "10", "redis", ip=ip)

