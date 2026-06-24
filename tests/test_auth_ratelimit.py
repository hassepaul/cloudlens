"""
Tests for auth (API key + bearer scope) and the in-process rate limiter.
Run: pytest tests/test_auth_ratelimit.py -v
"""
from __future__ import annotations
import os
import pytest

os.environ.setdefault("INTERNAL_API_KEY", "super-secret-key")
os.environ.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("RATE_LIMIT_STARTER", "3")
os.environ.setdefault("RATE_LIMIT_GROWTH", "10")
os.environ.setdefault("RATE_LIMIT_ENTERPRISE", "30")

from fastapi import HTTPException

from app.auth import require_api_key, enforce_tenant_scope, AuthContext
from app.rate_limit import check_rate_limit, reset as reset_buckets
from app.models.tenant import PlanTier


# ══════════════════════════════════════════════════════════════════════════════
# API KEY AUTH
# ══════════════════════════════════════════════════════════════════════════════

class TestApiKeyAuth:
    @pytest.mark.asyncio
    async def test_valid_key_passes(self):
        # Should not raise
        from app.config import get_settings
        await require_api_key(x_api_key=get_settings().internal_api_key)

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(x_api_key=None)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(x_api_key="wrong-key")
        assert exc.value.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# TENANT SCOPE ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════════════

class TestTenantScope:
    def test_matching_tenant_allowed(self):
        ctx = AuthContext(subject="user@a.com", tenant_id="tenant-a", scopes=[])
        # Should not raise
        enforce_tenant_scope("tenant-a", ctx)

    def test_mismatched_tenant_forbidden(self):
        ctx = AuthContext(subject="user@a.com", tenant_id="tenant-a", scopes=[])
        with pytest.raises(HTTPException) as exc:
            enforce_tenant_scope("tenant-b", ctx)
        assert exc.value.status_code == 403

    def test_internal_api_key_caller_bypasses_scope(self):
        # tenant_id=None means an internal API-key caller — allowed on any tenant
        ctx = AuthContext(subject="internal", tenant_id=None, scopes=[])
        enforce_tenant_scope("any-tenant", ctx)  # should not raise


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiter:
    def setup_method(self):
        reset_buckets()

    def test_allows_up_to_capacity(self):
        # Starter = 3/min capacity → first 3 calls pass
        for _ in range(3):
            check_rate_limit("tenant-x", PlanTier.STARTER)

    def test_blocks_when_exhausted(self):
        for _ in range(3):
            check_rate_limit("tenant-y", PlanTier.STARTER)
        with pytest.raises(HTTPException) as exc:
            check_rate_limit("tenant-y", PlanTier.STARTER)
        assert exc.value.status_code == 429
        assert exc.value.headers.get("Retry-After") == "5"

    def test_tenants_isolated(self):
        # Exhaust tenant-a
        for _ in range(3):
            check_rate_limit("tenant-a", PlanTier.STARTER)
        # tenant-b has its own bucket, unaffected
        check_rate_limit("tenant-b", PlanTier.STARTER)

    def test_higher_plan_higher_capacity(self):
        # Enterprise = 30/min → 4 calls trivially fine
        for _ in range(4):
            check_rate_limit("tenant-ent", PlanTier.ENTERPRISE)

    def test_reset_clears_buckets(self):
        for _ in range(3):
            check_rate_limit("tenant-z", PlanTier.STARTER)
        reset_buckets()
        # After reset the bucket is full again
        check_rate_limit("tenant-z", PlanTier.STARTER)
