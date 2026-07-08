"""
SAML 2.0 Service-Provider (SP) integration for enterprise SSO.

Wraps the OneLogin ``python3-saml`` toolkit (optional dependency). If the
toolkit or its native ``xmlsec`` deps are not installed, callers get a
``SamlUnavailable`` error which the router surfaces as HTTP 503 — matching the
graceful-degradation pattern used elsewhere (e.g. python-jose in auth.py).

Per-tenant IdP configuration is stored in the ``identity_config`` container.
The SP entityID / ACS URLs are derived from the public API base URL.
"""
from __future__ import annotations
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request

from app.config import get_settings
from app.logging_config import get_logger
from app.models.identity import SamlConfig

log = get_logger(__name__)


class SamlUnavailable(RuntimeError):
    """python3-saml toolkit is not installed."""


class SamlError(RuntimeError):
    """SAML processing/validation error."""


def _load_toolkit():
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth  # type: ignore
        from onelogin.saml2.settings import OneLogin_Saml2_Settings  # type: ignore
        return OneLogin_Saml2_Auth, OneLogin_Saml2_Settings
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SamlUnavailable("python3-saml is not installed") from exc


def base_url(request: Request) -> str:
    """External base URL for building SP entityID / ACS. Prefers the configured
    public_base_url; otherwise reconstructs from the request."""
    configured = get_settings().public_base_url
    if configured:
        return configured.rstrip("/")
    return f"{request.url.scheme}://{request.headers.get('host', request.url.hostname or '')}".rstrip("/")


def sp_entity_id(tenant_id: str, base: str) -> str:
    return f"{base}/api/v1/auth/saml/{tenant_id}/metadata"


def acs_url(tenant_id: str, base: str) -> str:
    return f"{base}/api/v1/auth/saml/{tenant_id}/acs"


def _normalize_cert(cert: str) -> str:
    """Strip PEM headers/whitespace — python3-saml wants the base64 body only."""
    lines = [l.strip() for l in cert.strip().splitlines()
             if l.strip() and "CERTIFICATE" not in l]
    return "".join(lines) if lines else cert.strip()


def build_settings(tenant_id: str, cfg: SamlConfig, base: str) -> dict:
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity_id(tenant_id, base),
            "assertionConsumerService": {
                "url": acs_url(tenant_id, base),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            "x509cert": "",
            "privateKey": "",
        },
        "idp": {
            "entityId": cfg.idp_entity_id,
            "singleSignOnService": {
                "url": cfg.idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": _normalize_cert(cfg.idp_x509_cert),
        },
        "security": {
            "wantAssertionsSigned": cfg.want_assertions_signed,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "requestedAuthnContext": False,
        },
    }


def _prepare_request(request: Request, base: str, post_data: Optional[dict] = None) -> dict:
    parsed = urlparse(base)
    https = "on" if parsed.scheme == "https" else "off"
    host = parsed.netloc or request.headers.get("host", "")
    return {
        "https": https,
        "http_host": host,
        "server_port": str(parsed.port or (443 if https == "on" else 80)),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def _auth(tenant_id: str, cfg: SamlConfig, request: Request, base: str,
          post_data: Optional[dict] = None):
    OneLogin_Saml2_Auth, _ = _load_toolkit()
    req = _prepare_request(request, base, post_data)
    settings = build_settings(tenant_id, cfg, base)
    return OneLogin_Saml2_Auth(req, old_settings=settings)


def init_login(tenant_id: str, cfg: SamlConfig, request: Request, base: str,
               relay_state: str = "") -> str:
    """Return the IdP redirect URL for an SP-initiated AuthnRequest."""
    auth = _auth(tenant_id, cfg, request, base)
    return auth.login(return_to=relay_state or None)


def metadata_xml(tenant_id: str, cfg: SamlConfig, base: str) -> str:
    """Return SP metadata XML (validated)."""
    _, OneLogin_Saml2_Settings = _load_toolkit()
    settings = OneLogin_Saml2_Settings(build_settings(tenant_id, cfg, base), sp_validation_only=True)
    meta = settings.get_sp_metadata()
    errors = settings.validate_metadata(meta)
    if errors:
        raise SamlError(f"Invalid SP metadata: {', '.join(errors)}")
    return meta.decode("utf-8") if isinstance(meta, (bytes, bytearray)) else meta


def process_acs(tenant_id: str, cfg: SamlConfig, request: Request, base: str,
                post_data: dict) -> dict:
    """
    Validate a SAMLResponse posted to the ACS. Returns
    {nameid, email, name, attributes, relay_state}. Raises SamlError on failure.
    """
    auth = _auth(tenant_id, cfg, request, base, post_data=post_data)
    auth.process_response()
    errors = auth.get_errors()
    if errors:
        reason = auth.get_last_error_reason()
        log.warning("saml.acs_error", tenant_id=tenant_id, errors=errors, reason=reason)
        raise SamlError(f"SAML validation failed: {', '.join(errors)}")
    if not auth.is_authenticated():
        raise SamlError("SAML assertion not authenticated")

    attrs = auth.get_attributes() or {}
    nameid = auth.get_nameid() or ""

    def _pick(attr_name: str) -> str:
        if attr_name and attr_name in attrs and attrs[attr_name]:
            v = attrs[attr_name]
            return v[0] if isinstance(v, list) else str(v)
        return ""

    email = _pick(cfg.attr_email) or nameid
    name = _pick(cfg.attr_name)
    return {
        "nameid": nameid,
        "email": email,
        "name": name,
        "attributes": attrs,
        "relay_state": post_data.get("RelayState", ""),
    }
