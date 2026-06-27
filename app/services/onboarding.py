"""
Self-service tenant onboarding — credential validation + provisioning.

Validation flow:
  Azure  → acquire AAD token, verify Cost Management Reader on each subscription
  AWS    → format-validate role ARN + account IDs; generate ExternalId + trust policy
  GCP    → structural validation of service account JSON + required fields

Provisioning stores credentials in Key Vault, writes the tenant document to
Cosmos, and returns the tenant_id ready for an initial ingest trigger.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos, keyvault

log = get_logger(__name__)

# ── Azure ────────────────────────────────────────────────────────────────────
_AAD_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_ARM_BASE = "https://management.azure.com"
_ARM_SUB_API = "2022-12-01"
_COST_MGMT_ROLE = "Cost Management Reader"

# ── AWS ──────────────────────────────────────────────────────────────────────
_AWS_ROLE_ARN_RE = re.compile(
    r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,256}$", re.ASCII
)
_AWS_ACCOUNT_RE = re.compile(r"^\d{12}$")

# ── GCP ──────────────────────────────────────────────────────────────────────
_GCP_REQUIRED_FIELDS = frozenset({
    "type", "project_id", "private_key_id", "private_key",
    "client_email", "token_uri",
})

# ── CloudLens AWS account (used in generated trust policy) ───────────────────
_CLOUDLENS_AWS_ACCOUNT = "CLOUDLENS_ACCOUNT_ID"


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: bool
    provider: str
    account_info: dict = field(default_factory=dict)
    error: Optional[str] = None


# ── Azure validation ─────────────────────────────────────────────────────────

async def validate_azure_credentials(
    client_id: str,
    client_secret: str,
    tenant_id: str,
    subscription_ids: list[str],
) -> ValidationResult:
    """Acquire an AAD token and verify read access on every subscription."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as http:
        # 1. Token acquisition
        resp = await http.post(
            _AAD_TOKEN_URL.format(tenant=tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://management.azure.com/.default",
            },
        )
        if resp.status_code != 200:
            err = resp.json().get("error_description", resp.text)[:240]
            return ValidationResult(
                valid=False, provider="azure",
                error=f"Authentication failed: {err}",
            )

        token = resp.json().get("access_token", "")
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Verify subscription access
        accessible: list[dict] = []
        inaccessible: list[str] = []
        for sub_id in subscription_ids:
            r = await http.get(
                f"{_ARM_BASE}/subscriptions/{sub_id}?api-version={_ARM_SUB_API}",
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                accessible.append({
                    "id": sub_id,
                    "name": data.get("displayName", sub_id),
                    "state": data.get("state", "Unknown"),
                })
            else:
                inaccessible.append(sub_id)

        if inaccessible:
            return ValidationResult(
                valid=False, provider="azure",
                error=(
                    f"No read access to subscription(s): {', '.join(inaccessible)}. "
                    f"Assign the '{_COST_MGMT_ROLE}' role to the service principal."
                ),
                account_info={"accessible": accessible, "inaccessible": inaccessible},
            )

        return ValidationResult(
            valid=True, provider="azure",
            account_info={
                "subscriptions": accessible,
                "sp_tenant_id": tenant_id,
                "required_role": _COST_MGMT_ROLE,
            },
        )


# ── AWS validation ───────────────────────────────────────────────────────────

async def validate_aws_credentials(
    role_arn: str,
    account_ids: list[str],
    external_id: Optional[str] = None,
) -> ValidationResult:
    """
    Validate role ARN and account ID formats, then return a ready-to-use
    IAM trust policy and required permission list.

    Full STS assume-role validation requires boto3 (not available in this
    environment); format validation is performed here and the caller should
    complete the IAM setup before the first ingest.
    """
    if not _AWS_ROLE_ARN_RE.match(role_arn):
        return ValidationResult(
            valid=False, provider="aws",
            error=(
                "Invalid role ARN format. "
                "Expected: arn:aws:iam::123456789012:role/CloudLens-ReadRole"
            ),
        )

    bad_accounts = [a for a in account_ids if not _AWS_ACCOUNT_RE.match(a)]
    if bad_accounts:
        return ValidationResult(
            valid=False, provider="aws",
            error=f"Invalid AWS account IDs (must be 12 digits): {', '.join(bad_accounts)}",
        )

    arn_account = role_arn.split(":")[4]
    if arn_account not in account_ids:
        return ValidationResult(
            valid=False, provider="aws",
            error=(
                f"Role ARN account '{arn_account}' is not in the provided account IDs list. "
                f"The role must reside in one of: {', '.join(account_ids)}"
            ),
        )

    ext_id = external_id or str(uuid4())
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{_CLOUDLENS_AWS_ACCOUNT}:root"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"sts:ExternalId": ext_id}},
        }],
    }
    return ValidationResult(
        valid=True, provider="aws",
        account_info={
            "role_arn": role_arn,
            "accounts": account_ids,
            "external_id": ext_id,
            "iam_trust_policy": trust_policy,
            "required_managed_policies": ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
            "required_inline_actions": [
                "ce:GetCostAndUsage",
                "ce:GetReservationCoverage",
                "ce:GetSavingsPlansCoverage",
                "ce:GetRightsizingRecommendation",
            ],
            "setup_instructions": (
                "1. Create an IAM role in your AWS account with the trust policy above. "
                "2. Attach the managed policy and inline actions shown. "
                "3. The ExternalId is stored securely in CloudLens Key Vault."
            ),
        },
    )


