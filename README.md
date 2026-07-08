# CloudLens — Multi-Cloud FinOps Platform

Cloud-native FinOps platform that connects to customer Azure, AWS, and GCP
accounts with read-only credentials, ingests cost data nightly, detects waste
via 12+ configurable rules, and surfaces recoverable spend through an API, an
interactive dashboard, an AI agent, and monthly PDF reports.

## Layout
```
app/            FastAPI backend
  main.py         App entry, lifespan, middleware, exception handlers
  config.py       Pydantic settings (all config from env)
  auth.py         API-key + Azure AD bearer (JWKS) auth dependencies
  rate_limit.py   In-process per-tenant token-bucket limiter (no Redis)
  exceptions.py   12 typed domain exceptions → HTTP status codes
  models/         Pydantic v2 models (tenant, cost, waste, report, audit)
  routers/        tenants, costs, waste, reports, ingest, health,
                  forecast, multicloud, insights, optimization, hierarchy,
                  policies, budgets, alerts, k8s, unit_economics,
                  commitment_advisor, context_map, maturity, nl_query,
                  sustainability, agent, ai_analyst, genai_cost, bots,
                  commitment_purchaser, cost_estimate, drilldown, escalation,
                  fx, onboarding, terraform_sync, admin
  services/       azure_cost, resource_graph, waste_engine, cosmos,
                  blob, keyvault, report_builder, forecast, multicloud,
                  anomaly, chargeback, insights, compliance, policy_engine,
                  hierarchy, k8s_cost, unit_economics, commitment_advisor,
                  context_mapper, maturity, nl_query, sustainability,
                  ai_agent, ai_analyst, ai_briefing, genai_cost,
                  bot_notifications, commitment_purchaser, cost_estimator,
                  escalation, fx, onboarding, terraform_sync,
                  action_executor, realtime_ingest  (bus = optional scale-out)
  jobs/ingest.py  Nightly Container Apps Job entrypoint
frontend/       Multi-page Static Web App console — explorer, forecast,
                insights, optimization, multicloud, sustainability, maturity,
                commitment_advisor, genai, agent, cicd_costs, unit_economics,
                context_mapper, nl_query, onboarding, compliance_admin
infra/          Terraform — flat config.tfvars per environment
tests/          29 test modules (29 files, 300+ assertions)
.github/        CI/CD: backend (test→build→deploy) + infra (plan→approve→apply)
```

## Cheapest-infra design
- Container Apps **scale to zero** between requests/jobs — no idle compute cost
- Cosmos DB **serverless** — pay per request unit
- **No Service Bus** — the nightly job and manual trigger run inline; the queue
  module is retained only as an optional scale-out path
- Log Analytics with a **daily ingestion cap** to prevent runaway bills
- Static Web Apps **free tier** for the frontend
- ~€30–50/month at 10 tenants

## Run tests
```
pip install -r requirements.txt pytest pytest-asyncio httpx
INTERNAL_API_KEY=test AZURE_TENANT_ID=test AZURE_CLIENT_ID=test \
COSMOS_ENDPOINT=https://x.documents.azure.com STORAGE_ACCOUNT_NAME=x \
KEY_VAULT_NAME=x pytest tests/ -q
```

## Deploy
One script does everything — infra, image build/push, app + ingest job,
frontend, and a smoke test:
```
export TF_VAR_internal_api_key="$(openssl rand -hex 32)"
./deploy.sh prod        # or: dev | staging
```
Then onboard a tenant — see `TENANT_ONBOARDING.md` (PDF in the docs output).
The first cost ingest runs automatically at 02:00 UTC, or trigger it on-demand
via `POST /api/v1/ingest/<tenant_id>`.

## Features

### Cost Intelligence

