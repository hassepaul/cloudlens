#!/usr/bin/env python3
"""CloudLens Architecture Document — comprehensive bilingual PDF."""
import io
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether,
)

W, H = A4
M = 18 * mm
CW = W - 2 * M

# ── Colours ────────────────────────────────────────────────────────────────
C = {
    "blue":        colors.HexColor("#1D4ED8"),
    "blue_light":  colors.HexColor("#DBEAFE"),
    "blue_dark":   colors.HexColor("#1E3A5F"),
    "teal":        colors.HexColor("#059669"),
    "teal_light":  colors.HexColor("#D1FAE5"),
    "amber":       colors.HexColor("#B45309"),
    "amber_light": colors.HexColor("#FEF3C7"),
    "red":         colors.HexColor("#991B1B"),
    "red_light":   colors.HexColor("#FEE2E2"),
    "purple":      colors.HexColor("#5B21B6"),
    "purple_light":colors.HexColor("#EDE9FE"),
    "gray_dark":   colors.HexColor("#1A1A18"),
    "gray_mid":    colors.HexColor("#4A4A46"),
    "gray_light":  colors.HexColor("#F1F0EC"),
    "gray_border": colors.HexColor("#CCCCCC"),
    "white":       colors.white,
    "it_blue":     colors.HexColor("#4A4A9A"),
}

ss = getSampleStyleSheet()

def S(name, **kw):
    return ParagraphStyle(name, parent=ss["Normal"], **kw)

# ── Styles ─────────────────────────────────────────────────────────────────
COVER_T   = S("ct",  fontName="Helvetica-Bold",    fontSize=38, textColor=C["blue"],      spaceAfter=6,  leading=44)
COVER_S   = S("cs",  fontName="Helvetica",          fontSize=18, textColor=C["gray_dark"], spaceAfter=4,  leading=22)
COVER_IT  = S("ci",  fontName="Helvetica-Oblique",  fontSize=14, textColor=C["it_blue"],   spaceAfter=8,  leading=18)
H1        = S("h1",  fontName="Helvetica-Bold",    fontSize=16, textColor=C["blue_dark"],  spaceBefore=18, spaceAfter=5)
H2        = S("h2",  fontName="Helvetica-Bold",    fontSize=13, textColor=C["blue"],       spaceBefore=12, spaceAfter=4)
H3        = S("h3",  fontName="Helvetica-Bold",    fontSize=11, textColor=C["teal"],       spaceBefore=8,  spaceAfter=3)
BODY      = S("b",   fontName="Helvetica",          fontSize=9.5, textColor=C["gray_mid"],  leading=14,    spaceAfter=4)
BODY_IT   = S("bi",  fontName="Helvetica-Oblique",  fontSize=9.5, textColor=C["it_blue"],   leading=14,    spaceAfter=4)
MONO_S    = S("mo",  fontName="Courier",             fontSize=7.5, textColor=C["gray_dark"], leading=11,    spaceAfter=1,
              backColor=C["gray_light"])
CAP       = S("cap", fontName="Helvetica",           fontSize=8,   textColor=C["gray_mid"])
CAP_B     = S("cb",  fontName="Helvetica-Bold",      fontSize=8,   textColor=C["gray_dark"])
SMALL     = S("sm",  fontName="Helvetica",           fontSize=7.5, textColor=C["gray_mid"])


def hr(color=None, thickness=0.4):
    return HRFlowable(width="100%", thickness=thickness, color=color or C["blue_light"], spaceAfter=4)

def sp(n=3):
    return Spacer(1, n * mm)

def mono_table(lines):
    data = [[Paragraph(l.replace(" ", "\u00a0"), MONO_S)] for l in lines]
    t = Table(data, colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C["gray_light"]),
        ("BOX", (0, 0), (-1, -1), 0.4, C["blue_light"]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t

def info_box(en_text, it_text=None):
    rows = [[Paragraph(en_text, BODY)]]
    if it_text:
        rows.append([Paragraph(it_text, BODY_IT)])
    t = Table(rows, colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C["blue_light"]),
        ("BOX", (0, 0), (-1, -1), 0.6, C["blue"]),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t

def warn_box(text):
    t = Table([[Paragraph(text, CAP_B)]], colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C["amber_light"]),
        ("BOX", (0, 0), (-1, -1), 0.8, C["amber"]),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t

def styled_table(headers, rows, col_widths, accent="blue"):
    hbg  = C[f"{accent}_light"]
    hfg  = C[accent]
    header_row = [Paragraph(f"<b>{h}</b>", S("th", fontName="Helvetica-Bold", fontSize=8, textColor=hfg)) for h in headers]
    data = [header_row] + [[Paragraph(str(c), CAP) for c in row] for row in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), hbg),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C["white"], C["gray_light"]]),
        ("GRID", (0, 0), (-1, -1), 0.3, C["gray_border"]),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t

def kv_table(rows, w1=50*mm):
    data = [[Paragraph(f"<b>{k}</b>", CAP_B), Paragraph(str(v), CAP)] for k, v in rows]
    t = Table(data, colWidths=[w1, CW - w1])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [C["white"], C["gray_light"]]),
        ("GRID", (0, 0), (-1, -1), 0.3, C["gray_border"]),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t

def cover_table(rows):
    data = [[Paragraph(f"<b>{k}</b>", CAP_B), Paragraph(str(v), CAP)] for k, v in rows]
    t = Table(data, colWidths=[55*mm, CW - 55*mm])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [C["white"], C["gray_light"]]),
        ("BOX", (0, 0), (-1, -1), 0.5, C["gray_border"]),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, C["gray_border"]),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t

