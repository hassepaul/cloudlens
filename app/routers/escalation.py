"""
Escalation integrations router.

Manages per-tenant escalation configurations for PagerDuty, Jira, Azure
DevOps, and Microsoft Teams.  Credentials (API tokens, PATs, routing keys)
are never stored in Cosmos — only the Key Vault secret *name* is referenced.

Endpoints
---------
GET    /{tenant_id}/integrations                      list all integrations
GET    /{tenant_id}/integrations/{channel_type}       get one integration
PUT    /{tenant_id}/integrations/{channel_type}       create or update
DELETE /{tenant_id}/integrations/{channel_type}       remove
POST   /{tenant_id}/integrations/{channel_type}/test  send a test event
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, Field, model_validator

from app.auth import require_api_key
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.models.alert import AlertEvent, AlertType, AlertSeverity
from app.services.escalation import (
    EscalationConfig,
    CHANNEL_TYPES,
    get_escalation_config,
    list_escalation_configs,
    save_escalation_config,
    delete_escalation_config,
    deliver_escalation,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/escalation",
    tags=["escalation"],
    dependencies=[Depends(require_api_key)],
)


# ── Request model ─────────────────────────────────────────────────────────────

class EscalationConfigIn(BaseModel):
    enabled: bool = True

    # PagerDuty
    pagerduty_service_name: str = "CloudLens"

    # Jira
    jira_base_url: str = ""
    jira_email: str = ""
    jira_project_key: str = ""
    jira_issue_type: str = "Bug"
    jira_labels: list[str] = Field(default_factory=lambda: ["cloudlens", "finops"])

    # ADO
    ado_org: str = ""
    ado_project: str = ""
    ado_work_item_type: str = "Bug"
    ado_area_path: str = ""
    ado_iteration_path: str = ""
    ado_tags: str = "CloudLens; FinOps"

    # Teams
    teams_webhook_url: str = ""
    teams_action_url: str = ""

    @model_validator(mode="after")
    def _validate_jira(self) -> "EscalationConfigIn":
        if self.jira_base_url and not self.jira_base_url.startswith("https://"):
            raise ValueError("jira_base_url must start with https://")
        return self


def _to_response(cfg: EscalationConfig) -> dict:
    return {
        "id": cfg.id,
        "tenant_id": cfg.tenant_id,
        "channel_type": cfg.channel_type,
        "enabled": cfg.enabled,
        "pagerduty_service_name": cfg.pagerduty_service_name,
        "jira_base_url": cfg.jira_base_url,
        "jira_email": cfg.jira_email,
        "jira_project_key": cfg.jira_project_key,
        "jira_issue_type": cfg.jira_issue_type,
        "jira_labels": cfg.jira_labels,
        "ado_org": cfg.ado_org,
        "ado_project": cfg.ado_project,
        "ado_work_item_type": cfg.ado_work_item_type,
        "ado_area_path": cfg.ado_area_path,
        "ado_iteration_path": cfg.ado_iteration_path,
        "ado_tags": cfg.ado_tags,
        "teams_webhook_url": cfg.teams_webhook_url,
        "teams_action_url": cfg.teams_action_url,
        "updated_at": cfg.updated_at,
        # Remind caller which KV secret names to populate
        "kv_secret_names": _kv_secret_names(cfg.tenant_id, cfg.channel_type),
    }


def _kv_secret_names(tenant_id: str, channel_type: str) -> dict:
    """Return the Key Vault secret names this channel reads at delivery time."""
    m: dict[str, str] = {
        "pagerduty": f"pd-routing-key-{tenant_id}",
        "jira":      f"jira-api-token-{tenant_id}",
        "ado":       f"ado-pat-{tenant_id}",
        "teams":     f"teams-webhook-{tenant_id}",
    }
    return {"required_secret": m.get(channel_type, "")}


def _validate_channel(channel_type: str) -> None:
    if channel_type not in CHANNEL_TYPES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_CHANNEL",
                "message": f"channel_type must be one of {sorted(CHANNEL_TYPES)}",
            },
        )


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/integrations")
async def list_integrations(tenant_id: str) -> dict:
    """List all escalation integrations configured for a tenant."""
    cfgs = await list_escalation_configs(tenant_id)
    return {
        "tenant_id": tenant_id,
        "count": len(cfgs),
        "integrations": [_to_response(c) for c in cfgs],
    }


@router.get("/{tenant_id}/integrations/{channel_type}")
async def get_integration(tenant_id: str, channel_type: str) -> dict:
    """Return a single escalation integration."""
    _validate_channel(channel_type)
    cfg = await get_escalation_config(tenant_id, channel_type)
    if not cfg:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND",
                    "message": f"No {channel_type} integration configured for {tenant_id}"},
        )
    return _to_response(cfg)


@router.put("/{tenant_id}/integrations/{channel_type}", status_code=status.HTTP_200_OK)
async def upsert_integration(
    tenant_id: str,
    channel_type: str,
    body: EscalationConfigIn,
) -> dict:
    """
    Create or update an escalation integration for a tenant.

    This stores **non-secret** configuration only.  Credentials must be
    placed in Azure Key Vault under the secret name shown in
    `kv_secret_names.required_secret` in the response.
    """
    _validate_channel(channel_type)
    existing = await get_escalation_config(tenant_id, channel_type)
    cfg = existing or EscalationConfig(
        id=EscalationConfig.make_id(tenant_id, channel_type),
        tenant_id=tenant_id,
        channel_type=channel_type,
    )
    cfg.enabled = body.enabled
    cfg.pagerduty_service_name = body.pagerduty_service_name
    cfg.jira_base_url = body.jira_base_url
    cfg.jira_email = body.jira_email
    cfg.jira_project_key = body.jira_project_key
    cfg.jira_issue_type = body.jira_issue_type
    cfg.jira_labels = body.jira_labels
    cfg.ado_org = body.ado_org
    cfg.ado_project = body.ado_project
    cfg.ado_work_item_type = body.ado_work_item_type
    cfg.ado_area_path = body.ado_area_path
    cfg.ado_iteration_path = body.ado_iteration_path
    cfg.ado_tags = body.ado_tags
    cfg.teams_webhook_url = body.teams_webhook_url
    cfg.teams_action_url = body.teams_action_url

    try:
        cfg = await save_escalation_config(cfg)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    log.info("escalation.config_saved", tenant_id=tenant_id, channel=channel_type)
    return _to_response(cfg)


@router.delete(
    "/{tenant_id}/integrations/{channel_type}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_integration(tenant_id: str, channel_type: str) -> None:
    """Remove an escalation integration for a tenant."""
    _validate_channel(channel_type)
    removed = await delete_escalation_config(tenant_id, channel_type)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND",
                    "message": f"No {channel_type} integration found for {tenant_id}"},
        )


@router.post("/{tenant_id}/integrations/{channel_type}/test")
async def test_integration(tenant_id: str, channel_type: str) -> dict:
    """
    Send a synthetic test alert event through the configured integration.

    Useful to verify credentials and connectivity before enabling the channel
    on production alert rules.  Does **not** require an existing alert rule.
    """
    _validate_channel(channel_type)
    cfg = await get_escalation_config(tenant_id, channel_type)
    if not cfg:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND",
                    "message": f"No {channel_type} integration configured for {tenant_id}"},
        )

    test_event = AlertEvent(
        tenant_id=tenant_id,
        rule_id="test-rule-id",
        rule_name="CloudLens Integration Test",
        alert_type=AlertType.SPEND_SPIKE,
        severity=AlertSeverity.INFO,
        title=f"[TEST] CloudLens {channel_type.upper()} integration test from {tenant_id}",
        detail={"note": "This is a connectivity test from CloudLens", "impact": "€0.00"},
        impact_eur=0.0,
    )

    label = await deliver_escalation(test_event, tenant_id, channel_type)
    success = not any(
        x in label for x in ("failed", "unconfigured", "incomplete", "auth_failed", "error")
    )
    return {
        "tenant_id": tenant_id,
        "channel_type": channel_type,
        "status": label,
        "success": success,
        "message": (
            "Test event delivered successfully."
            if success
            else f"Delivery did not succeed: {label}. "
                 f"Check Key Vault secret '{_kv_secret_names(tenant_id, channel_type)['required_secret']}' "
                 f"and integration configuration."
        ),
    }
