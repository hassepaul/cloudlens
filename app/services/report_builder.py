"""PDF report builder using ReportLab — professional CloudLens monthly report."""
from __future__ import annotations
import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable,
)
from reportlab.platypus.flowables import KeepTogether

from app.models.report import ReportMeta

# ── Brand colours ────────────────────────────────────────────────────────────
BLUE = colors.HexColor("#1D4ED8")
BLUE_LIGHT = colors.HexColor("#DBEAFE")
TEAL = colors.HexColor("#059669")
TEAL_LIGHT = colors.HexColor("#D1FAE5")
RED = colors.HexColor("#DC2626")
RED_LIGHT = colors.HexColor("#FEE2E2")
AMBER = colors.HexColor("#D97706")
AMBER_LIGHT = colors.HexColor("#FEF3C7")
GRAY_DARK = colors.HexColor("#1A1A18")
GRAY_MID = colors.HexColor("#4A4A46")
GRAY_LIGHT = colors.HexColor("#F1F0EC")
WHITE = colors.white

W, H = A4
MARGIN = 20 * mm
CONTENT_W = W - 2 * MARGIN

# ── Styles ───────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

TITLE_STYLE = ParagraphStyle("cloudlens_title", parent=styles["Title"],
    fontName="Helvetica-Bold", fontSize=28, textColor=GRAY_DARK, spaceAfter=4)
SUBTITLE_STYLE = ParagraphStyle("cloudlens_subtitle", parent=styles["Normal"],
    fontName="Helvetica", fontSize=14, textColor=GRAY_MID, spaceAfter=6)
H1_STYLE = ParagraphStyle("cl_h1", parent=styles["Heading1"],
    fontName="Helvetica-Bold", fontSize=16, textColor=BLUE, spaceBefore=14, spaceAfter=6)
H2_STYLE = ParagraphStyle("cl_h2", parent=styles["Heading2"],
    fontName="Helvetica-Bold", fontSize=13, textColor=GRAY_DARK, spaceBefore=10, spaceAfter=4)
BODY_STYLE = ParagraphStyle("cl_body", parent=styles["Normal"],
    fontName="Helvetica", fontSize=10, textColor=GRAY_MID, leading=14, spaceAfter=4)
BODY_IT_STYLE = ParagraphStyle("cl_body_it", parent=styles["Normal"],
    fontName="Helvetica-Oblique", fontSize=10, textColor=colors.HexColor("#5A5A8A"), leading=14)
CAPTION_STYLE = ParagraphStyle("cl_caption", parent=styles["Normal"],
    fontName="Helvetica", fontSize=8, textColor=GRAY_MID)
MONO_STYLE = ParagraphStyle("cl_mono", parent=styles["Code"],
    fontName="Courier", fontSize=9, textColor=GRAY_DARK, backColor=GRAY_LIGHT, leading=13)


def _priority_color(priority: str) -> colors.Color:
    return {
        "critical": RED,
        "high": AMBER,
        "medium": TEAL,
        "low": colors.HexColor("#888780"),
    }.get(priority.lower(), GRAY_MID)


