"""
CloudLens session tokens.

After a successful SAML SSO login, CloudLens mints a short-lived HS256 JWT that
the SPA presents as a normal ``Authorization: Bearer`` token. These tokens are
distinct from Azure AD tokens and are recognised by ``app.auth.verify_bearer_token``
via their issuer claim.

Signing secret comes from ``settings.session_jwt_secret``; when empty, SSO
session issuance is disabled (the SAML router returns 503).
"""
from __future__ import annotations
import time
from typing import Optional

from app.config import get_settings
from app.exceptions import UnauthorizedError
from app.logging_config import get_logger

log = get_logger(__name__)


def is_enabled() -> bool:
    return bool(get_settings().session_jwt_secret)


def mint_session(
    subject: str,
    tenant_id: str,
    email: str = "",
    name: str = "",
    roles: Optional[list[str]] = None,
) -> tuple[str, int]:
    """
    Mint a CloudLens session JWT. Returns (token, expires_in_seconds).
    Raises AuthError if session issuance is disabled or jose is missing.
    """
    settings = get_settings()
    if not settings.session_jwt_secret:
        raise UnauthorizedError("SSO session issuance is not configured")
    try:
        from jose import jwt  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise UnauthorizedError("JWT library unavailable") from exc

    ttl = settings.session_ttl_hours * 3600
    now = int(time.time())
    claims = {
        "iss": settings.session_issuer,
        "sub": subject,
        "tid": tenant_id,
        "email": email,
        "name": name,
        "roles": roles or [],
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "token_use": "session",
    }
    token = jwt.encode(claims, settings.session_jwt_secret, algorithm="HS256")
    return token, ttl


def verify_session(token: str) -> dict:
    """
    Validate a CloudLens session JWT and return its claims.
    Raises AuthError on any failure.
    """
    settings = get_settings()
    if not settings.session_jwt_secret:
        raise UnauthorizedError("SSO session issuance is not configured")
    try:
        from jose import jwt  # type: ignore
        from jose.exceptions import JWTError  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise UnauthorizedError("JWT library unavailable") from exc
    try:
        claims = jwt.decode(
            token,
            settings.session_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.session_issuer,
            options={"verify_aud": False},
        )
    except JWTError as exc:
        log.warning("session.token_invalid", error=str(exc))
        raise UnauthorizedError("Invalid or expired session token") from exc
    return claims
