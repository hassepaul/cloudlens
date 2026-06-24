# CloudLens Frontend

Single-file FinOps console for CloudLens. Deploys to Azure Static Web Apps (free tier).

## Drill-down hierarchy
Portfolio → Tenant → Service → Resource Group → Resource → Waste finding

Each level shows spend, recoverable amount, and a waste-ratio bar. The pinned
breadcrumb "spine" lets you jump back to any level. The lowest level opens a
detail drawer with evidence, bilingual (EN/IT) recommendations, resource tags,
and resolve / snooze actions.

## Wiring to the API
`index.html` ships with mock data (`MOCK.*`) shaped to the exact API contract.
To go live, replace the mock arrays with fetch() calls — the render functions
need no changes:

| UI level        | Endpoint                                              |
|-----------------|-------------------------------------------------------|
| Portfolio       | `GET /api/v1/tenants`                                  |
| Tenant          | `GET /api/v1/costs/{tid}/breakdown?dimension=service` |
| Service         | `GET /api/v1/costs/{tid}/breakdown?dimension=resource_group` |
| Resource group  | `GET /api/v1/costs/{tid}` (filtered)                  |
| Resource        | `GET /api/v1/waste/{tid}`                             |
| Resolve action  | `PATCH /api/v1/waste/{id}/resolve`                    |

Auth: the SPA acquires an Azure AD bearer token (MSAL) and sends it as
`Authorization: Bearer <jwt>`; the backend enforces tenant scope from the token.

## Local preview
Open `index.html` in any browser — no build step, no dependencies.
