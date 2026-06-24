"""
Shared pytest fixtures.

The rate limiter keeps per-tenant token buckets in process memory. Across a full
test run those buckets accumulate, so a tenant id reused by many tests can hit
its limit and return 429 where the test expects a 2xx/4xx. Reset the buckets
before every test so each starts with a full allowance.

We also seed the plan cache so the rate-limiter's plan lookup does not attempt a
(doomed, slow) Cosmos call against the fake test endpoint. Tests that exercise
the limiter directly call check_rate_limit with an explicit plan, so this does
not weaken their coverage.
"""
import time
import pytest

import app.rate_limit as rl
from app.rate_limit import reset as reset_rate_limits
from app.models.tenant import PlanTier


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    reset_rate_limits()
    # Pre-warm the plan cache for common test tenant ids so _resolve_plan
    # returns immediately instead of hitting Cosmos.
    now = time.monotonic()
    for tid in ("t-1", "t-acme", "t1", "tenant-a", "tenant-b", "tenant-x",
                "tenant-y", "tenant-z", "tenant-ent", "sub-1"):
        rl._plan_cache[tid] = (PlanTier.GROWTH, now)
    yield

