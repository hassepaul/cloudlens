#!/usr/bin/env bash
#
# CloudLens — single end-to-end deploy, setup & configuration script.
#
# Provisions all Azure infrastructure, builds and pushes the container image,
# deploys the API + nightly ingest job, deploys the frontend, and runs a smoke
# test. Idempotent: safe to re-run. Designed for the cheapest-infra topology
# (scale-to-zero Container Apps, serverless Cosmos, no Service Bus).
#
# USAGE:
#   ./deploy.sh <env>
#       env = dev | staging | prod   (default: prod)
#
# PREREQUISITES (the script checks these and exits if missing):
#   - az CLI (logged in:  az login)        - docker
#   - terraform >= 1.9                      - an Azure subscription with Owner/Contributor + UAA
#
# SECRETS (must be exported before running — never stored in tfvars):
#   export TF_VAR_internal_api_key="$(openssl rand -hex 32)"
#
# WHAT IT DOES, IN ORDER:
#   0. Validate prerequisites and inputs
#   1. Bootstrap the Terraform remote-state backend (resource group + storage)
#   2. Build & push the container image to ACR  (creates ACR first if needed)
#   3. terraform init + apply  (provisions everything, wires the image)
#   4. Deploy the frontend to Azure Static Web Apps
#   5. Smoke test  (GET /api/v1/health)
#   6. Print a summary with the API URL and next steps
#
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
ENVIRONMENT="${1:-prod}"
IMAGE_NAME="cloudlens-api"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/infra"
FRONTEND_DIR="${SCRIPT_DIR}/frontend"
TFVARS="environments/${ENVIRONMENT}.tfvars"

# Colours
RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLU=$'\e[34m'; BLD=$'\e[1m'; RST=$'\e[0m'
say()  { echo "${BLU}${BLD}▸ $*${RST}"; }
ok()   { echo "${GRN}  ✓ $*${RST}"; }
warn() { echo "${YEL}  ! $*${RST}"; }
die()  { echo "${RED}${BLD}✗ $*${RST}" >&2; exit 1; }

# ──────────────────────────────────────────────────────────────────────────────
# 0. Validate
# ──────────────────────────────────────────────────────────────────────────────
say "0/6  Validating prerequisites"

case "$ENVIRONMENT" in
  dev|staging|prod) ;;
  *) die "Invalid environment '$ENVIRONMENT' — use dev | staging | prod" ;;
esac

command -v az        >/dev/null || die "az CLI not found — install the Azure CLI"
command -v terraform >/dev/null || die "terraform not found — install Terraform >= 1.9"
command -v docker    >/dev/null || die "docker not found — install Docker"
command -v jq        >/dev/null || die "jq not found — install jq"

[[ -f "${INFRA_DIR}/${TFVARS}" ]] || die "Missing ${INFRA_DIR}/${TFVARS}"
[[ -n "${TF_VAR_internal_api_key:-}" ]] || \
  die "TF_VAR_internal_api_key is not set. Run:  export TF_VAR_internal_api_key=\"\$(openssl rand -hex 32)\""

# Confirm az login + capture subscription/tenant
ACCOUNT_JSON="$(az account show 2>/dev/null)" || die "Not logged in. Run:  az login"
SUBSCRIPTION_ID="$(echo "$ACCOUNT_JSON" | jq -r '.id')"
AAD_TENANT_ID="$(echo "$ACCOUNT_JSON"   | jq -r '.tenantId')"
ok "Azure subscription: ${SUBSCRIPTION_ID}"
ok "Azure AD tenant:    ${AAD_TENANT_ID}"

# Read resource names from the tfvars (single source of truth)
tfvar() { grep -E "^\s*$1\s*=" "${INFRA_DIR}/${TFVARS}" | sed -E 's/.*=\s*"?([^"]*)"?\s*$/\1/' | tr -d '[:space:]'; }
RG_NAME="$(tfvar resource_group_name)"
ACR_NAME="$(tfvar acr_name)"
LOCATION="$(tfvar location)"; LOCATION="${LOCATION:-italynorth}"
[[ -n "$RG_NAME" && -n "$ACR_NAME" ]] || die "Could not parse resource_group_name / acr_name from ${TFVARS}"

# State backend names (derived, deterministic)
STATE_RG="rg-cloudlens-tfstate"
STATE_SA="stcllens$(echo "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-12)"   # globally-unique-ish
STATE_CONTAINER="tfstate"
IMAGE_TAG="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)"

echo
say "Deploying CloudLens → ${BLD}${ENVIRONMENT}${RST}${BLU}  (image tag: ${IMAGE_TAG})${RST}"
echo

# ──────────────────────────────────────────────────────────────────────────────
# 1. Bootstrap Terraform remote-state backend
# ──────────────────────────────────────────────────────────────────────────────
say "1/6  Bootstrapping Terraform state backend"

az group create -n "$STATE_RG" -l "$LOCATION" -o none
ok "state resource group: ${STATE_RG}"

if ! az storage account show -n "$STATE_SA" -g "$STATE_RG" -o none 2>/dev/null; then
  az storage account create -n "$STATE_SA" -g "$STATE_RG" -l "$LOCATION" \
    --sku Standard_LRS --encryption-services blob --min-tls-version TLS1_2 -o none
fi
SA_KEY="$(az storage account keys list -n "$STATE_SA" -g "$STATE_RG" --query '[0].value' -o tsv)"
az storage container create -n "$STATE_CONTAINER" --account-name "$STATE_SA" \
  --account-key "$SA_KEY" -o none