| Feature | Endpoint | Description |
|---------|----------|-------------|
| Cost Explorer | `GET /api/v1/costs/{tenant_id}` | Drill-down from cloud → service → resource |
| Anomaly Detection | `GET /api/v1/insights/{tenant_id}/anomalies` | Holt-Winters z-score detection with AI root-cause |
| AI Cost Analyst | `GET /api/v1/insights/{tenant_id}/explain/{day}` | LLM root-cause explanation for spend anomalies with 7-day caching |
| Forecasting | `GET /api/v1/forecast/{tenant_id}` | Additive Holt-Winters + cost-of-inaction dual trajectory |
| Waste Detection | `GET /api/v1/waste/{tenant_id}` | 12 configurable waste rules |
| Multi-cloud | `GET /api/v1/multicloud/{tenant_id}` | Azure, AWS, GCP, Alibaba, OCI — FOCUS normalised |
| Real-time Streaming | `GET /api/v1/costs/{tenant_id}/stream` | SSE cost stream (15–300s interval) |
| Drill-down | `GET /api/v1/drilldown/{tenant_id}` | Walk the hierarchy: Portfolio → Provider → Account → Service → Resource |
| Sustainability | `GET /api/v1/sustainability/{tenant_id}/summary` | CO₂ emissions tracking across ~130 cloud regions (kgCO₂e per service/region) |

### Intelligence Layer (v2.1)

| Feature | Endpoint | Description |
|---------|----------|-------------|
| **Smart Commitment Advisor** | `GET /api/v1/commitment-advisor/{tenant_id}` | Calendar-aware RI/SP timing via Holt-Winters stability analysis. Outputs confidence score, trend direction, and `commit_now / wait` recommendation per service. |
| **Commitment Auto-Purchaser** | `POST /api/v1/commitment-purchaser/{tenant_id}/execute` | Safety-gated automated RI/SP purchasing with global kill switch, per-tenant caps, dry-run mode, and confidence threshold enforcement. |
| **Business Context Auto-Mapping** | `GET /api/v1/context/{tenant_id}/map` | Zero-config product/feature cost attribution by scanning resource tags and Kubernetes namespace patterns. |
| **FinOps Maturity Score** | `GET /api/v1/maturity/{tenant_id}/score` | 6-dimension scorecard (tagging, waste, RI coverage, unit economics, anomaly response, budget adherence) benchmarked against industry cohorts. |
| **Natural Language Cost Querying** | `POST /api/v1/nl-query/{tenant_id}` | LLM function-calling translates plain-English questions to Cosmos queries. Falls back to rule-based intent matching when no API key is set. |

### AI & Automation (v2.2)

| Feature | Endpoint | Description |
|---------|----------|-------------|
| **AI Agent** | `POST /api/v1/agent/{tenant_id}/chat` | Multi-turn conversational FinOps assistant with 14 tools (12 auto-execute, 2 approval-gated). Streaming SSE, session history, daily briefing. |
| **AI Daily Briefing** | `GET /api/v1/agent/{tenant_id}/briefing` | Proactive on-demand FinOps briefing: anomalies, waste delta, budget risk, and top commitment opportunity. |
| **GenAI Cost Intelligence** | `GET /api/v1/genai/{tenant_id}/summary` | Track LLM API spend across OpenAI, Azure OpenAI, AWS Bedrock, and GCP Vertex AI. Model comparison, daily trends, token budgets. |

### Governance

| Feature | Endpoint | Description |
|---------|----------|-------------|
| Policy Engine | `GET/POST /api/v1/policies/{tenant_id}/rules` | 8 condition types, 4 action types, cooldown, auto-evaluated on ingest |
| Cost Hierarchy | `GET /api/v1/hierarchy/{tenant_id}/rollup` | Company → BU → Team tree rollup with budget tracking |
| Budgets + Alerts | `/api/v1/budgets/`, `/api/v1/alerts/` | Budget thresholds and webhook/email alerts |
| Kubernetes | `GET /api/v1/k8s/{tenant_id}/workloads` | Namespace-level cost from OpenCost |
| Unit Economics | `GET /api/v1/unit-economics/{tenant_id}/metrics` | Cost per user/API call/transaction |
| Compliance & Audit | `GET /api/v1/admin/audit` | SOC 2-aligned tamper-evident audit chain with control matrix and evidence export |
| Escalation Integrations | `/api/v1/escalation/{tenant_id}/integrations` | Route alerts to PagerDuty, Jira, Azure DevOps, and Microsoft Teams |
| Bot Integrations | `/api/v1/bots/{tenant_id}/slack/events` | Slack (Events API, slash commands) and Teams chatbot for spend queries and budget alerts |

### Developer Platform

