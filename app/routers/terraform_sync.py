"""
Terraform Drift router — /api/v1/terraform
==========================================

Provides the engineer-facing reconciliation UI for autonomous execution drift.

GET  /{tenant_id}/drift               — list drift records (optional ?status=pending|acknowledged|imported)
GET  /{tenant_id}/drift/summary       — KPI counts by status
GET  /{tenant_id}/drift/{record_id}   — single record with full HCL + import cmd
POST /{tenant_id}/drift/{record_id}/acknowledge   — mark as acknowledged (IaC updated)
POST /{tenant_id}/drift/{record_id}/imported      — mark as fully imported (terraform import ran)
DELETE /{tenant_id}/drift/{record_id}  — dismiss/delete a record
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services.terraform_sync import (
    TerraformDriftRecord,
    DRIFT_STATUS_ACKNOWLEDGED,
    DRIFT_STATUS_IMPORTED,
    DRIFT_STATUS_PENDING,
    list_drift,
    get_drift_record,
    acknowledge_drift,
    dismiss_drift,
    get_drift_summary,
    generate_hcl,
    generate_import_cmd,
    build_autonomous_tags,
    _TOOL_RESOURCE_MAP,
)

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/terraform",
    tags=["terraform-drift"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_tenant)],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class DriftRecordOut(BaseModel):
    id: str
    tenant_id: str
    action_id: str
    approval_id: str
    approved_by: str
    tool_name: str
    resource_type: str
    resource_name: str
    resource_id: str
    provider: str
    region: str
    hcl_snippet: str
    import_cmd: str
    tags: dict
    status: str
    created_at: str
    acknowledged_at: str
    acknowledged_by: str
    notification_sent: bool


class DriftSummaryOut(BaseModel):
    pending: int
    acknowledged: int
    imported: int
    total: int
    all_reconciled: bool


class AcknowledgeIn(BaseModel):
    acknowledged_by: str = Field(default="", description="Identifier of the engineer acknowledging")


def _record_out(r: TerraformDriftRecord) -> DriftRecordOut:
    return DriftRecordOut(
        id=r.id,
        tenant_id=r.tenant_id,
        action_id=r.action_id,
        approval_id=r.approval_id,
        approved_by=r.approved_by,
        tool_name=r.tool_name,
        resource_type=r.resource_type,
        resource_name=r.resource_name,
        resource_id=r.resource_id,
        provider=r.provider,
        region=r.region,
        hcl_snippet=r.hcl_snippet,
        import_cmd=r.import_cmd,
        tags=r.tags,
        status=r.status,
        created_at=r.created_at,
        acknowledged_at=r.acknowledged_at,
        acknowledged_by=r.acknowledged_by,
        notification_sent=r.notification_sent,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/drift/summary", response_model=DriftSummaryOut)
async def drift_summary(tenant_id: str) -> DriftSummaryOut:
    """Return KPI counts of drift records by status."""
    data = await get_drift_summary(tenant_id)
    return DriftSummaryOut(**data)


@router.get("/{tenant_id}/drift", response_model=list[DriftRecordOut])
async def list_drift_records(
    tenant_id: str,
    status: Optional[str] = Query(
        default=None,
        pattern="^(pending|acknowledged|imported)$",
        description="Filter by status",
    ),
) -> list[DriftRecordOut]:
    """List Terraform drift records for a tenant.

    Use ``?status=pending`` to see only unreconciled records — the default
    view for the drift dashboard.
    """
    records = await list_drift(tenant_id, status_filter=status or "")
    return [_record_out(r) for r in records]


@router.get("/{tenant_id}/drift/{record_id}", response_model=DriftRecordOut)
async def get_drift(tenant_id: str, record_id: str) -> DriftRecordOut:
    """Get a single drift record including full HCL snippet and import command."""
    record = await get_drift_record(tenant_id, record_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": f"Drift record {record_id} not found"},
        )
    return _record_out(record)


@router.post("/{tenant_id}/drift/{record_id}/acknowledge", response_model=DriftRecordOut)
async def acknowledge_drift_record(
    tenant_id: str,
    record_id: str,
    body: AcknowledgeIn,
) -> DriftRecordOut:
    """Mark a drift record as **acknowledged** — IaC updated, ``terraform import`` not yet run.

    Moves status: ``pending`` → ``acknowledged``.
    """
    record = await acknowledge_drift(
        tenant_id, record_id, body.acknowledged_by, new_status=DRIFT_STATUS_ACKNOWLEDGED
    )
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": f"Drift record {record_id} not found"},
        )
    return _record_out(record)


@router.post("/{tenant_id}/drift/{record_id}/imported", response_model=DriftRecordOut)
async def mark_imported(
    tenant_id: str,
    record_id: str,
    body: AcknowledgeIn,
) -> DriftRecordOut:
    """Mark a drift record as **imported** — ``terraform import`` completed successfully.

    Moves status: ``pending|acknowledged`` → ``imported``.
    """
    record = await acknowledge_drift(
        tenant_id, record_id, body.acknowledged_by, new_status=DRIFT_STATUS_IMPORTED
    )
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": f"Drift record {record_id} not found"},
        )
    return _record_out(record)


@router.delete(
    "/{tenant_id}/drift/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def dismiss_drift_record(tenant_id: str, record_id: str) -> None:
    """Dismiss (delete) a drift record — use only when the resource has been destroyed
    or the record is otherwise invalid."""
    deleted = await dismiss_drift(tenant_id, record_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": f"Drift record {record_id} not found"},
        )


@router.get("/{tenant_id}/tag-policy", response_model=dict)
async def tag_policy(tenant_id: str) -> dict:
    """Return the standard autonomous-execution tag policy for this tenant.

    Use this as input to an OPA / Azure Policy / SCP rule that requires the
    ``cloudlens:source=autonomous`` tag on any resource created by the
    CloudLens service principal.  All autonomously provisioned resources
    will have all of these tags applied.
    """
    return {
        "description": (
            "Tags applied to every resource autonomously provisioned by CloudLens. "
            "Use these with terraform-compliance, OPA Gatekeeper, AWS Config Rules, "
            "or Azure Policy to detect unmanaged resources."
        ),
        "required_tags": {
            "cloudlens:source": "autonomous",
            "cloudlens:action_id": "<uuid>",
            "cloudlens:approval_id": "<uuid>",
            "cloudlens:approved_by": "<user_or_session_id>",
            "cloudlens:tenant_id": tenant_id,
            "cloudlens:created_at": "<iso8601_utc>",
            "cloudlens:resource_type": "<terraform_resource_type>",
        },
        "terraform_import_workflow": [
            "1. Run `terraform plan -refresh-only` to see out-of-band changes",
            "2. Copy the HCL snippet from GET /terraform/{tenant}/drift/{id}",
            "3. Paste into the appropriate .tf file",
            "4. Run the import command from the same endpoint",
            "5. Run `terraform plan` to verify zero-diff",
            "6. Call POST /terraform/{tenant}/drift/{id}/imported to close the record",
        ],
        "aws_config_rule_tag_key": "cloudlens:source",
        "azure_policy_tag_key": "cloudlens:source",
        "supported_resource_types": [
            rt for (_, _), (rt, _) in _TOOL_RESOURCE_MAP.items()
        ],
    }
