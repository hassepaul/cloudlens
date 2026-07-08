"""
SCIM 2.0 provisioning API (RFC 7644 core User) — per-tenant.

Base:  /api/v1/scim/{tenant_id}/v2
Auth:  Bearer <tenant SCIM token>  (rotate via the operator endpoints below)

Identity providers (Okta, Azure AD, OneLogin) call these to create / update /
deactivate users. Deactivation (active=false) is the standard SCIM
de-provisioning signal and is fully supported via PUT and PATCH.

Operator (API-key) endpoints:
  POST /api/v1/scim/{tenant_id}/token   — (re)generate the SCIM bearer token
  GET  /api/v1/scim/{tenant_id}/status  — SCIM enablement + endpoint URL
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.models.audit import AuditAction
from app.models.identity import USER_SCHEMA, scim_error, scim_list
from app.routers.admin import write_audit
from app.services import scim

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/scim", tags=["scim"])

_SCIM_MEDIA = "application/scim+json"


def _scim_json(content: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, media_type=_SCIM_MEDIA)


def _base(request: Request) -> str:
    b = get_settings().public_base_url
    if b:
        return b.rstrip("/")
    return f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")


def _users_location(request: Request, tenant_id: str) -> str:
    return f"{_base(request)}/api/v1/scim/{tenant_id}/v2/Users"


# ── SCIM bearer-token authentication ────────────────────────────────────────

async def require_scim(tenant_id: str, authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail=scim_error(401, "Bearer token required"))
    token = authorization.split(" ", 1)[1].strip()
    try:
        ok = await scim.verify_scim_token(tenant_id, token)
    except CosmosError:
        raise HTTPException(status_code=503, detail=scim_error(503, "Directory unavailable"))
    if not ok:
        raise HTTPException(status_code=401, detail=scim_error(401, "Invalid SCIM token"))
    return tenant_id


# ── Operator: token management (API key) ────────────────────────────────────

@router.post("/{tenant_id}/token")
async def rotate_token(tenant_id: str, request: Request, _: None = Depends(require_api_key)) -> dict:
    """(Re)generate the tenant SCIM bearer token. Returned once — store it in the IdP."""
    try:
        token = await scim.rotate_scim_token(tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    await write_audit(tenant_id, AuditAction.SCIM_TOKEN_ROTATED, "api-key",
                      actor_type="api_key", resource_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "scim_token": token,
        "base_url": f"{_base(request)}/api/v1/scim/{tenant_id}/v2",
        "note": "Store this token in your IdP now — it is not shown again.",
    }


@router.get("/{tenant_id}/status")
async def scim_status(tenant_id: str, request: Request, _: None = Depends(require_api_key)) -> dict:
    cfg = await scim.load_identity(tenant_id)
    return {
        "tenant_id": tenant_id,
        "scim_enabled": bool(cfg and cfg.scim_enabled),
        "has_token": bool(cfg and cfg.scim_token_hash),
        "base_url": f"{_base(request)}/api/v1/scim/{tenant_id}/v2",
    }


# ── Discovery endpoints ─────────────────────────────────────────────────────

@router.get("/{tenant_id}/v2/ServiceProviderConfig")
async def service_provider_config(tenant_id: str = Depends(require_scim)) -> JSONResponse:
    return _scim_json({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": "https://cloudlens.io/docs/scim",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token",
            "description": "Authentication via the per-tenant SCIM bearer token",
            "primary": True,
        }],
    })


@router.get("/{tenant_id}/v2/ResourceTypes")
async def resource_types(request: Request, tenant_id: str = Depends(require_scim)) -> JSONResponse:
    loc = f"{_base(request)}/api/v1/scim/{tenant_id}/v2"
    rt = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User", "name": "User", "endpoint": "/Users",
        "description": "User Account", "schema": USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": f"{loc}/ResourceTypes/User"},
    }
    return _scim_json(scim_list([rt], 1, 1, 100))


@router.get("/{tenant_id}/v2/Schemas")
async def schemas(tenant_id: str = Depends(require_scim)) -> JSONResponse:
    user_schema = {
        "id": USER_SCHEMA, "name": "User", "description": "User Account",
        "attributes": [
            {"name": "userName", "type": "string", "required": True, "uniqueness": "server"},
            {"name": "name", "type": "complex", "required": False},
            {"name": "displayName", "type": "string", "required": False},
            {"name": "emails", "type": "complex", "multiValued": True, "required": False},
            {"name": "active", "type": "boolean", "required": False},
        ],
        "meta": {"resourceType": "Schema"},
    }
    return _scim_json(scim_list([user_schema], 1, 1, 100))


# ── Users ───────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/v2/Users")
async def list_users(
    request: Request,
    tenant_id: str = Depends(require_scim),
    filter: str = Query(default=""),
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=0, le=1000),
) -> JSONResponse:
    try:
        users, total = await scim.list_users(tenant_id, filter, startIndex, count)
    except CosmosError:
        raise HTTPException(status_code=503, detail=scim_error(503, "Directory unavailable"))
    loc = _users_location(request, tenant_id)
    resources = [u.to_scim(loc) for u in users]
    return _scim_json(scim_list(resources, total, startIndex, count))


@router.post("/{tenant_id}/v2/Users")
async def create_user(request: Request, tenant_id: str = Depends(require_scim)) -> JSONResponse:
    payload = await request.json()
    try:
        user, err = await scim.create_user(tenant_id, payload)
    except CosmosError:
        raise HTTPException(status_code=503, detail=scim_error(503, "Directory unavailable"))
    if err == "conflict":
        return _scim_json(scim_error(409, "userName already exists", "uniqueness"), 409)
    if err:
        return _scim_json(scim_error(400, err, "invalidValue"), 400)
    await write_audit(tenant_id, AuditAction.SCIM_USER_CREATED, "scim",
                      actor_type="system", resource_id=user.id,
                      detail={"user_name": user.user_name})
    return _scim_json(user.to_scim(_users_location(request, tenant_id)), 201)


@router.get("/{tenant_id}/v2/Users/{user_id}")
async def get_user(request: Request, user_id: str, tenant_id: str = Depends(require_scim)) -> JSONResponse:
    user = await scim.get_user(tenant_id, user_id)
    if not user:
        return _scim_json(scim_error(404, "User not found"), 404)
    return _scim_json(user.to_scim(_users_location(request, tenant_id)))


@router.put("/{tenant_id}/v2/Users/{user_id}")
async def replace_user(request: Request, user_id: str, tenant_id: str = Depends(require_scim)) -> JSONResponse:
    payload = await request.json()
    user = await scim.replace_user(tenant_id, user_id, payload)
    if not user:
        return _scim_json(scim_error(404, "User not found"), 404)
    action = AuditAction.SCIM_USER_DEACTIVATED if not user.active else AuditAction.SCIM_USER_UPDATED
    await write_audit(tenant_id, action, "scim", actor_type="system",
                      resource_id=user.id, detail={"active": user.active})
    return _scim_json(user.to_scim(_users_location(request, tenant_id)))


@router.patch("/{tenant_id}/v2/Users/{user_id}")
async def patch_user(request: Request, user_id: str, tenant_id: str = Depends(require_scim)) -> JSONResponse:
    payload = await request.json()
    ops = payload.get("Operations") or payload.get("operations") or []
    user = await scim.patch_user(tenant_id, user_id, ops)
    if not user:
        return _scim_json(scim_error(404, "User not found"), 404)
    action = AuditAction.SCIM_USER_DEACTIVATED if not user.active else AuditAction.SCIM_USER_UPDATED
    await write_audit(tenant_id, action, "scim", actor_type="system",
                      resource_id=user.id, detail={"active": user.active})
    return _scim_json(user.to_scim(_users_location(request, tenant_id)))


@router.delete("/{tenant_id}/v2/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, tenant_id: str = Depends(require_scim)) -> Response:
    ok = await scim.delete_user(tenant_id, user_id)
    if not ok:
        return _scim_json(scim_error(404, "User not found"), 404)
    await write_audit(tenant_id, AuditAction.SCIM_USER_DELETED, "scim",
                      actor_type="system", resource_id=user_id)
    return Response(status_code=204)


# ── Groups (read-only; user-only provisioning) ──────────────────────────────

@router.get("/{tenant_id}/v2/Groups")
async def list_groups(
    tenant_id: str = Depends(require_scim),
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=0, le=1000),
) -> JSONResponse:
    # CloudLens provisions users only; advertise an empty group set so IdPs that
    # probe /Groups do not error.
    return _scim_json(scim_list([], 0, startIndex, count))