| Feature | Endpoint | Description |
|---------|----------|-------------|
| Terraform Cost Estimator | `POST /api/v1/estimate/terraform` | Parse `terraform show -json` output → per-resource monthly cost estimate + CI/CD budget gate (AWS, Azure, GCP) |
| Terraform Drift Management | `GET /api/v1/terraform/{tenant_id}/drift` | Track autonomous execution drift; generates HCL snippets and `terraform import` commands for reconciliation |
| Self-Service Onboarding | `POST /api/v1/onboarding/provision` | Credential validation + automated tenant provisioning for Azure SP, AWS cross-account role, and GCP service account |
| FX / Currency | `GET /api/v1/fx/rates` | ECB reference rates for 20+ currencies; all stored values are EUR and converted on the way out |

## API Reference

### Intelligence APIs (v2.1)

### Smart Commitment Advisor
```
GET /api/v1/commitment-advisor/{tenant_id}?lookback_days=90
POST /api/v1/commitment-advisor/{tenant_id}
  body: {"lookback_days": 90, "planned_events": [{"date": "2025-06-01", "description": "Migration"}]}
```

The advisor analyses 90 days of on-demand FOCUS records per service, fits
Holt-Winters to detect stability and trend, and outputs:
- `confidence_score` (0–1) — combined stability, trend risk, and event-free horizon
- `timing` — `commit_now` (conf ≥ 0.70) or `wait N months`
- `stability_score`, `trend_direction`, `trend_pct_30d`
- `calendar_notes` — observed weekday patterns
- `estimated_monthly_saving_eur` using conservative no-upfront discount rates

### Business Context Auto-Mapping
```
GET /api/v1/context/{tenant_id}/map?lookback_days=30
GET /api/v1/context/{tenant_id}/products
GET /api/v1/context/{tenant_id}/features
```

Automatically attributes spend to product lines by scanning tags
(`product`, `app`, `application`, `service`, `component`) and inferring from
Kubernetes namespace patterns (`namespaces/{ns}/...`) and resource name slugs.
Returns `attribution_pct` and per-product cost + feature breakdown.

### FinOps Maturity Score
```
GET /api/v1/maturity/{tenant_id}/score?vertical=saas
```
Valid verticals: `saas`, `enterprise`, `ecommerce`, `startup`.

Scores across 6 weighted dimensions (0–100 each):

| Dimension | Weight | Benchmark source |
|-----------|--------|-----------------|
| Tagging completeness | 20% | % records with required tag |
| Waste ratio (inverted) | 20% | open waste / total spend |
| RI/SP coverage | 15% | committed / eligible spend |
| Unit economics adoption | 15% | metrics defined + recent data |
| Anomaly response time | 15% | avg hours violation → resolution |
| Budget adherence | 15% | % budgets not in breach |

Returns `overall_label` (Crawl/Walk/Run/Fly), `overall_percentile` vs industry
cohort, dimension-level evidence, and a `top_recommendation`.

### Natural Language Cost Querying
```
POST /api/v1/nl-query/{tenant_id}
  body: {"question": "Which service cost the most last month?"}
```

**LLM path** (when `openai_api_key` is set): Uses OpenAI function-calling to
select the right Cosmos query, executes it server-side, then asks the LLM to
narrate the result. The LLM never touches Cosmos — zero SQL injection risk.

**Rule-based fallback**: Regex intent matching → same query functions → same
API response shape. No code changes needed when upgrading to LLM later.

Available intents: `top_services`, `by_cloud`, `trend`, `compare`, `top_resources`.

Returns `{question, intent, chart_type, chart_data, narrative, confidence, suggestions}`.

### Sustainability (CO₂ Emissions)
```
GET /api/v1/sustainability/{tenant_id}/summary?lookback_days=30
GET /api/v1/sustainability/{tenant_id}/by-region?lookback_days=30&top_n=10
GET /api/v1/sustainability/{tenant_id}/by-service?lookback_days=30&top_n=10
GET /api/v1/sustainability/{tenant_id}/trend?days=30
```

Estimates carbon emissions from cloud spend using the formula:
```
kgCO₂e = effective_cost × kWh/USD[svc_category] × PUE(1.13) × grid_gCO₂e_per_kWh[region] / 1000
```
~130 cloud regions mapped across Azure, AWS, and GCP. Returns emissions by
cloud, region, and service, plus daily trend and total `estimated_kgco2e`.