def _hf(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(C["blue"])
    canvas.setLineWidth(0.8)
    canvas.line(M, H - 13*mm, W - M, H - 13*mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(C["gray_mid"])
    canvas.drawString(M, H - 11*mm, "CloudLens — Software Architecture Document  |  CONFIDENTIAL")
    canvas.drawRightString(W - M, H - 11*mm, "v1.0 · June 2026")
    canvas.setStrokeColor(C["blue_light"])
    canvas.setLineWidth(0.5)
    canvas.line(M, 11*mm, W - M, 11*mm)
    canvas.drawString(M, 7.5*mm, "CloudLens · Azure FinOps Managed Service · cloudlens.io")
    canvas.drawRightString(W - M, 7.5*mm, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf(out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4,
        topMargin=18*mm, bottomMargin=18*mm, leftMargin=M, rightMargin=M)
    story = []

    # ══ COVER ═════════════════════════════════════════════════════════════
    story += [sp(20)]
    story.append(Paragraph("CloudLens", COVER_T))
    story.append(Paragraph("Azure FinOps Managed Service", COVER_S))
    story.append(Paragraph("Sistema di Ottimizzazione Costi Azure", COVER_IT))
    story += [sp(4)]
    story.append(hr(C["blue"], 1.2))
    story += [sp(2)]
    story.append(Paragraph("Software Architecture Document", H2))
    story.append(Paragraph("Documento di Architettura Software", BODY_IT))
    story += [sp(6)]
    story.append(cover_table([
        ("Version / Versione", "1.0"),
        ("Date / Data", "June 2026"),
        ("Author / Autore", "CloudLens Engineering"),
        ("Status / Stato", "Draft — Internal Review"),
        ("Classification", "CONFIDENTIAL"),
    ]))
    story.append(PageBreak())

    # ══ 1. EXECUTIVE SUMMARY ══════════════════════════════════════════════
    story.append(Paragraph("1. Executive Summary / Sintesi Esecutiva", H1))
    story.append(hr())
    story.append(Paragraph(
        "CloudLens is a lightweight, cloud-native Azure FinOps managed service. It connects to customer "
        "Azure subscriptions using read-only service principals, ingests cost and usage data via the Azure "
        "Cost Management API, detects waste automatically through 12 configurable rules, and delivers "
        "actionable recommendations through a live dashboard and monthly PDF reports.", BODY))
    story.append(Paragraph(
        "CloudLens è un servizio gestito FinOps cloud-native per Azure, progettato per essere leggero e "
        "facile da configurare. Si connette alle sottoscrizioni Azure dei clienti tramite service principal "
        "in sola lettura, acquisisce dati di costo, rileva sprechi tramite 12 regole e consegna "
        "raccomandazioni tramite dashboard live e report PDF mensili.", BODY_IT))
    story += [sp(3)]
    story.append(styled_table(
        ["Principle", "What it means (EN)", "Cosa significa (IT)"],
        [
            ["Serverless-first",     "Container Apps scale to zero; no idle compute cost",               "Container Apps scalano a zero; nessun costo compute inattivo"],
            ["Read-only by design",  "Zero write permissions on customer tenants; trust by architecture", "Nessun permesso scrittura sui tenant clienti; fiducia per architettura"],
            ["Single config file",   "One config per tenant, versioned in Key Vault",                    "Un config per tenant, versionato in Key Vault"],
            ["Inline & cheap",       "Nightly job processes tenants inline — no queue to pay for",       "Il job notturno elabora i tenant inline — nessuna coda da pagare"],
            ["Observable by default","Structured JSON logs to Log Analytics from day one",               "Log JSON strutturati verso Log Analytics dal primo giorno"],
            ["IaC-only infra",       "All resources defined in Terraform; no manual portal clicks",      "Tutte le risorse in Terraform; nessun click manuale via portale"],
        ],
        [CW*0.20, CW*0.40, CW*0.40],
    ))
    story.append(PageBreak())

    # ══ 2. HLD ════════════════════════════════════════════════════════════
    story.append(Paragraph("2. High-Level Design (HLD) / Progettazione ad Alto Livello", H1))
    story.append(hr())
    story.append(Paragraph(
        "Five logical layers map directly to Azure services: Customer Azure Tenants → Ingestion Layer "
        "(ACA Job, inline) → FastAPI Backend (Container Apps) → Storage Layer (Cosmos DB, Blob, "
        "Key Vault) → Frontend (Static Web App drill-down cost explorer).", BODY))
    story += [sp(2)]
    story.append(mono_table([
        "┌──────────────────────────────────────────────────────────────────────┐",
        "│                     CLOUDLENS — HLD OVERVIEW                         │",
        "│                                                                      │",
        "│  CUSTOMER TENANTS                                                    │",
        "│  ┌──────────────┐  read-only SP  ┌──────────────────────────┐       │",
        "│  │ Azure Sub A  │ ───────────────▶│ Cost Management API      │       │",
        "│  ├──────────────┤                │ Advisor API              │       │",
        "│  │ Azure Sub B  │ ───────────────▶└──────────┬───────────────┘       │",
        "│  └──────────────┘                            │ pull nightly          │",
        "│                               ┌──────────────▼──────────────┐       │",
        "│                               │  INGESTION JOB (ACA Job)    │       │",
        "│                               │  02:00 UTC · inline         │       │",
        "│                               │  fetch → waste → persist    │       │",
        "│                               └──────────────┬──────────────┘       │",
        "│                                              │ write                 │",
        "│          ┌───────────────────────────────────▼──────────────────┐   │",
        "│          │  FASTAPI BACKEND  (Azure Container Apps)             │   │",
        "│          │  /tenants  /costs  /waste  /reports  /ingest  /health│   │",
        "│          └──────────────┬──────────────┬────────────────────────┘   │",
        "│                         │              │                             │",
        "│          ┌──────────────▼───┐  ┌───────▼──────┐  ┌───────────────┐ │",
        "│          │  Cosmos DB       │  │  Blob Storage │  │  Key Vault    │ │",
        "│          │  4 containers    │  │  reports/     │  │  sp-creds-*   │ │",
        "│          └──────────────────┘  └───────────────┘  └───────────────┘ │",
        "│                                                                      │",
        "│          ┌──────────────────────────────────────────────────────┐   │",
        "│          │  FRONTEND: Static Web App — drill-down cost explorer │   │",
        "│          └──────────────────────────────────────────────────────┘   │",
        "└──────────────────────────────────────────────────────────────────────┘",
    ]))
    story += [sp(3)]
    story.append(styled_table(
        ["Step", "Component", "Action (EN)", "Azione (IT)"],
        [
            ["1","Scheduler (ACA Job)",  "Triggers nightly at 02:00 UTC per tenant",                 "Avvia ogni notte alle 02:00 UTC per tenant"],
            ["2","Ingestion Job",         "Calls Cost Mgmt + Advisor APIs with tenant SP",             "Chiama le API con il service principal del tenant"],
            ["3","Resource Graph",        "One bulk KQL query per resource type (state + tags)",        "Una query KQL bulk per tipo di risorsa (stato + tag)"],
            ["4","FastAPI Worker",        "Consumes queue, normalises, runs 12 waste rules",           "Consuma la coda, normalizza, esegue le 12 regole sprechi"],
            ["5","Cosmos DB",             "Persists cost_records + waste_items (TTL 90 days)",         "Persiste cost_records + waste_items (TTL 90 giorni)"],
            ["6","Report Job",            "Generates PDF, uploads to Blob, stores SAS URL in Cosmos", "Genera PDF, carica su Blob, salva URL SAS in Cosmos"],
            ["7","Frontend",              "Reads REST API, renders drill-down cost explorer",          "Legge l'API REST, renderizza il cost explorer drill-down"],
        ],
        [CW*0.06, CW*0.22, CW*0.36, CW*0.36],
    ))
    story.append(PageBreak())

    # ══ 3. AZURE SERVICES ═════════════════════════════════════════════════
    story.append(Paragraph("3. Azure Services / Servizi Azure", H1))
    story.append(hr())
    story.append(Paragraph(
        "All services provisioned in a single Resource Group per environment. All resources in Terraform. "
        "All inter-service communication uses Managed Identity — no connection strings in code.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["Azure Service", "Tier / SKU", "Purpose (EN)", "Scopo (IT)"],
        [
            ["Azure Container Apps",      "Consumption",     "FastAPI backend + async worker",                "Backend FastAPI + worker asincrono"],
            ["Container Apps Jobs",       "Consumption",     "Nightly cost ingestion (02:00 UTC)",            "Job acquisizione notturna (02:00 UTC)"],
            ["Cosmos DB (NoSQL)",         "Serverless",      "Tenants, cost records, waste items, reports",   "Tenant, record costi, sprechi, report"],
            ["Blob Storage",              "LRS Standard",    "PDF report files + raw cost exports",           "File report PDF + export costi grezzi"],
            ["Azure Service Bus",         "Optional",        "Scale-out queue — NOT in default cheap config", "Coda scale-out — NON nella config economica"],
            ["Azure Key Vault",           "Standard",        "Customer SP credentials + API secrets",         "Credenziali SP clienti + segreti API"],
            ["Azure Container Registry",  "Basic",           "Docker images for API + ingest job",            "Immagini Docker per API e job ingest"],
            ["Azure Static Web Apps",     "Free tier",       "React SPA frontend hosting",                    "Hosting frontend React SPA"],
            ["Azure Log Analytics",       "Pay-as-you-go",   "Centralised structured JSON logs",              "Log JSON strutturati centralizzati"],
            ["Azure Monitor + Alerts",    "Pay-as-you-go",   "Error rate, latency, job failure, budget",      "Alert errori, latenza, job, budget"],
            ["Azure AD (Entra ID)",       "Free",            "App registrations + managed identity",          "Registrazioni app + managed identity"],
            ["Cost Management API",       "Free (built-in)", "Source of truth for billing data",              "Fonte dati di fatturazione"],
            ["Azure Advisor API",         "Free (built-in)", "Rightsizing + optimisation recommendations",    "Raccomandazioni rightsizing e ottimizzazione"],
        ],
        [CW*0.27, CW*0.15, CW*0.29, CW*0.29],
        "teal",
    ))
    story += [sp(3)]
    story.append(Paragraph("3.1  Estimated Monthly Infrastructure Cost / Costo Infrastruttura Stimato", H2))
    story.append(Paragraph("At 10 tenants, 30-day cycle, ~2M API calls/month. Under 6% of Starter plan revenue.", BODY))
    story += [sp(1)]
    story.append(styled_table(
        ["Service", "Est. Cost/mo", "Note"],
        [
            ["Container Apps (backend + jobs)", "€8–15",  "Scales to zero between jobs"],
            ["Cosmos DB (serverless)",           "€5–12",  "~500k RU/month at 10 tenants"],
            ["Blob Storage (LRS)",               "€2–4",   "~10 GB reports and exports"],
            ["Service Bus",                      "€0",     "Not deployed — ingest runs inline"],
            ["Key Vault",                        "€2",     "~200 secret operations/day"],
            ["Log Analytics",                    "€5–10",  "~500 MB logs/day ingestion"],
            ["Container Registry (Basic)",       "€5",     "Fixed monthly"],
            ["Static Web Apps",                  "€0",     "Free tier"],
            ["Total / Totale",                   "€27–48/mo", "< 6% of €499/mo Starter plan"],
        ],
        [CW*0.45, CW*0.18, CW*0.37],
    ))
    story.append(PageBreak())

    # ══ 4. LLD ════════════════════════════════════════════════════════════
    story.append(Paragraph("4. Low-Level Design (LLD) / Progettazione a Basso Livello", H1))
    story.append(hr())
    story.append(Paragraph(
        "Single FastAPI application on Azure Container Apps. All Azure SDK calls use "
        "ManagedIdentityCredential. Tenacity retry (3 attempts, exponential backoff) on all external "
        "calls. Structlog JSON with request_id context per HTTP request.", BODY))
    story += [sp(2)]
    story.append(Paragraph("4.1  Module Structure / Struttura Moduli", H2))
    story.append(mono_table([
        "cloudlens/",
        "  app/",
        "    main.py                FastAPI app, lifespan, CORS, middleware, exception handlers",
        "    config.py              Pydantic Settings (lru_cache singleton, all config from env)",
        "    auth.py                API-key + Azure AD bearer (JWKS) auth dependencies",
        "    rate_limit.py          In-process per-tenant token-bucket limiter (no Redis)",
        "    exceptions.py          CloudLensError hierarchy — 12 typed domain exceptions",
        "    logging_config.py      structlog JSON setup, Azure Log Analytics compatible",
        "    models/  tenant.py  cost.py  waste.py  report.py",
        "    routers/ tenants.py  costs.py  waste.py  reports.py  ingest.py",
        "    services/",
        "      azure_cost.py        AzureCostClient: Cost Mgmt + Advisor + VM metrics",
        "      resource_graph.py    Bulk KQL: disk/IP/snapshot/cert state + resource tags",
        "      waste_engine.py      12 async rules, asyncio.gather orchestrator",
        "      cosmos.py            Async Cosmos wrapper (upsert, get, query, bulk_upsert)",
        "      blob.py              Upload + user-delegation SAS URL generation",
        "      keyvault.py          Secret get/set, SP credential store/retrieve",
        "      bus.py               OPTIONAL Service Bus scale-out — not used by default",
        "      report_builder.py    ReportLab PDF generator (bilingual EN/IT, A4)",
        "    jobs/ingest.py         Nightly ingest job — ACA Job entrypoint (inline)",
        "  frontend/index.html      Single-file drill-down console (Static Web App)",
        "  tests/test_cloudlens.py  Model + waste-engine + exception tests",
        "  tests/test_routers.py    Router integration tests (mocked Cosmos)",
        "  tests/test_auth_ratelimit.py  Auth + rate-limit tests (55 total)",
        "  infra/main.tf            All Azure resources (Terraform v1.9+)",
        "  .github/workflows/",
        "    backend.yml            pytest → docker → push ACR → deploy ACA → rollback",
        "    infra.yml              terraform plan → PR comment → approval → apply",
        "  Dockerfile               Multi-stage, non-root UID 1001, HEALTHCHECK",
        "  requirements.txt         Pinned production dependencies",
        "  .env.example             All required environment variables documented",
    ]))
    story.append(PageBreak())

    # ── 4.2 Data Models ────────────────────────────────────────────────────
    story.append(Paragraph("4.2  Data Models / Modelli Dati", H2))
    story.append(Paragraph("Pydantic v2 for all models. Cosmos documents include type discriminator + _partitionKey.", BODY))
    story += [sp(2)]

    story.append(Paragraph("4.2.1  TenantConfig — container: tenants  |  partition key: id", H3))
    story.append(styled_table(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
            ["id",                "str (UUID4)",    "Partition key — auto-generated",                    "Chiave di partizione — auto-generata"],
            ["tenant_name",       "str (2–120)",    "Display name of the customer",                      "Nome visualizzato del cliente"],
            ["subscription_ids",  "list[str]",      "Azure subscription IDs (UUID format validated)",    "ID sottoscrizioni Azure (UUID validato)"],
            ["plan_tier",         "Enum",           "starter | growth | enterprise",                     "Piano di fatturazione"],
            ["sp_secret_ref",     "str",            "Key Vault secret name for SP credentials",          "Nome segreto Key Vault per credenziali SP"],
            ["alert_email",       "str",            "Weekly digest + alert recipient",                   "Destinatario digest settimanale e alert"],
            ["active",            "bool",           "Soft-delete flag (false = deactivated)",            "Flag soft-delete"],
            ["last_ingested_at",  "datetime|None",  "Last successful ingest run",                        "Ultima acquisizione riuscita"],
            ["last_ingest_error", "str|None",       "Last error from ingest job (max 500 chars)",        "Ultimo errore dal job ingest (max 500 char)"],
        ],
        [CW*0.20, CW*0.18, CW*0.31, CW*0.31], "teal",
    ))
    story += [sp(2)]
    story.append(Paragraph("4.2.2  CostRecord — container: cost_records  |  TTL: 90 days", H3))
    story.append(styled_table(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
            ["tenant_id",      "str",          "FK → TenantConfig.id (partition key)",  "FK → TenantConfig.id (chiave di partizione)"],
            ["record_date",    "date",         "Cost date — daily grain",               "Data del costo — granularità giornaliera"],
            ["service_name",   "str",          "Azure service name",                    "Nome servizio Azure"],
            ["resource_id",    "str",          "Full ARM resource ID (lowercase)",       "ARM resource ID completo (minuscolo)"],
            ["cost_eur",       "float (≥0)",   "Normalised cost in EUR",                "Costo normalizzato in EUR"],
            ["tags",           "dict[str,str]","Resource tags at ingest time",          "Tag risorsa al momento acquisizione"],
            ["ttl",            "int",          "7776000 = 90-day Cosmos auto-expiry",   "7776000 = scadenza automatica 90gg in Cosmos"],
        ],
        [CW*0.20, CW*0.18, CW*0.31, CW*0.31], "teal",
    ))
    story += [sp(2)]
    story.append(Paragraph("4.2.3  WasteItem — container: waste_items", H3))
    story.append(styled_table(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
            ["waste_type",       "Enum (12)",    "idle_vm | unattached_disk | orphan_public_ip | ...", "Categoria spreco (12 tipi)"],
            ["monthly_cost_eur", "float",        "Current monthly cost of this resource",             "Costo mensile attuale della risorsa"],
            ["saving_eur",       "float",        "Estimated monthly saving if remediated",            "Risparmio mensile stimato se risolto"],
            ["saving_pct",       "float",        "Saving % — computed by engine, not constructor",   "% risparmio — calcolato dal motore"],
            ["priority",         "Enum",         "critical | high | medium | low",                   "Priorità di triage"],
            ["recommendation",   "str",          "Human-readable action text (EN)",                  "Testo azione leggibile (EN)"],
            ["recommendation_it","str",          "Testo azione in italiano",                         "Testo azione in italiano"],
            ["evidence",         "dict",         "Supporting metrics: cpu_avg_pct, disk_state, ...", "Metriche a supporto"],
            ["resolved_at",      "datetime|None","When marked resolved; null if still open",         "Quando segnato risolto; null se aperto"],
        ],
        [CW*0.20, CW*0.18, CW*0.31, CW*0.31], "teal",
    ))
    story += [sp(2)]
    story.append(Paragraph("4.2.4  ReportMeta — container: reports", H3))
    story.append(styled_table(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
            ["id",               "str (UUID4)",  "Document ID + Blob file name",                  "ID documento e nome file Blob"],
            ["period_start/end", "date",         "Reporting period boundaries",                   "Limiti del periodo di report"],
            ["total_spend_eur",  "float",        "Total spend in reporting period",               "Spesa totale nel periodo"],
            ["total_waste_eur",  "float",        "Total identified waste (sum of saving_eur)",    "Totale sprechi identificati"],
            ["waste_pct",        "float",        "Waste as % of spend",                          "Sprechi come % della spesa"],
            ["blob_url",         "str|None",     "1-hour SAS URL for PDF download",              "URL SAS 1h per download PDF"],
            ["status",           "Enum",         "pending | generating | ready | failed",         "Stato generazione"],
        ],
        [CW*0.22, CW*0.16, CW*0.31, CW*0.31], "teal",
    ))
    story.append(PageBreak())

    # ══ 5. SERVICE COMMUNICATION ══════════════════════════════════════════
    story.append(Paragraph("5. Service Communication / Comunicazione tra Servizi", H1))
    story.append(hr())
    story.append(Paragraph(
        "All inter-service communication uses Azure Managed Identity. No connection strings in code. "
        "All external calls wrapped in Tenacity retry (3 attempts, exponential backoff). "
        "Structured error hierarchy — 12 typed exceptions map directly to HTTP status codes.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["From", "To", "Protocol", "Auth", "Error handling"],
        [
            ["Ingestion Job",     "Cost Mgmt API",   "HTTPS REST",  "Customer SP (OAuth2)",   "3 retries; 429 → sleep(Retry-After)"],
            ["Ingestion Job",     "Advisor API",     "HTTPS REST",  "Customer SP (OAuth2)",   "3 retries; non-200 → AzureAPIError"],
            ["Ingestion Job",     "Resource Graph",  "HTTPS REST",  "Customer SP (OAuth2)",   "3 retries; per-collector failures isolated"],
            ["FastAPI Backend",   "Cosmos DB",       "HTTPS REST",  "Managed Identity",       "3 retries; 404 → NotFoundError"],
            ["FastAPI Backend",   "Blob Storage",    "HTTPS REST",  "Managed Identity",       "3 retries → StorageError"],
            ["FastAPI Backend",   "Key Vault",       "HTTPS REST",  "Managed Identity",       "3 retries → KeyVaultError"],
            ["Frontend SPA",      "FastAPI Backend", "HTTPS REST",  "Azure AD bearer token",  "HTTP 4xx/5xx as structured JSON"],
        ],
        [CW*0.16, CW*0.16, CW*0.12, CW*0.18, CW*0.38],
    ))
    story += [sp(3)]
    story.append(Paragraph("5.1  Exception Hierarchy / Gerarchia Eccezioni", H2))
    story.append(styled_table(
        ["Exception", "HTTP", "Code", "When raised"],
        [
            ["NotFoundError",          "404", "NOT_FOUND",        "Cosmos read_item returns 404"],
            ["ValidationError",        "422", "VALIDATION_ERROR", "Invalid request body or query param"],
            ["ConflictError",          "409", "CONFLICT",         "Duplicate tenant_name on creation"],
            ["UnauthorizedError",      "401", "UNAUTHORIZED",     "Missing or invalid bearer token"],
            ["RateLimitError",         "429", "RATE_LIMITED",     "Per-tenant rate limit exceeded"],
            ["AzureAPIError",          "502", "AZURE_API_ERROR",  "Cost Management / Advisor API error"],
            ["CosmosError",            "503", "COSMOS_ERROR",     "Cosmos DB failure after retries"],
            ["StorageError",           "503", "STORAGE_ERROR",    "Blob Storage failure after retries"],
            ["ServiceBusError",        "503", "SERVICE_BUS_ERROR","Service Bus send/receive failure"],
            ["KeyVaultError",          "503", "KEY_VAULT_ERROR",  "Key Vault secret retrieval failure"],
            ["IngestError",            "500", "INGEST_ERROR",     "Ingest job failure — wraps upstream"],
            ["ReportGenerationError",  "500", "REPORT_ERROR",     "Report generation or upload failure"],
        ],
        [CW*0.28, CW*0.08, CW*0.22, CW*0.42],
    ))
    story.append(PageBreak())

    # ══ 6. API ENDPOINTS ══════════════════════════════════════════════════
    story.append(Paragraph("6. API Endpoints / Endpoint API", H1))
    story.append(hr())
    story.append(Paragraph(
        "All endpoints versioned under /api/v1. Auth: Azure AD bearer token (external) or "
        "managed identity (internal). /docs and /redoc disabled in production.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["Method", "Path", "Response", "Description (EN)"],
        [
            ["GET",    "/api/v1/tenants",                      "200 list",      "List all tenants, ordered by name"],
            ["POST",   "/api/v1/tenants",                      "201 created",   "Create tenant — SP creds stored to Key Vault"],
            ["GET",    "/api/v1/tenants/{id}",                 "200 single",    "Get tenant by ID"],
            ["PATCH",  "/api/v1/tenants/{id}",                 "200 updated",   "Partial update — only provided fields"],
            ["DELETE", "/api/v1/tenants/{id}",                 "204 no content","Soft-delete: active=false, data preserved"],
            ["GET",    "/api/v1/costs/{tenant_id}",            "200 summary",   "Aggregated cost + % change vs previous period"],
            ["GET",    "/api/v1/costs/{tenant_id}/breakdown",  "200 breakdown", "Cost by service|resource_group|location"],
            ["GET",    "/api/v1/costs/{tenant_id}/trend",      "200 trend",     "Daily data points — 7–90 day window"],
            ["GET",    "/api/v1/waste/{tenant_id}",            "200 list",      "Waste items, filterable by priority + resolved"],
            ["PATCH",  "/api/v1/waste/{id}/resolve",           "200 updated",   "Mark waste item resolved"],
            ["POST",   "/api/v1/reports/{tenant_id}/generate", "202 accepted",  "Enqueue PDF generation — returns immediately"],
            ["GET",    "/api/v1/reports/{tenant_id}",          "200 list",      "List reports, newest first"],
            ["GET",    "/api/v1/reports/{id}/download",        "200 url",       "Fresh 1-hour SAS URL for PDF"],
            ["POST",   "/api/v1/ingest/{tenant_id}",           "202 queued",    "Manual ingest trigger (admin)"],
            ["GET",    "/api/v1/health",                       "200 healthy",   "Liveness + Cosmos dependency check"],
        ],
        [CW*0.10, CW*0.32, CW*0.14, CW*0.44],
    ))
    story.append(PageBreak())

    # ══ 7. WASTE ENGINE ═══════════════════════════════════════════════════
    story.append(Paragraph("7. Waste Detection Engine / Motore di Rilevamento Sprechi", H1))
    story.append(hr())
    story.append(Paragraph(
        "Pure async Python module at services/waste_engine.py. Rules run concurrently via "
        "asyncio.gather(). Individual rule failures are isolated — engine logs and continues. "
        "Results sorted by saving_eur descending. Priority: CRITICAL if saving > €100/mo.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["Rule ID", "Priority", "Signal", "Threshold", "Avg saving/mo"],
        [
            ["idle_vm",            "Critical/High", "VM CPU avg %",           "< 5% over 14d",         "€150–2,000"],
            ["unattached_disk",    "Critical",      "Disk state",             "= Unattached",           "€30–200"],
            ["orphan_public_ip",   "High",          "IP association",         "Not associated",         "€5–15"],
            ["oversized_vm",       "High",          "Azure Advisor rec",      "Rightsize present",      "€50–500"],
            ["dev_test_eligible",  "High",          "Sub offer type",         "PAYG non-prod env",      "€100–800"],
            ["reserved_instance",  "Medium",        "VM uptime days",         "> 30d consecutive",      "30–60% via RI"],
            ["idle_app_service",   "Medium",        "App Service req/min",    "< 1 req/min 14d",        "€30–150"],
            ["unused_lb",          "Medium",        "Backend pool count",     "= 0 instances",          "€15–50"],
            ["old_snapshots",      "Low",           "Snapshot age",           "> 90 days",              "€5–50"],
            ["cold_storage",       "Low",           "Blob access tier",       "No access 30d",          "€10–80"],
            ["duplicated_backup",  "Low",           "Backup policies",        "Multiple on same res",   "€20–100"],
            ["expired_cert",       "Low",           "KV cert expiry",         "< 30d to expiry",        "Risk item"],
        ],
        [CW*0.22, CW*0.14, CW*0.20, CW*0.22, CW*0.22],
    ))
    story.append(PageBreak())

    # ══ 8. SECURITY ═══════════════════════════════════════════════════════
    story.append(Paragraph("8. Security Model / Modello di Sicurezza", H1))
    story.append(hr())
    story.append(warn_box(
        "Core security guarantee: A compromise of the CloudLens platform CANNOT lead to modification "
        "of customer Azure resources. Read-only SP constraint is enforced by Azure RBAC, not just code."
    ))
    story += [sp(3)]
    story.append(styled_table(
        ["Control", "Implementation (EN)", "Implementazione (IT)"],
        [
            ["Read-only SP",        "Customer assigns only Reader + Cost Mgmt Reader — no Owner/Contributor", "Solo Reader + Cost Mgmt Reader — nessun Owner/Contributor"],
            ["Secrets in KV",       "SP creds in KV as sp-creds-{tenant_id} JSON. Never in config files", "SP creds in KV come JSON. Mai in file config o env var"],
            ["Managed Identity",    "API + Job use user-assigned MI for all Azure service calls", "API + Job usano MI per tutte le chiamate Azure"],
            ["Network isolation",   "ACA in internal env; only Static Web App has public ingress", "ACA in ambiente interno; solo SWA ha ingress pubblico"],
            ["TLS everywhere",      "All calls over HTTPS/AMQPS — no plaintext endpoints", "Tutte le chiamate su HTTPS/AMQPS — nessun endpoint in chiaro"],
            ["Cosmos RBAC",         "Per-collection read/write; minimal privilege per service", "Read/write per collection; privilegio minimo per servizio"],
            ["Blob SAS tokens",     "1-hour user-delegation SAS for downloads — no permanent access", "SAS user-delegation 1h per download — nessun accesso permanente"],
            ["Audit logging",       "All API calls to Log Analytics with tenant_id + request_id", "Tutte le chiamate a Log Analytics con tenant_id + request_id"],
            ["Non-root container",  "Docker runs as UID 1001 (cloudlens user) — no root", "Docker gira come UID 1001 (cloudlens) — nessun root"],
            ["Input validation",    "Pydantic v2 validates subscription IDs (UUID regex), email, enums", "Pydantic v2 valida subscription ID (UUID regex), email, enum"],
        ],
        [CW*0.23, CW*0.385, CW*0.385],
        "amber",
    ))
    story.append(PageBreak())

    # ══ 9. INFRASTRUCTURE ═════════════════════════════════════════════════
    story.append(Paragraph("9. Infrastructure as Code / Infrastruttura come Codice", H1))
    story.append(hr())
    story.append(Paragraph(
        "Terraform v1.9+, flat config.tfvars pattern (consistent with Mouritech IaC conventions). "
        "One tfvars per environment. internal_api_key via TF_VAR_internal_api_key env var only — "
        "never in tfvars files.", BODY))
    story += [sp(2)]
    story.append(mono_table([
        "infra/",
        "  main.tf              RG, ACA, ACA Job, Cosmos (4 containers), Storage,",
        "                       Key Vault, ACR, Managed Identity, RBAC assignments,",
        "                       Managed Identity, Monitor alerts, Action groups",
        "  variables.tf         9 variables + sensitive internal_api_key",
        "  environments/",
        "    prod.tfvars        italynorth · rg-cloudlens-prod · kv-cloudlens-prod",
        "    staging.tfvars     italynorth · rg-cloudlens-staging",
        "    dev.tfvars         westeurope · rg-cloudlens-dev",
        "    *.backend.tfvars   Terraform state: Azure Storage backend config",
    ]))
    story += [sp(3)]
    story.append(Paragraph("9.1  Azure Resource Naming Conventions", H2))
    story.append(styled_table(
        ["Resource type", "Naming pattern", "Example (prod)"],
        [
            ["Resource Group",            "rg-cloudlens-{env}",       "rg-cloudlens-prod"],
            ["Container Apps Env",        "cae-cloudlens-{env}",      "cae-cloudlens-prod"],
            ["Container App (API)",       "cloudlens-api",             "cloudlens-api"],
            ["Container App Job",         "cloudlens-ingest",          "cloudlens-ingest"],
            ["Cosmos DB Account",         "cosmos-cloudlens-{env}",   "cosmos-cloudlens-prod"],
            ["Storage Account",           "stcloudlens{env}",          "stcloudlensprod"],
            ["Key Vault",                 "kv-cloudlens-{env}",       "kv-cloudlens-prod"],
            ["Container Registry",        "acrcloudlens{env}",         "acrcloudlensprod"],
            ["Managed Identity",          "id-cloudlens-api-{env}",   "id-cloudlens-api-prod"],
            ["Log Analytics Workspace",   "law-cloudlens-{env}",      "law-cloudlens-prod"],
            ["KV Secret (SP creds)",      "sp-creds-{tenant_id}",     "sp-creds-00000000-..."],
        ],
        [CW*0.32, CW*0.32, CW*0.36],
    ))
    story.append(PageBreak())

    # ══ 10. CI/CD ═════════════════════════════════════════════════════════
    story.append(Paragraph("10. CI/CD Pipeline / Pipeline CI/CD", H1))
    story.append(hr())
    story.append(Paragraph(
        "GitHub Actions with OIDC federated credentials — no stored client secrets in GitHub. "
        "Docker images tagged with git SHA. Manual approval gate on prod deploy.", BODY))
    story += [sp(2)]
    story.append(Paragraph("10.1  Backend Workflow — backend.yml", H2))
    story.append(styled_table(
        ["Job", "Step", "Action", "On failure"],
        [
            ["test",   "pytest",             "55 tests + JUnit XML artifact",             "Pipeline stops immediately"],
            ["build",  "docker build + push","Multi-stage, SHA + latest tags to ACR",     "Pipeline stops; no deploy"],
            ["deploy", "az containerapp update","Rolling deploy — existing replicas serve", "Rollback triggered"],
            ["deploy", "Smoke test",         "GET /api/v1/health — assert 'healthy'",     "Traffic reverted to prev revision"],
        ],
        [CW*0.13, CW*0.20, CW*0.37, CW*0.30],
    ))
    story += [sp(3)]
    story.append(Paragraph("10.2  Infrastructure Workflow — infra.yml", H2))
    story.append(styled_table(
        ["Job", "Step", "Action", "Gate"],
        [
            ["plan",  "terraform fmt+validate", "Format + schema check",                     "Fails if fmt diff"],
            ["plan",  "terraform plan",         "Plan output posted as PR comment",          "Plan must succeed"],
            ["apply", "manual approval",        "GitHub Environment approval gate for prod", "Human must approve"],
            ["apply", "terraform apply",        "Apply pre-generated plan artifact",         "main branch only"],
        ],
        [CW*0.13, CW*0.23, CW*0.37, CW*0.27],
    ))
    story.append(PageBreak())

    # ══ 11. OBSERVABILITY ═════════════════════════════════════════════════
    story.append(Paragraph("11. Observability / Osservabilità", H1))
    story.append(hr())
    story.append(Paragraph(
        "Every HTTP request injects a request_id UUID into structlog context — present in all log lines "
        "for that request. JSON logs → stdout → Container Apps stream → Log Analytics. "
        "OpenTelemetry traces → Azure Monitor Application Insights.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["Signal", "Tool", "Configuration (EN)", "Configurazione (IT)"],
        [
            ["Structured logs",   "structlog + Log Analytics", "JSON stdout; request_id in every line",             "JSON stdout; request_id in ogni riga"],
            ["Metrics",           "Container Apps built-in",   "CPU, mem, replica count, req count, error rate",    "CPU, memoria, repliche, richieste, tasso errori"],
            ["Traces",            "OTel → App Insights",       "FastAPI instrumentation; span per endpoint + call", "Instrumentazione FastAPI; span per endpoint"],
            ["Error rate alert",  "Azure Monitor",             "5xx count > 0 in 15-min window; severity 1",       "5xx > 0 in 15 minuti; severità 1"],
            ["Latency alert",     "Azure Monitor",             "p99 latency > 2s in 5-min window",                 "p99 > 2s in finestra 5 minuti"],
            ["Job failure alert", "Azure Monitor",             "ACA Job exit code != 0; emails ops@cloudlens.io",  "Codice uscita ACA Job != 0; email ops"],
            ["Cost budget",       "Azure Cost Management",     "Alert at 80% and 100% of €60/mo budget",           "Alert all'80% e 100% del budget €60/mese"],
            ["Uptime probe",      "Azure Monitor",             "GET /api/v1/health every 5 min; 3 fails = alert",  "GET /health ogni 5 min; 3 fail = alert"],
        ],
        [CW*0.18, CW*0.19, CW*0.315, CW*0.315],
    ))
    story.append(PageBreak())

    # ══ 12. MULTI-TENANCY ═════════════════════════════════════════════════
    story.append(Paragraph("12. Multi-Tenancy Design / Progettazione Multi-Tenant", H1))
    story.append(hr())
    story.append(info_box(
        "Shared infrastructure, data isolated by tenant_id at every layer. "
        "No application-level filter alone — isolation enforced at the storage layer.",
        "Infrastruttura condivisa, dati isolati per tenant_id a ogni livello. "
        "Nessun solo filtro applicativo — isolamento imposto a livello storage."
    ))
    story += [sp(3)]
    story.append(kv_table([
        ("Cosmos partition key", "tenant_id on all 4 containers. Cross-tenant queries structurally impossible without explicit flag."),
        ("Ingest isolation",     "Nightly job processes each tenant in its own try/except; one failure never blocks another."),
        ("Blob Storage",         "Path: reports/{tenant_id}/report-{id}.pdf — tenant ID is in the physical storage path."),
        ("Key Vault",            "Secret naming: sp-creds-{tenant_id}. No cross-tenant access via MI policy."),
        ("REST API",             "JWT claims include tenant_id. Middleware enforces path tenant_id == token claim on every request."),
        ("Rate limiting",        "Starter: 60 req/min · Growth: 200 req/min · Enterprise: 600 req/min — enforced per tenant at router."),
        ("Ingest scheduling",    "ACA Job iterates tenants sequentially. One tenant failure does not block others."),
    ], 50*mm))
    story.append(PageBreak())

    # ══ 13. ONBOARDING ════════════════════════════════════════════════════
    story.append(Paragraph("13. Tenant Onboarding / Onboarding Tenant", H1))
    story.append(hr())
    story.append(Paragraph(
        "Under 20 minutes, zero code changes. Customer creates SP in their Azure AD — "
        "CloudLens never has Owner or Contributor access.", BODY))
    story += [sp(2)]
    story.append(styled_table(
        ["#", "Actor", "Step (EN)", "Passo (IT)", "Time"],
        [
            ["1","Customer",      "Create App Registration in Azure AD",                          "Crea App Registration in Azure AD",             "2 min"],
            ["2","Customer",      "Assign Reader + Cost Mgmt Reader to SP on subscription",       "Assegna Reader + Cost Mgmt Reader al SP",       "3 min"],
            ["3","Customer",      "Share client_id, client_secret, tenant_id via secure channel", "Condivide credenziali via canale sicuro",        "2 min"],
            ["4","CloudLens ops", "Store SP creds to Key Vault as sp-creds-{tenant_id}",          "Salva credenziali SP in Key Vault",              "2 min"],
            ["5","CloudLens ops", "POST /api/v1/tenants with TenantCreate payload",               "POST /api/v1/tenants con payload TenantCreate", "1 min"],
            ["6","System",        "API validates SP, stores config to Cosmos",                    "API valida il SP, salva config in Cosmos", "< 1 min"],
            ["7","System",        "First ingest runs next nightly cycle (or admin trigger)",      "Primo ingest al ciclo notturno (o trigger admin)", "~5 min"],
        ],
        [CW*0.04, CW*0.16, CW*0.34, CW*0.34, CW*0.12],
    ))
    story.append(PageBreak())

    # ══ 14. FRONTEND ══════════════════════════════════════════════════════
    story.append(Paragraph("14. Frontend Console / Console Frontend", H1))
    story.append(hr())
    story.append(Paragraph(
        "A single-file Static Web App (free tier) that reads the REST API and renders a drill-down "
        "cost explorer. The interface is built around one job: show where money is leaking and let an "
        "operator drill from the whole portfolio down to the individual wasteful resource.", BODY))
    story.append(Paragraph(
        "Una Static Web App a file singolo (tier gratuito) che legge l'API REST e renderizza un cost "
        "explorer drill-down. L'interfaccia ha un solo obiettivo: mostrare dove si perde denaro e "
        "permettere di scendere dal portfolio fino alla singola risorsa che spreca.", BODY_IT))
    story += [sp(2)]
    story.append(Paragraph("14.1  Drill-down hierarchy / Gerarchia drill-down", H2))
    story.append(styled_table(
        ["Level", "Shows", "Backing endpoint"],
        [
            ["Portfolio",      "All tenants — spend, recoverable, waste ratio", "GET /api/v1/tenants"],
            ["Tenant",         "Spend by Azure service",                        "GET /costs/{tid}/breakdown?dimension=service"],
            ["Service",        "Spend by resource group",                       "GET /costs/{tid}/breakdown?dimension=resource_group"],
            ["Resource group", "Resources with detected waste",                 "GET /costs/{tid} (filtered)"],
            ["Resource",       "Individual waste findings by priority",         "GET /waste/{tid}"],
            ["Finding detail", "Evidence, EN/IT recommendation, tags, actions", "PATCH /waste/{id}/resolve"],
        ],
        [CW*0.18, CW*0.42, CW*0.40],
    ))
    story += [sp(3)]
    story.append(Paragraph("14.2  Design / Design", H2))
    story.append(kv_table([
        ("Signature element", "A pinned breadcrumb 'spine' shows the full drill path; click any level to jump back."),
        ("Hero metric", "Recoverable spend per month (not total spend) — savings is what the product delivers."),
        ("Colour coding", "Teal <12% optimized · amber 12–25% review · red >25% act now — applied to every waste-ratio bar."),
        ("Detail drawer", "Lowest level opens a side drawer: evidence metrics, bilingual recommendation, resource tags, resolve/snooze."),
        ("Auth", "SPA acquires an Azure AD token (MSAL) and sends Bearer <jwt>; backend enforces tenant scope from the token."),
        ("Deployment", "Static Web Apps free tier. No build step — single self-contained index.html."),
    ], 50*mm))
    story += [sp(8)]
    story.append(hr(C["blue_light"], 0.4))
    story += [sp(2)]
    story.append(Paragraph(
        "— End of Document / Fine del Documento —   CloudLens Engineering · June 2026",
        S("end", fontName="Helvetica-Oblique", fontSize=8, textColor=C["gray_mid"], alignment=1)
    ))

    doc.build(story, onFirstPage=_hf, onLaterPages=_hf)
    print(f"PDF written → {out_path}")


if __name__ == "__main__":
    build_pdf("/mnt/user-data/outputs/CloudLens_Architecture_v1.pdf")
