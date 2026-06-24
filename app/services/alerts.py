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

async def deliver(event: AlertEvent, rule: AlertRule) -> AlertEvent:
    """Deliver an event over its rule's channels. in_app is implicit (stored)."""
    delivered = ["in_app"]
    for ch in rule.channels:
        if ch == AlertChannel.WEBHOOK and rule.webhook_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(rule.webhook_url, json={
                        "text": event.title, "severity": event.severity.value,
                        "impact_eur": event.impact_eur, "detail": event.detail,
                    })
                delivered.append("webhook")
            except Exception as exc:   # delivery must never break ingest
                log.warning("alert.webhook_failed", rule_id=rule.id, error=str(exc))
        elif ch == AlertChannel.EMAIL and rule.email_to:
            # Extension point: wire SMTP / SendGrid / Azure Communication Services.
            # Until configured we record intent rather than silently dropping.
            log.info("alert.email_pending", to=rule.email_to, title=event.title)
            delivered.append("email_pending")
    event.delivered_channels = delivered
    return event