### AI Agent
```
POST /api/v1/agent/{tenant_id}/chat
  body: {"message": "...", "session_id": "optional"}
POST /api/v1/agent/{tenant_id}/stream          — SSE streaming response
POST /api/v1/agent/{tenant_id}/approve/{action_id}
GET  /api/v1/agent/{tenant_id}/history
GET  /api/v1/agent/{tenant_id}/history/{session_id}
DELETE /api/v1/agent/{tenant_id}/history/{session_id}
GET  /api/v1/agent/{tenant_id}/briefing        — on-demand daily briefing
```

Agentic conversational layer: natural-language input → tool planning →
multi-step execution → structured response with narrative, charts, and
approval-gated action proposals.

- **14 tools**: 12 read-only (auto-execute) + 2 write operations (approval-gated)
- The LLM never touches Cosmos — all data access is through scoped tool handlers
- Session memory stored in Cosmos, scoped to `tenant_id` (cross-tenant access structurally impossible)
- `pending_actions` returned for user approval before any write executes
- Graceful degradation: rule-based intent routing when no `openai_api_key` is set

Returns `{session_id, turn_id, reply, chart_data, metric_cards, pending_actions, suggestions, tools_used}`.

### AI Cost Analyst
```
GET  /api/v1/insights/{tenant_id}/explain/{day}   — YYYY-MM-DD
POST /api/v1/insights/{tenant_id}/explain         — pre-computed AnomalyContext
```

LLM-powered root-cause explanation for spend anomalies. Runs Holt-Winters
detection, gathers service/resource/tag deltas, and asks the LLM to narrate
the cause. Responses are cached in Cosmos for 7 days.

Fallback when `openai_api_key` is unset: deterministic rule-based explanation
with identical API shape.

Returns `{explanation, confidence, factors, action_recommendation, generated_by}`.

### AI Daily Briefing
```
GET /api/v1/agent/{tenant_id}/briefing
```

Proactive on-demand (or scheduled nightly) FinOps briefing. Gathers in parallel:
anomalies (last 24 h), waste inventory delta, budget utilisation warnings,
top commitment opportunity, and maturity score. Returns structured `cards` per
category plus a single `top_action`.

### GenAI Cost Intelligence
```
POST /api/v1/genai/{tenant_id}/usage           — ingest one usage record
POST /api/v1/genai/{tenant_id}/usage/batch     — ingest an array
GET  /api/v1/genai/{tenant_id}/summary
GET  /api/v1/genai/{tenant_id}/models          — per-model stats + efficiency
GET  /api/v1/genai/{tenant_id}/trends          — daily spend by model/provider/app
GET  /api/v1/genai/{tenant_id}/apps            — top apps by GenAI spend
POST /api/v1/genai/{tenant_id}/budgets         — create a token budget alert
GET  /api/v1/genai/{tenant_id}/budgets
DELETE /api/v1/genai/{tenant_id}/budgets/{id}
GET  /api/v1/genai/{tenant_id}/alerts          — budgets in warning (≥80%) or breach
GET  /api/v1/genai/{tenant_id}/pricing         — built-in pricing table
```

Supported providers: `openai`, `azure_openai`, `bedrock`, `vertex_ai`, `custom`.
All costs stored in USD (source-of-truth) and converted to EUR via ECB FX rate.

### Terraform Cost Estimator
```
POST /api/v1/estimate/terraform              — stateless parse + estimate
POST /api/v1/estimate/terraform/record       — parse + persist + drift delta
GET  /api/v1/estimate/catalog                — supported resource type catalog
GET  /api/v1/estimate/runs/{tenant_id}       — pipeline run history
POST /api/v1/estimate/gate                   — budget gate check (no auth)
```

Accepts `terraform show -json <plan-file>` output. Returns per-resource
monthly cost estimates (EUR) across AWS, Azure, and GCP using a built-in
approximate pricing catalog. The `/gate` endpoint returns `pass/fail` against
a `budget_gate_eur` threshold — designed for CI/CD PR checks.

