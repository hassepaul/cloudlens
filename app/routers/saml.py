"""
SAML 2.0 SSO endpoints (SP-initiated) + SAML IdP configuration.

  PUT  /api/v1/auth/saml/{tenant_id}/config    (operator, API key) — set IdP config
  GET  /api/v1/auth/saml/{tenant_id}/config     (operator, API key)
  GET  /api/v1/auth/saml/{tenant_id}/metadata   (public) — SP metadata XML
  GET  /api/v1/auth/saml/{tenant_id}/login       (public) — 302 to IdP
  POST /api/v1/auth/saml/{tenant_id}/acs         (public) — assertion consumer

Successful ACS mints a CloudLens session JWT (HS256). When frontend_base_url is
configured the user is redirected there with the token in the URL fragment;
otherwise the token is returned as JSON.
"""
from __future__ import annotations
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response, JSONResponse

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.models.audit import AuditAction
from app.models.identity import IdentityConfig, SamlConfig, SamlConfigIn, _now
from app.rate_limit import enforce_ip_rate_limit, get_client_ip
from app.routers.admin import write_audit
from app.services import saml_sso, scim, session_token
from app.services.saml_sso import SamlError, SamlUnavailable

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth/saml", tags=["sso"])


async def _get_saml_config(tenant_id: str) -> SamlConfig:
    cfg = await scim.load_identity(tenant_id)
    if not cfg or not cfg.saml or not cfg.saml.enabled:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": "SAML is not configured for this tenant"})
    return cfg.saml


# ── Configuration (operator) ────────────────────────────────────────────────

@router.put("/{tenant_id}/config")
async def set_saml_config(tenant_id: str, body: SamlConfigIn,
                          _: None = Depends(require_api_key)) -> dict:
    """Create or update the tenant's SAML IdP configuration."""
    try:
        cfg = await scim.load_identity(tenant_id) or IdentityConfig(id=tenant_id, tenant_id=tenant_id)
        cfg.saml = SamlConfig(**body.model_dump())
        cfg.updated_at = _now()
        await scim.save_identity(cfg)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    await write_audit(tenant_id, AuditAction.SAML_CONFIG_UPDATED, "api-key",
                      actor_type="api_key", resource_id=tenant_id,
                      detail={"idp_entity_id": body.idp_entity_id})
    return cfg.saml.model_dump()


@router.get("/{tenant_id}/config")
async def get_saml_config(tenant_id: str, _: None = Depends(require_api_key)) -> dict:
    cfg = await scim.load_identity(tenant_id)
    if not cfg or not cfg.saml:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": "No SAML config"})
    return cfg.saml.model_dump()


# ── SP metadata (public) ────────────────────────────────────────────────────

@router.get("/{tenant_id}/metadata")
async def sp_metadata(tenant_id: str, request: Request) -> Response:
    cfg = await _get_saml_config(tenant_id)
    base = saml_sso.base_url(request)
    try:
        xml = saml_sso.metadata_xml(tenant_id, cfg, base)
    except SamlUnavailable:
        raise HTTPException(status_code=503,
                            detail={"error": "SAML_UNAVAILABLE", "message": "SAML support not installed"})
    except SamlError as exc:
        raise HTTPException(status_code=500, detail={"error": "SAML_ERROR", "message": str(exc)})
    return Response(content=xml, media_type="application/xml")


# ── SP-initiated login (public) ─────────────────────────────────────────────

@router.get("/{tenant_id}/login")
async def saml_login(tenant_id: str, request: Request, relay_state: str = "") -> RedirectResponse:
    await enforce_ip_rate_limit(get_client_ip(request), limit_per_min=30)
    cfg = await _get_saml_config(tenant_id)
    base = saml_sso.base_url(request)
    try:
        redirect_url = saml_sso.init_login(tenant_id, cfg, request, base, relay_state=relay_state)
    except SamlUnavailable:
        raise HTTPException(status_code=503,
                            detail={"error": "SAML_UNAVAILABLE", "message": "SAML support not installed"})
    except SamlError as exc:
        raise HTTPException(status_code=400, detail={"error": "SAML_ERROR", "message": str(exc)})
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


# ── Assertion Consumer Service (public) ─────────────────────────────────────

@router.post("/{tenant_id}/acs")
async def saml_acs(tenant_id: str, request: Request):
    await enforce_ip_rate_limit(get_client_ip(request), limit_per_min=30)
    if not session_token.is_enabled():
        raise HTTPException(status_code=503,
                            detail={"error": "SSO_DISABLED", "message": "SSO session issuance is not configured"})
    cfg = await _get_saml_config(tenant_id)
    base = saml_sso.base_url(request)
    form = await request.form()
    post_data = {k: v for k, v in form.items()}

    try:
        result = saml_sso.process_acs(tenant_id, cfg, request, base, post_data)
    except SamlUnavailable:
        raise HTTPException(status_code=503,
                            detail={"error": "SAML_UNAVAILABLE", "message": "SAML support not installed"})
    except SamlError as exc:
        await write_audit(tenant_id, AuditAction.SSO_LOGIN_FAILED, "saml",
                          actor_type="system", outcome="denied",
                          source_ip=get_client_ip(request), detail={"reason": str(exc)})
        raise HTTPException(status_code=401, detail={"error": "SSO_FAILED", "message": str(exc)})

    email = result["email"]
    if not email:
        raise HTTPException(status_code=401,
                            detail={"error": "SSO_FAILED", "message": "No email/NameID in SAML assertion"})

    token, ttl = session_token.mint_session(
        subject=email, tenant_id=tenant_id, email=email,
        name=result.get("name", ""), roles=[cfg.default_role],
    )
    await write_audit(tenant_id, AuditAction.SSO_LOGIN, email,
                      actor_type="user", source_ip=get_client_ip(request),
                      detail={"nameid": result.get("nameid", "")})

    frontend = get_settings().frontend_base_url
    relay = result.get("relay_state") or ""
    if frontend:
        target = relay if relay.startswith("http") else frontend.rstrip("/")
        url = f"{target}#access_token={quote(token)}&token_type=Bearer&expires_in={ttl}"
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    return JSONResponse({
        "access_token": token, "token_type": "Bearer",
        "expires_in": ttl, "email": email, "tenant_id": tenant_id,
    })