# ── GCP validation ───────────────────────────────────────────────────────────

async def validate_gcp_credentials(
    service_account_json: str,
    project_ids: list[str],
) -> ValidationResult:
    """Validate the structure and required fields of a GCP service account JSON key."""
    try:
        sa = json.loads(service_account_json)
    except (json.JSONDecodeError, ValueError) as exc:
        return ValidationResult(
            valid=False, provider="gcp",
            error=f"Invalid JSON: {exc}",
        )

    missing = _GCP_REQUIRED_FIELDS - set(sa.keys())
    if missing:
        return ValidationResult(
            valid=False, provider="gcp",
            error=f"Service account JSON missing required fields: {', '.join(sorted(missing))}",
        )

    if sa.get("type") != "service_account":
        return ValidationResult(
            valid=False, provider="gcp",
            error=f"JSON 'type' must be 'service_account', got '{sa.get('type')}'",
        )

    return ValidationResult(
        valid=True, provider="gcp",
        account_info={
            "project_id": sa["project_id"],
            "client_email": sa["client_email"],
            "projects": project_ids,
            "required_roles": [
                "roles/bigquery.dataViewer",
                "roles/bigquery.jobUser",
                "roles/billing.viewer",
                "roles/recommender.viewer",
            ],
            "setup_instructions": (
                "Grant the required roles to the service account in each GCP project. "
                "Enable the BigQuery and Cloud Billing APIs if not already active."
            ),
        },
    )


# ── Provisioning ─────────────────────────────────────────────────────────────

async def provision_tenant(
    tenant_name: str,
    alert_email: str,
    plan_tier: str,
    azure_config: Optional[dict] = None,
    aws_config: Optional[dict] = None,
    gcp_config: Optional[dict] = None,
) -> dict:
    """
    Persist a new tenant to Cosmos and store credentials in Key Vault.

    Returns a dict with tenant_id and immediate next-step API links.
    """
    settings = get_settings()
    tenant_id = str(uuid4())

    enabled_clouds: list[str] = []
    cloud_accounts: dict[str, list[str]] = {}
    sp_secret_ref = ""

    if azure_config:
        enabled_clouds.append("azure")
        sp_secret_ref = await keyvault.store_sp_credentials(
            tenant_id=tenant_id,
            client_id=azure_config["client_id"],
            client_secret=azure_config["client_secret"],
            azure_tenant_id=azure_config["tenant_id"],
        )

    if aws_config:
        enabled_clouds.append("aws")
        account_ids = aws_config.get("account_ids", [])
        cloud_accounts["aws"] = account_ids
        # Store role ARN + external_id as the "secret"
        await keyvault.store_sp_credentials(
            tenant_id=f"{tenant_id}-aws",
            client_id=aws_config.get("role_arn", ""),
            client_secret=aws_config.get("external_id", ""),
            azure_tenant_id="aws",
        )

    if gcp_config:
        enabled_clouds.append("gcp")
        project_ids = gcp_config.get("project_ids", [])
        cloud_accounts["gcp"] = project_ids
        await keyvault.store_sp_credentials(
            tenant_id=f"{tenant_id}-gcp",
            client_id=gcp_config.get("client_email", ""),
            client_secret=gcp_config.get("service_account_json", ""),
            azure_tenant_id="gcp",
        )

    # Subscription IDs are Azure-specific; other providers use cloud_accounts
    subscription_ids = azure_config.get("subscription_ids", []) if azure_config else []
    if not subscription_ids:
        subscription_ids = []

    doc = {
        "id": tenant_id,
        "type": "tenant",
        "tenant_name": tenant_name,
        "subscription_ids": subscription_ids,
        "plan_tier": plan_tier,
        "alert_email": alert_email,
        "active": True,
        "sp_secret_ref": sp_secret_ref,
        "enabled_clouds": enabled_clouds,
        "cloud_accounts": cloud_accounts,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "onboarded_via": "self_service_wizard",
    }

    await cosmos.upsert_item(settings.cosmos_container_tenants, doc)
    log.info(
        "onboarding.provisioned",
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        clouds=enabled_clouds,
    )

    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "enabled_clouds": enabled_clouds,
        "next_steps": {
            "trigger_ingest": f"POST /api/v1/ingest/{tenant_id}",
            "view_costs": f"GET /api/v1/costs/{tenant_id}/summary",
            "check_lag": f"GET /api/v1/ingest/{tenant_id}/lag",
            "dashboard": f"/frontend/index.html?tenant={tenant_id}",
        },
        "created_at": doc["created_at"],
    }


