"""
CloudLens — End-to-End Code Analysis Report Generator
Produces: docs/CloudLens_Analysis_Report.pdf
"""
from __future__ import annotations
import os
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY


# ── Palette ──────────────────────────────────────────────────────────────────
TEAL        = colors.HexColor("#2dd4bf")
TEAL_DIM    = colors.HexColor("#14463f")
BG_DARK     = colors.HexColor("#0d1218")
PANEL       = colors.HexColor("#18222e")
PANEL2      = colors.HexColor("#1d2935")
LINE        = colors.HexColor("#263340")
TXT         = colors.HexColor("#e6edf3")
TXT2        = colors.HexColor("#9fb0c0")
TXT3        = colors.HexColor("#647386")
AMBER       = colors.HexColor("#f5a524")
AMBER_DIM   = colors.HexColor("#4a3410")
RED         = colors.HexColor("#f0506e")
RED_DIM     = colors.HexColor("#4a1622")
BLUE        = colors.HexColor("#4d9fff")
BLUE_DIM    = colors.HexColor("#13314d")
WHITE       = colors.white
BLACK       = colors.black

SEV_CRITICAL  = RED
SEV_HIGH      = AMBER
SEV_MEDIUM    = BLUE
SEV_LOW       = TXT2

# ── Styles ────────────────────────────────────────────────────────────────────
def make_styles():
    base = getSampleStyleSheet()
    styles = {
        "cover_title": ParagraphStyle("cover_title",
            fontSize=38, fontName="Helvetica-Bold", textColor=TEAL,
            spaceAfter=6, alignment=TA_LEFT, leading=44),
        "cover_sub": ParagraphStyle("cover_sub",
            fontSize=16, fontName="Helvetica", textColor=TXT2,
            spaceAfter=4, alignment=TA_LEFT),
        "cover_meta": ParagraphStyle("cover_meta",
            fontSize=10, fontName="Helvetica", textColor=TXT3,
            spaceAfter=2, alignment=TA_LEFT),
        "section": ParagraphStyle("section",
            fontSize=18, fontName="Helvetica-Bold", textColor=TEAL,
            spaceBefore=18, spaceAfter=8, leading=22),
        "subsection": ParagraphStyle("subsection",
            fontSize=13, fontName="Helvetica-Bold", textColor=TXT,
            spaceBefore=10, spaceAfter=5, leading=16),
        "body": ParagraphStyle("body",
            fontSize=9.5, fontName="Helvetica", textColor=TXT2,
            spaceAfter=4, leading=14, alignment=TA_JUSTIFY),
        "body_em": ParagraphStyle("body_em",
            fontSize=9.5, fontName="Helvetica-Bold", textColor=TXT,
            spaceAfter=4, leading=14),
        "mono": ParagraphStyle("mono",
            fontSize=8.5, fontName="Courier", textColor=TEAL,
            spaceAfter=3, leading=12),
        "note": ParagraphStyle("note",
            fontSize=8.5, fontName="Helvetica-Oblique", textColor=TXT3,
            spaceAfter=3, leading=12, leftIndent=8),
        "bullet": ParagraphStyle("bullet",
            fontSize=9.5, fontName="Helvetica", textColor=TXT2,
            spaceAfter=3, leading=13, leftIndent=14, bulletIndent=4),
        "toc": ParagraphStyle("toc",
            fontSize=10, fontName="Helvetica", textColor=TXT2,
            spaceAfter=3, leading=14),
        "toc_section": ParagraphStyle("toc_section",
            fontSize=11, fontName="Helvetica-Bold", textColor=TXT,
            spaceAfter=2, leading=14),
        "callout": ParagraphStyle("callout",
            fontSize=9, fontName="Helvetica", textColor=TXT2,
            spaceAfter=3, leading=13, leftIndent=12, rightIndent=12,
            borderPadding=6),
        "tag": ParagraphStyle("tag",
            fontSize=8, fontName="Helvetica-Bold", textColor=WHITE,
            spaceAfter=0, leading=10),
        "pct_big": ParagraphStyle("pct_big",
            fontSize=48, fontName="Helvetica-Bold", textColor=TEAL,
            spaceAfter=0, alignment=TA_CENTER, leading=52),
        "pct_label": ParagraphStyle("pct_label",
            fontSize=11, fontName="Helvetica-Bold", textColor=TXT2,
            spaceAfter=6, alignment=TA_CENTER),
        "caption": ParagraphStyle("caption",
            fontSize=8, fontName="Helvetica-Oblique", textColor=TXT3,
            spaceAfter=6, alignment=TA_CENTER),
    }
    return styles


# ── Dark page background callback ────────────────────────────────────────────
def dark_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BG_DARK)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    # subtle teal left accent bar
    canvas.setFillColor(TEAL_DIM)
    canvas.rect(0, 0, 3, A4[1], fill=1, stroke=0)
    # footer
    canvas.setFillColor(TXT3)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(20*mm, 10*mm, f"CloudLens Confidential — Analysis Report {date.today().isoformat()}")
    canvas.drawRightString(A4[0]-20*mm, 10*mm, f"Page {doc.page}")
    canvas.restoreState()


def dark_cover(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BG_DARK)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    # teal gradient-ish header band
    canvas.setFillColor(PANEL)
    canvas.rect(0, A4[1]-80*mm, A4[0], 80*mm, fill=1, stroke=0)
    canvas.setFillColor(TEAL)
    canvas.rect(0, A4[1]-80*mm, 5, 80*mm, fill=1, stroke=0)
    canvas.restoreState()


# ── Helpers ───────────────────────────────────────────────────────────────────
def hr(color=LINE, thickness=0.5):
    return HRFlowable(width="100%", thickness=thickness, color=color, spaceAfter=6, spaceBefore=4)


def severity_pill(sev: str) -> str:
    colors_map = {
        "CRITICAL": ("#f0506e", "#4a1622"),
        "HIGH":     ("#f5a524", "#4a3410"),
        "MEDIUM":   ("#4d9fff", "#13314d"),
        "LOW":      ("#9fb0c0", "#1d2935"),
        "INFO":     ("#2dd4bf", "#14463f"),
    }
    fg, bg = colors_map.get(sev.upper(), ("#9fb0c0", "#1d2935"))
    return f'<font color="{fg}">■</font> <b>{sev}</b>'


