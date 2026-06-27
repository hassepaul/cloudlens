"""
Bot notification service — Slack and Microsoft Teams.

Architecture
────────────
  Slack:   Incoming Webhooks for push notifications.
           Slash-command handler for interactive queries (/cloudlens …).
           Events API for the URL-verification handshake.

  Teams:   Incoming Webhooks (Adaptive Cards) for push notifications.
           Bot Framework message handler for interactive queries.

Notification types pushed by the alert engine:
  • budget_breach  — budget threshold crossed
  • spend_spike    — anomaly detected
  • waste          — recoverable waste above threshold
  • cost_summary   — daily/on-demand spend summary

Slash / @ commands:
  /cloudlens spend  <tenant_id>  — today's spend summary
  /cloudlens budget <tenant_id>  — budget status
  /cloudlens status              — scheduler + poll state

Secrets stored in Key Vault:
  slack-webhook-{tenant_id}        — Slack incoming webhook URL
  slack-signing-secret-{tenant_id} — Slack signing secret (request verification)
  teams-webhook-{tenant_id}        — Teams incoming webhook URL
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.logging_config import get_logger
from app.services import keyvault

log = get_logger(__name__)

_HTTP_TIMEOUT = 10  # seconds


# ── Slack block-kit helpers ───────────────────────────────────────────────────

def _slack_budget_blocks(
    tenant_id: str,
    budget_name: str,
    spent_eur: float,
    budget_eur: float,
    pct: float,
) -> list[dict]:
    emoji = "🔴" if pct >= 100 else "🟠" if pct >= 80 else "🟡"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} Budget Alert — {tenant_id}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Budget*\n{budget_name}"},
                {"type": "mrkdwn", "text": f"*Spent*\n€{spent_eur:,.2f} / €{budget_eur:,.2f} ({pct:.0f}%)"},
            ],
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "CloudLens FinOps Platform"}],
        },
    ]


def _slack_spend_blocks(
    tenant_id: str,
    total_eur: float,
    by_service: list[dict],
    period: str = "today",
) -> list[dict]:
    top = sorted(by_service, key=lambda x: x.get("cost_eur", 0), reverse=True)[:5]
    lines = "\n".join(
        f"• {r['service']}: €{r['cost_eur']:,.2f}" for r in top
    ) or "_No spend data_"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"💰 Spend Summary — {tenant_id}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Period:* {period}\n*Total:* €{total_eur:,.2f}\n\n*Top services:*\n{lines}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "CloudLens FinOps Platform"}],
        },
    ]


def _slack_anomaly_blocks(
    tenant_id: str,
    service: str,
    current_eur: float,
    baseline_eur: float,
    pct_increase: float,
) -> list[dict]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ Spend Spike — {tenant_id}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service*\n{service}"},
                {"type": "mrkdwn", "text": f"*Current*\n€{current_eur:,.2f}"},
                {"type": "mrkdwn", "text": f"*Baseline*\n€{baseline_eur:,.2f}"},
                {"type": "mrkdwn", "text": f"*Increase*\n+{pct_increase:.0f}%"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "CloudLens FinOps Platform"}],
        },
    ]


# ── Teams Adaptive Card helpers ───────────────────────────────────────────────

def _teams_budget_card(
    tenant_id: str,
    budget_name: str,
    spent_eur: float,
    budget_eur: float,
    pct: float,
) -> dict:
    color = "attention" if pct >= 100 else "warning" if pct >= 80 else "default"
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"🔔 Budget Alert — {tenant_id}",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": color,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Budget", "value": budget_name},
                                {"title": "Spent", "value": f"€{spent_eur:,.2f}"},
                                {"title": "Limit", "value": f"€{budget_eur:,.2f}"},
                                {"title": "Usage", "value": f"{pct:.0f}%"},
                            ],
                        },
                    ],
                    "msteams": {"width": "Full"},
                },
            }
        ],
    }


def _teams_spend_card(
    tenant_id: str,
    total_eur: float,
    by_service: list[dict],
    period: str = "today",
) -> dict:
    top = sorted(by_service, key=lambda x: x.get("cost_eur", 0), reverse=True)[:5]
    facts = [{"title": r["service"], "value": f"€{r['cost_eur']:,.2f}"} for r in top]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"💰 Spend Summary — {tenant_id}",
                            "weight": "Bolder",
                            "size": "Medium",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Period", "value": period},
                                {"title": "Total", "value": f"€{total_eur:,.2f}"},
                            ] + facts,
                        },
                    ],
                    "msteams": {"width": "Full"},
                },
            }
        ],
    }


def _teams_anomaly_card(
    tenant_id: str,
    service: str,
    current_eur: float,
    baseline_eur: float,
    pct_increase: float,
) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"⚠️ Spend Spike — {tenant_id}",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "warning",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Service", "value": service},
                                {"title": "Current", "value": f"€{current_eur:,.2f}"},
                                {"title": "Baseline", "value": f"€{baseline_eur:,.2f}"},
                                {"title": "Increase", "value": f"+{pct_increase:.0f}%"},
                            ],
                        },
                    ],
                    "msteams": {"width": "Full"},
                },
            }
        ],
    }


# ── Delivery ──────────────────────────────────────────────────────────────────

async def _get_webhook_url(secret_name: str) -> Optional[str]:
    """Fetch a webhook URL from Key Vault, return None if not configured."""
    try:
        return await keyvault.get_secret(secret_name)
    except Exception:
        return None


async def send_slack(
    tenant_id: str,
    *,
    text: str,
    blocks: Optional[list[dict]] = None,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    POST a message to a Slack incoming webhook.

    Returns True on success, False if the webhook is not configured or fails.
    Uses the per-tenant webhook URL from Key Vault unless override provided.
    """
    url = webhook_url or await _get_webhook_url(f"slack-webhook-{tenant_id}")
    if not url:
        log.debug("bot_notifications.slack_not_configured", tenant_id=tenant_id)
        return False

    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        log.info("bot_notifications.slack_sent", tenant_id=tenant_id)
        return True
    except httpx.HTTPStatusError as exc:
        log.warning(
            "bot_notifications.slack_delivery_failed",
            tenant_id=tenant_id,
            status=exc.response.status_code,
        )
        return False
    except Exception as exc:
        log.warning("bot_notifications.slack_error", tenant_id=tenant_id, error=str(exc))
        return False


