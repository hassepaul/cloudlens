"""
Onboarding router — self-service tenant provisioning.

  POST /api/v1/onboarding/validate-credentials   (no auth — rate-limited at LB)
  POST /api/v1/onboarding/provision              (requires internal API key)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import keyvault as _keyvault
from app.services.onboarding import (
    ValidationResult,
    provision_tenant,
    validate_aws_credentials,
    validate_azure_credentials,
    validate_gcp_credentials,
    create_wizard_session,
    get_wizard_session,
    update_wizard_session,
    create_invite,
    get_invite_by_token,
    mark_invite_used,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


# ── Request models ────────────────────────────────────────────────────────────

class AzureCredentials(BaseModel):
    client_id: str = Field(..., description="Service principal application (client) ID")
    client_secret: str = Field(..., description="Service principal client secret")
    tenant_id: str = Field(..., description="Azure Active Directory tenant ID")
    subscription_ids: list[str] = Field(..., min_length=1, description="Azure subscription IDs to monitor")


class AwsCredentials(BaseModel):
    role_arn: str = Field(..., description="Cross-account IAM role ARN")
    account_ids: list[str] = Field(..., min_length=1, description="AWS account IDs (12-digit)")
    external_id: Optional[str] = Field(None, description="IAM ExternalId (generated if omitted)")


class GcpCredentials(BaseModel):
    service_account_json: str = Field(..., description="Full GCP service account key JSON string")
    project_ids: list[str] = Field(..., min_length=1, description="GCP project IDs to monitor")


class ValidateCredentialsRequest(BaseModel):
    provider: str = Field(..., pattern="^(azure|aws|gcp)$", description="Cloud provider to validate")
    azure: Optional[AzureCredentials] = None
    aws: Optional[AwsCredentials] = None
    gcp: Optional[GcpCredentials] = None


class ProvisionRequest(BaseModel):
    tenant_name: str = Field(..., min_length=2, max_length=120)
    alert_email: str = Field(..., description="Alert and digest recipient email")
    plan_tier: str = Field(default="growth", pattern="^(starter|growth|enterprise)$")
    azure: Optional[AzureCredentials] = None
    aws: Optional[AwsCredentials] = None
    gcp: Optional[GcpCredentials] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/validate-credentials")
async def validate_credentials(body: ValidateCredentialsRequest) -> dict:
    """
    Validate cloud credentials before provisioning a tenant.

    Returns `valid: true/false` plus `account_info` (subscriptions, role ARN,
    generated ExternalId, trust policy, required roles) or an `error` message.
    Does not persist anything — safe to call repeatedly during the wizard flow.
    """
    result: ValidationResult

    if body.provider == "azure":
        if not body.azure:
            raise HTTPException(status_code=422, detail="azure credentials object required")
        result = await validate_azure_credentials(
            body.azure.client_id,
            body.azure.client_secret,
            body.azure.tenant_id,
            body.azure.subscription_ids,
        )
    elif body.provider == "aws":
        if not body.aws:
            raise HTTPException(status_code=422, detail="aws credentials object required")
        result = await validate_aws_credentials(
            body.aws.role_arn,
            body.aws.account_ids,
            body.aws.external_id,
        )
    else:  # gcp
        if not body.gcp:
            raise HTTPException(status_code=422, detail="gcp credentials object required")
        result = await validate_gcp_credentials(
            body.gcp.service_account_json,
            body.gcp.project_ids,
        )

    return {
        "valid": result.valid,
        "provider": result.provider,
        "account_info": result.account_info,
        "error": result.error,
    }


@router.post("/provision", status_code=201, dependencies=[Depends(require_api_key)])
async def provision(body: ProvisionRequest) -> dict:
    """
    Create a new tenant from the onboarding wizard submission.

    Stores credentials in Key Vault, writes the tenant document to Cosmos,
    and returns the tenant_id + next-step API links. Trigger
    POST /api/v1/ingest/{tenant_id} immediately after to start the first ingest.
    """
    if not any([body.azure, body.aws, body.gcp]):
        raise HTTPException(
            status_code=422,
            detail="At least one cloud provider must be configured.",
        )

    try:
        result = await provision_tenant(
            tenant_name=body.tenant_name,
            alert_email=body.alert_email,
            plan_tier=body.plan_tier,
            azure_config=body.azure.model_dump() if body.azure else None,
            aws_config=body.aws.model_dump() if body.aws else None,
            gcp_config=body.gcp.model_dump() if body.gcp else None,
        )
        log.info("onboarding.complete", tenant_id=result["tenant_id"])
        return result
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except Exception as exc:
        log.error("onboarding.provision_error", error=str(exc))
        raise HTTPException(status_code=500, detail={"error": "PROVISION_FAILED", "message": str(exc)})


# ── Wizard session endpoints ──────────────────────────────────────────────────

class WizardSessionUpdate(BaseModel):
    current_step: Optional[int] = None
    step_account: Optional[dict] = None
    step_clouds: Optional[dict] = None
    step_credentials: Optional[dict] = None
    step_notifications: Optional[dict] = None
    status: Optional[str] = None
    provisioned_tenant_id: Optional[str] = None


class WizardCompleteRequest(BaseModel):
    session_id: str
    tenant_name: str = Field(..., min_length=2, max_length=120)
    alert_email: str
    plan_tier: str = Field(default="growth", pattern="^(starter|growth|enterprise)$")
    azure: Optional[AzureCredentials] = None
    aws: Optional[AwsCredentials] = None
    gcp: Optional[GcpCredentials] = None
    slack_webhook: Optional[str] = None
    teams_webhook: Optional[str] = None
    monthly_budget_eur: Optional[float] = Field(None, gt=0)


@router.post("/wizard/session", status_code=201)
async def start_wizard_session(invite_token: Optional[str] = None) -> dict:
    """
    Create a new wizard session. Returns session_id to track progress.
    Optionally links to an invite token.
    """
    if invite_token:
        inv = await get_invite_by_token(invite_token)
        if not inv:
            raise HTTPException(status_code=404, detail="Invite token not found or expired")
        if inv.get("used"):
            raise HTTPException(status_code=409, detail="Invite token has already been used")
    try:
        session = await create_wizard_session(invite_token=invite_token)
        return {"session_id": session["id"], "status": session["status"], "current_step": session["current_step"]}
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/wizard/session/{session_id}")
async def get_session(session_id: str) -> dict:
    """Return wizard session state. Used to resume an interrupted wizard."""
    session = await get_wizard_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Strip internal fields
    return {k: v for k, v in session.items() if not k.startswith("_")}


@router.patch("/wizard/session/{session_id}")
async def save_session_step(session_id: str, body: WizardSessionUpdate) -> dict:
    """
    Persist progress at each wizard step.
    Call after the user completes each step so they can resume on reconnect.
    """
    session = await get_wizard_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    updates = body.model_dump(exclude_none=True)
    try:
        updated = await update_wizard_session(session_id, updates)
        return {"session_id": session_id, "current_step": updated["current_step"], "status": updated["status"]}
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/wizard/session/{session_id}/complete", status_code=201)
async def complete_wizard(session_id: str, body: WizardCompleteRequest) -> dict:
    """
    Finalize wizard: provision the tenant, store bot webhooks, mark session done.
    Requires no API key — the session_id acts as a short-lived bearer token.
    """
    session = await get_wizard_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    if not any([body.azure, body.aws, body.gcp]):
        raise HTTPException(status_code=422, detail="At least one cloud provider must be configured")

    try:
        result = await provision_tenant(
            tenant_name=body.tenant_name,
            alert_email=body.alert_email,
            plan_tier=body.plan_tier,
            azure_config=body.azure.model_dump() if body.azure else None,
            aws_config=body.aws.model_dump() if body.aws else None,
            gcp_config=body.gcp.model_dump() if body.gcp else None,
        )
        tenant_id = result["tenant_id"]

        # Store bot webhooks in Key Vault if provided
        if body.slack_webhook:
            try:
                await _keyvault.set_secret(f"slack-webhook-{tenant_id}", body.slack_webhook)
            except Exception:
                pass
        if body.teams_webhook:
            try:
                await _keyvault.set_secret(f"teams-webhook-{tenant_id}", body.teams_webhook)
            except Exception:
                pass

        # Store monthly budget limit in result for budget creation step
        if body.monthly_budget_eur:
            result["monthly_budget_eur"] = body.monthly_budget_eur

        # Mark session completed
        inv_token = session.get("invite_token")
        await update_wizard_session(session_id, {
            "status": "completed",
            "provisioned_tenant_id": tenant_id,
            "current_step": 6,
        })
        if inv_token:
            await mark_invite_used(inv_token, tenant_id)

        log.info("onboarding.wizard_complete", session_id=session_id, tenant_id=tenant_id)
        return result
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except Exception as exc:
        log.error("onboarding.wizard_complete_error", error=str(exc))
        raise HTTPException(status_code=500, detail={"error": "PROVISION_FAILED", "message": str(exc)})


# ── Invite endpoints ──────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    email: str
    plan_tier: str = Field(default="growth", pattern="^(starter|growth|enterprise)$")
    notes: str = Field(default="")


@router.post("/invite", status_code=201, dependencies=[Depends(require_api_key)])
async def create_invite_link(body: InviteRequest) -> dict:
    """Generate a single-use invite link for a new tenant. Requires API key."""
    try:
        inv = await create_invite(
            email=body.email,
            plan_tier=body.plan_tier,
            notes=body.notes,
        )
        return {
            "invite_id": inv["id"],
            "token": inv["token"],
            "email": inv["email"],
            "plan_tier": inv["plan_tier"],
            "wizard_url": f"/onboarding.html?invite={inv['token']}",
            "expires_in_hours": 168,
        }
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/invite/{token}")
async def validate_invite(token: str) -> dict:
    """Check if an invite token is valid and unused."""
    inv = await get_invite_by_token(token)
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found or expired")
    return {
        "valid": not inv.get("used", False),
        "email": inv.get("email"),
        "plan_tier": inv.get("plan_tier", "growth"),
        "used": inv.get("used", False),
    }

