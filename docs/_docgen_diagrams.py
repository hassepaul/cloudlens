#!/usr/bin/env python3
"""Build the complete CloudLens documentation site (single HTML file)."""
import sys
sys.path.insert(0, "/home/claude/cloudlens/docs")
from _docgen_api import build_api_reference

API_REF = build_api_reference()

# ── Mermaid diagrams ──────────────────────────────────────────────────────────

HLD = r"""flowchart TB
  subgraph CUST["Customer Azure Tenants (read-only)"]
    SUB1["Subscription A"]
    SUB2["Subscription B"]
  end
  subgraph AZAPI["Azure Platform APIs"]
    CM["Cost Management API"]
    ADV["Advisor API"]
    RG["Resource Graph API"]
  end
  subgraph CL["CloudLens (single Resource Group, scale-to-zero)"]
    JOB["Ingestion Job<br/>(ACA Job, 02:00 UTC, inline)"]
    API["FastAPI Backend<br/>(Container Apps)"]
    subgraph STORE["Storage layer"]
      COSMOS[("Cosmos DB<br/>4 containers, serverless")]
      BLOB[("Blob Storage<br/>PDF reports")]
      KV[("Key Vault<br/>SP credentials")]
    end
    FE["Static Web App<br/>Drill-down + Forecast + Insights"]
  end
  SUB1 & SUB2 -->|service principal| CM & ADV & RG
  CM & ADV & RG -->|nightly pull| JOB
  JOB -->|waste engine, persist| COSMOS
  JOB -->|read SP creds| KV
  API --> COSMOS & BLOB & KV
  FE -->|Bearer JWT| API
  classDef store fill:#14463f,stroke:#2dd4bf,color:#e6edf3;
  class COSMOS,BLOB,KV store;
"""

LLD = r"""flowchart LR
  subgraph ROUTERS["Routers (/api/v1)"]
    R1["tenants"]
    R2["costs / waste"]
    R4["forecast"]
    R5["insights"]
    R6["budgets"]
    R10["multicloud"]
    R11["drilldown"]
    R12["alerts"]
    R13["optimization"]
    R14["admin / compliance"]
    R8["ingest / health"]
  end
  subgraph MW["Cross-cutting"]
    AUTH["auth<br/>API-key + JWKS<br/>+ tenant scope"]
    RL["rate_limit<br/>token bucket"]
    EXC["exceptions<br/>typed"]
    LOG["logging + audit<br/>structlog + hash chain"]
  end
  subgraph SERVICES["Services"]
    S1["azure_cost / providers<br/>(AWS/GCP/Alibaba/OCI/AI)"]
    S3["waste_engine<br/>12 rules"]
    S4["forecast"]
    S5["anomaly<br/>tenant + resource"]
    S6["chargeback / allocation"]
    S7["insights"]
    SC["commitments"]
    SO["rightsizing / scheduling<br/>/ utilization"]
    SK["compliance + audit"]
    S8["cosmos / blob / keyvault"]
  end
  ROUTERS --> MW
  R1 --> S8
  R2 --> S8
  R4 --> S4
  R5 --> S5 & S6 & S7 & S4
  R6 --> S4 & S8
  R10 --> S1 & S6 & SC
  R11 --> S5 & S8
  R12 --> S5 & S8
  R13 --> SO & S8
  R14 --> SK & S8
  R8 --> S1 & S3 & S8
  S4 --> S5
  S5 & S6 & S4 --> S7
"""

SEQ_INGEST = r"""sequenceDiagram
  participant SCH as ACA Scheduler
  participant JOB as Ingestion Job
  participant KV as Key Vault
  participant AZ as Azure APIs
  participant WE as Waste Engine
  participant DB as Cosmos DB
  SCH->>JOB: trigger 02:00 UTC
  loop for each active tenant
    JOB->>KV: get sp-creds-{tenant_id}
    KV-->>JOB: client_id / secret / tenant
    JOB->>AZ: Cost Management query (daily)
    AZ-->>JOB: columnar cost rows
    JOB->>AZ: Resource Graph (state + tags, bulk KQL)
    AZ-->>JOB: disk/IP/snapshot state + tags
    JOB->>JOB: parse (name-based), enrich w/ tags
    JOB->>DB: bulk upsert cost_records (TTL 90d)
    JOB->>WE: run 12 rules (asyncio.gather)
    WE-->>JOB: waste items (ranked by saving)
    JOB->>DB: upsert waste_items
    JOB->>DB: update tenant.last_ingested_at
  end
"""