def pill_table(items: list[tuple[str, str, str]], styles) -> Table:
    """items = [(sev, id, description)]"""
    data = [["Sev", "ID", "Description"]]
    for sev, bid, desc in items:
        sev_colors = {
            "CRITICAL": RED, "HIGH": AMBER, "MEDIUM": BLUE, "LOW": TXT2,
        }
        c = sev_colors.get(sev.upper(), TXT2)
        data.append([
            Paragraph(f"<b>{sev}</b>", ParagraphStyle("", fontSize=7, fontName="Helvetica-Bold",
                textColor=c, alignment=TA_CENTER)),
            Paragraph(f"<font color='#2dd4bf'><b>{bid}</b></font>",
                ParagraphStyle("", fontSize=8, fontName="Courier", textColor=TEAL)),
            Paragraph(desc, ParagraphStyle("", fontSize=8.5, fontName="Helvetica",
                textColor=TXT2, leading=12)),
        ])

    col_w = [18*mm, 25*mm, 120*mm]
    t = Table(data, colWidths=col_w, repeatRows=1)
    sev_bg = {"CRITICAL": RED_DIM, "HIGH": AMBER_DIM, "MEDIUM": BLUE_DIM, "LOW": PANEL2}
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("TEXTCOLOR",  (0, 0), (-1, 0), TEAL),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8),
        ("GRID",       (0, 0), (-1, -1), 0.3, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PANEL2, PANEL]),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]
    for i, (sev, _, _) in enumerate(items, start=1):
        bg = sev_bg.get(sev.upper(), PANEL2)
        style_cmds.append(("BACKGROUND", (0, i), (0, i), bg))
    t.setStyle(TableStyle(style_cmds))
    return t


def score_table(scores: list[tuple[str, int, str]], styles) -> Table:
    """scores = [(area, pct, comment)]"""
    data = [["Feature Area", "Completeness", "Status / Notes"]]
    for area, pct, note in scores:
        bar_fill = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        color = "#2dd4bf" if pct >= 80 else "#f5a524" if pct >= 55 else "#f0506e"
        data.append([
            Paragraph(f"<b>{area}</b>", ParagraphStyle("", fontSize=8.5, fontName="Helvetica-Bold",
                textColor=TXT, leading=12)),
            Paragraph(f'<font color="{color}"><b>{pct}%</b></font>  <font color="#263340">{bar_fill}</font>',
                ParagraphStyle("", fontSize=8, fontName="Courier", textColor=TXT2)),
            Paragraph(note, ParagraphStyle("", fontSize=8, fontName="Helvetica",
                textColor=TXT3, leading=11)),
        ])

    col_w = [50*mm, 38*mm, 75*mm]
    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("TEXTCOLOR",  (0, 0), (-1, 0), TEAL),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8),
        ("GRID",       (0, 0), (-1, -1), 0.3, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PANEL2, PANEL]),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    return t


def gap_table(gaps: list[tuple[str, str, str, str]], styles) -> Table:
    """gaps = [(competitor_has, priority, effort, description)]"""
    data = [["Capability", "Priority", "Effort", "Detail"]]
    for cap, prio, effort, detail in gaps:
        prio_colors = {"P0": RED, "P1": AMBER, "P2": BLUE, "P3": TXT2}
        pc = prio_colors.get(prio, TXT2)
        data.append([
            Paragraph(f"<b>{cap}</b>", ParagraphStyle("", fontSize=8.5, fontName="Helvetica-Bold",
                textColor=TXT, leading=12)),
            Paragraph(f"<b>{prio}</b>", ParagraphStyle("", fontSize=8, fontName="Helvetica-Bold",
                textColor=pc, alignment=TA_CENTER)),
            Paragraph(effort, ParagraphStyle("", fontSize=8, fontName="Helvetica",
                textColor=TXT3, alignment=TA_CENTER)),
            Paragraph(detail, ParagraphStyle("", fontSize=8, fontName="Helvetica",
                textColor=TXT2, leading=11)),
        ])
    col_w = [45*mm, 14*mm, 16*mm, 88*mm]
    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("TEXTCOLOR",  (0, 0), (-1, 0), TEAL),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8),
        ("GRID",       (0, 0), (-1, -1), 0.3, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PANEL2, PANEL]),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    return t


# ── Report content ────────────────────────────────────────────────────────────