### Terraform Drift Management
```
GET    /api/v1/terraform/{tenant_id}/drift               — list (filter by status)
GET    /api/v1/terraform/{tenant_id}/drift/summary       — KPI counts by status
GET    /api/v1/terraform/{tenant_id}/drift/{record_id}   — full HCL + import cmd
POST   /api/v1/terraform/{tenant_id}/drift/{id}/acknowledge
POST   /api/v1/terraform/{tenant_id}/drift/{id}/imported
DELETE /api/v1/terraform/{tenant_id}/drift/{id}
```

Every resource provisioned by the AI agent is tagged and recorded as a drift
record. Engineers get a ready-to-paste HCL block and the exact `terraform import`
command. Status lifecycle: `pending → acknowledged → imported`.

### Commitment Auto-Purchaser
```
GET  /api/v1/commitment-purchaser/{tenant_id}/settings
PUT  /api/v1/commitment-purchaser/{tenant_id}/settings
POST /api/v1/commitment-purchaser/{tenant_id}/execute
GET  /api/v1/commitment-purchaser/{tenant_id}/history
```

**Safety gates** (both must be true before any purchase executes):
1. Global kill switch: `COMMITMENT_AUTO_PURCHASE_ENABLED=true` env var
2. Per-tenant `enabled` flag in settings

Settings include `dry_run` (default `true`), `max_single_purchase_eur`,
`max_monthly_budget_eur`, `min_confidence_score` (default 0.70), and
`allowed_commitment_types`. Calling `POST /execute` with either gate closed
returns HTTP 403 — it never silently succeeds.

### Self-Service Onboarding
```
POST /api/v1/onboarding/validate-credentials   (no auth — rate-limited at LB)
POST /api/v1/onboarding/provision              (requires internal API key)
```

Validates cloud credentials before provisioning:
- **Azure**: acquires AAD token, verifies Cost Management Reader on each subscription
- **AWS**: validates role ARN format + generates ExternalId with trust policy
- **GCP**: validates service account JSON structure and required fields

On success, stores credentials in Key Vault, writes tenant document to Cosmos,
and returns the `tenant_id` ready for an initial ingest trigger.

### Escalation Integrations
```
GET    /api/v1/escalation/{tenant_id}/integrations
GET    /api/v1/escalation/{tenant_id}/integrations/{channel_type}
PUT    /api/v1/escalation/{tenant_id}/integrations/{channel_type}
DELETE /api/v1/escalation/{tenant_id}/integrations/{channel_type}
POST   /api/v1/escalation/{tenant_id}/integrations/{channel_type}/test
```

Channel types: `pagerduty`, `jira`, `ado`, `teams`.
Credentials (API tokens, routing keys) are stored in Key Vault — only the
secret name is referenced in Cosmos. Use the `/test` endpoint to send a
verification event before enabling.

### Bot Integrations
```
POST /api/v1/bots/{tenant_id}/slack/events      — Slack Events API
POST /api/v1/bots/{tenant_id}/slack/command     — Slack slash commands
POST /api/v1/bots/{tenant_id}/teams/message     — Teams Bot Framework
POST /api/v1/bots/{tenant_id}/notify/budget     — push budget breach notification
POST /api/v1/bots/{tenant_id}/notify/spend      — push spend summary
GET  /api/v1/bots/{tenant_id}/channels          — list configured channels
```

Slack: handles `url_verification`, `app_mention`, message events, and
sign-verified slash commands with Block Kit responses (< 3 s Slack timeout).
Teams: routes `spend`, `budget`, and `status` commands to CloudLens data.

### FX / Currency
```
GET /api/v1/fx/rates?currencies=USD,GBP,CHF    — ECB reference rates (base EUR)
GET /api/v1/fx/convert?amount=100&currency=USD  — convert EUR amount
```

Rates sourced from the European Central Bank (refreshed hourly).
20+ currencies supported. All CloudLens monetary values are stored in EUR and
converted on the way out.

### Admin & Compliance
```
GET  /api/v1/admin/audit?tenant_id=...&limit=100   — tamper-evident audit log
GET  /api/v1/admin/compliance/controls              — SOC 2 control matrix
GET  /api/v1/admin/compliance/verify-chain          — audit chain integrity check
GET  /api/v1/admin/compliance/evidence-pack         — compliance evidence export
```

Operator-only (internal API key). Every security and change event is written
as a chained audit record (each record's `record_hash` includes the previous
record's hash). The evidence pack includes CLI proof commands for auditors.