async def send_teams(
    tenant_id: str,
    *,
    card: Optional[dict] = None,
    text: Optional[str] = None,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    POST an Adaptive Card (or plain text) to a Teams incoming webhook.

    Returns True on success, False if not configured or delivery fails.
    """
    url = webhook_url or await _get_webhook_url(f"teams-webhook-{tenant_id}")
    if not url:
        log.debug("bot_notifications.teams_not_configured", tenant_id=tenant_id)
        return False

    if card:
        payload = card
    else:
        payload = {"text": text or ""}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        log.info("bot_notifications.teams_sent", tenant_id=tenant_id)
        return True
    except httpx.HTTPStatusError as exc:
        log.warning(
            "bot_notifications.teams_delivery_failed",
            tenant_id=tenant_id,
            status=exc.response.status_code,
        )
        return False
    except Exception as exc:
        log.warning("bot_notifications.teams_error", tenant_id=tenant_id, error=str(exc))
        return False


# ── High-level notification dispatch ─────────────────────────────────────────

async def notify_budget_breach(
    tenant_id: str,
    budget_name: str,
    spent_eur: float,
    budget_eur: float,
) -> dict[str, bool]:
    """Send a budget breach alert to all configured channels for a tenant."""
    pct = (spent_eur / budget_eur * 100) if budget_eur > 0 else 0.0
    text = f"Budget alert: {budget_name} at {pct:.0f}% (€{spent_eur:,.2f} / €{budget_eur:,.2f})"

    slack_ok = await send_slack(
        tenant_id,
        text=text,
        blocks=_slack_budget_blocks(tenant_id, budget_name, spent_eur, budget_eur, pct),
    )
    teams_ok = await send_teams(
        tenant_id,
        card=_teams_budget_card(tenant_id, budget_name, spent_eur, budget_eur, pct),
    )
    return {"slack": slack_ok, "teams": teams_ok}


async def notify_spend_spike(
    tenant_id: str,
    service: str,
    current_eur: float,
    baseline_eur: float,
) -> dict[str, bool]:
    """Send a spend anomaly alert to all configured channels."""
    pct = ((current_eur - baseline_eur) / baseline_eur * 100) if baseline_eur > 0 else 0.0
    text = f"Spend spike: {service} up {pct:.0f}% (€{current_eur:,.2f} vs €{baseline_eur:,.2f} baseline)"

    slack_ok = await send_slack(
        tenant_id,
        text=text,
        blocks=_slack_anomaly_blocks(tenant_id, service, current_eur, baseline_eur, pct),
    )
    teams_ok = await send_teams(
        tenant_id,
        card=_teams_anomaly_card(tenant_id, service, current_eur, baseline_eur, pct),
    )
    return {"slack": slack_ok, "teams": teams_ok}


async def notify_spend_summary(
    tenant_id: str,
    total_eur: float,
    by_service: list[dict],
    period: str = "today",
) -> dict[str, bool]:
    """Push a spend summary (daily digest or on-demand) to all channels."""
    text = f"Spend summary ({period}): €{total_eur:,.2f} total"

    slack_ok = await send_slack(
        tenant_id,
        text=text,
        blocks=_slack_spend_blocks(tenant_id, total_eur, by_service, period),
    )
    teams_ok = await send_teams(
        tenant_id,
        card=_teams_spend_card(tenant_id, total_eur, by_service, period),
    )
    return {"slack": slack_ok, "teams": teams_ok}


# ── Slack request verification ────────────────────────────────────────────────

async def verify_slack_signature(
    tenant_id: str,
    *,
    timestamp: str,
    signature: str,
    raw_body: bytes,
    max_age_seconds: int = 300,
) -> bool:
    """
    Verify a Slack request using HMAC-SHA256.
    Rejects requests older than max_age_seconds to prevent replay attacks.
    Returns True if valid, False otherwise.
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    if abs(time.time() - ts) > max_age_seconds:
        return False

    signing_secret = await _get_webhook_url(f"slack-signing-secret-{tenant_id}")
    if not signing_secret:
        return False

    base = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base.encode(), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# ── Slash command response builder ───────────────────────────────────────────

def build_slash_response(
    tenant_id: str,
    command: str,
    total_eur: Optional[float] = None,
    by_service: Optional[list[dict]] = None,
    error: Optional[str] = None,
) -> dict:
    """
    Build an ephemeral Slack slash-command response payload.
    Used by the /cloudlens slash command handler.
    """
    if error:
        return {
            "response_type": "ephemeral",
            "text": f"❌ {error}",
        }

    if command == "spend" and total_eur is not None:
        top = sorted(by_service or [], key=lambda x: x.get("cost_eur", 0), reverse=True)[:5]
        lines = "\n".join(
            f"• {r['service']}: €{r['cost_eur']:,.2f}" for r in top
        ) or "_No data_"
        return {
            "response_type": "ephemeral",
            "blocks": _slack_spend_blocks(tenant_id, total_eur, by_service or []),
        }

    if command == "budget":
        return {
            "response_type": "ephemeral",
            "text": f"Budget data for *{tenant_id}* — use the CloudLens dashboard for full details.",
        }

    return {
        "response_type": "ephemeral",
        "text": (
            "*CloudLens commands:*\n"
            "• `/cloudlens spend <tenant_id>` — today's spend summary\n"
            "• `/cloudlens budget <tenant_id>` — budget status\n"
            "• `/cloudlens status` — scheduler status"
        ),
    }