SEQ_ONBOARD = r"""sequenceDiagram
  participant C as Customer
  participant OPS as CloudLens Ops
  participant API as CloudLens API
  participant KV as Key Vault
  participant DB as Cosmos DB
  C->>C: az ad sp create-for-rbac (Reader)
  C->>C: assign Cost Management Reader
  C->>OPS: share client_id / secret / tenant (secure)
  OPS->>KV: store sp-creds-{tenant_id}
  OPS->>API: POST /api/v1/tenants (X-API-Key)
  API->>API: validate SP format + email
  API->>KV: store SP creds (deterministic id)
  API->>DB: upsert tenant config
  API-->>OPS: 201 TenantConfig
  note over API,DB: First ingest at next 02:00 UTC,<br/>or POST /ingest/{id} on demand
"""

SEQ_REQUEST = r"""sequenceDiagram
  participant FE as Frontend SPA
  participant API as FastAPI
  participant RL as Rate limiter
  participant DB as Cosmos DB
  FE->>API: GET /api/v1/insights/{tid}/digest (Bearer)
  API->>API: verify JWT (JWKS) + tenant scope
  API->>RL: check per-tenant bucket
  alt within limit
    API->>DB: query cost/waste/tags
    DB-->>API: rows
    API->>API: fuse waste+anomaly+chargeback+forecast
    API-->>FE: 200 InsightDigest (ranked, bilingual)
  else exceeded
    API-->>FE: 429 Retry-After
  end
"""

STATE_WASTE = r"""stateDiagram-v2
  [*] --> Open: detected by waste engine
  Open --> Snoozed: PATCH resolve (snooze 30d)
  Snoozed --> Open: snooze expires
  Open --> Resolved: PATCH resolve (resolved_by)
  Snoozed --> Resolved: PATCH resolve
  Resolved --> [*]
  Open --> Open: re-detected next ingest (refreshed)
"""

STATE_REPORT = r"""stateDiagram-v2
  [*] --> pending: POST /reports/generate
  pending --> generating: background task starts
  generating --> ready: PDF built + uploaded to Blob
  generating --> failed: build/upload error
  ready --> [*]: download via 1h SAS URL
  failed --> [*]
"""

STATE_BUDGET = r"""stateDiagram-v2
  [*] --> ok: consumed < warning_threshold
  ok --> warning: consumed >= warning_threshold
  ok --> projected_breach: forecast >= 100%
  warning --> breach: consumed >= 100%
  warning --> projected_breach: forecast >= 100%
  projected_breach --> breach: consumed >= 100%
  breach --> ok: new month / budget raised
  warning --> ok: new month
  projected_breach --> ok: remediation lowers run-rate
"""

STATE_TENANT = r"""stateDiagram-v2
  [*] --> Active: POST /tenants (active=true)
  Active --> Active: nightly ingest
  Active --> Inactive: DELETE (soft, active=false)
  Inactive --> Active: PATCH active=true
  Inactive --> [*]: data retained (TTL on cost_records)
"""

CLASS_DIAGRAM = r"""classDiagram
  class TenantConfig {
    +str id
    +str tenant_name
    +list~str~ subscription_ids
    +PlanTier plan_tier
    +str alert_email
    +bool active
    +str sp_secret_ref
    +datetime last_ingested_at
    +to_cosmos()
    +from_cosmos()
  }
  class CostRecord {
    +str id
    +str tenant_id
    +date record_date
    +str service_name
    +str resource_id
    +str resource_group
    +float cost_eur
    +dict tags
    +int ttl
  }
  class WasteItem {
    +str id
    +str tenant_id
    +str resource_id
    +WasteType waste_type
    +float monthly_cost_eur
    +float saving_eur
    +Priority priority
    +str recommendation
    +str recommendation_it
    +dict evidence
    +datetime resolved_at
  }
  class ReportMeta {
    +str id
    +str tenant_id
    +date period_start
    +date period_end
    +float total_spend_eur
    +float total_waste_eur
    +ReportStatus status
    +str blob_url
  }
  class Budget {
    +str id
    +str tenant_id
    +str name
    +float amount_eur
    +str scope_dimension
    +str scope_value
    +int warning_threshold_pct
  }
  TenantConfig "1" --> "*" CostRecord : owns
  TenantConfig "1" --> "*" WasteItem : owns
  TenantConfig "1" --> "*" ReportMeta : owns
  TenantConfig "1" --> "*" Budget : owns
  CostRecord ..> WasteItem : analysed into
  WasteItem ..> ReportMeta : summarised in
"""

ERD = r"""erDiagram
  TENANTS ||--o{ COST_RECORDS : tenant_id
  TENANTS ||--o{ WASTE_ITEMS : tenant_id
  TENANTS ||--o{ REPORTS : tenant_id
  TENANTS ||--o{ BUDGETS : tenant_id
  TENANTS {
    string id PK
    string tenant_name
    string plan_tier
    bool active
  }
  COST_RECORDS {
    string id PK
    string tenant_id FK
    date record_date
    float cost_eur
    map tags
    int ttl "90d"
  }
  WASTE_ITEMS {
    string id PK
    string tenant_id FK
    string waste_type
    float saving_eur
    string priority
  }
  REPORTS {
    string id PK
    string tenant_id FK
    string status
    string blob_url
  }
  BUDGETS {
    string id PK
    string tenant_id FK
    float amount_eur
    string scope_dimension
  }
"""