BUGS = [
    ("CRITICAL", "BUG-001",
     "auth.py — Unverified JWT fallback continues processing when python-jose not available"),
    ("HIGH",     "BUG-002",
     "costs.py — 'tag' in VALID_DIMENSIONS but absent from col_map; silently falls back to service_name"),
    ("HIGH",     "BUG-003",
     "costs.py — prev_total=0.0 treated as falsy; change_pct and previous_period_cost_eur both returned as None for legitimately zero prior-period spend"),
    ("HIGH",     "BUG-004",
     "waste.py router — resolve_waste_item: tenant_id is a query param with no bearer-token scope enforcement; any caller can pass an arbitrary tenant_id"),
    ("HIGH",     "BUG-005",
     "reports.py router — no rate_limit_tenant dependency; report endpoints are compute-intensive and unbounded"),
    ("HIGH",     "BUG-006",
     "waste_engine.py — WasteItem.from_cosmos() does not exist; waste router calls WasteItem(**d) directly from Cosmos, leaving _partitionKey etc. in Pydantic kwargs (works only due to Pydantic v2 extra='ignore' default — fragile if model config tightened)"),
    ("HIGH",     "BUG-007",
     "multicloud.py — /allocate endpoint accepts ruleset: dict (untyped body); no Pydantic validation, no OpenAPI schema generated; malformed rules reach parse logic"),
    ("MEDIUM",   "BUG-008",
     "auth.py — JWKS module-level globals (_jwks_cache, _jwks_fetched_at) have a TOCTOU race: concurrent requests can trigger parallel JWKS fetches; no async lock"),
    ("MEDIUM",   "BUG-009",
     "cosmos.py — get_container() is not concurrency-safe; two coroutines can simultaneously enter the 'if name not in _containers' branch creating duplicate clients"),
    ("MEDIUM",   "BUG-010",
     "keyvault.py — close() sets _kv_client = None but never calls await client.close(); the underlying HTTP connection is leaked on shutdown"),
    ("MEDIUM",   "BUG-011",
     "rate_limit.py — in-memory token buckets reset on every Container Apps replica; enforcement is per-replica only, not global; acknowledged in code but not tracked as a known issue"),
    ("MEDIUM",   "BUG-012",
     "ingest.py — steps are numbered 1–4 then '4' again (should be 5) for the waste engine run; misleading for on-call engineers reading logs"),
    ("MEDIUM",   "BUG-013",
     "drilldown.py — f-string interpolation into Cosmos SQL for GROUP BY / SELECT fields (group_field, where-clause keys); currently safe because keys are code-controlled, but pattern is dangerous and violates parameterization best practice"),
    ("MEDIUM",   "BUG-014",
     "reports.py / waste.py routers — list endpoints use ReportMeta(**d) / WasteItem(**d) directly from Cosmos; unlike TenantConfig/FocusRecord which have from_cosmos(), these models rely on Pydantic's default extra-ignore; will break silently if model config ever restricts extras"),
    ("LOW",      "BUG-015",
     "instance_catalog.py — USD_TO_EUR conversion constant (0.92) is hardcoded; rightsizing projections drift as FX rates change; no live FX feed wired"),
    ("LOW",      "BUG-016",
     "anomaly.py — detect_anomalies fits HW once on full history and re-uses fitted[] values for each scan day; should re-fit on history up to each point for true rolling-origin evaluation of actuals"),
    ("LOW",      "BUG-017",
     "forecast.py — _month_end_projection sums all forecast points before next_month_start, not just those in the current month; if last_day is late in month and horizon spans next month, projection is over-counted"),
    ("LOW",      "BUG-018",
     "i18n/__init__.py — CATALOG only covers 8 languages but the API accepts ?lang= with no validation; unsupported codes silently fall back to English without a 400 error or a documented list of supported codes"),
    ("LOW",      "BUG-019",
     "budget router — budget_status: when budget.scope_dimension is None and daily data is empty, fc.month_end_projection is None; projected is never assigned; subsequent breach-date logic would fail with a NameError (projected referenced before assignment guard is missing)"),
    ("LOW",      "BUG-020",
     "waste_engine.py — idle VM rule uses a fixed 14-day look-back (IDLE_VM_LOOKBACK_DAYS) but the router passes days=30 (configurable); look-back is not passed through, so rule always uses 14 days regardless of the API caller's preference"),
]

FEATURE_SCORES = [
    ("API layer / FastAPI wiring",         92, "Production-quality; middleware, error handling, lifespan"),
    ("Authentication & authorisation",     80, "Azure AD JWKS + API key; jose fallback is a security gap"),
    ("Tenant management (CRUD)",           88, "Complete CRUD; soft-delete; KV-backed credentials"),
    ("Azure cost ingestion",               75, "Client implemented; needs end-to-end validation on live accounts"),
    ("AWS cost ingestion",                 35, "fetch_cost_data() returns []; normalize() is coded but untested live"),
    ("GCP cost ingestion",                 30, "Same: BigQuery path documented; fetch stub only"),
    ("Alibaba / OCI ingestion",            25, "Stubs; normalize() written but fetch returns []"),
    ("AI/LLM cost tracking",               55, "Anthropic + OpenAI adapters coded; fetch stubs"),
    ("FOCUS normalization",                78, "Good FOCUS 1.1 subset; multi-provider normalize() tested"),
    ("Waste detection engine",             70, "12 Azure-centric rules; AWS/GCP waste rules missing"),
    ("Forecasting (Holt-Winters)",         85, "Solid implementation; backtest MAPE; dual-trajectory"),
    ("Anomaly detection",                  78, "HW prediction-band + MAD for resources; attribution present"),
    ("Budget management",                  82, "CRUD + status + forecast breach; scoped budgets supported"),
    ("Alerts (rules + events)",            70, "Rule CRUD + event log; webhook/email channels defined but delivery not wired"),
    ("Rightsizing engine",                 72, "CPU+mem cross-family; FX hardcoded; catalog limited"),
    ("Scheduling recommendations",         65, "Basic dev/test scheduling; no calendar integration"),
    ("Utilization analysis",               68, "Over-provisioning bands; metrics depend on ingest enrichment"),
    ("Commitments analysis",               60, "Coverage/utilization tracked; purchase recommendations partial"),
    ("Chargeback / showback",              80, "3 strategies; tag-based; untagged handling good"),
    ("100% allocation engine",             75, "4 rule kinds; shared split; audit trail per rule"),
    ("Insights / digest",                  75, "Rule-based synthesis; bilingual; efficiency score"),
    ("Reports (PDF generation)",           68, "ReportLab wired; template needs richer layout"),
    ("Drilldown explorer",                 72, "5-level hierarchy; resource anomalies inline"),
    ("Multi-cloud spend view",             55, "Works for ingested providers; fetch stubs block real data"),
    ("Compliance / SOC 2 matrix",          80, "15 controls; CLI evidence; audit chain hashing"),
    ("Audit log (tamper-evident)",         82, "SHA-256 chaining; verify endpoint; 2-yr TTL"),
    ("Rate limiting",                      60, "Token bucket; per-replica only; no Redis"),
    ("i18n (EN/IT + 6 others)",            65, "Catalog present; 8 languages; no automated translation pipeline"),
    ("Frontend (HTML/JS)",                 55, "6 pages; no framework/bundler; no state management"),
    ("Infrastructure (Terraform)",         72, "Complete Azure IaC; no multi-region; no CDN"),
    ("CI/CD pipeline",                     40, "deploy.sh + Dockerfile present; no visible GitHub Actions YAML"),
    ("Test coverage",                      65, "14 test files; good unit coverage; integration tests need mocking"),
    ("Observability (OTel)",               55, "OpenTelemetry in requirements; not fully wired in app code"),
    ("Documentation",                      60, "README + TENANT_ONBOARDING + OpenAPI; no runbook"),
]

