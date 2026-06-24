"""
CloudLens Business Insights Engine
==================================

The layer that separates CloudLens from a raw dashboard. It fuses the outputs of
the waste engine, anomaly detector, budget tracker, chargeback engine, and
forecast into a small set of ranked, plain-language, decision-ready statements —
the kind a finance lead or engineering director acts on without needing to read
a cost table.

Each insight carries: a business-language headline, a quantified € impact, a
recommended action, a severity, and the bilingual (EN/IT) phrasing CloudLens
targets for the Italian mid-market.

This is deliberately rule-based and transparent (no opaque "AI" black box): every
insight states the figures it is derived from, so a CFO can trust and audit it.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Insight:
    rank: int
    category: str            # "waste" | "anomaly" | "budget" | "allocation" | "forecast" | "efficiency"
    severity: str            # "critical" | "high" | "medium" | "info"
    headline: str            # business-language EN
    headline_it: str         # business-language IT
    impact_eur: float        # € figure that gives the insight its weight (monthly unless noted)
    action: str
    evidence: dict = field(default_factory=dict)


@dataclass
class InsightDigest:
    tenant_id: str
    monthly_spend_eur: float
    monthly_recoverable_eur: float
    efficiency_score: int                 # 0–100, higher = leaner
    headline_summary: str                 # one-paragraph executive summary (EN)
    headline_summary_it: str
    insights: list[Insight] = field(default_factory=list)


def _efficiency_score(monthly_spend: float, monthly_waste: float) -> int:
    """100 = no detectable waste; drops as waste ratio rises (capped, non-linear)."""
    if monthly_spend <= 0:
        return 100
    ratio = monthly_waste / monthly_spend
    # 0% waste → 100, 10% → ~85, 25% → ~60, 40%+ → ~35
    return max(0, min(100, round(100 - (ratio * 165))))


def synthesize(
    tenant_id: str,
    tenant_name: str,
    monthly_spend: float,
    waste_items: list[dict],                  # saving_eur, priority, waste_type, resource_name
    anomalies: Optional[list] = None,         # Anomaly dataclasses
    budget_statuses: Optional[list] = None,   # BudgetStatus models/dicts
    chargeback=None,                          # ChargebackResult
    forecast_month_end: Optional[float] = None,
) -> InsightDigest:
    """Fuse all signals into a ranked, business-language digest."""
    anomalies = anomalies or []
    budget_statuses = budget_statuses or []

    monthly_waste = round(sum(float(w.get("saving_eur", 0.0)) for w in waste_items), 2)
    eff = _efficiency_score(monthly_spend, monthly_waste)
    insights: list[Insight] = []

    # ── 1. Waste / recoverable spend ──────────────────────────────────────
    if monthly_waste > 0:
        crit = [w for w in waste_items if (w.get("priority") or "").lower() == "critical"]
        crit_save = sum(float(w.get("saving_eur", 0.0)) for w in crit)
        top = max(waste_items, key=lambda w: float(w.get("saving_eur", 0.0)))
        sev = "critical" if monthly_waste / max(monthly_spend, 1) > 0.2 else "high"
        insights.append(Insight(
            rank=0, category="waste", severity=sev,
            headline=(f"€{monthly_waste:,.0f}/month is recoverable ("
                      f"€{monthly_waste*12:,.0f}/year) across {len(waste_items)} findings; "
                      f"the single biggest is {top.get('resource_name','a resource')} at "
                      f"€{float(top.get('saving_eur',0)):,.0f}/mo."),
            headline_it=(f"€{monthly_waste:,.0f}/mese recuperabili ("
                         f"€{monthly_waste*12:,.0f}/anno) su {len(waste_items)} criticità; "
                         f"la maggiore è {top.get('resource_name','una risorsa')} a "
                         f"€{float(top.get('saving_eur',0)):,.0f}/mese."),
            impact_eur=monthly_waste,
            action=(f"Action the {len(crit)} critical item(s) first to recover "
                    f"€{crit_save:,.0f}/mo within a week." if crit
                    else "Work the remediation roadmap in ROI order."),
            evidence={"findings": len(waste_items), "critical": len(crit),
                      "annual_eur": round(monthly_waste*12, 2)},
        ))

    # ── 2. Anomalies ──────────────────────────────────────────────────────
    spikes = [a for a in anomalies if getattr(a, "direction", "") == "spike"]
    if spikes:
        worst = max(spikes, key=lambda a: a.excess_eur)
        driver = worst.drivers[0] if getattr(worst, "drivers", None) else None
        drv_txt = (f", driven by {driver.name}" if driver else "")
        drv_txt_it = (f", causato da {driver.name}" if driver else "")
        insights.append(Insight(
            rank=0, category="anomaly",
            severity="high" if worst.severity == "high" else "medium",
            headline=(f"Unexpected spend spike of €{worst.excess_eur:,.0f} on "
                      f"{worst.day}{drv_txt} — {worst.z_score:.1f}σ above the seasonal forecast."),
            headline_it=(f"Picco di spesa imprevisto di €{worst.excess_eur:,.0f} il "
                         f"{worst.day}{drv_txt_it} — {worst.z_score:.1f}σ sopra la previsione."),
            impact_eur=worst.excess_eur,
            action=("Investigate the driver resource group; if it is a new steady-state, "
                    "update the budget — if not, it may be a fresh waste finding."),
            evidence={"day": worst.day, "z_score": worst.z_score,
                      "spikes_in_window": len(spikes)},
        ))

    # ── 3. Budget breaches ────────────────────────────────────────────────
    for bs in budget_statuses:
        status = bs.status if hasattr(bs, "status") else bs.get("status")
        if status in ("breach", "projected_breach", "warning"):
            name = bs.name if hasattr(bs, "name") else bs.get("name")
            consumed = bs.consumed_pct if hasattr(bs, "consumed_pct") else bs.get("consumed_pct")
            proj = (bs.projected_consumed_pct if hasattr(bs, "projected_consumed_pct")
                    else bs.get("projected_consumed_pct"))
            amt = bs.amount_eur if hasattr(bs, "amount_eur") else bs.get("amount_eur")
            sev = "critical" if status == "breach" else "high"
            proj_txt = f" and is projected to hit {proj:.0f}% by month-end" if proj else ""
            insights.append(Insight(
                rank=0, category="budget", severity=sev,
                headline=(f"Budget '{name}' (€{amt:,.0f}/mo) is at {consumed:.0f}% consumed"
                          f"{proj_txt}."),
                headline_it=(f"Il budget '{name}' (€{amt:,.0f}/mese) è al {consumed:.0f}% "
                             f"di consumo{(' e proiettato al %.0f%% a fine mese' % proj) if proj else ''}."),
                impact_eur=float(amt) * (max(0.0, (proj or consumed) - 100) / 100) if (proj or consumed) > 100 else 0.0,
                action="Reallocate budget or accelerate remediation in this scope.",
                evidence={"consumed_pct": consumed, "projected_pct": proj},
            ))

    # ── 4. Cost concentration (allocation) ────────────────────────────────
    if chargeback and getattr(chargeback, "groups", None):
        groups = chargeback.groups
        if groups:
            top = groups[0]
            if top.pct_of_total >= 40:
                insights.append(Insight(
                    rank=0, category="allocation", severity="info",
                    headline=(f"{top.name} accounts for {top.pct_of_total:.0f}% of spend "
                              f"(€{top.total_eur:,.0f}/mo) — the largest cost-center."),
                    headline_it=(f"{top.name} rappresenta il {top.pct_of_total:.0f}% della spesa "
                                 f"(€{top.total_eur:,.0f}/mese) — il maggiore cost-center."),
                    impact_eur=top.total_eur,
                    action="Focus optimization and budget governance on this cost-center first.",
                    evidence={"cost_center": top.name, "pct": top.pct_of_total},
                ))
            if chargeback.tagging_coverage_pct < 70:
                insights.append(Insight(
                    rank=0, category="allocation", severity="medium",
                    headline=(f"Only {chargeback.tagging_coverage_pct:.0f}% of spend carries a "
                              f"'{chargeback.dimension}' tag — €{chargeback.untagged_spend_eur:,.0f}/mo "
                              "cannot be attributed to a team."),
                    headline_it=(f"Solo il {chargeback.tagging_coverage_pct:.0f}% della spesa ha il tag "
                                 f"'{chargeback.dimension}' — €{chargeback.untagged_spend_eur:,.0f}/mese "
                                 "non attribuibili a un team."),
                    impact_eur=chargeback.untagged_spend_eur,
                    action="Enforce tagging policy (Azure Policy) so chargeback is accurate and fair.",
                    evidence={"coverage_pct": chargeback.tagging_coverage_pct},
                ))

    # ── 5. Forecast trajectory ────────────────────────────────────────────
    if forecast_month_end and monthly_spend > 0:
        drift = (forecast_month_end - monthly_spend) / monthly_spend * 100
        if abs(drift) >= 8:
            up = drift > 0
            insights.append(Insight(
                rank=0, category="forecast", severity="medium" if up else "info",
                headline=(f"Spend is trending {'up' if up else 'down'} {abs(drift):.0f}% — "
                          f"month-end projected at €{forecast_month_end:,.0f}."),
                headline_it=(f"La spesa è in {'aumento' if up else 'calo'} del {abs(drift):.0f}% — "
                             f"proiezione fine mese €{forecast_month_end:,.0f}."),
                impact_eur=abs(forecast_month_end - monthly_spend),
                action=("Validate the increase is intended capacity growth, not drift."
                        if up else "Trajectory improving — keep actioning the backlog."),
                evidence={"month_end_projection": forecast_month_end, "drift_pct": round(drift, 1)},
            ))

    # rank by severity then € impact
    sev_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    insights.sort(key=lambda i: (sev_order.get(i.severity, 9), -i.impact_eur))
    for n, ins in enumerate(insights, start=1):
        ins.rank = n

    # executive summary
    summary, summary_it = _exec_summary(
        tenant_name, monthly_spend, monthly_waste, eff, insights)

    return InsightDigest(
        tenant_id=tenant_id, monthly_spend_eur=round(monthly_spend, 2),
        monthly_recoverable_eur=monthly_waste, efficiency_score=eff,
        headline_summary=summary, headline_summary_it=summary_it,
        insights=insights,
    )


def _exec_summary(name, spend, waste, eff, insights) -> tuple[str, str]:
    pct = (waste / spend * 100) if spend > 0 else 0
    top = insights[0].headline if insights else "No material issues detected this period."
    en = (f"{name} spent €{spend:,.0f} this period with an efficiency score of {eff}/100. "
          f"€{waste:,.0f} ({pct:.0f}%) is recoverable. Top priority: {top}")
    it = (f"{name} ha speso €{spend:,.0f} nel periodo con un punteggio di efficienza di {eff}/100. "
          f"€{waste:,.0f} ({pct:.0f}%) sono recuperabili. Priorità principale: "
          f"{insights[0].headline_it if insights else 'Nessuna criticità rilevante.'}")
    return en, it