ok "state storage: ${STATE_SA}/${STATE_CONTAINER}"

# ──────────────────────────────────────────────────────────────────────────────
# 2. Build & push the image (ACR is created here so it exists before apply)
# ──────────────────────────────────────────────────────────────────────────────
say "2/6  Building & pushing container image"

az group create -n "$RG_NAME" -l "$LOCATION" -o none
if ! az acr show -n "$ACR_NAME" -o none 2>/dev/null; then
  az acr create -n "$ACR_NAME" -g "$RG_NAME" --sku Basic --admin-enabled false -o none
  ok "created ACR: ${ACR_NAME}"
fi

# Build remotely in ACR (no local docker daemon needed; cheaper than self-hosted)
az acr build --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${IMAGE_TAG}" \
  --image "${IMAGE_NAME}:latest" \
  --file "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}" -o none
ok "pushed ${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG} (+ :latest)"

# ──────────────────────────────────────────────────────────────────────────────
# 3. Terraform init + apply
# ──────────────────────────────────────────────────────────────────────────────
say "3/6  Provisioning infrastructure with Terraform"

pushd "$INFRA_DIR" >/dev/null

terraform init -upgrade \
  -backend-config="resource_group_name=${STATE_RG}" \
  -backend-config="storage_account_name=${STATE_SA}" \
  -backend-config="container_name=${STATE_CONTAINER}" \
  -backend-config="key=cloudlens-${ENVIRONMENT}.tfstate" \
  -input=false

terraform apply -auto-approve -input=false \
  -var-file="${TFVARS}" \
  -var="backend_image_tag=${IMAGE_TAG}"

# Capture outputs
API_URL="$(terraform output -raw api_url)"
API_FQDN="$(terraform output -raw api_fqdn)"
MI_CLIENT_ID="$(terraform output -raw managed_identity_client_id)"
ACA_NAME="$(terraform output -raw container_app_name)"
KV_NAME="$(terraform output -raw key_vault_name)"
popd >/dev/null

ok "infrastructure applied"
ok "API:           ${API_URL}"
ok "managed id:    ${MI_CLIENT_ID}"
ok "API image:     ${IMAGE_TAG} (pinned by terraform apply)"

# ──────────────────────────────────────────────────────────────────────────────
# 4. Deploy frontend to Azure Static Web Apps
# ──────────────────────────────────────────────────────────────────────────────
say "4/6  Deploying frontend"

SWA_NAME="swa-cloudlens-${ENVIRONMENT}"
if ! az staticwebapp show -n "$SWA_NAME" -g "$RG_NAME" -o none 2>/dev/null; then
  az staticwebapp create -n "$SWA_NAME" -g "$RG_NAME" -l "westeurope" --sku Free -o none
fi
SWA_TOKEN="$(az staticwebapp secrets list -n "$SWA_NAME" -g "$RG_NAME" \
  --query 'properties.apiKey' -o tsv)"

if command -v swa >/dev/null; then
  swa deploy "$FRONTEND_DIR" --deployment-token "$SWA_TOKEN" --env production || \
    warn "swa deploy failed — upload ${FRONTEND_DIR}/index.html manually in the portal"
else
  warn "SWA CLI not installed. Deploy the frontend with:"
  warn "  npm i -g @azure/static-web-apps-cli"
  warn "  swa deploy ${FRONTEND_DIR} --deployment-token <token> --env production"
fi
SWA_URL="$(az staticwebapp show -n "$SWA_NAME" -g "$RG_NAME" --query 'defaultHostname' -o tsv 2>/dev/null || echo '')"
[[ -n "$SWA_URL" ]] && ok "frontend: https://${SWA_URL}"

# ──────────────────────────────────────────────────────────────────────────────
# 5. Smoke test
# ──────────────────────────────────────────────────────────────────────────────
say "5/6  Smoke testing the API"

HEALTH_OK=false
for i in $(seq 1 12); do
  if curl -fsS "${API_URL}/api/v1/health" 2>/dev/null | grep -q '"status"'; then
    HEALTH_OK=true; break
  fi
  sleep 5
done
if $HEALTH_OK; then
  ok "health check passed"
else
  warn "health check did not pass within 60s — the app may still be cold-starting"
  warn "check logs:  az containerapp logs show -n ${ACA_NAME} -g ${RG_NAME} --follow"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 6. Summary
# ──────────────────────────────────────────────────────────────────────────────
echo
echo "${GRN}${BLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
echo "${GRN}${BLD} CloudLens deployed → ${ENVIRONMENT}${RST}"
echo "${GRN}${BLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
echo "  API URL          : ${API_URL}"
echo "  API docs (non-prod): ${API_URL}/docs"
[[ -n "${SWA_URL:-}" ]] && echo "  Frontend         : https://${SWA_URL}"
echo "  Resource group   : ${RG_NAME}"
echo "  Image tag        : ${IMAGE_TAG}"
echo "  Key Vault        : ${KV_NAME}"
echo "  Managed identity : ${MI_CLIENT_ID}"
echo
echo "  ${BLD}Next:${RST} onboard a tenant — see TENANT_ONBOARDING.md"
echo "        The first cost ingest runs automatically at 02:00 UTC,"
echo "        or trigger it now:"
echo "          curl -X POST ${API_URL}/api/v1/ingest/<tenant_id> \\"
echo "               -H \"X-API-Key: \$TF_VAR_internal_api_key\""
echo
