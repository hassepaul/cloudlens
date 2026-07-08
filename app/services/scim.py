"""
SCIM 2.0 provisioning service (RFC 7643/7644 core User).

Stores provisioned users in the `scim_users` container (partition = tenant_id)
and reads per-tenant SCIM enablement + token hash from `identity_config`.
Identity providers (Okta, Azure AD, OneLogin) call the SCIM router with a
per-tenant bearer token to create / update / deactivate users.
"""
from __future__ import annotations
import re
import secrets
from typing import Optional

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.identity import IdentityConfig, ScimUser, hash_token
from app.services import cosmos

log = get_logger(__name__)

_FILTER_RE = re.compile(r'^\s*userName\s+eq\s+"([^"]+)"\s*$', re.IGNORECASE)


def _container() -> str:
    return get_settings().cosmos_container_scim_users


def _identity_container() -> str:
    return get_settings().cosmos_container_identity


# ── Identity config (SAML + SCIM enablement) ────────────────────────────────

async def load_identity(tenant_id: str) -> Optional[IdentityConfig]:
    try:
        doc = await cosmos.get_item(_identity_container(), tenant_id, tenant_id)
        return IdentityConfig.from_cosmos(doc)
    except NotFoundError:
        return None


async def save_identity(cfg: IdentityConfig) -> IdentityConfig:
    await cosmos.upsert_item(_identity_container(), cfg.to_cosmos())
    return cfg


async def verify_scim_token(tenant_id: str, token: str) -> bool:
    """Constant-time compare the presented SCIM token against the stored hash."""
    cfg = await load_identity(tenant_id)
    if not cfg or not cfg.scim_enabled or not cfg.scim_token_hash:
        return False
    return secrets.compare_digest(cfg.scim_token_hash, hash_token(token))


async def rotate_scim_token(tenant_id: str) -> str:
    """Generate a new SCIM bearer token, store its hash, enable SCIM.
    Returns the plaintext token (shown to the operator once)."""
    cfg = await load_identity(tenant_id) or IdentityConfig(id=tenant_id, tenant_id=tenant_id)
    token = "scim_" + secrets.token_urlsafe(32)
    cfg.scim_token_hash = hash_token(token)
    cfg.scim_enabled = True
    from app.models.identity import _now
    cfg.updated_at = _now()
    await save_identity(cfg)
    return token


# ── SCIM user CRUD ──────────────────────────────────────────────────────────

async def get_user(tenant_id: str, user_id: str) -> Optional[ScimUser]:
    try:
        doc = await cosmos.get_item(_container(), user_id, tenant_id)
        return ScimUser.from_cosmos(doc)
    except NotFoundError:
        return None


async def find_by_username(tenant_id: str, user_name: str) -> Optional[ScimUser]:
    rows = await cosmos.query_items(
        _container(),
        "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='scim_user' AND c.user_name=@u",
        parameters=[{"name": "@t", "value": tenant_id}, {"name": "@u", "value": user_name}],
        partition_key=tenant_id,
    )
    return ScimUser.from_cosmos(rows[0]) if rows else None


async def create_user(tenant_id: str, payload: dict) -> tuple[Optional[ScimUser], Optional[str]]:
    """Create a user. Returns (user, error). error='conflict' if userName exists."""
    user_name = (payload.get("userName") or "").strip()
    if not user_name:
        return None, "userName is required"
    existing = await find_by_username(tenant_id, user_name)
    if existing:
        return None, "conflict"
    user = ScimUser.from_scim(tenant_id, payload)
    await cosmos.upsert_item(_container(), user.to_cosmos())
    log.info("scim.user_created", tenant_id=tenant_id, user_id=user.id, user_name=user_name)
    return user, None


async def replace_user(tenant_id: str, user_id: str, payload: dict) -> Optional[ScimUser]:
    existing = await get_user(tenant_id, user_id)
    if not existing:
        return None
    updated = ScimUser.from_scim(tenant_id, payload, existing=existing)
    await cosmos.upsert_item(_container(), updated.to_cosmos())
    log.info("scim.user_replaced", tenant_id=tenant_id, user_id=user_id)
    return updated


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


async def patch_user(tenant_id: str, user_id: str, operations: list[dict]) -> Optional[ScimUser]:
    """Apply a SCIM PatchOp. Supports replace/add on active, name.*, displayName, emails."""
    existing = await get_user(tenant_id, user_id)
    if not existing:
        return None
    from app.models.identity import _now
    for op in operations or []:
        action = (op.get("op") or "").lower()
        if action not in ("replace", "add"):
            continue
        path = (op.get("path") or "").strip()
        value = op.get("value")
        if not path and isinstance(value, dict):
            # No path: value is a map of attributes to set.
            if "active" in value:
                existing.active = _coerce_bool(value["active"])
            if "displayName" in value:
                existing.display_name = value["displayName"]
            name = value.get("name") or {}
            if name.get("givenName") is not None:
                existing.given_name = name["givenName"]
            if name.get("familyName") is not None:
                existing.family_name = name["familyName"]
            continue
        p = path.lower()
        if p == "active":
            existing.active = _coerce_bool(value)
        elif p == "displayname":
            existing.display_name = value if isinstance(value, str) else str(value)
        elif p == "name.givenname":
            existing.given_name = value
        elif p == "name.familyname":
            existing.family_name = value
        elif p == "username":
            existing.user_name = value
    existing.updated_at = _now()
    await cosmos.upsert_item(_container(), existing.to_cosmos())
    log.info("scim.user_patched", tenant_id=tenant_id, user_id=user_id, active=existing.active)
    return existing


async def delete_user(tenant_id: str, user_id: str) -> bool:
    try:
        await cosmos.delete_item(_container(), user_id, tenant_id)
        log.info("scim.user_deleted", tenant_id=tenant_id, user_id=user_id)
        return True
    except NotFoundError:
        return False


async def list_users(
    tenant_id: str, filter_str: str = "", start_index: int = 1, count: int = 100
) -> tuple[list[ScimUser], int]:
    """List users, optionally filtered by `userName eq "..."`. 1-based start_index."""
    if filter_str:
        m = _FILTER_RE.match(filter_str)
        if m:
            u = await find_by_username(tenant_id, m.group(1))
            return ([u], 1) if u else ([], 0)
        # Unsupported filter → empty (IdPs treat as "not found")
        return [], 0

    rows = await cosmos.query_items(
        _container(),
        "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='scim_user' ORDER BY c.created_at ASC",
        parameters=[{"name": "@t", "value": tenant_id}],
        partition_key=tenant_id,
        max_item_count=1000,
    )
    total = len(rows)
    start = max(0, start_index - 1)
    page = rows[start:start + max(0, count)]
    return [ScimUser.from_cosmos(r) for r in page], total
