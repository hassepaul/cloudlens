const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, BorderStyle, WidthType, ShadingType,
  PageBreak, LevelFormat, Header, Footer, TabStopType, TabStopPosition,
  PageNumberElement, TableOfContents, StyleLevel,
} = require('docx');
const fs = require('fs');

// ── Palette ──────────────────────────────────────────────────────────────────
const BLUE        = "1D4ED8";
const BLUE_LIGHT  = "DBEAFE";
const BLUE_DARK   = "1E3A5F";
const TEAL        = "059669";
const TEAL_LIGHT  = "D1FAE5";
const AMBER       = "B45309";
const AMBER_LIGHT = "FEF3C7";
const RED         = "991B1B";
const RED_LIGHT   = "FEE2E2";
const PURPLE      = "5B21B6";
const PURPLE_LIGHT= "EDE9FE";
const GRAY_DARK   = "1A1A18";
const GRAY_MID    = "4A4A46";
const GRAY_LIGHT  = "F1F0EC";
const GRAY_BORDER = "CCCCCC";
const WHITE       = "FFFFFF";
const COVER_BG    = "0F2137";

// ── Page geometry (A4) ───────────────────────────────────────────────────────
const PAGE_W   = 11906;
const PAGE_H   = 16838;
const MARGIN   = 1134;  // ~2cm
const CONTENT_W = PAGE_W - 2 * MARGIN;  // 9638 DXA

// ── Border helpers ────────────────────────────────────────────────────────────
const bdr = (c = GRAY_BORDER, sz = 1) => ({ style: BorderStyle.SINGLE, size: sz, color: c });
const borders = (c = GRAY_BORDER) => ({ top: bdr(c), bottom: bdr(c), left: bdr(c), right: bdr(c) });
const noBorder = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };
const btmBorder = (c, sz = 6) => ({
  top: noBorder, left: noBorder, right: noBorder,
  bottom: { style: BorderStyle.SINGLE, size: sz, color: c, space: 1 },
});

// ── Cell factory ──────────────────────────────────────────────────────────────
const cell = (text, opts = {}) => {
  const children = Array.isArray(text) ? text : [
    new Paragraph({
      alignment: opts.align || AlignmentType.LEFT,
      spacing: { after: 0 },
      children: [new TextRun({
        text: String(text),
        font: "Arial",
        size: opts.size || 18,
        bold: opts.bold || false,
        color: opts.color || GRAY_DARK,
        italics: opts.italic || false,
      })],
    })
  ];
  return new TableCell({
    borders: opts.borders || borders(opts.borderColor || GRAY_BORDER),
    width: { size: opts.width || 2400, type: WidthType.DXA },
    shading: opts.shade ? { fill: opts.shade, type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    verticalAlign: "top",
    children,
  });
};

const hCell = (text, w, color = BLUE, shade = BLUE_LIGHT) =>
  cell(text, { width: w, bold: true, color, shade, size: 18, borderColor: color });

// ── Paragraph helpers ─────────────────────────────────────────────────────────
const h1 = t => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  children: [new TextRun({ text: t, font: "Arial", bold: true, size: 36, color: BLUE_DARK })],
});
const h2 = t => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  children: [new TextRun({ text: t, font: "Arial", bold: true, size: 28, color: BLUE })],
});
const h3 = t => new Paragraph({
  heading: HeadingLevel.HEADING_3,
  children: [new TextRun({ text: t, font: "Arial", bold: true, size: 22, color: TEAL })],
});
const body = (t, opts = {}) => new Paragraph({
  spacing: { after: 120 },
  children: [new TextRun({ text: t, font: "Arial", size: 20, color: opts.color || GRAY_MID, italics: opts.italic || false })],
});
const bodyBold = (t, color = GRAY_DARK) => new Paragraph({
  spacing: { after: 80 },
  children: [new TextRun({ text: t, font: "Arial", size: 20, bold: true, color })],
});
const bodyIT = t => body(t, { color: "4A4A9A", italic: true });
const mono = t => new Paragraph({
  spacing: { after: 0, before: 0, line: 240 },
  children: [new TextRun({ text: t, font: "Courier New", size: 16, color: GRAY_DARK })],
});
const sp = () => new Paragraph({ spacing: { after: 120 }, children: [new TextRun("")]});
const pb = () => new Paragraph({ children: [new PageBreak()] });
const hr = (color = BLUE_LIGHT) => new Paragraph({
  border: btmBorder(color, 4),
  spacing: { before: 120, after: 200 },
  children: [new TextRun("")],
});
const bullet = (t, lvl = 0) => new Paragraph({
  numbering: { reference: "main-bullets", level: lvl },
  spacing: { after: 80 },
  children: [new TextRun({ text: t, font: "Arial", size: 20, color: GRAY_MID })],
});

// ── Multi-line mono block ─────────────────────────────────────────────────────
const monoBlock = lines => new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: [CONTENT_W],
  rows: [new TableRow({
    children: [new TableCell({
      borders: borders(BLUE_LIGHT),
      width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { fill: GRAY_LIGHT, type: ShadingType.CLEAR },
      margins: { top: 140, bottom: 140, left: 180, right: 180 },
      children: lines.map(l => new Paragraph({
        spacing: { after: 0, before: 0, line: 240 },
        children: [new TextRun({ text: l, font: "Courier New", size: 16, color: GRAY_DARK })],
      })),
    })],
  })],
});

// ── Section header box ────────────────────────────────────────────────────────
const sectionBox = (en, it) => new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: [CONTENT_W],
  rows: [new TableRow({
    children: [new TableCell({
      borders: borders(BLUE),
      width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { fill: BLUE_LIGHT, type: ShadingType.CLEAR },
      margins: { top: 120, bottom: 120, left: 200, right: 200 },
      children: [
        new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: en, font: "Arial", bold: true, size: 22, color: BLUE })] }),
        new Paragraph({ spacing: { after: 0 }, children: [new TextRun({ text: it, font: "Arial", size: 18, color: "4A4A9A", italics: true })] }),
      ],
    })],
  })],
});

// ── Generic data table ────────────────────────────────────────────────────────
const dataTable = (headers, rows, widths, accentColor = BLUE) => {
  const shade = accentColor === TEAL ? TEAL_LIGHT : accentColor === AMBER ? AMBER_LIGHT : BLUE_LIGHT;
  const headerRow = new TableRow({
    children: headers.map((h, i) => hCell(h, widths[i], accentColor, shade)),
  });
  const dataRows = rows.map((row, ri) => new TableRow({
    children: row.map((c, ci) => cell(c, {
      width: widths[ci],
      shade: ri % 2 === 1 ? GRAY_LIGHT : WHITE,
    })),
  }));
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: widths,
    rows: [headerRow, ...dataRows],
  });
};

// ── Two-column KV table ───────────────────────────────────────────────────────
const kvTable = (rows, w1 = 3000) => {
  const w2 = CONTENT_W - w1;
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [w1, w2],
    rows: rows.map(([k, v], i) => new TableRow({
      children: [
        cell(k, { width: w1, bold: true, color: GRAY_DARK, shade: i % 2 === 1 ? GRAY_LIGHT : WHITE }),
        cell(v, { width: w2, shade: i % 2 === 1 ? GRAY_LIGHT : WHITE }),
      ],
    })),
  });
};

