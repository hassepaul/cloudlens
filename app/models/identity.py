"""
Identity models for enterprise SSO (SAML 2.0) and SCIM 2.0 user provisioning.

Two documents live in the `identity_config` container (one per tenant, id=tenant_id):
  - SAML IdP configuration (non-secret; the IdP signing cert is public)
  - SCIM enablement + a hash of the tenant's SCIM bearer token

Provisioned users live in the `scim_users` container, partitioned by tenant_id.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SAML ────────────────────────────────────────────────────────────────────

class SamlConfigIn(BaseModel):
    """Operator-supplied SAML IdP configuration (PUT payload)."""
    idp_entity_id: str = Field(..., min_length=1, description="IdP EntityID / issuer")
    idp_sso_url: str = Field(..., min_length=1, description="IdP SSO (SingleSignOnService) URL")
    idp_slo_url: str = Field(default="", description="IdP SLO (SingleLogout) URL (optional)")
    idp_x509_cert: str = Field(..., min_length=1, description="IdP signing certificate (PEM or base64 body)")
    want_assertions_signed: bool = Field(default=True)
    attr_email: str = Field(default="", description="SAML attribute name carrying the email (blank = use NameID)")
    attr_name: str = Field(default="", description="SAML attribute name carrying the display name")
    default_role: str = Field(default="viewer", description="Role granted to SSO users")
    enabled: bool = Field(default=True)


class SamlConfig(SamlConfigIn):
    updated_at: str = Field(default_factory=_now)


class IdentityConfig(BaseModel):
    """Per-tenant identity configuration document (id == tenant_id)."""
    id: str
    type: str = Field(default="identity_config")
    tenant_id: str
    saml: Optional[SamlConfig] = None
    scim_enabled: bool = Field(default=False)
    scim_token_hash: str = Field(default="")
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "IdentityConfig":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a SCIM/session token (never store plaintext)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── SCIM ────────────────────────────────────────────────────────────────────

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class ScimUser(BaseModel):
    """A provisioned user (SCIM core User). Partition key = tenant_id."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="scim_user")
    tenant_id: str
    user_name: str
    external_id: str = ""
    active: bool = True
    given_name: str = ""
    family_name: str = ""
    formatted_name: str = ""
    display_name: str = ""
    emails: list[dict] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    # ── serialization ──
    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "ScimUser":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)

    def primary_email(self) -> str:
        for e in self.emails:
            if e.get("primary"):
                return e.get("value", "")
        return self.emails[0]["value"] if self.emails else ""

    def to_scim(self, location_base: str) -> dict:
        """Render as a SCIM 2.0 User resource. `location_base` is the Users URL."""
        return {
            "schemas": [USER_SCHEMA],
            "id": self.id,
            "externalId": self.external_id or None,
            "userName": self.user_name,
            "name": {
                "givenName": self.given_name,
                "familyName": self.family_name,
                "formatted": self.formatted_name or f"{self.given_name} {self.family_name}".strip(),
            },
            "displayName": self.display_name or self.formatted_name or self.user_name,
            "emails": self.emails,
            "active": self.active,
            "meta": {
                "resourceType": "User",
                "created": self.created_at,
                "lastModified": self.updated_at,
                "location": f"{location_base}/{self.id}",
            },
        }

    @classmethod
    def from_scim(cls, tenant_id: str, payload: dict, existing: Optional["ScimUser"] = None) -> "ScimUser":
        """Build/patch a ScimUser from a SCIM create/replace payload."""
        name = payload.get("name") or {}
        emails = payload.get("emails") or []
        # normalize emails: ensure at least one primary
        norm_emails = []
        for e in emails:
            if isinstance(e, dict) and e.get("value"):
                norm_emails.append({
                    "value": e["value"],
                    "type": e.get("type", "work"),
                    "primary": bool(e.get("primary", False)),
                })
        if norm_emails and not any(e["primary"] for e in norm_emails):
            norm_emails[0]["primary"] = True

        base = existing.model_dump() if existing else {}
        base.update({
            "tenant_id": tenant_id,
            "user_name": payload.get("userName", base.get("user_name", "")),
            "external_id": payload.get("externalId", base.get("external_id", "")),
            "active": payload.get("active", base.get("active", True)),
            "given_name": name.get("givenName", base.get("given_name", "")),
            "family_name": name.get("familyName", base.get("family_name", "")),
            "formatted_name": name.get("formatted", base.get("formatted_name", "")),
            "display_name": payload.get("displayName", base.get("display_name", "")),
            "emails": norm_emails or base.get("emails", []),
            "updated_at": _now(),
        })
        if existing:
            base["id"] = existing.id
            base["created_at"] = existing.created_at
        return cls(**base)


def scim_error(status_code: int, detail: str, scim_type: str | None = None) -> dict:
    body = {"schemas": [ERROR_SCHEMA], "status": str(status_code), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return body


def scim_list(resources: list[dict], total: int, start_index: int, count: int) -> dict:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }
