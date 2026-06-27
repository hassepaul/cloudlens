"""
Alert engine
============

Evaluates alert rules against the current signals (budgets, anomalies, resource
anomalies, waste, idle commitments) and produces AlertEvents. Delivery is
abstracted behind channels:

  in_app   — always works; the event is stored and shown in the console.
  webhook  — POST the event JSON to a URL (Slack/Teams/PagerDuty incoming hook).
  email    — requires an SMTP / SendGrid / Azure Communication Services
             integration. The channel is implemented as a clear extension point;
             until a provider is configured, email events are recorded as
             "pending delivery" rather than silently dropped.

Evaluation is pure (signals in → events out), so it is fully unit-testable
without Cosmos or any network.
"""
from __future__ import annotations
from typing import Optional

import httpx

from app.logging_config import get_logger
from app.models.alert import (
    AlertRule, AlertEvent, AlertType, AlertSeverity, AlertChannel,
)

log = get_logger(__name__)


# ── Evaluation ───────────────────────────────────────────────────────────────

def evaluate_rules(
    rules: list[AlertRule],
    *,
    budget_statuses: Optional[list[dict]] = None,
    spend_anomalies: Optional[list] = None,        # tenant-level Anomaly objects
    resource_anomalies: Optional[list] = None,     # ResourceAnomaly objects
    recoverable_eur: float = 0.0,
    idle_commitment_eur: float = 0.0,
) -> list[AlertEvent]:
    """Return the AlertEvents that should fire given the current signals."""
    events: list[AlertEvent] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.alert_type == AlertType.BUDGET_BREACH:
            events += _eval_budget(rule, budget_statuses or [])
        elif rule.alert_type == AlertType.SPEND_SPIKE:
            events += _eval_spend_spike(rule, spend_anomalies or [])
        elif rule.alert_type == AlertType.RESOURCE_ANOMALY:
            events += _eval_resource_anomaly(rule, resource_anomalies or [])
        elif rule.alert_type == AlertType.WASTE_THRESHOLD:
            events += _eval_waste(rule, recoverable_eur)
        elif rule.alert_type == AlertType.COMMITMENT_IDLE:
            events += _eval_idle_commitment(rule, idle_commitment_eur)
    return events


def _mk(rule, severity, title, title_it, detail, impact) -> AlertEvent:
    return AlertEvent(
        tenant_id=rule.tenant_id, rule_id=rule.id, rule_name=rule.name,
        alert_type=rule.alert_type, severity=severity, title=title,
        title_it=title_it, detail=detail, impact_eur=round(impact, 2),
    )


def _eval_budget(rule, statuses) -> list[AlertEvent]:
    out = []
    for bs in statuses:
        consumed = bs.get("consumed_pct", 0)
        projected = bs.get("projected_consumed_pct")
        worst = max(consumed, projected or 0)
        if worst >= rule.threshold:
            breached = consumed >= 100
            sev = AlertSeverity.CRITICAL if breached else AlertSeverity.HIGH
            name = bs.get("name", "budget")
            out.append(_mk(
                rule, sev,
                f"Budget '{name}' at {worst:.0f}% of €{bs.get('amount_eur',0):,.0f}",
                f"Budget '{name}' al {worst:.0f}% di €{bs.get('amount_eur',0):,.0f}",
                {"budget_id": bs.get("budget_id"), "consumed_pct": consumed,
                 "projected_pct": projected}, bs.get("amount_eur", 0)))
    return out


def _eval_spend_spike(rule, anomalies) -> list[AlertEvent]:
    out = []
    for a in anomalies:
        if getattr(a, "direction", "") == "spike" and abs(a.z_score) >= rule.threshold:
            drv = a.drivers[0].name if getattr(a, "drivers", None) else "unknown"
            out.append(_mk(
                rule, AlertSeverity.HIGH if a.severity == "high" else AlertSeverity.MEDIUM,
                f"Spend spike €{a.excess_eur:,.0f} on {a.day} (driver: {drv})",
                f"Picco di spesa €{a.excess_eur:,.0f} il {a.day} (causa: {drv})",
                {"day": a.day, "z_score": a.z_score, "driver": drv}, a.excess_eur))
    return out