# ── Wizard session ────────────────────────────────────────────────────────────

_SESSION_TTL = 86_400  # 24 h — sessions expire if not completed


async def create_wizard_session(*, invite_token: Optional[str] = None) -> dict:
    """
    Create a new onboarding wizard session document in Cosmos.
    Returns the session document (id = session_id).
    """
    settings = get_settings()
    session_id = f"wiz-{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": session_id,
        "type": "wizard_session",
        "tenant_id": session_id,          # partition key
        "status": "in_progress",          # in_progress | completed | abandoned
        "current_step": 1,
        "invite_token": invite_token,
        # Step data, filled incrementally
        "step_account": {},
        "step_clouds": {},
        "step_credentials": {},
        "step_notifications": {},
        "provisioned_tenant_id": None,
        "created_at": now,
        "updated_at": now,
        "ttl": _SESSION_TTL,
    }
    await cosmos.upsert_item(settings.cosmos_container_onboarding_sessions, doc)
    return doc


async def get_wizard_session(session_id: str) -> Optional[dict]:
    """Return the session doc or None if not found."""
    settings = get_settings()
    try:
        return await cosmos.get_item(
            settings.cosmos_container_onboarding_sessions,
            session_id,
            session_id,
        )
    except Exception:
        return None


async def update_wizard_session(session_id: str, updates: dict) -> dict:
    """
    Merge updates into the session document and persist.
    Returns the updated document.
    """
    settings = get_settings()
    doc = await get_wizard_session(session_id)
    if doc is None:
        raise ValueError(f"Session {session_id} not found")
    doc.update(updates)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    await cosmos.upsert_item(settings.cosmos_container_onboarding_sessions, doc)
    return doc


# ── Invite links ──────────────────────────────────────────────────────────────

import secrets as _secrets

_INVITE_TTL = 7 * 86_400  # 7 days


async def create_invite(
    *,
    email: str,
    plan_tier: str = "growth",
    notes: str = "",
    created_by: str = "admin",
) -> dict:
    """
    Generate a single-use invite link token and persist it.
    The token is a 32-byte URL-safe secret.
    """
    settings = get_settings()
    token = _secrets.token_urlsafe(32)
    invite_id = f"inv-{token[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": invite_id,
        "type": "invite",
        "tenant_id": invite_id,          # partition key
        "token": token,
        "email": email,
        "plan_tier": plan_tier,
        "notes": notes,
        "created_by": created_by,
        "used": False,
        "used_by_tenant_id": None,
        "created_at": now,
        "expires_at": None,              # TTL enforced by Cosmos
        "ttl": _INVITE_TTL,
    }
    await cosmos.upsert_item(settings.cosmos_container_onboarding_sessions, doc)
    log.info("onboarding.invite_created", invite_id=invite_id, email=email)
    return doc


async def get_invite_by_token(token: str) -> Optional[dict]:
    """Look up an invite by its token value."""
    settings = get_settings()
    try:
        docs = await cosmos.query_items(
            settings.cosmos_container_onboarding_sessions,
            "SELECT * FROM c WHERE c.type='invite' AND c.token=@tok",
            parameters=[{"name": "@tok", "value": token}],
        )
        return docs[0] if docs else None
    except Exception:
        return None


async def mark_invite_used(token: str, tenant_id: str) -> None:
    """Mark an invite as used once provisioning completes."""
    invite = await get_invite_by_token(token)
    if invite:
        settings = get_settings()
        invite["used"] = True
        invite["used_by_tenant_id"] = tenant_id
        invite["used_at"] = datetime.now(timezone.utc).isoformat()
        await cosmos.upsert_item(settings.cosmos_container_onboarding_sessions, invite)

