"""
CloudLens Escalation Integrations
==================================

Delivers alert events to external incident / ticket platforms:

  PagerDuty  — Events API v2 (trigger/acknowledge lifecycle, deduplication)
  Jira       — REST API v3 (create Bug with ADF description + priority)
  ADO        — Azure DevOps Work Items API v7.1 (JSON Patch, Bug / Task)
  Teams      — Adaptive Card v1.5 (richer than MessageCard; action buttons)

Architecture
------------
Each integration is configured per-tenant.  Non-secret fields live in Cosmos
DB (container: escalation_configs).  Secrets (API tokens, PATs, routing keys)
live in Azure Key Vault using standard naming:

  pd-routing-key-{tenant_id}    PagerDuty Events API v2 routing key
  jira-api-token-{tenant_id}    Jira API token  (used with jira_email for Basic)
  ado-pat-{tenant_id}           Azure DevOps personal access token

All HTTP calls retry up to 3 times with exponential backoff.  Delivery
failures are logged but never re-raise — they must not interrupt the alert
pipeline.  Each delivery returns a short status string that is appended to
AlertEvent.delivered_channels for observability.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos
from app.services import keyvault as _keyvault

log = get_logger(__name__)

_CONTAINER = "escalation_configs"
_HTTP_TIMEOUT = 15
_MAX_RETRIES = 3

_PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

# Severity mappings
_PD_SEVERITY: dict[str, str] = {
    "critical": "critical",
    "high": "error",
    "medium": "warning",
    "info": "info",
}
_JIRA_PRIORITY: dict[str, str] = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "info": "Low",
}
_ADO_PRIORITY: dict[str, str] = {
    "critical": "1",
    "high": "2",
    "medium": "3",
    "info": "4",
}


# ── Per-tenant integration config ─────────────────────────────────────────────

CHANNEL_TYPES = frozenset({"pagerduty", "jira", "ado", "teams"})


@dataclass
class EscalationConfig:
    id: str                         # f"{tenant_id}_{channel_type}"
    tenant_id: str
    channel_type: str               # "pagerduty" | "jira" | "ado" | "teams"
    enabled: bool = True

    # ── PagerDuty ─────────────────────────────────────────────────────────
    pagerduty_service_name: str = "CloudLens"
    # KV secret name: pd-routing-key-{tenant_id}

    # ── Jira ──────────────────────────────────────────────────────────────
    jira_base_url: str = ""         # e.g. "https://acme.atlassian.net"
    jira_email: str = ""            # Atlassian account email for Basic auth
    jira_project_key: str = ""      # e.g. "OPS"
    jira_issue_type: str = "Bug"
    jira_labels: list[str] = field(default_factory=lambda: ["cloudlens", "finops"])
    # KV secret name: jira-api-token-{tenant_id}

    # ── Azure DevOps ──────────────────────────────────────────────────────
    ado_org: str = ""               # e.g. "mycompany"
    ado_project: str = ""           # e.g. "Operations"
    ado_work_item_type: str = "Bug"
    ado_area_path: str = ""         # e.g. "Operations\\FinOps"
    ado_iteration_path: str = ""
    ado_tags: str = "CloudLens; FinOps"
    # KV secret name: ado-pat-{tenant_id}

    # ── Teams Adaptive Card ───────────────────────────────────────────────
    teams_webhook_url: str = ""     # Direct URL (alternative to KV secret)
    teams_action_url: str = ""      # Deep-link into CloudLens portal
    # KV secret name (preferred over direct URL): teams-webhook-{tenant_id}

    updated_at: str = ""

    def to_cosmos(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "channel_type": self.channel_type,
            "enabled": self.enabled,
            "pagerduty_service_name": self.pagerduty_service_name,
            "jira_base_url": self.jira_base_url,
            "jira_email": self.jira_email,
            "jira_project_key": self.jira_project_key,
            "jira_issue_type": self.jira_issue_type,
            "jira_labels": self.jira_labels,
            "ado_org": self.ado_org,
            "ado_project": self.ado_project,
            "ado_work_item_type": self.ado_work_item_type,
            "ado_area_path": self.ado_area_path,
            "ado_iteration_path": self.ado_iteration_path,
            "ado_tags": self.ado_tags,
            "teams_webhook_url": self.teams_webhook_url,
            "teams_action_url": self.teams_action_url,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_cosmos(cls, doc: dict) -> "EscalationConfig":
        return cls(
            id=doc["id"],
            tenant_id=doc["tenant_id"],
            channel_type=doc["channel_type"],
            enabled=bool(doc.get("enabled", True)),
            pagerduty_service_name=doc.get("pagerduty_service_name", "CloudLens"),
            jira_base_url=doc.get("jira_base_url", ""),
            jira_email=doc.get("jira_email", ""),
            jira_project_key=doc.get("jira_project_key", ""),
            jira_issue_type=doc.get("jira_issue_type", "Bug"),
            jira_labels=list(doc.get("jira_labels", ["cloudlens", "finops"])),
            ado_org=doc.get("ado_org", ""),
            ado_project=doc.get("ado_project", ""),
            ado_work_item_type=doc.get("ado_work_item_type", "Bug"),
            ado_area_path=doc.get("ado_area_path", ""),
            ado_iteration_path=doc.get("ado_iteration_path", ""),
            ado_tags=doc.get("ado_tags", "CloudLens; FinOps"),
            teams_webhook_url=doc.get("teams_webhook_url", ""),
            teams_action_url=doc.get("teams_action_url", ""),
            updated_at=doc.get("updated_at", ""),
        )

    @classmethod
    def make_id(cls, tenant_id: str, channel_type: str) -> str:
        return f"{tenant_id}_{channel_type}"


# ── Config CRUD ───────────────────────────────────────────────────────────────

async def get_escalation_config(
    tenant_id: str,
    channel_type: str,
) -> Optional[EscalationConfig]:
    """Return the escalation config for (tenant, channel_type), or None."""
    try:
        rows = await cosmos.query_items(
            _CONTAINER,
            "SELECT * FROM c WHERE c.tenant_id=@t AND c.channel_type=@ch",
            parameters=[
                {"name": "@t", "value": tenant_id},
                {"name": "@ch", "value": channel_type},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        return None
    return EscalationConfig.from_cosmos(rows[0]) if rows else None


async def list_escalation_configs(tenant_id: str) -> list[EscalationConfig]:
    """Return all escalation configs for a tenant."""
    try:
        rows = await cosmos.query_items(
            _CONTAINER,
            "SELECT * FROM c WHERE c.tenant_id=@t",
            parameters=[{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
    except CosmosError:
        return []
    return [EscalationConfig.from_cosmos(r) for r in rows]


async def save_escalation_config(cfg: EscalationConfig) -> EscalationConfig:
    """Upsert an escalation config."""
    cfg.updated_at = datetime.now(timezone.utc).isoformat()
    await cosmos.upsert_item(_CONTAINER, cfg.to_cosmos(), partition_key=cfg.tenant_id)
    return cfg


async def delete_escalation_config(tenant_id: str, channel_type: str) -> bool:
    """Delete an escalation config. Returns True if it existed."""
    doc_id = EscalationConfig.make_id(tenant_id, channel_type)
    try:
        await cosmos.delete_item(_CONTAINER, doc_id, tenant_id)
        return True
    except Exception:
        return False


# ── HTTP retry helper ─────────────────────────────────────────────────────────

async def _post_retry(
    url: str,
    payload: dict,
    headers: dict,
    method: str = "POST",
) -> tuple[bool, int, str]:
    """POST with retry. Returns (success, status_code, body_snippet)."""
    body = json.dumps(payload, default=str).encode()
    delay = 1.0
    last_status, last_body = 0, ""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.request(
                    method, url, content=body, headers=headers
                )
                last_status = resp.status_code
                last_body = resp.text[:200]
                if resp.status_code < 300:
                    return True, resp.status_code, last_body
                log.warning(
                    "escalation.http_error",
                    url=url[:80], status=resp.status_code, attempt=attempt,
                )
        except Exception as exc:
            log.warning("escalation.http_exception", url=url[:80], error=str(exc), attempt=attempt)
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(delay)
            delay *= 2
    return False, last_status, last_body


# ── PagerDuty ─────────────────────────────────────────────────────────────────

def _build_pagerduty_payload(
    event,  # AlertEvent
    routing_key: str,
    service_name: str = "CloudLens",
    event_action: str = "trigger",
) -> dict:
    """
    PagerDuty Events API v2 payload.

    dedup_key is derived from the rule_id so PagerDuty can deduplicate
    repeated alerts for the same rule (e.g. a budget that stays breached).
    """
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    pd_sev = _PD_SEVERITY.get(sev.lower(), "warning")
    custom_details: dict = {
        "tenant_id": event.tenant_id,
        "rule_id": event.rule_id,
        "alert_type": (
            event.alert_type.value
            if hasattr(event.alert_type, "value")
            else str(event.alert_type)
        ),
        "impact_eur": event.impact_eur,
    }
    if isinstance(event.detail, dict):
        custom_details.update(event.detail)
    return {
        "routing_key": routing_key,
        "event_action": event_action,
        "dedup_key": f"cloudlens-{event.rule_id}",
        "payload": {
            "summary": event.title,
            "severity": pd_sev,
            "source": service_name,
            "component": "CloudLens FinOps",
            "custom_details": custom_details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "links": [],
    }


async def escalate_pagerduty(event, tenant_id: str) -> str:
    """
    Trigger a PagerDuty incident for the alert event.
    Returns a status label for AlertEvent.delivered_channels.
    """
    cfg = await get_escalation_config(tenant_id, "pagerduty")
    if not cfg or not cfg.enabled:
        log.debug("escalation.pagerduty_not_configured", tenant_id=tenant_id)
        return "pagerduty_unconfigured"

    try:
        routing_key = await _keyvault.get_secret(f"pd-routing-key-{tenant_id}")
    except Exception as exc:
        log.error("escalation.pagerduty_secret_fetch_failed", tenant_id=tenant_id, error=str(exc))
        return "pagerduty_auth_failed"

    payload = _build_pagerduty_payload(event, routing_key, cfg.pagerduty_service_name)
    ok, status, _ = await _post_retry(
        _PD_EVENTS_URL, payload, {"Content-Type": "application/json"}
    )
    label = "pagerduty" if ok else f"pagerduty_failed({status})"
    log.info("escalation.pagerduty_delivered", tenant_id=tenant_id, ok=ok, status=status)
    return label


# ── Jira ─────────────────────────────────────────────────────────────────────

def _build_jira_payload(event, cfg: EscalationConfig) -> dict:
    """
    Jira REST API v3 issue creation payload.

    Description uses Atlassian Document Format (ADF) — the v3 requirement.
    """
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    priority_name = _JIRA_PRIORITY.get(sev.lower(), "Medium")

    # Build ADF paragraph nodes from detail dict
    detail_nodes: list[dict] = []
    if isinstance(event.detail, dict):
        for k, v in event.detail.items():
            if v is not None:
                detail_nodes.append({
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{k.replace('_', ' ').title()}: {v}",
                        }
                    ],
                })
    adf_description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": event.title, "marks": [{"type": "strong"}]}
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Tenant: {event.tenant_id}  |  "
                            f"Severity: {sev.upper()}  |  "
                            f"Impact: €{event.impact_eur:,.2f}"
                        ),
                    }
                ],
            },
            *detail_nodes,
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": f"Rule: {event.rule_name}  |  Generated by CloudLens FinOps",
                        "marks": [{"type": "em"}],
                    }
                ],
            },
        ],
    }

    return {
        "fields": {
            "project": {"key": cfg.jira_project_key},
            "summary": f"[CloudLens] {event.title}",
            "description": adf_description,
            "issuetype": {"name": cfg.jira_issue_type},
            "priority": {"name": priority_name},
            "labels": cfg.jira_labels,
        }
    }


async def escalate_jira(event, tenant_id: str) -> str:
    """Create a Jira issue for the alert event."""
    cfg = await get_escalation_config(tenant_id, "jira")
    if not cfg or not cfg.enabled:
        return "jira_unconfigured"
    if not cfg.jira_base_url or not cfg.jira_project_key or not cfg.jira_email:
        log.warning("escalation.jira_incomplete_config", tenant_id=tenant_id)
        return "jira_config_incomplete"

    try:
        api_token = await _keyvault.get_secret(f"jira-api-token-{tenant_id}")
    except Exception as exc:
        log.error("escalation.jira_secret_fetch_failed", tenant_id=tenant_id, error=str(exc))
        return "jira_auth_failed"

    creds = base64.b64encode(f"{cfg.jira_email}:{api_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{cfg.jira_base_url.rstrip('/')}/rest/api/3/issue"
    payload = _build_jira_payload(event, cfg)
    ok, status, body = await _post_retry(url, payload, headers)
    label = "jira" if ok else f"jira_failed({status})"
    log.info("escalation.jira_delivered", tenant_id=tenant_id, ok=ok, status=status,
             issue=body[:60] if ok else "")
    return label


# ── Azure DevOps ──────────────────────────────────────────────────────────────

def _build_ado_payload(event, cfg: EscalationConfig) -> list[dict]:
    """
    Azure DevOps Work Items JSON Patch document.

    Uses the standard ADO Bug fields; area path and iteration path are
    optional (omitted when empty so ADO uses the project defaults).
    """
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    priority = _ADO_PRIORITY.get(sev.lower(), "3")

    detail_lines = [f"**{event.title}**\n"]
    detail_lines.append(f"Tenant: {event.tenant_id}")
    detail_lines.append(f"Severity: {sev.upper()}")
    detail_lines.append(f"Impact: €{event.impact_eur:,.2f}")
    detail_lines.append(f"Rule: {event.rule_name}")
    if isinstance(event.detail, dict):
        for k, v in event.detail.items():
            if v is not None:
                detail_lines.append(f"{k.replace('_', ' ').title()}: {v}")
    detail_lines.append("\n_Generated by CloudLens FinOps Platform_")
    description_html = "<br>".join(detail_lines)

    ops = [
        {"op": "add", "path": "/fields/System.Title",
         "value": f"[CloudLens] {event.title}"},
        {"op": "add", "path": "/fields/System.Description",
         "value": description_html},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority",
         "value": int(priority)},
        {"op": "add", "path": "/fields/System.Tags",
         "value": cfg.ado_tags},
    ]
    if cfg.ado_area_path:
        ops.append({"op": "add", "path": "/fields/System.AreaPath",
                    "value": cfg.ado_area_path})
    if cfg.ado_iteration_path:
        ops.append({"op": "add", "path": "/fields/System.IterationPath",
                    "value": cfg.ado_iteration_path})
    return ops


async def escalate_ado(event, tenant_id: str) -> str:
    """Create an Azure DevOps work item for the alert event."""
    cfg = await get_escalation_config(tenant_id, "ado")
    if not cfg or not cfg.enabled:
        return "ado_unconfigured"
    if not cfg.ado_org or not cfg.ado_project:
        log.warning("escalation.ado_incomplete_config", tenant_id=tenant_id)
        return "ado_config_incomplete"

    try:
        pat = await _keyvault.get_secret(f"ado-pat-{tenant_id}")
    except Exception as exc:
        log.error("escalation.ado_secret_fetch_failed", tenant_id=tenant_id, error=str(exc))
        return "ado_auth_failed"

    creds = base64.b64encode(f":{pat}".encode()).decode()
    wit = cfg.ado_work_item_type.replace(" ", "%20")
    url = (
        f"https://dev.azure.com/{cfg.ado_org}/{cfg.ado_project}"
        f"/_apis/wit/workitems/${wit}?api-version=7.1"
    )
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json-patch+json",
    }
    payload_list = _build_ado_payload(event, cfg)
    # ADO takes a JSON array, not an object — use raw content
    body_bytes = json.dumps(payload_list, default=str).encode()
    delay = 1.0
    ok, last_status = False, 0
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)
                last_status = resp.status_code
                if resp.status_code < 300:
                    ok = True
                    break
        except Exception as exc:
            log.warning("escalation.ado_exception", error=str(exc), attempt=attempt)
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(delay)
            delay *= 2

    label = "ado" if ok else f"ado_failed({last_status})"
    log.info("escalation.ado_delivered", tenant_id=tenant_id, ok=ok, status=last_status)
    return label


# ── Teams Adaptive Card ───────────────────────────────────────────────────────

def _build_teams_adaptive_card(event, cfg: EscalationConfig) -> dict:
    """
    Microsoft Teams Adaptive Card v1.5 payload.

    Richer than the legacy MessageCard: uses column sets, fact sets, and
    optional action buttons ("View in CloudLens", external link).
    Delivered via an incoming webhook URL.
    """
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    color_map = {
        "critical": "attention",
        "high": "warning",
        "medium": "accent",
        "info": "good",
    }
    color = color_map.get(sev.lower(), "accent")

    facts = [
        {"title": "Severity", "value": sev.upper()},
        {"title": "Impact", "value": f"€{event.impact_eur:,.2f}"},
        {"title": "Tenant", "value": event.tenant_id},
        {"title": "Rule", "value": event.rule_name},
    ]
    if isinstance(event.detail, dict):
        for k, v in list(event.detail.items())[:6]:
            if v is not None:
                facts.append({"title": k.replace("_", " ").title(), "value": str(v)})

    card_body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"🔔 CloudLens Alert",
            "weight": "bolder",
            "size": "medium",
            "color": color,
        },
        {
            "type": "TextBlock",
            "text": event.title,
            "wrap": True,
            "weight": "bolder",
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
    ]

    actions: list[dict] = []
    if cfg.teams_action_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "View in CloudLens",
            "url": cfg.teams_action_url,
        })

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": card_body,
                    **({"actions": actions} if actions else {}),
                },
            }
        ],
    }


async def escalate_teams(event, tenant_id: str) -> str:
    """Deliver an Adaptive Card to Teams for the alert event."""
    cfg = await get_escalation_config(tenant_id, "teams")
    if not cfg or not cfg.enabled:
        return "teams_unconfigured"

    # Webhook URL: direct config takes priority, then KV secret
    webhook_url = cfg.teams_webhook_url
    if not webhook_url:
        try:
            webhook_url = await _keyvault.get_secret(f"teams-webhook-{tenant_id}")
        except Exception as exc:
            log.error("escalation.teams_secret_fetch_failed", tenant_id=tenant_id, error=str(exc))
            return "teams_auth_failed"

    if not webhook_url:
        return "teams_unconfigured"

    payload = _build_teams_adaptive_card(event, cfg)
    ok, status, _ = await _post_retry(
        webhook_url, payload, {"Content-Type": "application/json"}
    )
    label = "teams_adaptive" if ok else f"teams_adaptive_failed({status})"
    log.info("escalation.teams_delivered", tenant_id=tenant_id, ok=ok, status=status)
    return label


# ── Unified dispatcher ────────────────────────────────────────────────────────

async def deliver_escalation(event, tenant_id: str, channel_type: str) -> str:
    """
    Route an alert event to the appropriate escalation channel.
    Returns a status label for AlertEvent.delivered_channels.
    Never raises.
    """
    try:
        if channel_type == "pagerduty":
            return await escalate_pagerduty(event, tenant_id)
        elif channel_type == "jira":
            return await escalate_jira(event, tenant_id)
        elif channel_type == "ado":
            return await escalate_ado(event, tenant_id)
        elif channel_type == "teams":
            return await escalate_teams(event, tenant_id)
        else:
            log.warning("escalation.unknown_channel", channel=channel_type)
            return f"unknown_channel({channel_type})"
    except Exception as exc:
        log.error("escalation.unexpected_error", channel=channel_type, error=str(exc))
        return f"{channel_type}_error"