DEPLOY_FLOW = r"""flowchart TB
  A["./deploy.sh prod"] --> B["0. Validate prereqs<br/>az / terraform / docker / jq"]
  B --> C["1. Bootstrap tfstate<br/>RG + storage account"]
  C --> D["2. az acr build<br/>push image :SHA + :latest"]
  D --> E["3. terraform apply<br/>provision all resources"]
  E --> F["4. Deploy frontend<br/>Static Web Apps"]
  F --> G["5. Smoke test<br/>GET /health"]
  G --> H{"healthy?"}
  H -->|yes| I["6. Summary<br/>API URL + next steps"]
  H -->|no| J["Rollback / inspect logs"]
"""

MULTICLOUD = r"""flowchart TB
  subgraph SRC["Provider billing sources"]
    AZ["Azure<br/>Cost Management"]
    AWS["AWS<br/>Cost Explorer / CUR"]
    GCP["GCP<br/>BigQuery export"]
    ALI["Alibaba<br/>BSS OpenAPI"]
    OCI["OCI<br/>Usage API"]
    AI["AI/LLM<br/>Bedrock / OpenAI / Anthropic"]
  end
  NORM["FOCUS normalizer<br/>billed / effective / list cost,<br/>service category, commitments, tags"]
  subgraph CORE["Provider-agnostic core"]
    ALLOC["Allocation engine<br/>100% via rule chain"]
    COMMIT["Commitment manager<br/>coverage / utilization / recs"]
    FORE["Forecast / anomaly / insights"]
  end
  AZ --> NORM
  AWS --> NORM
  GCP --> NORM
  ALI --> NORM
  OCI --> NORM
  AI --> NORM
  NORM -->|FocusRecord| ALLOC
  NORM -->|FocusRecord| COMMIT
  NORM -->|FocusRecord| FORE
"""

# (continued in build_docs_part2 — content sections)
OPTIMIZATION = r"""flowchart LR
  subgraph IN["Per-resource signals"]
    COST["billed cost"]
    CPU["CPU peak %"]
    MEM["memory peak %"]
    ENV["environment tag"]
  end
  subgraph ENG["Optimization engines"]
    UTIL["Utilization<br/>over-capacity score"]
    RIGHT["Rightsizing<br/>CPU+mem, cross-family"]
    SCHED["Scheduling<br/>non-prod on/off"]
  end
  LEDGER["Savings ledger<br/>identified to actioned to realized"]
  COST --> UTIL
  CPU --> UTIL
  MEM --> UTIL
  COST --> RIGHT
  CPU --> RIGHT
  MEM --> RIGHT
  ENV --> SCHED
  COST --> SCHED
  UTIL --> LEDGER
  RIGHT --> LEDGER
  SCHED --> LEDGER
"""

AUDIT_CHAIN = r"""flowchart LR
  E1["event 1<br/>tenant_created<br/>hash=H1"] -->|prev_hash=H1| E2["event 2<br/>budget_updated<br/>hash=H2"]
  E2 -->|prev_hash=H2| E3["event 3<br/>alert_rule_created<br/>hash=H3"]
  E3 -->|prev_hash=H3| E4["event 4<br/>evidence_exported<br/>hash=H4"]
  V["verify_chain()<br/>recompute every hash,<br/>check prev links"] -.->|intact?| E1
"""

RIGHTSIZE_FLOW = r"""flowchart TB
  A["Resource + CPU/mem peak + current SKU + cost"] --> B["required vCPU = current vCPU x cpu% x 1.3"]
  A --> C["required mem = current mem x mem% x 1.3"]
  B --> D{"both near zero?"}
  C --> D
  D -->|yes| T["recommend TERMINATE"]
  D -->|no| E["scan catalog cheapest-first"]
  E --> F{"cheapest SKU with<br/>vCPU and mem >= required<br/>and cheaper?"}
  F -->|found| G["recommend DOWNSIZE<br/>(may be cross-family)"]
  F -->|none| H["NO CHANGE (well-sized)"]
"""

DIAGRAMS = {
    "hld": HLD, "lld": LLD, "seq_ingest": SEQ_INGEST, "seq_onboard": SEQ_ONBOARD,
    "seq_request": SEQ_REQUEST, "state_waste": STATE_WASTE, "state_report": STATE_REPORT,
    "state_budget": STATE_BUDGET, "state_tenant": STATE_TENANT, "class": CLASS_DIAGRAM,
    "erd": ERD, "deploy": DEPLOY_FLOW, "multicloud": MULTICLOUD,
    "optimization": OPTIMIZATION, "audit": AUDIT_CHAIN, "rightsize": RIGHTSIZE_FLOW,
}
