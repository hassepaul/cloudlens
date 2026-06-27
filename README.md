# CloudLens — Azure FinOps Managed Service

Lightweight, cloud-native service that connects to customer Azure subscriptions
with read-only service principals, ingests cost data nightly, detects waste via
12 configurable rules, and surfaces recoverable spend through an API, a
drill-down dashboard, and monthly PDF reports.

## Layout
```
app/            FastAPI backend
  main.py         App entry, lifespan, middleware, exception handlers
  config.py       Pydantic settings (all config from env)
  auth.py         API-key + Azure AD bearer (JWKS) auth dependencies
  rate_limit.py   In-process per-tenant token-bucket limiter (no Redis)
  exceptions.py   12 typed domain exceptions → HTTP status codes
  models/         Pydantic v2 models (tenant, cost, waste, report)
  routers/        tenants, costs, waste, reports, ingest, health
  services/       azure_cost, resource_graph, waste_engine, cosmos,
                  blob, keyvault, report_builder  (bus = optional scale-out)
  jobs/ingest.py  Nightly Container Apps Job entrypoint
frontend/       Single-file Static Web App console (drill-down dashboard)
infra/          Terraform — flat config.tfvars per environment
tests/          55 tests (models, waste engine, routers, auth, rate limit)
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
| Anomaly Detection | Embedded in Explorer | Holt-Winters z-score detection with AI root-cause |
| Forecasting | `GET /api/v1/forecast/{tenant_id}` | Additive Holt-Winters + cost-of-inaction dual trajectory |
| Waste Detection | `GET /api/v1/waste/{tenant_id}` | 12 configurable waste rules |
| Multi-cloud | `GET /api/v1/multicloud/{tenant_id}` | Azure, AWS, GCP, Alibaba, OCI — FOCUS normalised |
| Real-time Streaming | `GET /api/v1/costs/{tenant_id}/stream` | SSE cost stream (15–300s interval) |

### Intelligence Layer (v2.1)

| Feature | Endpoint | Description |
|---------|----------|-------------|
| **Smart Commitment Advisor** | `GET /api/v1/commitment-advisor/{tenant_id}` | Calendar-aware RI/SP timing via Holt-Winters stability analysis. Outputs confidence score, trend direction, and `commit_now / wait` recommendation per service. |
| **Business Context Auto-Mapping** | `GET /api/v1/context/{tenant_id}/map` | Zero-config product/feature cost attribution by scanning resource tags and Kubernetes namespace patterns. |
| **FinOps Maturity Score** | `GET /api/v1/maturity/{tenant_id}/score` | 6-dimension scorecard (tagging, waste, RI coverage, unit economics, anomaly response, budget adherence) benchmarked against industry cohorts. |
| **Natural Language Cost Querying** | `POST /api/v1/nl-query/{tenant_id}` | LLM function-calling translates plain-English questions to Cosmos queries. Falls back to rule-based intent matching when no API key is set. |

### Governance

| Feature | Endpoint | Description |
|---------|----------|-------------|
| Policy Engine | `GET/POST /api/v1/policies/{tenant_id}/rules` | 8 condition types, 4 action types, cooldown, auto-evaluated on ingest |
| Cost Hierarchy | `GET /api/v1/hierarchy/{tenant_id}/rollup` | Company → BU → Team tree rollup with budget tracking |
| Budgets + Alerts | `/api/v1/budgets/`, `/api/v1/alerts/` | Budget thresholds and webhook/email alerts |
| Kubernetes | `GET /api/v1/k8s/{tenant_id}/workloads` | Namespace-level cost from OpenCost |
| Unit Economics | `GET /api/v1/unit-economics/{tenant_id}/metrics` | Cost per user/API call/transaction |

## New Intelligence APIs (v2.1)

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

