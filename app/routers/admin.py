"""
Admin & compliance router — /api/v1/admin
=========================================

Operator-only (internal API key). Exposes the audit trail, the SOC 2-aligned
control matrix, audit-chain integrity verification, and the compliance evidence
pack export (with CLI proof commands). These are the surfaces the admin page
drives.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.auth import require_api_key
from app.services import cosmos
from app.services import compliance as comp
from app.models.audit import AuditRecord, AuditAction, chain_record, verify_chain

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/admin", tags=["admin"],
    dependencies=[Depends(require_api_key)],     # operator-only
)


def _c() -> str:
    return get_settings().cosmos_container_waste_items   # audit records co-located


# ── Audit log ────────────────────────────────────────────────────────────────

async def write_audit(
    tenant_id: str, action: AuditAction, actor: str, *,
    actor_type: str = "user", resource_id: str = "", outcome: str = "success",
    source_ip: str = "", detail: dict | None = None,
) -> AuditRecord:
    """
    Append a tamper-evident audit record. Called by routers on security/change
    events. Fetches the latest record's hash to chain the new one.
    """
    pk = tenant_id or "_system"
    try:
        prev = await cosmos.query_items(
            _c(),
            "SELECT TOP 1 c.record_hash FROM c WHERE c.tenant_id=@t AND c.type='audit_record' "
            "ORDER BY c.timestamp DESC",
            parameters=[{"name": "@t", "value": pk}], partition_key=pk)
        prev_hash = prev[0]["record_hash"] if prev else ""
    except Exception:
        prev_hash = ""
    rec = AuditRecord(
        tenant_id=pk, action=action, actor=actor, actor_type=actor_type,
        resource_id=resource_id, outcome=outcome, source_ip=source_ip, detail=detail or {})
    rec = chain_record(rec, prev_hash)
    try:
        await cosmos.upsert_item(_c(), rec.to_cosmos())
    except Exception as exc:        # auditing must never break the request path
        log.warning("audit.write_failed", action=action.value, error=str(exc))
    return rec


@router.get("/audit", response_model=list[AuditRecord])
async def get_audit(
    tenant_id: str = Query("_system"),
    action: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditRecord]:
    """Query the audit trail (most recent first)."""
    q = ("SELECT * FROM c WHERE c.tenant_id=@t AND c.type='audit_record'"
         + (" AND c.action=@a" if action else "")
         + " ORDER BY c.timestamp DESC OFFSET 0 LIMIT @lim")
    params = [{"name": "@t", "value": tenant_id}, {"name": "@lim", "value": limit}]
    if action:
        params.append({"name": "@a", "value": action})
    try:
        docs = await cosmos.query_items(_c(), q, parameters=params, partition_key=tenant_id)
        return [AuditRecord.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/compliance/audit-integrity/{tenant_id}")
async def audit_integrity(tenant_id: str) -> dict:
    """Verify the audit hash chain is intact — proof the trail is tamper-evident."""
    try:
        docs = await cosmos.query_items(
            _c(),
            "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='audit_record' ORDER BY c.timestamp ASC",
            parameters=[{"name": "@t", "value": tenant_id}], partition_key=tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    records = [AuditRecord.from_cosmos(d) for d in docs]
    intact, broken = verify_chain(records)
    return {"tenant_id": tenant_id, "records": len(records),
            "intact": intact, "first_broken_id": broken}


# ── Compliance control matrix + evidence pack ────────────────────────────────

@router.get("/compliance/matrix")
async def compliance_matrix() -> dict:
    """The SOC 2-aligned control matrix with per-control CLI evidence commands."""
    s = get_settings()
    deployment = {
        "resource_group": getattr(s, "resource_group_name", "rg-cloudlens-prod"),
        "cosmos_account": getattr(s, "cosmos_account_name", "cloudlens-cosmos"),
        "key_vault": s.key_vault_name,
        "storage_account": s.storage_account_name,
        "container_app": "cloudlens-api",
    }
    controls = comp.build_matrix(deployment)
    return {
        "framework": "SOC 2 (AICPA Trust Services Criteria)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": ("This is an audit-readiness control matrix, not a SOC report. "
                       "A SOC 1/SOC 2 attestation must be issued by a licensed CPA firm "
                       "over a defined audit period."),
        "summary": comp.matrix_summary(controls),
        "controls": [{
            "criteria_id": c.criteria_id, "criteria_name": c.criteria_name,
            "control": c.control, "status": c.status.value,
            "evidence_kind": c.evidence_kind, "notes": c.notes,
            "cli_evidence": [e.__dict__ for e in c.cli_evidence],
        } for c in controls],
    }


@router.post("/compliance/evidence-export")
async def evidence_export(
    tenant_id: str = Query("_system"),
    actor: str = Query("admin"),
) -> dict:
    """
    Generate a compliance evidence pack: the control matrix with CLI proof
    commands, plus the audit-chain integrity result. Records the export itself
    in the audit log (CC7.2). The admin page renders/downloads this.
    """
    matrix = await compliance_matrix()
    integrity = await audit_integrity(tenant_id) if tenant_id != "_system" else \
        {"tenant_id": tenant_id, "note": "system-scope export; run per-tenant for chain verification"}

    await write_audit("_system", AuditAction.EVIDENCE_EXPORTED, actor,
                      actor_type="api_key", detail={"tenant_scope": tenant_id})

    return {
        "export_id": f"evidence-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": matrix["generated_at"],
        "framework": matrix["framework"],
        "disclaimer": matrix["disclaimer"],
        "summary": matrix["summary"],
        "audit_integrity": integrity,
        "controls": matrix["controls"],
    }