def _kpi_table(kpis: list[tuple[str, str, str]]) -> Table:
    """kpis: list of (label, value, subtitle)"""
    cell_data = []
    for label, value, sub in kpis:
        cell_data.append([
            Paragraph(label, CAPTION_STYLE),
            Paragraph(f'<font size="20" color="{BLUE.hexval()}"><b>{value}</b></font>', styles["Normal"]),
            Paragraph(sub, CAPTION_STYLE),
        ])
    col_w = CONTENT_W / len(kpis)
    t = Table([[cd[0] for cd in cell_data],
               [cd[1] for cd in cell_data],
               [cd[2] for cd in cell_data]],
              colWidths=[col_w] * len(kpis))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GRAY_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _section_table(headers: list[str], rows: list[list[str]], col_widths: list[float]) -> Table:
    header_row = [Paragraph(f"<b>{h}</b>", CAPTION_STYLE) for h in headers]
    data = [header_row] + [[Paragraph(str(c), CAPTION_STYLE) for c in row] for row in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE_LIGHT),
        ("TEXTCOLOR", (0, 0), (-1, 0), BLUE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _header_footer(canvas, doc) -> None:
    canvas.saveState()
    # Header line
    canvas.setStrokeColor(BLUE)
    canvas.setLineWidth(1)
    canvas.line(MARGIN, H - 14 * mm, W - MARGIN, H - 14 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GRAY_MID)
    canvas.drawString(MARGIN, H - 12 * mm, "CloudLens — Azure FinOps Report")
    canvas.drawRightString(W - MARGIN, H - 12 * mm, "CONFIDENTIAL")
    # Footer line
    canvas.setStrokeColor(colors.HexColor("#DBEAFE"))
    canvas.line(MARGIN, 12 * mm, W - MARGIN, 12 * mm)
    canvas.drawString(MARGIN, 8 * mm, f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    canvas.drawRightString(W - MARGIN, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


async def build_pdf_report(
    meta: ReportMeta,
    waste_docs: list[dict],
    cost_rows: list[dict],
) -> bytes:
    """Build a PDF report and return the bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
    )

    story = []

    # ── Cover ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 20 * mm))
    story.append(Paragraph("CloudLens", TITLE_STYLE))
    story.append(Paragraph("Azure FinOps Report", SUBTITLE_STYLE))
    story.append(Paragraph("Rapporto di Ottimizzazione Costi Azure", BODY_IT_STYLE))
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=BLUE))
    story.append(Spacer(1, 4 * mm))

    period = f"{meta.period_start} — {meta.period_end}"
    cover_data = [
        ["Report period / Periodo", period],
        ["Tenant ID", meta.tenant_id],
        ["Total spend / Spesa totale", f"EUR {meta.total_spend_eur:,.2f}"],
        ["Identified waste / Sprechi identificati", f"EUR {meta.total_waste_eur:,.2f} ({meta.waste_pct:.1f}%)"],
        ["Waste items / Elementi di spreco", str(meta.waste_items_count)],
        ["Critical", str(meta.critical_count)],
        ["High", str(meta.high_count)],
    ]
    cover_t = Table(cover_data, colWidths=[70 * mm, CONTENT_W - 70 * mm])
    cover_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY_DARK),
        ("TEXTCOLOR", (1, 0), (1, -1), GRAY_MID),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, GRAY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(cover_t)
    story.append(PageBreak())

    # ── 1. KPI Summary ───────────────────────────────────────────────────────
    story.append(Paragraph("1. Executive Summary / Sintesi Esecutiva", H1_STYLE))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT))
    story.append(Spacer(1, 4 * mm))

    kpis = [
        ("Total spend", f"EUR {meta.total_spend_eur:,.0f}", period),
        ("Identified waste", f"EUR {meta.total_waste_eur:,.0f}", f"{meta.waste_pct:.1f}% of spend"),
        ("Waste items", str(meta.waste_items_count), f"Critical: {meta.critical_count} / High: {meta.high_count}"),
        ("Potential saving", f"EUR {meta.total_waste_eur:,.0f}/mo", f"EUR {meta.total_waste_eur * 12:,.0f}/yr"),
    ]
    story.append(_kpi_table(kpis))
    story.append(Spacer(1, 6 * mm))

    # ── 2. Cost by Service ───────────────────────────────────────────────────
    if cost_rows:
        story.append(Paragraph("2. Cost by Service / Costi per Servizio", H1_STYLE))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT))
        story.append(Spacer(1, 3 * mm))
        rows = [
            [r.get("service_name", "Unknown"), f"EUR {r.get('total', 0):,.2f}"]
            for r in sorted(cost_rows, key=lambda x: x.get("total", 0), reverse=True)[:20]
        ]
        t = _section_table(
            ["Service / Servizio", "Monthly cost / Costo mensile"],
            rows,
            [CONTENT_W * 0.65, CONTENT_W * 0.35],
        )
        story.append(t)
        story.append(Spacer(1, 6 * mm))

    # ── 3. Waste Items ───────────────────────────────────────────────────────
    if waste_docs:
        story.append(Paragraph("3. Waste Findings / Sprechi Rilevati", H1_STYLE))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT))
        story.append(Spacer(1, 3 * mm))

        for w in waste_docs[:50]:
            priority = w.get("priority", "low")
            pcolor = _priority_color(priority)
            header = [
                Paragraph(f'<font color="{pcolor.hexval()}"><b>[{priority.upper()}]</b></font> '
                          f'{w.get("waste_type", "").replace("_", " ").title()} — '
                          f'{w.get("resource_name", w.get("resource_id", "")[:60])}', BODY_STYLE),
            ]
            detail_rows = [
                ["Resource group", w.get("resource_group", "—")],
                ["Current cost", f"EUR {w.get('monthly_cost_eur', 0):,.2f}/mo"],
                ["Potential saving", f"EUR {w.get('saving_eur', 0):,.2f}/mo"],
                ["Recommendation (EN)", w.get("recommendation", "—")],
                ["Raccomandazione (IT)", w.get("recommendation_it", "—")],
            ]
            detail_t = Table(detail_rows, colWidths=[45 * mm, CONTENT_W - 48 * mm])
            detail_t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (0, -1), GRAY_DARK),
                ("TEXTCOLOR", (1, 0), (1, -1), GRAY_MID),
                ("BACKGROUND", (0, 0), (-1, -1), GRAY_LIGHT),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 3), (0, 4), 12),
            ]))
            story.append(KeepTogether([*header, detail_t, Spacer(1, 4 * mm)]))

    story.append(PageBreak())

    # ── 4. Methodology ───────────────────────────────────────────────────────
    story.append(Paragraph("4. Methodology / Metodologia", H1_STYLE))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "CloudLens ingests cost data daily from Azure Cost Management API and runs 12 automated waste "
        "detection rules across idle VMs, unattached disks, orphan IPs, oversized resources, "
        "Dev/Test pricing opportunities, Reserved Instance candidates, and more.",
        BODY_STYLE,
    ))
    story.append(Paragraph(
        "CloudLens acquisisce dati di costo giornalmente tramite Azure Cost Management API ed esegue "
        "12 regole automatizzate di rilevamento sprechi su VM inattive, dischi non collegati, IP orfani, "
        "risorse sovradimensionate, opportunità Dev/Test, candidati Reserved Instance e altro.",
        BODY_IT_STYLE,
    ))
    story.append(Spacer(1, 4 * mm))

    rules_data = [
        ["Rule / Regola", "Priority", "Signal / Segnale"],
        ["Idle VM", "Critical/High", "CPU avg < 5% over 14 days"],
        ["Unattached disk", "Critical", "Disk state = Unattached"],
        ["Orphan public IP", "High", "IP not associated"],
        ["Oversized VM", "High", "Azure Advisor recommendation"],
        ["Dev/Test eligible", "High", "Non-prod on PAYG pricing"],
        ["Reserved Instance", "Medium", "> 30 days stable uptime"],
        ["Idle App Service", "Medium", "< 1 req/min over 14 days"],
        ["Unused Load Balancer", "Medium", "Empty backend pool"],
        ["Old snapshots", "Low", "Age > 90 days"],
        ["Cold storage", "Low", "No access in 30 days"],
        ["Duplicate backup", "Low", "Multiple policies on same resource"],
        ["Expired certificate", "Low", "Key Vault cert expiry < 30 days"],
    ]
    rules_t = _section_table(
        rules_data[0],
        rules_data[1:],
        [CONTENT_W * 0.35, CONTENT_W * 0.2, CONTENT_W * 0.45],
    )
    story.append(rules_t)
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "CloudLens — Azure FinOps Managed Service | cloudlens.io | hello@cloudlens.io",
        CAPTION_STYLE,
    ))

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    buf.seek(0)
    return buf.read()
