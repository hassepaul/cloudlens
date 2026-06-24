"""
Compliance evidence (SOC 2-aligned)
==================================

CloudLens cannot self-certify SOC 1/SOC 2 — those are attestations issued by a
licensed CPA firm against the AICPA Trust Services Criteria over an audit period.
What this module provides is the *audit-ready* layer an organization brings to
that audit:

  1. A control matrix mapping Trust Services Criteria (the SOC 2 Common Criteria
     CC1–CC9 plus Availability/Confidentiality) to the specific CloudLens control
     that addresses them, the control's implementation status, and how it is
     evidenced.

  2. A CLI evidence generator: for each technical control, the exact az/terraform
     commands an auditor or admin runs to *prove* the control is live on the
     deployed resources, plus the expected output shape. This is the
     "CLI proof of particular resources" an auditor asks for during fieldwork.

Honest boundary: implementation_status reflects what the codebase/infra enforces.
"NEEDS_ORG_PROCESS" marks controls that require organizational policy or an audit
period (e.g. background checks, security training, incident-response drills) that
software cannot satisfy on its own.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class ControlStatus(str, Enum):
    IMPLEMENTED = "implemented"            # enforced in code/infra
    PARTIAL = "partial"                    # partially enforced; gap noted
    NEEDS_ORG_PROCESS = "needs_org_process"  # requires policy/process, not code


@dataclass
class CliEvidence:
    description: str
    command: str                           # the CLI command an auditor runs
    expected: str                          # what a passing result looks like


@dataclass
class Control:
    criteria_id: str                       # e.g. "CC6.1"
    criteria_name: str
    control: str                           # CloudLens control description
    status: ControlStatus
    evidence_kind: str                     # "code" | "infra" | "audit_log" | "process"
    cli_evidence: list[CliEvidence] = field(default_factory=list)
    notes: str = ""


# Placeholders substituted with the tenant/deployment's real names at export time.
def _matrix(rg: str, cosmos: str, kv: str, storage: str, app: str) -> list[Control]:
    return [
        # ── CC1: Control environment ──
        Control("CC1.1", "Integrity & ethical values",
                "Code of conduct and security policies maintained; not enforced by software.",
                ControlStatus.NEEDS_ORG_PROCESS, "process",
                notes="Provide signed policy documents and acknowledgement records."),
        Control("CC1.4", "Commitment to competence",
                "Engineers trained on secure SDLC; background checks on hire.",
                ControlStatus.NEEDS_ORG_PROCESS, "process",
                notes="HR/training records are the evidence here."),

        # ── CC6: Logical & physical access ──
        Control("CC6.1", "Logical access — authentication",
                "All per-tenant endpoints require a verified Azure AD bearer token (JWKS-validated); "
                "internal endpoints require an API key. No anonymous data access.",
                ControlStatus.IMPLEMENTED, "code",
                cli_evidence=[CliEvidence(
                    "Confirm the API enforces auth (401 without a token)",
                    f"curl -s -o /dev/null -w '%{{http_code}}' https://{app}.azurecontainerapps.io/api/v1/tenants/",
                    "401")]),
        Control("CC6.1b", "Logical access — tenant isolation",
                "A bearer token scoped to tenant A is rejected (403) when accessing tenant B "
                "(require_tenant_scope dependency).",
                ControlStatus.IMPLEMENTED, "code",
                cli_evidence=[CliEvidence(
                    "Cross-tenant request returns 403",
                    f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer $TOKEN_TENANT_A' "
                    f"https://{app}.azurecontainerapps.io/api/v1/costs/tenant-b",
                    "403")]),
        Control("CC6.2", "Least-privilege identities",
                "Customer service principals are read-only (Reader + Cost Management Reader); "
                "internal service uses a user-assigned managed identity, no stored secrets.",
                ControlStatus.IMPLEMENTED, "infra",
                cli_evidence=[CliEvidence(
                    "List role assignments for the CloudLens identity (expect only read roles)",
                    f"az role assignment list --assignee $(az identity show -g {rg} -n cloudlens-id --query principalId -o tsv) "
                    "--query \"[].roleDefinitionName\" -o tsv",
                    "Reader / Cost Management Reader (no Contributor/Owner)")]),
        Control("CC6.3", "Access modification logged",
                "Tenant/budget/alert-rule create/update/delete and waste resolutions are written to "
                "the tamper-evident audit log.",
                ControlStatus.IMPLEMENTED, "audit_log",
                cli_evidence=[CliEvidence(
                    "Pull recent access-change audit events",
                    "GET /api/v1/admin/audit?action=tenant_updated (admin API key)",
                    "Returns chained audit records with verified hashes")]),
        Control("CC6.6", "Encryption in transit",
                "TLS 1.2+ enforced on all ingress; Container Apps and Storage reject plaintext.",
                ControlStatus.IMPLEMENTED, "infra",
                cli_evidence=[CliEvidence(
                    "Confirm Storage enforces HTTPS-only and TLS1_2",
                    f"az storage account show -g {rg} -n {storage} "
                    "--query \"{{https:enableHttpsTrafficOnly, tls:minimumTlsVersion}}\"",
                    '{"https": true, "tls": "TLS1_2"}')]),
        Control("CC6.7", "Encryption at rest",
                "Cosmos DB, Blob Storage, and Key Vault encrypt data at rest with platform-managed keys.",
                ControlStatus.IMPLEMENTED, "infra",
                cli_evidence=[CliEvidence(
                    "Confirm Cosmos encryption + Storage encryption are enabled",
                    f"az cosmosdb show -g {rg} -n {cosmos} --query \"{{name:name}}\" && "
                    f"az storage account show -g {rg} -n {storage} --query \"encryption.services.blob.enabled\"",
                    "true (blob encryption enabled; Cosmos encrypts at rest by default)")]),
        Control("CC6.8", "Secret management",
                "Customer SP credentials and the internal API key are stored only in Key Vault "
                "(purge protection on); no secrets in code, config, or logs (redaction processor).",
                ControlStatus.IMPLEMENTED, "infra",
                cli_evidence=[CliEvidence(
                    "Confirm Key Vault purge protection + soft delete",
                    f"az keyvault show -n {kv} --query \"properties.{{purge:enablePurgeProtection, "
                    "softDelete:enableSoftDelete}}\"",
                    '{"purge": true, "softDelete": true}')]),

        # ── CC7: System operations / monitoring ──
        Control("CC7.2", "Monitoring of components",
                "Structured JSON logs with request_id to Log Analytics; tamper-evident audit log; "
                "health endpoint with dependency checks.",
                ControlStatus.IMPLEMENTED, "infra",
                cli_evidence=[CliEvidence(
                    "Confirm diagnostic logs flow to Log Analytics",
                    f"az monitor diagnostic-settings list --resource $(az containerapp show -g {rg} -n {app} "
                    "--query id -o tsv) --query \"[].name\"",
                    "At least one diagnostic setting targeting the Log Analytics workspace")]),
        Control("CC7.3", "Audit-log integrity",
                "Audit records form a SHA-256 hash chain; the export verifies the chain is intact.",
                ControlStatus.IMPLEMENTED, "audit_log",
                cli_evidence=[CliEvidence(
                    "Verify the audit chain for a tenant",
                    "GET /api/v1/admin/compliance/audit-integrity/{tenant_id}",
                    '{"intact": true, "records": N}')]),

        # ── CC8: Change management ──
        Control("CC8.1", "Change management",
                "All changes ship via CI/CD with mandatory tests; infra changes require plan review "
                "and manual approval; deployments are versioned by image SHA. GitHub OIDC (no secrets).",
                ControlStatus.IMPLEMENTED, "process",
                cli_evidence=[CliEvidence(
                    "Confirm the running image is a pinned SHA (not :latest)",
                    f"az containerapp show -g {rg} -n {app} --query \"properties.template.containers[0].image\"",
                    "registry/cloudlens:<git-sha> (immutable tag)")]),

        # ── A1: Availability ──
        Control("A1.2", "Availability — backup & recovery",
                "Cosmos serverless with point-in-time data; Key Vault soft-delete; IaC enables full "
                "environment rebuild from Terraform.",
                ControlStatus.PARTIAL, "infra",
                notes="Document RPO/RTO targets and run a restore test to fully satisfy A1.3.",
                cli_evidence=[CliEvidence(
                    "Confirm Cosmos backup policy",
                    f"az cosmosdb show -g {rg} -n {cosmos} --query \"backupPolicy.type\"",
                    "Periodic or Continuous")]),

        # ── C1: Confidentiality ──
        Control("C1.1", "Confidential data identified & protected",
                "Customer cost data partitioned by tenant_id; 90-day TTL on cost records; read-only "
                "architecture means CloudLens never holds customer workload data, only billing metadata.",
                ControlStatus.IMPLEMENTED, "code",
                cli_evidence=[CliEvidence(
                    "Confirm cost-record TTL is enforced",
                    f"az cosmosdb sql container show -g {rg} -a {cosmos} -d cloudlens -n cost_records "
                    "--query \"resource.defaultTtl\"",
                    "7776000 (90 days)")]),
        Control("C1.2", "Confidential data disposal",
                "TTL auto-expires cost records; tenant soft-delete retains then ages out data.",
                ControlStatus.IMPLEMENTED, "code"),
    ]


def build_matrix(deployment: dict) -> list[Control]:
    return _matrix(
        rg=deployment.get("resource_group", "rg-cloudlens-prod"),
        cosmos=deployment.get("cosmos_account", "cloudlens-cosmos"),
        kv=deployment.get("key_vault", "cloudlens-kv"),
        storage=deployment.get("storage_account", "cloudlensstorage"),
        app=deployment.get("container_app", "cloudlens-api"),
    )


def matrix_summary(controls: list[Control]) -> dict:
    by_status = {s.value: 0 for s in ControlStatus}
    for c in controls:
        by_status[c.status.value] += 1
    return {
        "total": len(controls),
        "implemented": by_status["implemented"],
        "partial": by_status["partial"],
        "needs_org_process": by_status["needs_org_process"],
        "coverage_pct": round(by_status["implemented"] / len(controls) * 100, 1) if controls else 0.0,
    }