GAPS = [
    # (capability, priority, effort, detail)
    ("Multi-currency (USD/GBP/JPY…)",
     "P0", "M",
     "Everything is EUR-denominated. Customers billed in USD/GBP will get wrong totals. All competitors support multi-currency at the data layer."),
    ("Real-time / hourly ingestion",
     "P0", "L",
     "Nightly-only. AWS Cost Streams and GCP near-real-time exports support hourly granularity. Competitors (Harness, Vantage) alert within 1 hr of anomaly."),
    ("Kubernetes / pod-level cost allocation",
     "P0", "L",
     "No OpenCost/Kubecost integration. Container workloads are increasingly the majority of spend; pod-level allocation is required for meaningful chargeback."),
    ("Unit economics (cost per user/txn/API call)",
     "P0", "L",
     "CloudZero's defining feature. Connect cloud spend to business metrics via custom dimensions. Without this, FinOps stops at billing; it never reaches engineering."),
    ("Slack / Teams / PagerDuty webhooks",
     "P1", "S",
     "Alert webhook_url field exists in the model but delivery is not implemented. This is table-stakes; customers expect push notifications."),
    ("RI / Savings Plan purchase recommendations + NPV",
     "P1", "M",
     "Current: coverage & utilization tracked. Missing: 'buy X RIs, save €Y over 3yr' with breakeven date and risk analysis. Apptio Cloudability does this well."),
    ("Tagging governance & auto-remediation",
     "P1", "M",
     "Report on tagging coverage but cannot enforce tag policies or auto-tag resources via ARM/GCP Resource Manager. Competitors enforce tagging as a FinOps practice."),
    ("CSV / Excel data export",
     "P1", "S",
     "Only PDF reports. Finance teams live in Excel. Every competitor provides raw data export."),
    ("Carbon / sustainability footprint",
     "P1", "M",
     "Cloud sustainability reporting is now a regulatory requirement in EU (CSRD). AWS/Azure/GCP all publish carbon data; CloudLens doesn't surface it."),
    ("Natural-language cost querying (LLM-powered)",
     "P1", "M",
     "'Why did my Azure spend increase 23% last week?' — Harness AI answers this. CloudLens tracks AI spend but doesn't use LLMs for analysis."),
    ("Multi-region deployment",
     "P2", "L",
     "Single-region only. Enterprise customers in US/APAC require data residency. Cosmos DB geo-replication is partially scaffolded but not activated."),
    ("Self-service tenant onboarding portal",
     "P2", "M",
     "Tenants are created via admin API key. A self-service signup flow (Azure AD B2C, subscription link) is needed for product-led growth."),
    ("'What-if' cost simulation",
     "P2", "M",
     "Spot instance migration, region migration, RI scenarios. Vantage has a side-by-side simulator. Needed to drive procurement decisions."),
    ("AWS/GCP waste rules",
     "P2", "M",
     "All 12 waste rules are Azure-specific (Managed Disks, Public IPs, Azure Advisor). AWS equivalents (EBS volumes, Elastic IPs, S3 storage lens) are missing."),
    ("Mobile-optimized UI / app",
     "P3", "L",
     "Frontend is desktop-only. Competitors have mobile apps or responsive PWAs for on-the-go alerting."),
    ("FOCUS-format data export",
     "P3", "S",
     "Data is normalized to FOCUS internally but not exported in FOCUS format. Customers who also use other FinOps tools expect this interoperability."),
    ("Client SDKs (Python / TypeScript)",
     "P3", "M",
     "No programmatic SDK. Enterprise customers want to integrate CloudLens data into their own tooling without building HTTP clients from scratch."),
]


