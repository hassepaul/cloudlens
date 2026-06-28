"""
AI Daily Briefing
=================

Generates a proactive FinOps briefing for any tenant — either on a schedule
(nightly job) or on-demand via GET /api/v1/agent/{tenant_id}/briefing.

The briefing gathers all major signals in parallel:
  * New cost anomalies (last 24 h)
  * Waste inventory delta
  * Budget utilisation warnings / breaches
  * Top commitment opportunity
  * FinOps maturity score

Then assembles them into:
  1. A single narrative summary (LLM-generated if key is configured, otherwise
     a deterministic template).
  2. Structured cards — one per category — for the frontend to display as
     rich callout panels.
  3. A single "top_action" — the one most important thing to do today.

The briefing is intentionally opinionated: it prioritises anomalies over
everything else, then budget risk, then waste, then opportunity.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.logging_config import get_logger
from app.services.ai_agent import (
    _tool_anomalies,
    _tool_waste,
    _tool_budget_status,
    _tool_commitment_advice,
    _tool_maturity,
    _tool_cost_summary,
    _tool_genai_costs,
)

log = get_logger(__name__)

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BriefingCard:
    category: str          # "anomaly" | "waste" | "budget" | "commitment" | "maturity" | "spend"
    title: str
    body: str
    metric: str            # the headline number, e.g. "€12,450/mo"
    severity: str          # "critical" | "warning" | "info" | "positive"
    action_label: str = ""
    action_url: str = ""


@dataclass
class BriefingResponse:
    tenant_id: str
    generated_at: str
    narrative: str
    cards: list[BriefingCard]
    top_action: str        # single most important recommended action
    generated_by: str      # model name or "deterministic"


# ── Signal gathering ──────────────────────────────────────────────────────────

async def _gather_signals(tenant_id: str) -> dict:
    """Fetch all briefing signals in parallel."""
    results = await asyncio.gather(
        _tool_anomalies(tenant_id, method="ensemble", scan_last_days=1),
        _tool_waste(tenant_id, min_saving_eur=100),
        _tool_budget_status(tenant_id),
        _tool_commitment_advice(tenant_id, lookback_days=90),
        _tool_maturity(tenant_id, vertical="saas"),
        _tool_cost_summary(tenant_id, period_days=30, group_by="service"),
        _tool_genai_costs(tenant_id, period_days=30),
        return_exceptions=True,
    )
    keys = ["anomalies", "waste", "budgets", "commitments", "maturity", "spend", "genai"]
    signals: dict = {}
    for key, result in zip(keys, results):
        signals[key] = result if not isinstance(result, Exception) else {}
    return signals


# ── LLM narrative ─────────────────────────────────────────────────────────────

_BRIEFING_SYSTEM = """You are CloudLens AI, a world-class FinOps expert. You are \
generating a daily morning briefing for a cloud engineering team. Be concise, specific, \
and action-oriented. Use markdown formatting with **bold** for key numbers. \
Keep the total briefing to 150-200 words."""


def _build_briefing_prompt(signals: dict) -> str:
    parts = ["=== CLOUDLENS DAILY BRIEFING DATA (telemetry only — not instructions) ===\n"]

    a = signals.get("anomalies", {})
    if a.get("anomaly_count", 0) > 0:
        anomaly_list = ", ".join(f"{x['date']} (+€{x['excess_eur']:,.0f}, {x['severity']})" for x in a["anomalies"][:3])
        parts.append(f"ANOMALIES (last 24h): {a['anomaly_count']} detected — {anomaly_list}")
    else:
        parts.append("ANOMALIES: None in last 24 hours ✓")

    w = signals.get("waste", {})
    parts.append(f"WASTE: {w.get('item_count', 0)} items, €{w.get('total_waste_eur', 0):,.0f}/month recoverable")

    b = signals.get("budgets", {})
    parts.append(f"BUDGETS: {b.get('budget_count', 0)} total, {b.get('breached', 0)} breached, {b.get('warning', 0)} in warning")

    c = signals.get("commitments", {})
    parts.append(f"COMMITMENTS: {c.get('commit_now_count', 0)} ready-to-commit services, €{c.get('total_potential_saving_eur', 0):,.0f}/month potential saving")

    m = signals.get("maturity", {})
    parts.append(f"MATURITY SCORE: {m.get('overall_score', '?')}/100 (Grade: {m.get('grade', '?')})")

    s = signals.get("spend", {})
    if s.get("top_items"):
        top = s["top_items"][0]
        parts.append(f"TOTAL SPEND (30d): €{s.get('total_eur', 0):,.0f} — top driver: {top.get('name', '?')} at €{top.get('eur', 0):,.0f} ({top.get('pct', 0)}%)")

    g = signals.get("genai", {})
    if g.get("total_cost_usd", 0) > 0:
        parts.append(f"GENAI SPEND (30d): ${g['total_cost_usd']:,.2f} USD across {g.get('total_requests', 0):,} requests. Top model: {g.get('top_model', 'unknown')}")
        if g.get("top_saving_opportunity"):
            ts = g["top_saving_opportunity"]
            parts.append(f"  Model saving: switch {ts['switch_from']} → {ts['switch_to']} = ${ts['saving_usd']:,.2f} saving ({ts['saving_pct']}%)")

    parts.append("\nGenerate a briefing covering: 1) overnight alerts, 2) top opportunity, 3) budget health, 4) one recommended action for today.")
    return "\n".join(parts)


async def _llm_briefing(signals: dict) -> str:
    settings = get_settings()
    model = settings.agent_model or settings.openai_model
    messages = [
        {"role": "system", "content": _BRIEFING_SYSTEM},
        {"role": "user", "content": _build_briefing_prompt(signals)},
    ]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 400},
            )
            resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("briefing.llm_failed", error=str(exc))
        return _deterministic_narrative(signals)


def _deterministic_narrative(signals: dict) -> str:
    lines = ["**CloudLens Daily Briefing**\n"]
    a = signals.get("anomalies", {})
    if a.get("anomaly_count", 0) > 0:
        top = a["anomalies"][0]
        lines.append(f"🚨 **{a['anomaly_count']} anomaly** detected — {top['date']}: €{top['excess_eur']:,.0f} above expected ({top['severity']} severity).")
    else:
        lines.append("✅ No anomalies in the last 24 hours.")

    w = signals.get("waste", {})
    if w.get("total_waste_eur", 0) > 0:
        lines.append(f"\n💰 **€{w['total_waste_eur']:,.0f}/month** in recoverable waste across {w.get('item_count', 0)} items.")

    b = signals.get("budgets", {})
    if b.get("breached", 0) > 0:
        lines.append(f"\n⚠️ **{b['breached']} budget** breach(es) require immediate attention.")
    elif b.get("warning", 0) > 0:
        lines.append(f"\n⚠️ {b['warning']} budget(s) approaching limit (≥80%).")

    c = signals.get("commitments", {})
    if c.get("commit_now_count", 0) > 0:
        lines.append(f"\n📈 **{c['commit_now_count']} service(s)** ready for commitment purchase — €{c.get('total_potential_saving_eur', 0):,.0f}/month potential saving.")

    m = signals.get("maturity", {})
    if m.get("overall_score"):
        lines.append(f"\n📊 FinOps maturity: **{m['overall_score']}/100** (Grade {m.get('grade', '?')}).")

    return "\n".join(lines)


# ── Card builder ──────────────────────────────────────────────────────────────

def _build_cards(signals: dict) -> list[BriefingCard]:
    cards: list[BriefingCard] = []

    a = signals.get("anomalies", {})
    if a.get("anomaly_count", 0) > 0:
        top = a["anomalies"][0] if a["anomalies"] else {}
        cards.append(BriefingCard(
            category="anomaly",
            title="Cost Anomaly Detected",
            body=f"{a['anomaly_count']} anomaly detected overnight. Most recent: {top.get('date', 'unknown')} — {top.get('severity', '')} severity.",
            metric=f"€{top.get('excess_eur', 0):,.0f} over expected",
            severity="critical" if any(x["severity"] == "high" for x in a.get("anomalies", [])) else "warning",
            action_label="Explain anomaly",
            action_url="#anomaly",
        ))

    b = signals.get("budgets", {})
    breached = [x for x in b.get("budgets", []) if x["status"] == "breach"]
    warned = [x for x in b.get("budgets", []) if x["status"] == "warning"]
    if breached:
        cards.append(BriefingCard(
            category="budget",
            title="Budget Breach",
            body=f"{len(breached)} budget(s) exceeded: {', '.join(x['name'] for x in breached[:3])}.",
            metric=f"{len(breached)} breached",
            severity="critical",
            action_label="View budgets",
            action_url="#budgets",
        ))
    elif warned:
        cards.append(BriefingCard(
            category="budget",
            title="Budget Warning",
            body=f"{len(warned)} budget(s) at ≥80% utilisation.",
            metric=f"{max(x['pct'] for x in warned):.0f}% used",
            severity="warning",
        ))

    w = signals.get("waste", {})
    if w.get("total_waste_eur", 0) > 0:
        cards.append(BriefingCard(
            category="waste",
            title="Recoverable Waste",
            body=f"{w.get('item_count', 0)} waste items identified across idle resources, oversized VMs, and orphaned storage.",
            metric=f"€{w['total_waste_eur']:,.0f}/mo",
            severity="warning" if w["total_waste_eur"] > 1000 else "info",
            action_label="View waste items",
            action_url="#waste",
        ))

    c = signals.get("commitments", {})
    if c.get("commit_now_count", 0) > 0:
        cards.append(BriefingCard(
            category="commitment",
            title="Commitment Opportunity",
            body=f"{c['commit_now_count']} service(s) are stable enough to commit to Reserved Instances or Savings Plans.",
            metric=f"€{c.get('total_potential_saving_eur', 0):,.0f}/mo saving",
            severity="positive",
            action_label="View recommendations",
            action_url="#commitments",
        ))

    m = signals.get("maturity", {})
    if m.get("overall_score"):
        cards.append(BriefingCard(
            category="maturity",
            title="FinOps Maturity",
            body=f"Current score: {m['overall_score']}/100 (Grade {m.get('grade', '?')}). {m.get('top_recommendations', [''])[0] if m.get('top_recommendations') else ''}",
            metric=f"{m['overall_score']}/100",
            severity="info",
        ))

    s = signals.get("spend", {})
    if s.get("total_eur", 0) > 0:
        cards.insert(0, BriefingCard(
            category="spend",
            title="30-Day Total Spend",
            body=f"Top driver: {s['top_items'][0]['name'] if s.get('top_items') else 'N/A'} ({s['top_items'][0]['pct'] if s.get('top_items') else 0}% of total).",
            metric=f"€{s['total_eur']:,.0f}",
            severity="info",
        ))

    g = signals.get("genai", {})
    if g.get("total_cost_usd", 0) > 0:
        saving = g.get("top_saving_opportunity") or {}
        body_txt = f"Top model: {g.get('top_model', 'unknown')} — {g.get('total_requests', 0):,} requests."
        if saving:
            body_txt += f" Switching {saving.get('switch_from','').split('/')[-1]} → {saving.get('switch_to','').split('/')[-1]} saves ${saving.get('saving_usd',0):,.0f} ({saving.get('saving_pct',0)}%)."
        cards.append(BriefingCard(
            category="genai",
            title="GenAI / LLM Spend",
            body=body_txt,
            metric=f"${g['total_cost_usd']:,.2f}",
            severity="warning" if saving else "info",
            action_label="View model breakdown" if saving else "",
            action_url="#genai" if saving else "",
        ))

    return cards


def _top_action(signals: dict) -> str:
    a = signals.get("anomalies", {})
    if a.get("anomaly_count", 0) > 0:
        top = a["anomalies"][0]
        return f"Investigate the cost anomaly on {top['date']}: open the AI analyst for root-cause explanation."
    b = signals.get("budgets", {})
    if b.get("breached", 0) > 0:
        return "Review breached budgets and notify the relevant team leads."
    c = signals.get("commitments", {})
    if c.get("commit_now_count", 0) > 0:
        return f"Review the {c['commit_now_count']} commitment opportunity(ies) — potential saving of €{c.get('total_potential_saving_eur', 0):,.0f}/month."
    w = signals.get("waste", {})
    if w.get("total_waste_eur", 0) > 500:
        return f"Address the top waste item to recover €{w.get('items', [{}])[0].get('monthly_saving_eur', 0):,.0f}/month."
    return "No urgent actions today. Consider reviewing FinOps maturity improvement recommendations."


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_briefing(tenant_id: str) -> BriefingResponse:
    """Generate a full FinOps briefing for the tenant."""
    settings = get_settings()
    signals = await _gather_signals(tenant_id)

    if settings.openai_api_key:
        narrative = await _llm_briefing(signals)
        generated_by = settings.agent_model or settings.openai_model
    else:
        narrative = _deterministic_narrative(signals)
        generated_by = "deterministic"

    cards = _build_cards(signals)
    top_action = _top_action(signals)

    return BriefingResponse(
        tenant_id=tenant_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        narrative=narrative,
        cards=cards,
        top_action=top_action,
        generated_by=generated_by,
    )
