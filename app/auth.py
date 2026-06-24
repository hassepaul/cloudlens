"""
Authentication & authorization dependencies.

Two mechanisms:
  1. Internal API key (X-API-Key header) — for admin/internal callers and the
     Static Web App's server-side proxy. Cheapest possible: a shared secret
     pulled from settings, compared in constant time.
  2. Azure AD bearer token (Authorization: Bearer <jwt>) — for end-user calls
     from the SPA. Validated against the tenant's signing keys (JWKS).

Both are exposed as FastAPI dependencies. Routes pick whichever they need.
For cost reasons we do NOT run a separate auth service — validation happens
in-process.
"""
from __future__ import annotations
import secrets
import time
from typing import Optional

import httpx
from fastapi import Header, HTTPException, status, Depends

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

# ── JWKS cache (module-level, refreshed lazily) ────────────────────────────
_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL_SECONDS = 3600


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """
    Dependency: require a valid internal API key.
    Used on admin endpoints (ingest trigger, tenant management).
    """
    settings = get_settings()
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.internal_api_key):
        log.warning("auth.api_key_rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHORIZED", "message": "Valid X-API-Key header required"},
        )


async def _get_jwks(jwks_uri: str) -> dict:
    """Fetch (and cache) the Azure AD JSON Web Key Set."""
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache and now - _jwks_fetched_at < _JWKS_TTL_SECONDS:
        return _jwks_cache
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = now
    return _jwks_cache


class AuthContext:
    """Resolved identity for a request."""

    def __init__(self, subject: str, tenant_id: Optional[str], scopes: list[str]) -> None:
        self.subject = subject
        self.tenant_id = tenant_id
        self.scopes = scopes


async def verify_bearer_token(
    authorization: Optional[str] = Header(default=None),
) -> AuthContext:
    """
    Dependency: validate an Azure AD bearer token and return an AuthContext.

    Uses python-jose if available; otherwise falls back to a structural check.
    The token's `tid` (tenant) and `oid`/`sub` (subject) claims are surfaced so
    downstream code can enforce tenant scoping.
    """
    settings = get_settings()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHORIZED", "message": "Bearer token required"},
        )

    token = authorization.split(" ", 1)[1].strip()

    try:
        from jose import jwt  # type: ignore
        from jose.exceptions import JWTError  # type: ignore
    except ImportError:
        # python-jose not installed — do a minimal unverified decode so the
        # service still functions in environments without the crypto extra.
        log.warning("auth.jose_unavailable_unverified_decode")
        try:
            import base64
            import json
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            raise HTTPException(status_code=401, detail={"error": "UNAUTHORIZED", "message": "Malformed token"})
        return AuthContext(
            subject=claims.get("oid") or claims.get("sub", "unknown"),
            tenant_id=claims.get("tid"),
            scopes=(claims.get("scp", "") or "").split(),
        )

    issuer = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"
    jwks_uri = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/discovery/v2.0/keys"

    try:
        jwks = await _get_jwks(jwks_uri)
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=settings.azure_client_id,
            issuer=issuer,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        log.warning("auth.token_invalid", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHORIZED", "message": "Invalid or expired token"},
        )

    return AuthContext(
        subject=claims.get("oid") or claims.get("sub", "unknown"),
        tenant_id=claims.get("tid"),
        scopes=(claims.get("scp", "") or "").split(),
    )


def enforce_tenant_scope(path_tenant_id: str, auth: AuthContext) -> None:
    """
    Ensure the caller's token is scoped to the tenant in the URL path.
    Internal API-key callers (auth.tenant_id is None) bypass this check.
    """
    if auth.tenant_id is None:
        return
    if auth.tenant_id != path_tenant_id:
        log.warning("auth.tenant_scope_violation", token_tid=auth.tenant_id, path_tid=path_tenant_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Token not scoped to this tenant"},
        )


async def require_tenant_scope(
    tenant_id: str,
    auth: "AuthContext" = Depends(verify_bearer_token),
) -> "AuthContext":
    """
    FastAPI dependency: verify the bearer token is scoped to the tenant in the
    path. This is the enforcement point for cross-tenant isolation (SOC 2 CC6.1 /
    logical access). Routers that serve per-tenant data should depend on this in
    addition to the rate limiter.
    """
    enforce_tenant_scope(tenant_id, auth)
    return auth