def _eval_resource_anomaly(rule, resource_anomalies) -> list[AlertEvent]:
    out = []
    for a in resource_anomalies:
        if a.z_score < rule.threshold:
            continue
        if rule.provider and a.provider_name != rule.provider:
            continue
        if rule.sub_account_id and a.sub_account_id != rule.sub_account_id:
            continue
        out.append(_mk(
            rule, AlertSeverity.HIGH if a.severity == "high" else AlertSeverity.MEDIUM,
            f"Resource '{a.resource_name}' cost spiked to €{a.actual_eur:,.0f} "
            f"(expected €{a.expected_eur:,.0f}) on {a.day}",
            f"La risorsa '{a.resource_name}' è salita a €{a.actual_eur:,.0f} "
            f"(attesi €{a.expected_eur:,.0f}) il {a.day}",
            {"resource_id": a.resource_id, "resource_name": a.resource_name,
             "provider": a.provider_name, "sub_account_id": a.sub_account_id,
             "service": a.service_name, "z_score": a.z_score, "day": a.day},
            a.excess_eur))
    return out


def _eval_waste(rule, recoverable_eur) -> list[AlertEvent]:
    if recoverable_eur >= rule.threshold:
        return [_mk(
            rule, AlertSeverity.HIGH,
            f"Recoverable spend €{recoverable_eur:,.0f} exceeds threshold €{rule.threshold:,.0f}",
            f"Spesa recuperabile €{recoverable_eur:,.0f} oltre la soglia €{rule.threshold:,.0f}",
            {"recoverable_eur": recoverable_eur}, recoverable_eur)]
    return []


def _eval_idle_commitment(rule, idle_eur) -> list[AlertEvent]:
    if idle_eur >= rule.threshold:
        return [_mk(
            rule, AlertSeverity.MEDIUM,
            f"Idle commitment €{idle_eur:,.0f}/mo exceeds threshold €{rule.threshold:,.0f}",
            f"Impegno inutilizzato €{idle_eur:,.0f}/mese oltre la soglia €{rule.threshold:,.0f}",
            {"idle_eur": idle_eur}, idle_eur)]
    return []


# ── Delivery ─────────────────────────────────────────────────────────────────
# Terminology for channel types stored in AlertRule.webhook_url:
#   auto-detected from URL pattern:
#     hooks.slack.com  → Slack Block Kit format
#     outlook.office.com / office365.com → Teams MessageCard format
#     anything else    → generic JSON webhook (HMAC-signed)
#
# Retry: 3 attempts, exponential backoff (1s, 2s, 4s). Delivery failures are
# logged but never raise — they must not interrupt billing ingest or API paths.

import hashlib
import hmac
import json
import time

_SLACK_HOST = "hooks.slack.com"
_TEAMS_HOSTS = ("outlook.office.com", "office365.com", "webhook.office.com")
_MAX_DELIVERY_ATTEMPTS = 3


def _is_slack_url(url: str) -> bool:
    return _SLACK_HOST in url


def _is_teams_url(url: str) -> bool:
    return any(h in url for h in _TEAMS_HOSTS)


def _build_slack_payload(event: "AlertEvent") -> dict:
    """Slack Block Kit message.

    Uses a *header* block (bold), a *section* with the detail text, and a
    *context* footer with the tenant and severity.  Colour is conveyed via an
    optional *attachment* (legacy attachment is the only way to set the sidebar
    colour in Slack).
    """
    colour_map = {
        "critical": "#FF0000",
        "high": "#FF6600",
        "medium": "#FFAA00",
        "low": "#36A64F",
        "info": "#0078D4",
    }
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    colour = colour_map.get(sev.lower(), "#0078D4")
    detail_text = ""
    if isinstance(event.detail, dict):
        detail_text = "\n".join(f"• *{k}*: {v}" for k, v in event.detail.items() if v is not None)
    elif event.detail:
        detail_text = str(event.detail)

    return {
        "text": event.title,   # fallback / notification preview
        "attachments": [
            {
                "color": colour,
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"🔔 {event.title}"},
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Severity:*\n{sev.upper()}"},
                            {"type": "mrkdwn",
                             "text": f"*Impact:*\n€{event.impact_eur:,.2f}"},
                            {"type": "mrkdwn",
                             "text": f"*Tenant:*\n{event.tenant_id}"},
                            {"type": "mrkdwn",
                             "text": f"*Rule:*\n{event.rule_name}"},
                        ],
                    },
                    *(
                        [{"type": "section",
                          "text": {"type": "mrkdwn", "text": detail_text}}]
                        if detail_text else []
                    ),
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    f"CloudLens Alert  |  "
                                    f"{event.created_at.strftime('%Y-%m-%d %H:%M UTC') if hasattr(event, 'created_at') and event.created_at else 'now'}"
                                ),
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _build_teams_payload(event: "AlertEvent") -> dict:
    """Microsoft Teams MessageCard (Adaptive Card v1 compatible incoming webhook).

    Office 365 Connectors use the legacy MessageCard schema by default — it
    works with all Teams webhook URLs without extra connector configuration.
    """
    sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
    theme_map = {
        "critical": "FF0000",
        "high": "FF6600",
        "medium": "FFAA00",
        "low": "36A64F",
        "info": "0078D4",
    }
    colour = theme_map.get(sev.lower(), "0078D4")
    facts = [
        {"name": "Severity", "value": sev.upper()},
        {"name": "Impact", "value": f"€{event.impact_eur:,.2f}"},
        {"name": "Tenant", "value": event.tenant_id},
        {"name": "Rule", "value": event.rule_name},
    ]
    if isinstance(event.detail, dict):
        for k, v in event.detail.items():
            if v is not None:
                facts.append({"name": k.replace("_", " ").title(), "value": str(v)})
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": colour,
        "summary": event.title,
        "sections": [
            {
                "activityTitle": f"**CloudLens Alert — {sev.upper()}**",
                "activitySubtitle": event.title,
                "facts": facts,
                "markdown": True,
            }
        ],
    }


