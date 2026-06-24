# CloudLens — Tenant Onboarding Guide
**Onboarding di un nuovo cliente · Guida operativa**

Version 1.0 · June 2026 · CONFIDENTIAL

Onboarding a new customer takes **under 20 minutes** and requires **zero code
changes**. The whole process is configuration-driven. The customer creates a
read-only service principal in *their own* Azure AD — CloudLens never receives
Owner or Contributor access to their subscription.

> L'onboarding di un nuovo cliente richiede **meno di 20 minuti** e **nessuna
> modifica al codice**. Il cliente crea un service principal in sola lettura nel
> *proprio* Azure AD — CloudLens non riceve mai accesso Owner o Contributor.

---

## At a glance / In sintesi

| Step | Who | Action | Time |
|------|-----|--------|------|
| 1 | Customer | Create an App Registration (service principal) | 2 min |
| 2 | Customer | Assign **Reader** + **Cost Management Reader** on the subscription | 3 min |
| 3 | Customer | Share `client_id`, `client_secret`, `tenant_id` securely | 2 min |
| 4 | CloudLens ops | Store the SP credentials in Key Vault | 2 min |
| 5 | CloudLens ops | `POST /api/v1/tenants` to register the tenant | 1 min |
| 6 | System | Validates the SP, stores config to Cosmos | < 1 min |
| 7 | System | First ingest runs at the next nightly cycle (or on-demand) | ~5 min |

---

## Security model / Modello di sicurezza

CloudLens is **read-only by architecture**. The service principal you create is
granted only two built-in Azure roles, both of which are read-only:

- **Reader** — list resources and read their configuration/state.
- **Cost Management Reader** — read cost and usage data.

There is no path by which CloudLens can modify, delete, or create anything in
your subscription. Even if CloudLens itself were compromised, the leaked
credentials could only *read* cost and resource metadata.

> CloudLens è **read-only per architettura**. Il service principal riceve solo
> due ruoli Azure integrati, entrambi in sola lettura (Reader e Cost Management
> Reader). Non esiste alcun modo per CloudLens di modificare, eliminare o creare
> risorse nella sottoscrizione del cliente.

---

## Customer steps / Passi a carico del cliente

### Step 1 — Create the service principal

The customer runs this in **their own** Azure tenant (Cloud Shell or local CLI):

```bash
# Create an app registration + service principal scoped to the subscription
az ad sp create-for-rbac \
  --name "cloudlens-readonly" \
  --role "Reader" \
  --scopes "/subscriptions/<SUBSCRIPTION_ID>"
```

This prints:

```json
{
  "appId":       "<client_id>",
  "password":    "<client_secret>",
  "tenant":      "<tenant_id>",
  "displayName": "cloudlens-readonly"
}
```

> Il cliente esegue il comando nel **proprio** tenant Azure. L'output contiene
> `appId` (client_id), `password` (client_secret) e `tenant` (tenant_id).

### Step 2 — Add the Cost Management Reader role

`create-for-rbac` grants **Reader**; add the cost role as well:

```bash
az role assignment create \
  --assignee "<client_id>" \
  --role "Cost Management Reader" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"
```

> `create-for-rbac` assegna solo **Reader**; aggiungere anche **Cost Management
> Reader** con il comando sopra.

### Step 3 — Share the credentials securely

Send `client_id`, `client_secret`, and `tenant_id` to CloudLens through a secure
channel — a one-time secret link (1Password Send, Bitwarden Send) or an
encrypted message. **Never** email the secret in plaintext.

> Inviare le credenziali tramite canale sicuro (link segreto monouso o messaggio
> cifrato). **Mai** inviare il secret in chiaro via email.

---

## CloudLens ops steps / Passi a carico di CloudLens

### Step 4 — Store the credentials in Key Vault

The tenant ID below is the **CloudLens-internal** tenant identifier (a UUID you
choose for this customer), not the customer's Azure AD tenant. Use the same UUID
in Step 5.

```bash
TENANT_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"

az keyvault secret set \
  --vault-name "kv-cloudlens-prod" \
  --name "sp-creds-${TENANT_ID}" \
  --value "$(jq -nc \
      --arg cid "<client_id>" \
      --arg sec "<client_secret>" \
      --arg tid "<customer_tenant_id>" \
      '{client_id:$cid, client_secret:$sec, azure_tenant_id:$tid}')"

echo "Use this tenant_id when registering: ${TENANT_ID}"
```

The secret value is JSON with exactly these keys:

```json
{ "client_id": "...", "client_secret": "...", "azure_tenant_id": "..." }
```

> Il `TENANT_ID` qui è l'identificativo **interno CloudLens** (un UUID scelto per
> questo cliente), non il tenant Azure AD del cliente. Il valore del secret è un
> JSON con le chiavi `client_id`, `client_secret`, `azure_tenant_id`.

