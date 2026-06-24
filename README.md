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