def _build_generic_payload(event: "AlertEvent") -> dict:
    """Generic signed JSON webhook payload."""
    return {
        "event_id": event.id,
        "tenant_id": event.tenant_id,
        "rule_id": event.rule_id,
        "rule_name": event.rule_name,
        "alert_type": event.alert_type.value if hasattr(event.alert_type, "value") else str(event.alert_type),
        "severity": event.severity.value if hasattr(event.severity, "value") else str(event.severity),
        "title": event.title,
        "impact_eur": event.impact_eur,
        "detail": event.detail,
        "created_at": event.created_at.isoformat() if hasattr(event, "created_at") and event.created_at else None,
    }


def _hmac_signature(body: bytes, secret: str) -> str:
    """HMAC-SHA256 signature for generic webhook; header: X-CloudLens-Signature."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post_with_retry(
    url: str,
    payload: dict,
    headers: dict | None = None,
    *,
    attempts: int = _MAX_DELIVERY_ATTEMPTS,
) -> bool:
    """POST payload to url with exponential backoff. Returns True on success."""
    body = json.dumps(payload, default=str).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, content=body, headers=h)
                if resp.status_code < 300:
                    return True
                log.warning(
                    "alert.delivery_http_error",
                    url=url[:60], status=resp.status_code, attempt=attempt,
                )
        except Exception as exc:
            log.warning("alert.delivery_exception", url=url[:60], error=str(exc), attempt=attempt)
        if attempt < attempts:
            import asyncio
            await asyncio.sleep(delay)
            delay *= 2
    return False


async def deliver(event: "AlertEvent", rule: "AlertRule") -> "AlertEvent":
    """Deliver an event over all configured channels.

    in_app is always recorded (the event is persisted to Cosmos by the caller).
    Slack, Teams, and generic webhooks use purpose-built payloads.
    Email delivery is recorded as 'email_pending' until an SMTP/ACS provider
    is wired into the config — we never silently drop the intent.
    """
    from app.models.alert import AlertChannel  # avoid circular at module level
    delivered = ["in_app"]

    for ch in rule.channels:
        if ch == AlertChannel.WEBHOOK and rule.webhook_url:
            url = rule.webhook_url
            if _is_slack_url(url):
                payload = _build_slack_payload(event)
                success = await _post_with_retry(url, payload)
                channel_label = "slack"
            elif _is_teams_url(url):
                payload = _build_teams_payload(event)
                success = await _post_with_retry(url, payload)
                channel_label = "teams"
            else:
                payload = _build_generic_payload(event)
                body_bytes = json.dumps(payload, default=str).encode()
                # Optional HMAC signing — key comes from rule.webhook_secret if present.
                headers: dict = {}
                webhook_secret = getattr(rule, "webhook_secret", None)
                if webhook_secret:
                    headers["X-CloudLens-Signature"] = _hmac_signature(body_bytes, webhook_secret)
                    headers["X-CloudLens-Timestamp"] = str(int(time.time()))
                success = await _post_with_retry(url, payload, headers)
                channel_label = "webhook"

            delivered.append(channel_label if success else f"{channel_label}_failed")
            if not success:
                log.error("alert.delivery_all_retries_failed",
                          rule_id=rule.id, channel=channel_label, url=url[:60])

        elif ch == AlertChannel.EMAIL and rule.email_to:
            # Extension point: wire SMTP / Azure Communication Services.
            # Mark intent rather than silently dropping.
            log.info("alert.email_pending", to=rule.email_to, title=event.title,
                     rule_id=rule.id)
            delivered.append("email_pending")

        elif ch in (
            AlertChannel.PAGERDUTY,
            AlertChannel.JIRA,
            AlertChannel.ADO,
            AlertChannel.TEAMS,
        ):
            from app.services.escalation import deliver_escalation
            label = await deliver_escalation(event, rule.tenant_id, ch.value)
            delivered.append(label)

    event.delivered_channels = delivered
    return event