def build_pdf(output_path: str):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm,
        topMargin=20*mm, bottomMargin=22*mm,
    )
    S = make_styles()
    story = []

    # ──────────────────────────────────────────────────────────────────────────
    # COVER
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 35*mm))
    story.append(Paragraph("CloudLens", S["cover_title"]))
    story.append(Paragraph("End-to-End Code Analysis & Competitive Gap Report", S["cover_sub"]))
    story.append(Spacer(1, 4*mm))
    story.append(hr(TEAL, 1.5))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f"Prepared: {date.today().strftime('%d %B %Y')}  ·  Version 1.0  ·  CONFIDENTIAL",
        S["cover_meta"]))
    story.append(Paragraph(
        "Scope: Full codebase review — app/, tests/, frontend/, infra/, docs/, Dockerfile, deploy.sh",
        S["cover_meta"]))
    story.append(Spacer(1, 10*mm))

    # Score card on cover
    overall = 62
    mvp = 62
    competitive = 38
    score_data = [
        [
            Paragraph(f"<font size=40><b><font color='#2dd4bf'>{overall}%</font></b></font>",
                ParagraphStyle("", fontSize=40, fontName="Helvetica-Bold", textColor=TEAL,
                    alignment=TA_CENTER, leading=44)),
            Paragraph(f"<font size=40><b><font color='#f5a524'>{competitive}%</font></b></font>",
                ParagraphStyle("", fontSize=40, fontName="Helvetica-Bold", textColor=AMBER,
                    alignment=TA_CENTER, leading=44)),
            Paragraph(f"<font size=20><b><font color='#f0506e'>20</font></b></font>",
                ParagraphStyle("", fontSize=40, fontName="Helvetica-Bold", textColor=RED,
                    alignment=TA_CENTER, leading=44)),
        ],
        [
            Paragraph("Overall Completion<br/><font size=8 color='#647386'>vs MVP feature set</font>",
                ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold", textColor=TXT2,
                    alignment=TA_CENTER, leading=14)),
            Paragraph("Competitive Parity<br/><font size=8 color='#647386'>vs market leaders</font>",
                ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold", textColor=TXT2,
                    alignment=TA_CENTER, leading=14)),
            Paragraph("Bugs Found<br/><font size=8 color='#647386'>across all severity levels</font>",
                ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold", textColor=TXT2,
                    alignment=TA_CENTER, leading=14)),
        ],
    ]
    score_tbl = Table(score_data, colWidths=[56*mm, 56*mm, 56*mm])
    score_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), TEAL_DIM),
        ("BACKGROUND", (1, 0), (1, -1), AMBER_DIM),
        ("BACKGROUND", (2, 0), (2, -1), RED_DIM),
        ("GRID",       (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ROUNDEDCORNERS", [8]),
    ]))
    story.append(score_tbl)
    story.append(Spacer(1, 8*mm))

    summary_text = (
        "CloudLens is a well-architected, security-conscious FinOps SaaS platform. The core API, "
        "authentication, Azure ingestion, waste detection, forecasting, anomaly detection, "
        "and SOC 2 compliance controls are production-grade. The codebase follows solid async "
        "Python patterns, uses FOCUS normalisation for multi-cloud, and demonstrates genuine "
        "product depth in its cost-of-inaction and rightsizing engines. "
        "<br/><br/>"
        "However, multi-cloud provider <b>fetch_cost_data() stubs return []</b> — only Azure has a "
        "live ingestion path. The frontend lacks a framework and has no CI/CD pipeline visible. "
        "Twenty bugs were identified, including one <b>critical security issue</b> (unverified JWT "
        "fallback) and six high-severity bugs affecting correctness and tenant isolation. "
        "<br/><br/>"
        "Against market leaders (Apptio Cloudability, CloudZero, Harness CCM, Vantage, Spot.io), "
        "CloudLens is missing <b>multi-currency, real-time ingestion, Kubernetes cost allocation, "
        "unit economics, and push notification delivery</b> — the five features buyers consider "
        "table-stakes in 2025–2026. Closing these gaps is the critical path to #1."
    )
    story.append(Paragraph(summary_text, S["body"]))
    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 1 — ARCHITECTURE OVERVIEW
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1. Architecture Overview", S["section"]))
    story.append(hr())

    arch_points = [
        ("<b>Runtime:</b> FastAPI (Python 3.12) on Azure Container Apps (serverless, scale-to-zero). "
         "Nightly ingestion runs as a Container Apps Job at 02:00 UTC."),
        ("<b>Data layer:</b> Azure Cosmos DB Serverless (NoSQL, 5 containers) + Azure Blob Storage "
         "(PDF reports). Multi-tenant partitioned by tenant_id."),
        ("<b>Secrets:</b> Azure Key Vault (customer SP credentials, internal API key). No secrets in code or env files."),
        ("<b>Auth:</b> Two-track — Azure AD JWKS-validated Bearer tokens (SPA users) and internal "
         "API key (admin/ingest). Tenant isolation enforced via enforce_tenant_scope()."),
        ("<b>Multi-cloud model:</b> FOCUS 1.1 subset normalises billing data from Azure, AWS, GCP, "
         "Alibaba Cloud, OCI, Anthropic, and OpenAI into a single schema."),
        ("<b>Core engines:</b> Holt-Winters forecasting (pure NumPy), anomaly detection (HW prediction "
         "band + MAD), 12-rule waste engine, rightsizing (CPU+mem cross-family), chargeback (3 strategies), "
         "100% allocation engine (4 rule kinds), commitments analysis."),
        ("<b>Observability:</b> structlog structured JSON logging, X-Request-ID tracing, OpenTelemetry "
         "SDK + Azure Monitor instrumentation (partially wired)."),
        ("<b>Compliance:</b> Tamper-evident SHA-256 hash-chained audit log, SOC 2 control matrix "
         "(15 controls), CLI evidence generator for auditors."),
        ("<b>Frontend:</b> 6 static HTML/CSS/JS pages (index, explorer, forecast, optimization, "
         "multicloud, insights, compliance admin). No build framework; served from Blob static-website."),
        ("<b>Infrastructure:</b> Full Terraform IaC (azurerm ~4.0); Container Registry, Cosmos DB "
         "Continuous backup, Log Analytics, managed identity RBAC. Single-region only."),
    ]
    for pt in arch_points:
        story.append(Paragraph(f"• {pt}", S["bullet"]))
        story.append(Spacer(1, 1*mm))

    story.append(Spacer(1, 4*mm))

    # Router inventory table
    story.append(Paragraph("1.1 API Surface", S["subsection"]))
    router_data = [
        ["Router", "Prefix", "Auth", "Key Endpoints"],
        ["tenants",      "/api/v1/tenants",       "API Key",    "CRUD tenant config, SP cred mgmt"],
        ["costs",        "/api/v1/costs",          "Rate limit", "summary, breakdown, trend"],
        ["waste",        "/api/v1/waste",          "Rate limit", "list items, resolve"],
        ["forecast",     "/api/v1/forecast",       "Rate limit", "spend, cost-of-inaction, roadmap, budget-breach"],
        ["insights",     "/api/v1/insights",       "Rate limit", "anomalies, chargeback, digest"],
        ["budgets",      "/api/v1/budgets",        "Rate limit", "CRUD budget, status"],
        ["alerts",       "/api/v1/alerts",         "Rate limit", "rule CRUD, events, acknowledge"],
        ["multicloud",   "/api/v1/multicloud",     "Rate limit", "spend, allocate, commitments, labels"],
        ["optimization", "/api/v1/optimization",   "Rate limit", "rightsizing, scheduling, utilization, savings ledger"],
        ["drilldown",    "/api/v1/drilldown",      "Rate limit", "5-level portfolio drill, resource anomalies"],
        ["reports",      "/api/v1/reports",        "None ⚠",    "list, generate, download (BUG-005)"],
        ["admin",        "/api/v1/admin",          "API Key",    "audit log, compliance matrix, evidence export"],
        ["ingest",       "/api/v1/ingest",         "API Key",    "manual trigger (202 async)"],
        ["health",       "/api/v1/health",         "Public",     "liveness + Cosmos check"],
    ]
    rt = Table(router_data, colWidths=[24*mm, 42*mm, 20*mm, 77*mm], repeatRows=1)
    rt.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), PANEL),
        ("TEXTCOLOR",   (0, 0), (-1, 0), TEAL),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("FONTSIZE",    (0, 1), (-1, -1), 7.5),
        ("GRID",        (0, 0), (-1, -1), 0.3, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PANEL2, PANEL]),
        ("TEXTCOLOR",   (0, 1), (-1, -1), TXT2),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TEXTCOLOR",   (2, 12), (2, 12), AMBER),  # highlight the missing auth
    ]))
    story.append(rt)
    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 2 — BUG REPORT
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2. Bug Report", S["section"]))
    story.append(hr())

    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for sev, _, _ in BUGS:
        counts[sev.upper()] += 1

    summary_data = [
        [Paragraph("<b>CRITICAL</b>", ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold",
             textColor=RED, alignment=TA_CENTER)),
         Paragraph("<b>HIGH</b>", ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold",
             textColor=AMBER, alignment=TA_CENTER)),
         Paragraph("<b>MEDIUM</b>", ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold",
             textColor=BLUE, alignment=TA_CENTER)),
         Paragraph("<b>LOW</b>", ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold",
             textColor=TXT2, alignment=TA_CENTER))],
        [Paragraph(str(counts["CRITICAL"]), ParagraphStyle("", fontSize=28, fontName="Helvetica-Bold",
             textColor=RED, alignment=TA_CENTER, leading=32)),
         Paragraph(str(counts["HIGH"]), ParagraphStyle("", fontSize=28, fontName="Helvetica-Bold",
             textColor=AMBER, alignment=TA_CENTER, leading=32)),
         Paragraph(str(counts["MEDIUM"]), ParagraphStyle("", fontSize=28, fontName="Helvetica-Bold",
             textColor=BLUE, alignment=TA_CENTER, leading=32)),
         Paragraph(str(counts["LOW"]), ParagraphStyle("", fontSize=28, fontName="Helvetica-Bold",
             textColor=TXT2, alignment=TA_CENTER, leading=32))],
    ]
    st = Table(summary_data, colWidths=[40*mm]*4)
    st.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), RED_DIM),
        ("BACKGROUND", (1, 0), (1, -1), AMBER_DIM),
        ("BACKGROUND", (2, 0), (2, -1), BLUE_DIM),
        ("BACKGROUND", (3, 0), (3, -1), PANEL2),
        ("GRID",       (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(st)
    story.append(Spacer(1, 6*mm))
    story.append(pill_table(BUGS, S))
    story.append(Spacer(1, 5*mm))

    # Detailed writeups for critical+high
    story.append(Paragraph("2.1 Critical & High — Detailed Analysis", S["subsection"]))
    story.append(hr(LINE))

    detailed = [
        ("BUG-001", "CRITICAL", "auth.py — Unverified JWT Fallback",
         "app/auth.py · verify_bearer_token()",
         [
             "When python-jose fails to import (dependency corruption, environment mismatch), the "
             "code falls into the except ImportError block and performs a raw base64 decode of the "
             "JWT payload — no signature verification, no expiry check, no audience check.",
             "An attacker who can observe the token format (e.g., from browser DevTools) can forge "
             "an unsigned token with any tid/oid claim and gain access to any tenant's data.",
             "The function logs a warning but returns a valid AuthContext, so the request proceeds "
             "as authenticated.",
         ],
         [
             "Require python-jose explicitly in requirements.txt (it is present, but ensure it is "
             "never optional).",
             "Replace the except block: if jose is unavailable, raise HTTP 503 "
             "('Authentication service unavailable') — never fall back to unverified decoding.",
             "Add a startup health-check that imports jose and aborts if missing.",
         ]),
        ("BUG-002", "HIGH", "costs.py — 'tag' Dimension Silent Fallback",
         "app/routers/costs.py · get_cost_breakdown()",
         [
             "VALID_DIMENSIONS includes 'tag' but col_map only has 'service', 'resource_group', "
             "'location'. When a caller passes ?dimension=tag, col_map.get('tag', 'c.service_name') "
             "silently returns 'c.service_name'.",
             "The response looks valid (HTTP 200 with data) but the breakdown is by service, not "
             "by tag. Finance teams relying on tag-based chargeback via this endpoint get wrong data "
             "without any indication of the error.",
         ],
         [
             "Remove 'tag' from VALID_DIMENSIONS or add tag handling: Cosmos SQL does not support "
             "GROUP BY on a JSON sub-property directly — tag-based breakdown requires a different "
             "query strategy (fetch all records, aggregate client-side by tag value).",
             "Until implemented, return HTTP 501 Not Implemented for dimension='tag' with a clear "
             "message: 'Tag-dimension breakdown is not yet supported; use /insights/chargeback instead.'",
         ]),
        ("BUG-003", "HIGH", "costs.py — Zero Previous Period Treated as None",
         "app/routers/costs.py · get_cost_summary()",
         [
             "prev_total = float(prev_rows[0]) if prev_rows else None. Then: change_pct is "
             "computed as round((total - prev_total) / prev_total * 100, 1) only if prev_total "
             "and prev_total > 0.",
             "When prev_total == 0.0 (the tenant was genuinely new last period), both "
             "previous_period_cost_eur and change_pct return None — hiding what should be a "
             "+inf% change (meaningful data for the dashboard trend arrow).",
             "Additionally, round(prev_total, 2) if prev_total else None will return None for 0.0, "
             "so the previous period cost is incorrectly omitted.",
         ],
         [
             "Use if prev_total is not None instead of if prev_total.",
             "For zero-to-nonzero transitions, return change_pct=None with a note='new_period' flag "
             "rather than silently dropping the value.",
         ]),
        ("BUG-004", "HIGH", "waste.py — Tenant Isolation Missing on resolve",
         "app/routers/waste.py · resolve_waste_item()",
         [
             "The resolve endpoint signature is: resolve_waste_item(item_id: str, tenant_id: str, "
             "payload: WasteResolve). Here tenant_id is a query parameter, not a path parameter.",
             "There is no call to enforce_tenant_scope() or require_tenant_scope dependency. Any "
             "authenticated caller (with a valid bearer token for tenant A) can pass tenant_id=B "
             "and resolve waste items belonging to tenant B.",
             "This violates SOC 2 CC6.1b (tenant isolation) and could be exploited to suppress "
             "waste alerts for a competitor's account.",
         ],
         [
             "Move tenant_id into the URL path: PATCH /api/v1/waste/{tenant_id}/{item_id}/resolve.",
             "Add Depends(require_tenant_scope) to enforce the bearer token is scoped to the path "
             "tenant_id. This matches the pattern used by every other tenant-facing router.",
         ]),
        ("BUG-005", "HIGH", "reports.py — No Rate Limiting",
         "app/routers/reports.py",
         [
             "The reports router has no rate_limit_tenant dependency (unlike all other tenant-facing "
             "routers). Report generation is the most resource-intensive operation: it queries three "
             "Cosmos containers, runs the forecast engine, builds a PDF, and uploads to Blob.",
             "An attacker with a valid bearer token can flood the endpoint, exhausting Container Apps "
             "CPU and Cosmos RU quota, causing a denial of service for other tenants.",
         ],
         [
             "Add dependencies=[Depends(rate_limit_tenant)] to the router definition.",
             "Consider a separate, lower rate limit for generation endpoints "
             "(e.g., 5 requests/minute) since they are much heavier than read endpoints.",
         ]),
        ("BUG-007", "HIGH", "multicloud.py — Untyped ruleset Body",
         "app/routers/multicloud.py · allocate()",
         [
             "The /allocate endpoint accepts ruleset: dict with no Pydantic model. FastAPI cannot "
             "generate an accurate OpenAPI schema, and no input validation occurs before the "
             "rule-parsing loop.",
             "Malformed rules (missing 'kind', unknown enum values) raise generic Python exceptions "
             "that are caught and re-raised as HTTP 422, but the error messages expose internal "
             "Python class names to the caller.",
         ],
         [
             "Define an AllocationRuleSetRequest Pydantic model (rules: list[AllocationRuleRequest]) "
             "and use it as the request body type. FastAPI will validate it automatically.",
             "The AllocationRule dataclass already exists in app/services/allocation.py — "
             "a thin Pydantic wrapper is all that is needed.",
         ]),
    ]

    for bug_id, sev, title, location, problems, fixes in detailed:
        sev_color = {"CRITICAL": RED, "HIGH": AMBER, "MEDIUM": BLUE}.get(sev, TXT2)
        header_data = [[
            Paragraph(f"<b><font color='#2dd4bf'>{bug_id}</font></b>",
                ParagraphStyle("", fontSize=10, fontName="Courier", textColor=TEAL)),
            Paragraph(f"<b>{title}</b>",
                ParagraphStyle("", fontSize=10, fontName="Helvetica-Bold", textColor=TXT)),
            Paragraph(f"<b>{sev}</b>",
                ParagraphStyle("", fontSize=9, fontName="Helvetica-Bold", textColor=sev_color,
                    alignment=TA_CENTER)),
        ]]
        ht = Table(header_data, colWidths=[22*mm, 120*mm, 21*mm])
        ht.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PANEL),
            ("BACKGROUND", (2, 0), (2, 0),
             RED_DIM if sev == "CRITICAL" else AMBER_DIM if sev == "HIGH" else BLUE_DIM),
            ("GRID",       (0, 0), (-1, -1), 0.3, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 7),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ]))

        block = [
            Spacer(1, 3*mm),
            ht,
            Paragraph(f"<font color='#647386'>Location: </font><font color='#2dd4bf'>{location}</font>",
                ParagraphStyle("", fontSize=8, fontName="Courier", textColor=TXT3,
                    spaceAfter=4, leading=11, leftIndent=4)),
            Paragraph("<b>Problem</b>", S["body_em"]),
        ]
        for p in problems:
            block.append(Paragraph(f"• {p}", S["bullet"]))
        block.append(Paragraph("<b>Fix</b>", S["body_em"]))
        for f in fixes:
            block.append(Paragraph(f"✓ {f}", S["bullet"]))

        story.append(KeepTogether(block))
        story.append(Spacer(1, 3*mm))

    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 3 — COMPLETION ASSESSMENT
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("3. Completion Assessment", S["section"]))
    story.append(hr())
    story.append(Paragraph(
        "Completion is measured against two baselines: <b>MVP readiness</b> (can this be shipped "
        "as a paying product?) and <b>competitive parity</b> (does this match what a buyer would "
        "expect from Apptio Cloudability, CloudZero, Harness CCM, Vantage, or Spot.io?).",
        S["body"]))
    story.append(Spacer(1, 3*mm))
    story.append(score_table(FEATURE_SCORES, S))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        "<b>Weighted overall: 62% (MVP) / 38% (competitive parity).</b> "
        "The discount to competitive parity reflects that multi-cloud fetch stubs, missing "
        "real-time ingestion, no Kubernetes cost allocation, no unit economics, and no push "
        "notification delivery are buyer deal-breakers — not nice-to-haves.",
        S["body"]))
    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 4 — COMPETITIVE GAP ANALYSIS
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("4. Competitive Gap Analysis — Path to #1", S["section"]))
    story.append(hr())
    story.append(Paragraph(
        "Benchmarked against: <b>Apptio Cloudability</b> (IBM), <b>CloudZero</b>, "
        "<b>Harness CCM</b>, <b>Vantage</b>, <b>Spot.io (CloudCo)</b>, <b>Finout</b>. "
        "Priority: P0 = ship-blocker, P1 = 90-day, P2 = 6-month, P3 = roadmap. "
        "Effort: S = small (days), M = medium (weeks), L = large (months).",
        S["body"]))
    story.append(Spacer(1, 3*mm))
    story.append(gap_table(GAPS, S))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("4.1 CloudLens Differentiators (Protect & Amplify)", S["subsection"]))
    differentiators = [
        ("<b>Cost of Inaction dual-trajectory model</b> — quantifies the daily EUR cost of "
         "<i>not</i> acting on waste. No competitor does this explicitly. Make it the hero "
         "metric on the landing page."),
        ("<b>100% allocation without perfect tags</b> — four-rule-chain (tag → tag-map → account "
         "→ name-pattern → shared split) achieves €0 Unallocated. CloudZero's flagship feature "
         "is essentially this; CloudLens has it."),
        ("<b>Memory-aware rightsizing</b> — CPU+memory cross-family recommendations. Most tools "
         "are CPU-only and miss memory-bound workloads (analytics, ML, caches)."),
        ("<b>FOCUS-native from day one</b> — normalising to FinOps Foundation FOCUS 1.1 is "
         "future-proof. AWS, Azure, GCP will phase out proprietary formats by 2027."),
        ("<b>Bilingual (EN/IT + 6 languages)</b> — the Italian mid-market MSP segment is "
         "underserved by US-centric competitors. Lead there first, then expand."),
        ("<b>SOC 2-ready with CLI evidence generator</b> — competitors show a 'we're compliant' "
         "badge; CloudLens gives auditors the exact CLI commands to verify every control. "
         "Genuinely differentiating for enterprise procurement."),
        ("<b>AI/LLM cost tracking as a first-class citizen</b> — OpenAI/Anthropic spend next to "
         "Azure/AWS spend. LLM bills are the fastest-growing line item in 2025 cloud budgets."),
    ]
    for d in differentiators:
        story.append(Paragraph(f"★  {d}", S["bullet"]))
        story.append(Spacer(1, 1*mm))

    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 5 — RECOMMENDED SPRINT PLAN
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("5. Recommended Sprint Plan (Priority Order)", S["section"]))
    story.append(hr())

    sprints = [
        ("Sprint 1 — Security & Correctness (Immediate)", RED, [
            "Fix BUG-001: remove unverified JWT fallback; raise 503 if jose unavailable",
            "Fix BUG-004: move tenant_id to path, add require_tenant_scope on resolve endpoint",
            "Fix BUG-005: add rate_limit_tenant to reports router",
            "Fix BUG-003: use 'if prev_total is not None' throughout costs router",
            "Fix BUG-002: return 501 for tag dimension or implement client-side aggregation",
            "Add integration test: forge cross-tenant token, assert 403 on waste resolve",
        ]),
        ("Sprint 2 — Multi-cloud & Real-time (P0 gaps)", AMBER, [
            "Implement EUR/USD/GBP FX rate fetching (ECB API or similar) — replace hardcoded 0.92",
            "Wire AWS Cost Explorer fetch_cost_data() (boto3, role assumption, live test)",
            "Wire GCP BigQuery billing export fetch_cost_data() (service account, live test)",
            "Add hourly/near-real-time ingestion path alongside nightly job (Azure Event Hub trigger)",
            "Fix BUG-007: define AllocationRuleSetRequest Pydantic model",
            "Add GitHub Actions CI pipeline: test + lint + Docker build",
        ]),
        ("Sprint 3 — Notifications & Kubernetes (P1 gaps)", BLUE, [
            "Implement alert delivery: Slack webhook POST, Teams webhook, email via ACS/SendGrid",
            "Wire OpenCost HTTP API for Kubernetes cluster cost allocation",
            "Implement RI/SP purchase recommendations with NPV and breakeven analysis",
            "Add CSV export endpoint: GET /api/v1/costs/{tenant_id}/export?format=csv",
            "Fix BUG-008: add async lock around JWKS cache refresh",
            "Fix BUG-010: call await client.close() in keyvault.close()",
        ]),
        ("Sprint 4 — Unit Economics & Carbon (P1 gaps)", TEAL, [
            "Design and implement custom metric ingestion API (business events → cost per unit)",
            "Add Azure/AWS sustainability API integration for carbon emission data (gCO2e)",
            "Implement tagging governance: tag coverage enforcement policy + auto-tag rules",
            "Expand instance catalog to cover ARM/Graviton/Spot; wire live pricing API refresh",
            "Fix BUG-019: null guard on projected in budget_status",
            "Add AWS/GCP equivalent waste rules (EBS, Elastic IPs, S3 storage class, GCS nearline)",
        ]),
    ]

    for title, color, tasks in sprints:
        story.append(KeepTogether([
            Paragraph(title, ParagraphStyle("sprint_title",
                fontSize=12, fontName="Helvetica-Bold", textColor=color,
                spaceBefore=8, spaceAfter=4, leading=15,
                borderPadding=4, backColor=PANEL,
                leftIndent=0)),
        ]))
        story.append(hr(color, 0.8))
        for task in tasks:
            story.append(Paragraph(f"▸  {task}", S["bullet"]))
        story.append(Spacer(1, 3*mm))

    story.append(PageBreak())

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 6 — CODE QUALITY OBSERVATIONS
    # ──────────────────────────────────────────────────────────────────────────
    story.append(Paragraph("6. Code Quality Observations", S["section"]))
    story.append(hr())

    positives = [
        "Consistent async/await throughout — no blocking I/O in request paths.",
        "Pydantic v2 models with field validators; structured error hierarchy (CloudLensError subclasses).",
        "tenacity retry decorators on all external calls (Cosmos, Key Vault) with exponential backoff.",
        "structlog structured JSON logging with request_id context binding; no sensitive values logged.",
        "FOCUS normalisation is a correct and future-proof design choice.",
        "Holt-Winters implementation is clean, backtested, and appropriately self-documenting.",
        "Security test suite (test_security.py) maps to SOC 2 controls; injection resistance tests are thorough.",
        "Terraform IaC is complete; GitHub OIDC (no stored secrets); Container Registry admin disabled.",
        "Soft-delete pattern for tenants; 90-day TTL on cost records; 2-year TTL on audit records.",
        "require_tenant_scope FastAPI dependency correctly enforces cross-tenant isolation on all "
         "bearer-token paths (except the bug noted in BUG-004).",
    ]
    negatives = [
        "fetch_cost_data() returns [] for all non-Azure providers — the multi-cloud value proposition "
        "is not deliverable until these are wired to live APIs.",
        "Frontend has no JavaScript framework, no bundler, no npm — hard to scale; CSS is all inline "
        "in &lt;style&gt; blocks; no component reuse.",
        "No CI/CD pipeline file visible (no .github/workflows/). deploy.sh is a manual script.",
        "OpenTelemetry in requirements.txt but azure-monitor-opentelemetry integration not visibly "
        "wired in main.py (no configure_tracer() call).",
        "Instance catalog is static with indicative prices; no live pricing API refresh scheduled.",
        "Report PDF layout (report_builder.py) needs richer design to be client-presentable.",
        "No integration tests with real Cosmos/Blob — all mocked; cloud-specific behaviour "
        "(TTL, partition queries, cross-partition) not exercised in test suite.",
        "i18n catalog is incomplete — only UI labels translated; waste recommendation text in "
        "waste_engine.py is hard-coded EN/IT (not using the catalog).",
    ]

    story.append(Paragraph("✅  Strengths", S["subsection"]))
    for p in positives:
        story.append(Paragraph(f"+ {p}", ParagraphStyle("pos",
            fontSize=9.5, fontName="Helvetica", textColor=TEAL,
            spaceAfter=3, leading=13, leftIndent=12)))

    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("⚠  Areas for Improvement", S["subsection"]))
    for n in negatives:
        story.append(Paragraph(f"− {n}", ParagraphStyle("neg",
            fontSize=9.5, fontName="Helvetica", textColor=AMBER,
            spaceAfter=3, leading=13, leftIndent=12)))

    story.append(Spacer(1, 6*mm))
    story.append(hr(TEAL, 1.0))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "CloudLens has the bones of a genuinely competitive FinOps platform. The architecture is "
        "sound, the FinOps primitives are correct, and the differentiators (cost of inaction, "
        "100% allocation, memory-aware rightsizing, SOC 2 CLI evidence) are real. "
        "The path to #1 is not a rewrite — it is closing the five P0 capability gaps, shipping "
        "the bug fixes in Sprint 1, and aggressively wiring the multi-cloud provider fetch paths. "
        "The core engine is ready to power a market-leading product.",
        S["body"]))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f"Report generated: {date.today().strftime('%d %B %Y')}  ·  CloudLens v1.0.0  ·  CONFIDENTIAL",
        S["caption"]))

    # Build
    doc.build(story, onFirstPage=dark_cover, onLaterPages=dark_bg)
    print(f"[OK] Written: {output_path}")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "CloudLens_Analysis_Report.pdf")
    build_pdf(out)