// ── DOCUMENT ──────────────────────────────────────────────────────────────────
const doc = new Document({
  numbering: {
    config: [{
      reference: "main-bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 1080, hanging: 360 } } } },
      ],
    }],
  },
  styles: {
    default: { document: { run: { font: "Arial", size: 20 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: BLUE_DARK },
        paragraph: { spacing: { before: 480, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: BLUE },
        paragraph: { spacing: { before: 320, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: "Arial", color: TEAL },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 2 } },
    ],
  },

  sections: [{
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          border: btmBorder(BLUE, 4),
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          spacing: { after: 0 },
          children: [
            new TextRun({ text: "CloudLens — Software Architecture Document", font: "Arial", size: 16, color: GRAY_MID }),
            new TextRun({ text: "\t", font: "Arial", size: 16 }),
            new TextRun({ text: "CONFIDENTIAL · v1.0 · June 2026", font: "Arial", size: 16, color: GRAY_MID }),
          ],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          border: { top: bdr(BLUE_LIGHT, 4), bottom: noBorder, left: noBorder, right: noBorder },
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          spacing: { before: 80, after: 0 },
          children: [
            new TextRun({ text: "CloudLens · Azure FinOps Managed Service", font: "Arial", size: 16, color: GRAY_MID }),
            new TextRun({ text: "\tPage ", font: "Arial", size: 16, color: GRAY_MID }),
            new PageNumberElement(),
          ],
        })],
      }),
    },

    children: [

      // ══════════════════════════════════════════════════════════════════════
      // COVER
      // ══════════════════════════════════════════════════════════════════════
      ...[sp(), sp(), sp()],
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 60 },
        children: [new TextRun({ text: "CloudLens", font: "Arial", bold: true, size: 80, color: BLUE })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 60 },
        children: [new TextRun({ text: "Azure FinOps Managed Service", font: "Arial", size: 36, color: GRAY_DARK })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 80 },
        children: [new TextRun({ text: "Sistema di Ottimizzazione Costi Azure", font: "Arial", italics: true, size: 28, color: "4A4A9A" })],
      }),
      hr(BLUE),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 40 },
        children: [new TextRun({ text: "Software Architecture Document", font: "Arial", bold: true, size: 32, color: GRAY_DARK })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({ text: "Documento di Architettura Software", font: "Arial", italics: true, size: 26, color: "4A4A9A" })],
      }),
      new Table({
        width: { size: 5400, type: WidthType.DXA },
        columnWidths: [2400, 3000],
        rows: [
          ["Version / Versione", "1.0"],
          ["Date / Data", "June 2026"],
          ["Author / Autore", "CloudLens Engineering"],
          ["Status / Stato", "Draft — Internal Review"],
          ["Classification", "CONFIDENTIAL"],
        ].map(([k,v], i) => new TableRow({
          children: [
            cell(k, { width: 2400, bold: true, color: GRAY_DARK, shade: i % 2 ? GRAY_LIGHT : WHITE }),
            cell(v, { width: 3000, shade: i % 2 ? GRAY_LIGHT : WHITE }),
          ],
        })),
      }),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // TABLE OF CONTENTS
      // ══════════════════════════════════════════════════════════════════════
      h1("Table of Contents / Indice"),
      hr(),
      new TableOfContents("Table of Contents", {
        hyperlink: true,
        headingStyleRange: "1-3",
        stylesWithLevels: [
          new StyleLevel("Heading1", 1),
          new StyleLevel("Heading2", 2),
          new StyleLevel("Heading3", 3),
        ],
      }),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 1. EXECUTIVE SUMMARY
      // ══════════════════════════════════════════════════════════════════════
      h1("1. Executive Summary / Sintesi Esecutiva"),
      hr(),
      body("CloudLens is a lightweight, cloud-native Azure FinOps managed service. It connects to customer Azure subscriptions using read-only service principals, ingests cost and usage data via the Azure Cost Management API, detects waste automatically through 12 configurable rules, and delivers actionable recommendations through a live dashboard and monthly PDF reports."),
      sp(),
      bodyIT("CloudLens è un servizio gestito FinOps cloud-native per Azure, progettato per essere leggero e facile da configurare. Si connette alle sottoscrizioni Azure dei clienti tramite service principal in sola lettura, acquisisce dati di costo tramite le API Azure Cost Management, rileva automaticamente gli sprechi tramite 12 regole configurabili e consegna raccomandazioni concrete tramite dashboard live e report PDF mensili."),
      sp(),
      h2("1.1 Design Principles / Principi di Design"),
      dataTable(
        ["Principle", "What it means (EN)", "Cosa significa (IT)"],
        [
          ["Serverless-first", "Container Apps scale to zero; no idle compute cost", "Container Apps scalano a zero; nessun costo di compute inattivo"],
          ["Read-only by design", "Zero write permissions on customer tenants; trust by architecture", "Nessun permesso di scrittura sui tenant clienti; fiducia per architettura"],
          ["Single config file", "One config per tenant, version-controlled in Key Vault", "Un file config per tenant, versionato in Key Vault"],
          ["Inline & cheap", "Nightly job processes tenants inline — no queue infra to pay for", "Il job notturno elabora i tenant inline — nessuna coda da pagare"],
          ["Observable by default", "Structured JSON logs to Log Analytics from day one", "Log JSON strutturati verso Log Analytics dal primo giorno"],
          ["IaC-only infra", "All Azure resources defined in Terraform; no manual portal clicks", "Tutte le risorse Azure definite in Terraform; nessun click manuale"],
        ],
        [2800, 3419, 3419],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 2. HLD
      // ══════════════════════════════════════════════════════════════════════
      h1("2. High-Level Design (HLD) / Progettazione ad Alto Livello"),
      hr(),
      body("The system has five logical layers that map directly to Azure services. Customer Azure Tenants provide read-only access via service principals. The Ingestion Layer pulls from Azure Cost Management, Advisor, and Resource Graph nightly via an ACA Job that processes each tenant inline. The FastAPI Backend processes data and exposes the REST API. The Storage Layer persists all data in Cosmos DB, Blob Storage, and Key Vault. The Frontend is a single-file Static Web App console that reads the API and renders a drill-down cost explorer (portfolio to individual resource)."),
      sp(),
      bodyIT("Il sistema ha cinque livelli logici che si mappano direttamente sui servizi Azure. I Tenant Azure dei Clienti forniscono accesso in sola lettura tramite service principal. Il Livello di Acquisizione interroga Azure Cost Management, Advisor e Resource Graph ogni notte tramite un ACA Job che elabora ogni tenant inline. Il Backend FastAPI elabora i dati ed espone l'API REST. Il Livello di Storage persiste tutti i dati in Cosmos DB, Blob Storage e Key Vault. Il Frontend è una console Static Web App a file singolo che legge l'API e renderizza un cost explorer drill-down (dal portfolio alla singola risorsa)."),
      sp(),
      monoBlock([
        "┌─────────────────────────────────────────────────────────────────────┐",
        "│                     CLOUDLENS — HLD OVERVIEW                        │",
        "│                                                                     │",
        "│  CUSTOMER TENANTS                                                   │",
        "│  ┌──────────────┐   read-only SP   ┌──────────────────────────┐    │",
        "│  │ Customer     │ ─────────────────▶│ Cost Mgmt + Advisor API  │    │",
        "│  │ Azure Sub A  │                  │ Azure Resource Graph      │    │",
        "│  └──────────────┘                  └─────────────┬────────────┘    │",
        "│  ┌──────────────┐                                │ pull (nightly)   │",
        "│  │ Customer     │ ─────────────────▶             │                 │",
        "│  │ Azure Sub B  │                  ┌─────────────▼────────────┐    │",
        "│  └──────────────┘                  │  INGESTION JOB (ACA Job) │    │",
        "│                                    │  02:00 UTC · inline       │    │",
        "│                                    │  fetch → waste engine →   │    │",
        "│                                    │  persist (no queue)       │    │",
        "│                                    └─────────────┬────────────┘    │",
        "│                                                  │ write            │",
        "│         ┌────────────────────────────────────────▼──────────────┐  │",
        "│         │          FASTAPI BACKEND  (Azure Container Apps)      │  │",
        "│         │  /tenants  /costs  /waste  /reports  /ingest  /health  │  │",
        "│         └──────────────┬─────────────┬──────────────────────────┘  │",
        "│                        │             │                              │",
        "│         ┌──────────────▼──┐   ┌──────▼──────┐  ┌───────────────┐  │",
        "│         │  Cosmos DB      │   │  Blob       │  │  Key Vault    │  │",
        "│         │  (4 containers) │   │  Storage    │  │  (SP creds)   │  │",
        "│         └─────────────────┘   └─────────────┘  └───────────────┘  │",
        "│                                                                     │",
        "│         ┌────────────────────────────────────────────────────────┐ │",
        "│         │   FRONTEND: Static Web App — drill-down cost explorer  │ │",
        "│         │   portfolio → tenant → service → RG → resource → waste │ │",
        "│         └────────────────────────────────────────────────────────┘ │",
        "└─────────────────────────────────────────────────────────────────────┘",
      ]),
      sp(),
      h2("2.1 Data Flow — Step by Step"),
      dataTable(
        ["Step", "Component", "Action (EN)", "Azione (IT)"],
        [
          ["1", "Scheduler (ACA Job)", "Triggers nightly at 02:00 UTC", "Avvia ogni notte alle 02:00 UTC"],
          ["2", "Ingestion Job", "For each active tenant: fetch costs with tenant SP", "Per ogni tenant attivo: recupera i costi con il SP del tenant"],
          ["3", "Resource Graph", "One bulk KQL query per resource type (state + tags)", "Una query KQL bulk per tipo di risorsa (stato + tag)"],
          ["4", "Waste engine", "Runs 12 rules inline, normalises, scores savings", "Esegue 12 regole inline, normalizza, calcola i risparmi"],
          ["5", "Cosmos DB", "Persists cost_records + waste_items (TTL 90d)", "Persiste cost_records + waste_items (TTL 90 giorni)"],
          ["6", "Report Job", "Generates PDF, uploads to Blob, stores SAS URL", "Genera PDF, carica su Blob, salva URL SAS"],
          ["7", "Frontend", "Reads REST API, renders drill-down cost explorer", "Legge l'API REST, renderizza il cost explorer drill-down"],
        ],
        [700, 2400, 3269, 3269],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 3. AZURE SERVICES
      // ══════════════════════════════════════════════════════════════════════
      h1("3. Azure Services / Servizi Azure"),
      hr(),
      body("All services are provisioned in a single Resource Group per environment (dev / staging / prod). Every resource is defined in Terraform — no manual portal configuration. All inter-service communication uses Managed Identity — no connection strings stored in application code."),
      bodyIT("Tutti i servizi sono in un unico Resource Group per ambiente. Ogni risorsa è definita in Terraform. Tutta la comunicazione inter-servizio usa Managed Identity — nessuna stringa di connessione nel codice."),
      sp(),
      dataTable(
        ["Azure Service", "Tier / SKU", "Purpose (EN)", "Scopo (IT)"],
        [
          ["Azure Container Apps", "Consumption plan", "FastAPI backend + async worker", "Backend FastAPI + worker asincrono"],
          ["Azure Container Apps Jobs", "Consumption plan", "Nightly cost ingestion job (02:00 UTC)", "Job acquisizione notturna (02:00 UTC)"],
          ["Azure Cosmos DB (NoSQL)", "Serverless", "Tenant configs, cost records, waste items, reports", "Config tenant, record costi, sprechi, report"],
          ["Azure Blob Storage", "LRS Standard", "PDF report files + raw cost exports", "File report PDF + export costi grezzi"],
          ["Azure Service Bus", "Optional", "Scale-out queue — NOT deployed in the default cheap config", "Coda scale-out — NON distribuita nella config economica"],
          ["Azure Key Vault", "Standard", "Customer SP credentials + API secrets", "Credenziali SP clienti + segreti API"],
          ["Azure Container Registry", "Basic", "Docker images for API + ingest job", "Immagini Docker per API e job ingest"],
          ["Azure Static Web Apps", "Free tier", "React SPA frontend hosting", "Hosting frontend React SPA"],
          ["Azure Log Analytics", "Pay-as-you-go", "Centralised structured JSON logs", "Log JSON strutturati centralizzati"],
          ["Azure Monitor + Alerts", "Pay-as-you-go", "Error rate, latency, budget alerts", "Alert tasso errori, latenza, budget"],
          ["Azure AD (Entra ID)", "Free", "App registrations + managed identity", "Registrazioni app + managed identity"],
          ["Cost Management API", "Free (built-in)", "Source of truth for billing data", "Fonte di verità per i dati di fatturazione"],
          ["Azure Advisor API", "Free (built-in)", "Rightsizing + optimisation recommendations", "Raccomandazioni rightsizing e ottimizzazione"],
        ],
        [2700, 1700, 2619, 2619],
      ),
      sp(),
      h2("3.1 Estimated Monthly Infrastructure Cost / Costo Infrastruttura Stimato"),
      body("At 10 tenants, 30-day cycle, approximately 2M API calls/month. Infrastructure cost is under 6% of the lowest plan revenue."),
      bodyIT("Con 10 tenant, ciclo 30 giorni, circa 2M chiamate API/mese. Il costo infrastruttura è inferiore al 6% del ricavo del piano più basso."),
      sp(),
      dataTable(
        ["Service", "Est. Cost/mo", "Note"],
        [
          ["Container Apps (backend + jobs)", "€8–15", "Scales to zero between scheduled jobs"],
          ["Cosmos DB (serverless)", "€5–12", "~500k RU/month at 10 tenants"],
          ["Blob Storage (LRS)", "€2–4", "~10 GB reports and exports"],
          ["Service Bus", "€0", "Not deployed — ingest runs inline"],
          ["Key Vault", "€2", "~200 secret operations/day"],
          ["Log Analytics", "€5–10", "~500 MB logs/day ingestion"],
          ["Container Registry (Basic)", "€5", "Fixed monthly"],
          ["Static Web Apps", "€0", "Free tier"],
          ["Total / Totale", "€27–48/mo", "< 6% of €499/mo Starter plan revenue"],
        ],
        [3800, 1700, 4138],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 4. LLD
      // ══════════════════════════════════════════════════════════════════════
      h1("4. Low-Level Design (LLD) / Progettazione a Basso Livello"),
      hr(),
      body("Single FastAPI application deployed as a Docker container on Azure Container Apps. Exposes 15 REST endpoints. The nightly ingest runs as a separate Container Apps Job that processes tenants inline — no message queue is required in the default deployment. All Azure SDK calls use ManagedIdentityCredential — no secrets in code or environment variables. Tenacity retry decorators on all external calls (3 attempts, exponential backoff). Structlog JSON logging with request_id context variable injected per HTTP request."),
      bodyIT("Un'unica applicazione FastAPI distribuita come container Docker su Azure Container Apps. Espone 15 endpoint REST. L'ingest notturno gira come Container Apps Job separato che elabora i tenant inline — nessuna coda richiesta nel deployment di default. Tutte le chiamate Azure SDK usano ManagedIdentityCredential. Decoratori Tenacity retry su tutte le chiamate esterne (3 tentativi, backoff esponenziale). Logging JSON strutturato con variabile di contesto request_id iniettata per ogni richiesta HTTP."),
      sp(),
      h2("4.1 Module Structure / Struttura Moduli"),
      monoBlock([
        "cloudlens/",
        "  app/",
        "    main.py              FastAPI app, lifespan, CORS, middleware, exception handlers",
        "    config.py            Pydantic Settings (lru_cache singleton, all config from env)",
        "    auth.py              API-key + Azure AD bearer (JWKS) auth dependencies",
        "    rate_limit.py        In-process per-tenant token-bucket limiter (no Redis)",
        "    exceptions.py        CloudLensError hierarchy — 12 typed domain exceptions",
        "    logging_config.py    structlog JSON setup, Azure Log Analytics compatible",
        "    models/",
        "      tenant.py          TenantConfig, TenantCreate, TenantUpdate, PlanTier enum",
        "      cost.py            CostRecord (TTL 90d), CostSummary, CostBreakdown, CostTrend",
        "      waste.py           WasteItem, WasteType (12 types), Priority, WasteResolve",
        "      report.py          ReportMeta, ReportStatus enum",
        "    routers/",
        "      tenants.py         CRUD /api/v1/tenants (5 endpoints, API-key protected)",
        "      costs.py           /api/v1/costs — summary, breakdown, 30/60/90d trend",
        "      waste.py           /api/v1/waste — list with priority filter + resolve",
        "      reports.py         /api/v1/reports — generate (background), list, download",
        "      ingest.py          /api/v1/ingest/{tenant_id} (inline) + /api/v1/health",
        "    services/",
        "      azure_cost.py      AzureCostClient (async, token refresh, retry, Cost+Advisor)",
        "      resource_graph.py  Bulk KQL collector — disk/IP/snapshot/cert state + tags",
        "      waste_engine.py    12 waste detection rules, asyncio.gather orchestrator",
        "      cosmos.py          Async Cosmos DB wrapper (upsert, get, query, bulk_upsert)",
        "      blob.py            Blob upload + user-delegation SAS URL generation",
        "      keyvault.py        Secret get/set, SP credential store/retrieve",
        "      bus.py             OPTIONAL Service Bus scale-out — not used by default",
        "      report_builder.py  ReportLab PDF generator (bilingual EN/IT, A4)",
        "    jobs/",
        "      ingest.py          Full nightly ingest job — ACA Job entrypoint (inline)",
        "  frontend/",
        "      index.html         Single-file drill-down console (Static Web App)",
        "  tests/",
        "    test_cloudlens.py    Model, waste-engine, exception unit tests",
        "    test_routers.py      Router integration tests (mocked Cosmos layer)",
        "    test_auth_ratelimit.py  Auth + rate-limit tests (55 tests total)",
        "  infra/",
        "    main.tf              All Azure resources (Terraform)",
        "    variables.tf         Input variable declarations",
        "    environments/        prod.tfvars, staging.tfvars, dev.tfvars",
        "  .github/workflows/",
        "    backend.yml          CI: test → build → push ACR → deploy ACA → health check",
        "    infra.yml            IaC: plan → PR comment → manual approval → apply",
        "  Dockerfile             Multi-stage, non-root user, HEALTHCHECK",
        "  requirements.txt       Pinned production dependencies",
        "  .env.example           All required environment variables documented",
      ]),
      sp(),
      pb(),

      // ── 4.2 Data Models ──────────────────────────────────────────────────
      h2("4.2 Data Models / Modelli Dati"),
      body("All models use Pydantic v2 for validation and serialisation. Every Cosmos DB document includes a 'type' discriminator field and a '_partitionKey' field set via to_cosmos(). Cosmos metadata fields (_rid, _etag, _ts, etc.) are stripped in from_cosmos() before model instantiation."),
      bodyIT("Tutti i modelli usano Pydantic v2 per validazione e serializzazione. Ogni documento Cosmos DB include un campo discriminatore 'type' e un campo '_partitionKey'. I metadati Cosmos (_rid, _etag, _ts, ecc.) vengono rimossi in from_cosmos() prima dell'istanziazione del modello."),
      sp(),

      h3("4.2.1 TenantConfig — Cosmos container: tenants / partition key: id"),
      dataTable(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
          ["id", "str (UUID4)", "Partition key — auto-generated", "Chiave di partizione — auto-generata"],
          ["tenant_name", "str (2–120)", "Display name of the customer", "Nome visualizzato del cliente"],
          ["subscription_ids", "list[str]", "Azure subscription IDs to monitor (UUID format validated)", "ID sottoscrizioni Azure (formato UUID validato)"],
          ["plan_tier", "Enum", "starter | growth | enterprise", "Piano di fatturazione"],
          ["sp_secret_ref", "str", "Key Vault secret name for SP credentials", "Nome segreto Key Vault per credenziali SP"],
          ["alert_email", "str", "Weekly digest + alert recipient", "Destinatario digest settimanale e alert"],
          ["active", "bool", "Soft-delete flag (false = deactivated)", "Flag soft-delete (false = disattivato)"],
          ["created_at", "datetime (UTC)", "Record creation timestamp", "Timestamp di creazione record"],
          ["last_ingested_at", "datetime | None", "Last successful ingest run", "Ultima acquisizione riuscita"],
          ["last_ingest_error", "str | None", "Last error message from ingest job", "Ultimo messaggio di errore dal job ingest"],
        ],
        [1900, 1900, 2919, 2919],
        TEAL,
      ),
      sp(),

      h3("4.2.2 CostRecord — Cosmos container: cost_records / TTL: 90 days"),
      dataTable(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
          ["id", "str (UUID4)", "Document ID", "ID documento"],
          ["tenant_id", "str", "FK → TenantConfig.id (partition key)", "FK → TenantConfig.id (chiave di partizione)"],
          ["subscription_id", "str", "Source Azure subscription ID", "ID sottoscrizione Azure di origine"],
          ["record_date", "date", "Cost date — daily grain", "Data del costo — granularità giornaliera"],
          ["service_name", "str", "Azure service (e.g. Virtual Machines)", "Servizio Azure (es. Virtual Machines)"],
          ["resource_id", "str", "Full ARM resource ID (lowercase)", "ARM resource ID completo (minuscolo)"],
          ["resource_group", "str", "Resource group name", "Nome resource group"],
          ["cost_eur", "float (≥ 0)", "Normalised cost in EUR", "Costo normalizzato in EUR"],
          ["tags", "dict[str,str]", "Resource tags at ingest time", "Tag della risorsa al momento dell'acquisizione"],
          ["ttl", "int", "7776000 = 90-day auto-expiry in Cosmos", "7776000 = scadenza automatica 90 giorni in Cosmos"],
        ],
        [1900, 1900, 2919, 2919],
        TEAL,
      ),
      sp(),

      h3("4.2.3 WasteItem — Cosmos container: waste_items"),
      dataTable(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
          ["waste_type", "Enum (12 values)", "idle_vm | unattached_disk | orphan_public_ip | ...", "Categoria spreco (12 tipi)"],
          ["monthly_cost_eur", "float (≥ 0)", "Current monthly cost of this resource", "Costo mensile attuale della risorsa"],
          ["saving_eur", "float (≥ 0)", "Estimated monthly saving if remediated", "Risparmio mensile stimato se risolto"],
          ["saving_pct", "float", "Saving as % of monthly cost — set by engine", "Risparmio come % del costo — impostato dal motore"],
          ["priority", "Enum", "critical | high | medium | low", "Priorità di triage"],
          ["recommendation", "str", "Human-readable action (EN)", "Azione in linguaggio naturale (EN)"],
          ["recommendation_it", "str", "Raccomandazione in italiano", "Testo azione in italiano"],
          ["evidence", "dict", "Supporting metrics: cpu_avg_pct, disk_state, etc.", "Metriche a supporto: cpu_avg_pct, disk_state, ecc."],
          ["advisor_ref", "str | None", "Azure Advisor recommendation ID", "ID raccomandazione Azure Advisor"],
          ["resolved_at", "datetime | None", "When marked resolved — null if open", "Quando segnato risolto — null se aperto"],
        ],
        [1900, 1900, 2919, 2919],
        TEAL,
      ),
      sp(),

      h3("4.2.4 ReportMeta — Cosmos container: reports"),
      dataTable(
        ["Field", "Type", "Description (EN)", "Descrizione (IT)"],
        [
          ["id", "str (UUID4)", "Document ID + Blob file name", "ID documento e nome file Blob"],
          ["tenant_id", "str", "FK → TenantConfig.id", "FK → TenantConfig.id"],
          ["period_start / period_end", "date", "Reporting period boundaries", "Limiti del periodo di report"],
          ["total_spend_eur", "float", "Total spend in reporting period", "Spesa totale nel periodo"],
          ["total_waste_eur", "float", "Total identified waste", "Totale sprechi identificati"],
          ["waste_pct", "float", "Waste as % of spend", "Sprechi come % della spesa"],
          ["critical_count / high_count", "int", "Waste item counts by priority", "Conteggio sprechi per priorità"],
          ["blob_url", "str | None", "Signed SAS URL for PDF download (1h expiry)", "URL SAS firmato per download PDF (scadenza 1h)"],
          ["status", "Enum", "pending | generating | ready | failed", "Stato generazione"],
          ["generated_at", "datetime | None", "Completion timestamp", "Timestamp di completamento"],
        ],
        [2400, 1700, 2769, 2769],
        TEAL,
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 5. SERVICE COMMUNICATION
      // ══════════════════════════════════════════════════════════════════════
      h1("5. Service Communication / Comunicazione tra Servizi"),
      hr(),
      body("All inter-service communication uses Azure managed identity — no connection strings, no shared secrets. The backend never holds credentials to customer Azure subscriptions in memory beyond the duration of a single async context manager. All external calls are wrapped in Tenacity retry decorators with exponential backoff."),
      bodyIT("Tutta la comunicazione inter-servizio usa Azure managed identity. Il backend non mantiene mai credenziali delle sottoscrizioni Azure dei clienti in memoria oltre la durata di un singolo context manager async. Tutte le chiamate esterne sono avvolte in decoratori Tenacity retry con backoff esponenziale."),
      sp(),
      dataTable(
        ["From", "To", "Protocol", "Auth", "Error handling"],
        [
          ["Ingestion Job (ACA Job)", "Azure Cost Mgmt API", "HTTPS REST", "Customer SP (OAuth2 client credentials)", "Tenacity: 3 retries, exp. backoff. 429 → sleep(Retry-After)"],
          ["Ingestion Job", "Azure Advisor API", "HTTPS REST", "Customer SP (OAuth2 client credentials)", "Tenacity: 3 retries. Non-200 raises AzureAPIError"],
          ["Ingestion Job", "Azure Resource Graph", "HTTPS REST", "Customer SP (OAuth2 client credentials)", "Tenacity: 3 retries. Per-collector failures isolated, default to empty"],
          ["FastAPI Backend", "Cosmos DB", "HTTPS REST", "Managed identity (CosmosDBContributor role)", "Tenacity: 3 retries. NotFoundError on 404, CosmosError otherwise"],
          ["FastAPI Backend", "Azure Blob Storage", "HTTPS REST", "Managed identity (StorageBlobDataContributor)", "Tenacity: 3 retries. StorageError raised on failure"],
          ["FastAPI Backend", "Azure Key Vault", "HTTPS REST", "Managed identity (KeyVault SecretsUser)", "Tenacity: 3 retries. KeyVaultError raised on failure"],
          ["Frontend SPA", "FastAPI Backend", "HTTPS REST", "Azure AD bearer token (MSAL)", "HTTP 4xx/5xx returned as structured JSON"],
        ],
        [1700, 1700, 1100, 2238, 2900],
      ),
      sp(),
      h2("5.1 Exception Hierarchy / Gerarchia Eccezioni"),
      body("All domain errors derive from CloudLensError. Each exception carries status_code (HTTP), error_code (machine-readable string), message (human-readable), and optional detail. FastAPI exception handlers map these directly to JSON responses — no unhandled stack traces ever reach the client."),
      sp(),
      dataTable(
        ["Exception class", "HTTP", "Code", "Raised when"],
        [
          ["NotFoundError", "404", "NOT_FOUND", "Cosmos get_item returns 404"],
          ["ValidationError", "422", "VALIDATION_ERROR", "Invalid request body or query param"],
          ["ConflictError", "409", "CONFLICT", "Duplicate tenant_name on creation"],
          ["UnauthorizedError", "401", "UNAUTHORIZED", "Missing or invalid bearer token"],
          ["RateLimitError", "429", "RATE_LIMITED", "Per-tenant rate limit exceeded"],
          ["AzureAPIError", "502", "AZURE_API_ERROR", "Cost Management / Advisor API error"],
          ["CosmosError", "503", "COSMOS_ERROR", "Cosmos DB operation failure after retries"],
          ["StorageError", "503", "STORAGE_ERROR", "Blob Storage operation failure after retries"],
          ["ServiceBusError", "503", "SERVICE_BUS_ERROR", "Service Bus send/receive failure"],
          ["KeyVaultError", "503", "KEY_VAULT_ERROR", "Key Vault secret retrieval failure"],
          ["IngestError", "500", "INGEST_ERROR", "Ingest job failure — wraps upstream exceptions"],
          ["ReportGenerationError", "500", "REPORT_ERROR", "Report generation or upload failure"],
        ],
        [2200, 700, 2138, 4600],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 6. API ENDPOINTS
      // ══════════════════════════════════════════════════════════════════════
      h1("6. API Endpoints / Endpoint API"),
      hr(),
      body("All endpoints versioned under /api/v1. Authentication: Azure AD bearer token via OAuth2 client credentials flow for external callers; managed identity for internal service-to-service. Non-production environments expose /docs (Swagger) and /redoc. Production disables both."),
      bodyIT("Tutti gli endpoint versionati sotto /api/v1. Autenticazione: bearer token Azure AD via flusso OAuth2 client credentials per chiamanti esterni; managed identity per inter-servizio. Gli ambienti non-prod espongono /docs e /redoc. La produzione li disabilita entrambi."),
      sp(),
      dataTable(
        ["Method", "Path", "Response", "Description (EN)"],
        [
          ["GET",    "/api/v1/tenants",                      "200 list[TenantConfig]",  "List all tenants, ordered by name"],
          ["POST",   "/api/v1/tenants",                      "201 TenantConfig",        "Create tenant — stores SP creds to Key Vault"],
          ["GET",    "/api/v1/tenants/{id}",                 "200 TenantConfig",        "Get single tenant by ID"],
          ["PATCH",  "/api/v1/tenants/{id}",                 "200 TenantConfig",        "Partial update — only provided fields changed"],
          ["DELETE", "/api/v1/tenants/{id}",                 "204 No Content",          "Soft-delete: sets active=false, data preserved"],
          ["GET",    "/api/v1/costs/{tenant_id}",            "200 CostSummary",         "Aggregated cost + % change vs previous period"],
          ["GET",    "/api/v1/costs/{tenant_id}/breakdown",  "200 CostBreakdown",       "Cost grouped by service|resource_group|location"],
          ["GET",    "/api/v1/costs/{tenant_id}/trend",      "200 CostTrend",           "Daily data points, avg, peak — 7–90 day window"],
          ["GET",    "/api/v1/waste/{tenant_id}",            "200 list[WasteItem]",     "Waste items, filterable by priority, resolved flag"],
          ["PATCH",  "/api/v1/waste/{id}/resolve",           "200 WasteItem",           "Mark waste item resolved with actor + notes"],
          ["POST",   "/api/v1/reports/{tenant_id}/generate", "202 ReportMeta",          "Enqueue PDF generation — returns immediately"],
          ["GET",    "/api/v1/reports/{tenant_id}",          "200 list[ReportMeta]",    "List reports for tenant, newest first"],
          ["GET",    "/api/v1/reports/{id}/download",        "200 {download_url}",      "Fresh 1-hour SAS URL for PDF download"],
          ["POST",   "/api/v1/ingest/{tenant_id}",           "202 {message_ids}",       "Manual ingest trigger — admin use"],
          ["GET",    "/api/v1/health",                       "200 {status, checks}",    "Liveness + Cosmos dependency check"],
        ],
        [900, 2900, 1938, 3900],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 7. WASTE DETECTION ENGINE
      // ══════════════════════════════════════════════════════════════════════
      h1("7. Waste Detection Engine / Motore di Rilevamento Sprechi"),
      hr(),
      body("Pure Python rules module at services/waste_engine.py. Rules run after each ingestion cycle against the last 30 days of cost_records plus Azure Advisor API output. Each rule is an isolated async function returning list[WasteItem]. All rules run concurrently via asyncio.gather(). Individual rule failures are isolated — the engine logs the error and continues. Results are sorted by saving_eur descending. Priority is CRITICAL when estimated saving > €100/mo, scaling down to LOW for informational/risk findings."),
      bodyIT("Modulo Python puro in services/waste_engine.py. Le regole girano dopo ogni ciclo di acquisizione sugli ultimi 30 giorni di cost_records più l'output API Azure Advisor. Ogni regola è una funzione async isolata che restituisce list[WasteItem]. Tutte le regole girano in concorrenza via asyncio.gather(). I fallimenti individuali sono isolati — il motore logga l'errore e continua. I risultati sono ordinati per saving_eur decrescente."),
      sp(),
      dataTable(
        ["Rule ID", "Priority", "Signal used", "Threshold", "Avg saving (monthly)"],
        [
          ["idle_vm",            "Critical/High", "VM CPU average %",          "< 5% over 14 days",        "€150–2,000"],
          ["unattached_disk",    "Critical",      "Managed disk state",         "= Unattached",             "€30–200"],
          ["orphan_public_ip",   "High",          "IP association status",      "Not associated to any resource", "€5–15"],
          ["oversized_vm",       "High",          "Azure Advisor rec. category","Rightsize recommendation present", "€50–500"],
          ["dev_test_eligible",  "High",          "Subscription offer type",    "PAYG on non-prod env tag",  "€100–800"],
          ["reserved_instance",  "Medium",        "VM consecutive uptime days", "> 30 days stable",          "30–60% via RI"],
          ["idle_app_service",   "Medium",        "App Service req/min average","< 1 req/min over 14 days", "€30–150"],
          ["unused_lb",          "Medium",        "Backend pool instance count","= 0 backends",             "€15–50"],
          ["old_snapshots",      "Low",           "Snapshot creation age",      "> 90 days old",            "€5–50"],
          ["cold_storage",       "Low",           "Blob access tier + logs",    "No access in 30 days",     "€10–80"],
          ["duplicated_backup",  "Low",           "Backup policies per resource","Multiple policies on same resource", "€20–100"],
          ["expired_cert",       "Low",           "Key Vault cert expiry date", "< 30 days to expiry",      "Risk, not cost"],
        ],
        [2000, 1200, 2000, 2238, 2200],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 8. SECURITY MODEL
      // ══════════════════════════════════════════════════════════════════════
      h1("8. Security Model / Modello di Sicurezza"),
      hr(),
      sectionBox(
        "Core security guarantee",
        "Un compromesso della piattaforma CloudLens NON può portare a modifiche delle risorse Azure dei clienti.",
      ),
      sp(),
      body("CloudLens is designed so that a breach of the CloudLens platform cannot lead to any modification of customer Azure resources. The read-only service principal constraint is architectural — enforced by Azure RBAC, not just by application code. Even if an attacker obtained a customer's SP credentials from Key Vault, those credentials only grant Reader + Cost Management Reader roles, which are read-only by definition in Azure."),
      bodyIT("CloudLens è progettato in modo che una violazione della piattaforma CloudLens non possa portare ad alcuna modifica delle risorse Azure del cliente. Il vincolo read-only è architetturale — imposto da Azure RBAC, non solo dal codice applicativo."),
      sp(),
      dataTable(
        ["Security Control", "Implementation (EN)", "Implementazione (IT)"],
        [
          ["Read-only service principal", "Customer assigns Reader + Cost Management Reader on their subscription only — no Owner, no Contributor", "Il cliente assegna solo Reader + Cost Management Reader sulla propria sottoscrizione — nessun Owner, nessun Contributor"],
          ["Secrets in Key Vault", "Customer SP credentials stored as JSON in Key Vault secret named sp-creds-{tenant_id}. Never in config files or env vars", "Credenziali SP clienti memorizzate come JSON in Key Vault con nome sp-creds-{tenant_id}. Mai in file di config o env var"],
          ["Managed Identity everywhere", "Backend, ingest job, and report builder all use user-assigned managed identity for KV, Cosmos, and Blob", "Backend, job ingest e report builder usano managed identity per KV, Cosmos e Blob"],
          ["Network isolation", "Container Apps deployed in internal environment; only Static Web App has public ingress", "Container Apps in ambiente interno; solo la Static Web App ha ingress pubblico"],
          ["TLS everywhere", "All service-to-service calls over HTTPS/AMQPS. No plaintext endpoints", "Tutte le chiamate inter-servizio su HTTPS/AMQPS. Nessun endpoint in chiaro"],
          ["Cosmos RBAC", "Per-collection read/write grants; ingest writes, API reads with minimal scope", "Grant read/write per collection; ingest scrive, API legge con scope minimo"],
          ["Blob SAS tokens", "Time-limited user-delegation SAS tokens for report downloads (1h expiry). No permanent access", "Token SAS user-delegation a tempo limitato per download report (1h). Nessun accesso permanente"],
          ["Audit logging", "All API calls logged to Log Analytics with tenant_id, request_id, user context. Retention 30 days", "Tutte le chiamate API loggate in Log Analytics con tenant_id, request_id, contesto utente. Retention 30 giorni"],
          ["Non-root container", "Docker container runs as UID 1001 (cloudlens user) — no root inside container", "Container Docker gira come UID 1001 (utente cloudlens) — nessun root nel container"],
          ["Input validation", "Pydantic v2 validates all API inputs including subscription ID format (UUID regex), email format", "Pydantic v2 valida tutti gli input API incluso formato subscription ID (UUID regex), formato email"],
        ],
        [2300, 3669, 3669],
        AMBER,
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 9. INFRASTRUCTURE AS CODE
      // ══════════════════════════════════════════════════════════════════════
      h1("9. Infrastructure as Code / Infrastruttura come Codice"),
      hr(),
      body("All Azure resources are provisioned with Terraform using a flat config.tfvars pattern — consistent with existing Mouritech IaC conventions. One tfvars file per environment (dev, staging, prod). No manual portal configuration permitted. The internal_api_key is passed via TF_VAR_internal_api_key environment variable — never stored in tfvars files."),
      bodyIT("Tutte le risorse Azure sono provisionate con Terraform usando il pattern config.tfvars piatto — coerente con le convenzioni IaC Mouritech esistenti. Un file tfvars per ambiente (dev, staging, prod). Nessuna configurazione manuale via portale. L'internal_api_key è passata tramite variabile TF_VAR_internal_api_key — mai memorizzata nei file tfvars."),
      sp(),
      h2("9.1 Terraform Module Layout"),
      monoBlock([
        "infra/",
        "  main.tf              All resources: RG, ACA, ACA Job, Cosmos, Storage,",
        "                       Key Vault, ACR, Managed Identity, RBAC, Monitor alerts",
        "  variables.tf         Input variable declarations (9 vars + sensitive api_key)",
        "  environments/",
        "    prod.tfvars        Production: italynorth, rg-cloudlens-prod",
        "    staging.tfvars     Staging: italynorth, rg-cloudlens-staging",
        "    dev.tfvars         Development: westeurope, rg-cloudlens-dev",
        "    *.backend.tfvars   Azure backend config (storage account for tfstate)",
      ]),
      sp(),
      h2("9.2 Key Resources and Naming Conventions"),
      dataTable(
        ["Resource type", "Naming pattern", "Example (prod)"],
        [
          ["Resource Group", "rg-cloudlens-{env}", "rg-cloudlens-prod"],
          ["Container Apps Environment", "cae-cloudlens-{env}", "cae-cloudlens-prod"],
          ["Container App (API)", "cloudlens-api", "cloudlens-api"],
          ["Container App Job (Ingest)", "cloudlens-ingest", "cloudlens-ingest"],
          ["Cosmos DB Account", "cosmos-cloudlens-{env}", "cosmos-cloudlens-prod"],
          ["Storage Account", "stcloudlens{env}", "stcloudlensprod"],
          ["Key Vault", "kv-cloudlens-{env}", "kv-cloudlens-prod"],
          ["Container Registry", "acrcloudlens{env}", "acrcloudlensprod"],
          ["Managed Identity", "id-cloudlens-api-{env}", "id-cloudlens-api-prod"],
          ["Log Analytics Workspace", "law-cloudlens-{env}", "law-cloudlens-prod"],
          ["Key Vault Secret (SP creds)", "sp-creds-{tenant_id}", "sp-creds-00000000-..."],
        ],
        [2800, 2600, 4238],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 10. CI/CD PIPELINE
      // ══════════════════════════════════════════════════════════════════════
      h1("10. CI/CD Pipeline / Pipeline CI/CD"),
      hr(),
      body("GitHub Actions with OIDC federated credentials — no stored client secrets in GitHub. Two workflows: backend (test → build → push → deploy → health check → rollback) and infrastructure (plan → PR comment → manual approval → apply). Docker images tagged with git SHA for exact traceability."),
      bodyIT("GitHub Actions con OIDC federated credentials — nessun segreto client memorizzato in GitHub. Due workflow: backend (test → build → push → deploy → health check → rollback) e infrastruttura (plan → commento PR → approvazione manuale → apply). Immagini Docker taggate con git SHA per tracciabilità esatta."),
      sp(),
      h2("10.1 Backend Workflow — backend.yml"),
      dataTable(
        ["Job", "Step", "Action", "On failure"],
        [
          ["test", "pytest", "55 tests, JUnit XML artifact uploaded", "Pipeline stops — no build"],
          ["build", "Docker build + push", "Multi-stage build, tagged with git SHA + latest", "Pipeline stops — no deploy"],
          ["deploy", "az containerapp update", "Rolling deploy — existing replicas continue serving", "Rollback triggered automatically"],
          ["deploy", "Health check smoke test", "GET /api/v1/health — asserts 'status':'healthy'", "Rollback to previous revision via traffic weights"],
        ],
        [1200, 2000, 3638, 2800],
      ),
      sp(),
      h2("10.2 Infrastructure Workflow — infra.yml"),
      dataTable(
        ["Job", "Step", "Action", "Gate"],
        [
          ["plan", "terraform fmt + validate", "Format check + schema validation", "Fails if fmt diff detected"],
          ["plan", "terraform plan", "Plan output posted as PR comment", "Plan must succeed"],
          ["apply", "manual approval", "GitHub Environment approval gate for prod", "Human must approve in GitHub UI"],
          ["apply", "terraform apply", "Apply pre-generated plan artifact", "Only on main branch"],
        ],
        [1200, 2000, 3638, 2800],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 11. OBSERVABILITY
      // ══════════════════════════════════════════════════════════════════════
      h1("11. Observability / Osservabilità"),
      hr(),
      body("Every HTTP request injects a request_id UUID into structlog context variables — present in all log lines emitted during that request. JSON logs stream to stdout, are captured by Container Apps, and forwarded to Log Analytics. OpenTelemetry traces go to Azure Monitor Application Insights."),
      bodyIT("Ogni richiesta HTTP inietta un UUID request_id nelle variabili di contesto structlog — presente in tutte le righe di log emesse durante quella richiesta. I log JSON vanno su stdout, vengono catturati da Container Apps e inoltrati a Log Analytics. Le tracce OpenTelemetry vanno ad Azure Monitor Application Insights."),
      sp(),
      dataTable(
        ["Signal", "Tool", "Configuration (EN)", "Configurazione (IT)"],
        [
          ["Structured logs", "structlog + Log Analytics", "JSON to stdout; ACA streams to LAW workspace; request_id in every line", "JSON su stdout; ACA forwarda a workspace LAW; request_id in ogni riga"],
          ["Metrics", "Container Apps built-in", "CPU %, memory %, replica count, request count, HTTP error rate", "CPU %, memoria %, numero repliche, conteggio richieste, tasso errori HTTP"],
          ["Distributed traces", "OpenTelemetry → App Insights", "FastAPI instrumentation; trace spans per endpoint + external call", "Instrumentazione FastAPI; trace span per endpoint + chiamata esterna"],
          ["Error rate alert", "Azure Monitor", "Triggers when 5xx count > 0 in 15-minute window; severity 1", "Scatta quando 5xx > 0 in finestra 15 minuti; severità 1"],
          ["Latency alert", "Azure Monitor", "Triggers when p99 latency > 2s in 5-minute window", "Scatta quando latenza p99 > 2s in finestra 5 minuti"],
          ["Job failure alert", "Azure Monitor", "ACA Job exit code != 0; notifies ops@cloudlens.io", "Codice uscita ACA Job != 0; notifica ops@cloudlens.io"],
          ["Cost budget alert", "Azure Cost Management", "80% and 100% of €60/mo infra budget", "All'80% e 100% del budget infra di €60/mese"],
          ["Uptime probe", "Azure Monitor", "GET /api/v1/health every 5 min; 3 failures → critical alert", "GET /api/v1/health ogni 5 min; 3 fallimenti → alert critico"],
        ],
        [1700, 2000, 3019, 3019],
      ),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 12. MULTI-TENANCY
      // ══════════════════════════════════════════════════════════════════════
      h1("12. Multi-Tenancy Design / Progettazione Multi-Tenant"),
      hr(),
      body("Shared infrastructure, data isolated by tenant_id at every layer. All tenants share the same Container Apps and Cosmos account — but data isolation is enforced at the storage level, not just at the application level. There is no way for a query scoped to tenant A to return data belonging to tenant B."),
      bodyIT("Infrastruttura condivisa, dati isolati per tenant_id a ogni livello. Tutti i tenant condividono le stesse Container Apps e lo stesso account Cosmos — ma l'isolamento dei dati è imposto a livello di storage, non solo applicativo."),
      sp(),
      kvTable([
        ["Cosmos DB partition key", "tenant_id on all four containers — Cosmos physically separates data per partition. Cross-tenant queries are structurally impossible without an explicit cross-partition flag."],
        ["Ingest isolation", "The nightly job processes each tenant in its own try/except; one tenant's ingest failure never blocks another's. Per-tenant SP credentials are loaded only for that tenant's run."],
        ["Blob Storage", "Blob path pattern: reports/{tenant_id}/report-{report_id}.pdf — tenant ID is part of the physical storage path, not just metadata."],
        ["Key Vault", "Secret naming convention: sp-creds-{tenant_id}. Access policy on the managed identity scopes secret access to matching names only."],
        ["REST API", "JWT bearer token claims include tenant_id. FastAPI middleware enforces that the tenant_id in the path/query matches the token claim on every request."],
        ["Rate limiting", "Per-tenant limits enforced at router level: Starter 60 req/min, Growth 200 req/min, Enterprise 600 req/min."],
        ["Ingest scheduling", "ACA Job iterates all active tenants sequentially. Each tenant's ingest failure is isolated — one tenant's error does not block others."],
      ], 2800),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 13. TENANT ONBOARDING
      // ══════════════════════════════════════════════════════════════════════
      h1("13. Tenant Onboarding / Onboarding Tenant"),
      hr(),
      body("Onboarding a new customer takes under 20 minutes and requires zero code changes. The entire process is configuration-driven. The customer creates a service principal in their own Azure AD — CloudLens never has Owner or Contributor access."),
      bodyIT("L'onboarding di un nuovo cliente richiede meno di 20 minuti e zero modifiche al codice. Il cliente crea un service principal nel proprio Azure AD — CloudLens non ha mai accesso Owner o Contributor."),
      sp(),
      dataTable(
        ["#", "Actor", "Step (EN)", "Passo (IT)", "Time"],
        [
          ["1", "Customer", "Create App Registration in their Azure AD tenant", "Crea App Registration nel proprio tenant Azure AD", "2 min"],
          ["2", "Customer", "Assign Reader + Cost Management Reader roles to the SP on their subscription", "Assegna ruoli Reader + Cost Mgmt Reader al SP sulla propria sottoscrizione", "3 min"],
          ["3", "Customer", "Share client_id, client_secret, tenant_id via secure channel (1Password Send / encrypted email)", "Condivide client_id, client_secret, tenant_id tramite canale sicuro", "2 min"],
          ["4", "CloudLens ops", "az keyvault secret set — stores credentials as JSON in Key Vault as sp-creds-{tenant_id}", "az keyvault secret set — memorizza credenziali in Key Vault come sp-creds-{id}", "2 min"],
          ["5", "CloudLens ops", "POST /api/v1/tenants with TenantCreate JSON payload (name, subscription_ids, plan_tier, alert_email, SP credentials)", "POST /api/v1/tenants con payload TenantCreate JSON", "1 min"],
          ["6", "System", "API validates SP format, stores config to Cosmos. First ingest runs at the next nightly cycle (or on-demand via the admin trigger)", "L'API valida il SP, salva config in Cosmos. Primo ingest al ciclo notturno successivo (o on-demand via trigger admin)", "—"],
          ["7", "System", "First ingest completes (~5 min for 30-day lookback). Dashboard populated, welcome email sent with report download link", "Primo ingest completato (~5 min). Dashboard popolata, email di benvenuto inviata con link download report", "~5 min"],
        ],
        [400, 1500, 2638, 2638, 800],
      ),
      sp(),
      pb(),

      // ══════════════════════════════════════════════════════════════════════
      // 14. FRONTEND CONSOLE
      // ══════════════════════════════════════════════════════════════════════
      h1("14. Frontend Console / Console Frontend"),
      hr(),
      body("A single-file Static Web App (free tier) that reads the REST API and renders a drill-down cost explorer. The interface is built around one job: show where money is leaking and let an operator drill from the whole portfolio down to the individual wasteful resource."),
      sp(),
      bodyIT("Una Static Web App a file singolo (tier gratuito) che legge l'API REST e renderizza un cost explorer drill-down. L'interfaccia ha un solo obiettivo: mostrare dove si perde denaro e permettere di scendere dal portfolio fino alla singola risorsa che spreca."),
      sp(),
      h2("14.1 Drill-Down Hierarchy / Gerarchia Drill-Down"),
      dataTable(
        ["Level", "Shows", "Backing endpoint"],
        [
          ["Portfolio", "All tenants — spend, recoverable, waste ratio", "GET /api/v1/tenants"],
          ["Tenant", "Spend by Azure service", "GET /costs/{tid}/breakdown?dimension=service"],
          ["Service", "Spend by resource group", "GET /costs/{tid}/breakdown?dimension=resource_group"],
          ["Resource group", "Resources with detected waste", "GET /costs/{tid} (filtered)"],
          ["Resource", "Individual waste findings by priority", "GET /waste/{tid}"],
          ["Finding detail", "Evidence, EN/IT recommendation, tags, actions", "PATCH /waste/{id}/resolve"],
        ],
        [1900, 3869, 3869],
      ),
      sp(),
      h2("14.2 Design / Design"),
      kvTable([
        ["Signature element", "A pinned breadcrumb 'spine' shows the full drill path; click any level to jump back to it."],
        ["Hero metric", "Recoverable spend per month (not total spend) — savings is what the product actually delivers."],
        ["Colour coding", "Teal < 12% optimized · amber 12–25% review · red > 25% act now — applied to every waste-ratio bar."],
        ["Detail drawer", "The lowest level opens a side drawer: evidence metrics, bilingual recommendation, resource tags, and resolve / snooze actions."],
        ["Auth", "The SPA acquires an Azure AD token (MSAL) and sends it as Bearer <jwt>; the backend enforces tenant scope from the token claim."],
        ["Data wiring", "Ships with mock data shaped to the exact API contract; going live means swapping the mock arrays for fetch() calls — render functions are unchanged."],
        ["Deployment", "Static Web Apps free tier. No build step — a single self-contained index.html."],
      ], 2800),
      sp(),
      sp(),
      h2("Document Revision History / Storico Revisioni"),
      dataTable(
        ["Version", "Date", "Changes (EN)", "Author"],
        [
          ["1.0", "June 2026", "Initial architecture document — HLD, LLD, all data models, API, security, IaC, CI/CD, observability, multi-tenancy, onboarding", "CloudLens Engineering"],
          ["1.1", "June 2026", "Cheapest-infra revision: Service Bus removed (inline ingest), Resource Graph + tag enrichment, auth + rate limiting, drill-down frontend, 55 tests", "CloudLens Engineering"],
        ],
        [1000, 1400, 5538, 1700],
      ),
      sp(),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 400, after: 0 },
        children: [new TextRun({ text: "— End of Document / Fine del Documento —", font: "Arial", size: 18, color: GRAY_MID, italics: true })],
      }),
    ],
  }],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("/mnt/user-data/outputs/CloudLens_Architecture_v1.docx", buf);
  console.log("Word OK");
});