### Step 5 — Register the tenant via the API

```bash
API_URL="https://<api-fqdn>"

curl -X POST "${API_URL}/api/v1/tenants" \
  -H "X-API-Key: ${INTERNAL_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "id":               "<TENANT_ID from Step 4>",
    "tenant_name":      "Acme Manufacturing SpA",
    "subscription_ids": ["<SUBSCRIPTION_ID>"],
    "plan_tier":        "growth",
    "alert_email":      "finops@acme.example",
    "active":           true
  }'
```

`plan_tier` is one of `starter`, `growth`, `enterprise`. The API validates that
each `subscription_id` is a well-formed UUID and that `alert_email` is valid,
then writes the tenant config to Cosmos DB.

> `plan_tier` può essere `starter`, `growth` o `enterprise`. L'API valida il
> formato di `subscription_ids` ed `alert_email`, poi salva la configurazione in
> Cosmos DB.

### Step 6 — (Optional) Trigger the first ingest now

The nightly job picks up the new tenant automatically at 02:00 UTC. To populate
the dashboard immediately:

```bash
curl -X POST "${API_URL}/api/v1/ingest/<TENANT_ID>" \
  -H "X-API-Key: ${INTERNAL_API_KEY}"
```

This runs the full ingest inline: pull costs → collect resource state via
Resource Graph → run the 12 waste rules → persist results. Allow ~5 minutes for
a 30-day lookback.

> Il job notturno acquisisce il nuovo tenant automaticamente alle 02:00 UTC. Per
> popolare subito la dashboard, usare il trigger manuale (richiede ~5 minuti).

### Step 7 — Verify

```bash
# Confirm the tenant exists
curl "${API_URL}/api/v1/tenants/<TENANT_ID>" -H "X-API-Key: ${INTERNAL_API_KEY}"

# Once ingest has run, confirm cost data and waste findings
curl "${API_URL}/api/v1/costs/<TENANT_ID>"  -H "Authorization: Bearer <jwt>"
curl "${API_URL}/api/v1/waste/<TENANT_ID>"  -H "Authorization: Bearer <jwt>"
```

The customer can now sign in to the frontend console and drill from their
portfolio total down to individual wasteful resources.

> Il cliente può ora accedere alla console frontend e navigare dal totale di
> portfolio fino alle singole risorse che generano sprechi.

---

## Troubleshooting / Risoluzione problemi

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `401` on `/tenants` | Wrong or missing `X-API-Key` | Use the `internal_api_key` set at deploy time |
| `409 CONFLICT` on create | A tenant with that `tenant_name` already exists | Use a unique name, or PATCH the existing tenant |
| `422` on create | Invalid `subscription_id` (not a UUID) or bad email | Check the payload format |
| Ingest runs but no cost data | SP missing **Cost Management Reader** | Re-run Step 2 |
| Ingest fails with auth error | Wrong `client_secret`, or secret expired | Rotate the SP secret and update Key Vault (Step 4) |
| Waste findings empty but costs present | Resource Graph access blocked | Ensure the SP has **Reader** at subscription scope |

### Rotating a service principal secret

SP secrets expire (default 1 year). To rotate without downtime:

```bash
# Customer regenerates the secret
az ad sp credential reset --id "<client_id>"

# CloudLens ops updates Key Vault with the new secret (same secret name)
az keyvault secret set --vault-name "kv-cloudlens-prod" \
  --name "sp-creds-<TENANT_ID>" \
  --value "$(jq -nc --arg cid '<client_id>' --arg sec '<new_secret>' \
             --arg tid '<customer_tenant_id>' \
             '{client_id:$cid, client_secret:$sec, azure_tenant_id:$tid}')"
```

The next ingest automatically picks up the new secret — no redeploy needed.

> I secret dei service principal scadono (default 1 anno). Per ruotarli senza
> downtime: il cliente rigenera il secret, CloudLens aggiorna Key Vault con lo
> stesso nome di secret. Il prossimo ingest usa automaticamente il nuovo secret.

---

## Offboarding / Disattivazione

To stop monitoring a tenant without deleting its history:

```bash
curl -X DELETE "${API_URL}/api/v1/tenants/<TENANT_ID>" \
  -H "X-API-Key: ${INTERNAL_API_KEY}"
```

This is a **soft delete** — it sets `active=false`, so the nightly job skips the
tenant but all historical cost and waste data is preserved. The customer should
also delete the `cloudlens-readonly` service principal on their side.

> Per smettere di monitorare un tenant senza eliminarne lo storico, usare DELETE
> (soft-delete: imposta `active=false`). Il cliente dovrebbe inoltre eliminare il
> service principal `cloudlens-readonly` dal proprio lato.
