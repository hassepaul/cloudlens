"""
Automated commitment purchasing router.

Endpoints
---------
GET  /{tenant_id}/settings        — retrieve current auto-purchase settings
PUT  /{tenant_id}/settings        — update settings (enable/disable, caps, dry_run)
POST /{tenant_id}/execute         — run a purchase cycle (respects dry_run flag)
GET  /{tenant_id}/history         — list recent purchase records

SAFETY NOTE
-----------
The global kill switch (COMMITMENT_AUTO_PURCHASE_ENABLED env var) and the
per-tenant enabled flag must both be true before any purchase executes.
Calling POST /execute with either gate closed returns HTTP 403 with a clear
explanation — it never silently succeeds.
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field, model_validator

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError, KeyVaultError
from app.logging_config import get_logger
from app.services.commitment_advisor import generate_advisories
from app.services.commitment_purchaser import (
    PurchaseSettings,
    CommitmentAutoDisabledError,
    get_purchase_settings,
    save_purchase_settings,
    run_purchase,
    get_purchase_history,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/commitment-purchaser",
    tags=["commitment-purchaser"],
    dependencies=[Depends(require_api_key)],
)


# ── Request / response models ────────────────────────────────────────────────

class PurchaseSettingsIn(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    max_single_purchase_eur: float = Field(default=5_000.0, gt=0)
    max_monthly_budget_eur: float = Field(default=20_000.0, gt=0)
    min_confidence_score: float = Field(default=0.70, ge=0.0, le=1.0)
    allowed_commitment_types: list[str] = Field(default_factory=list)
    allowed_services: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_types(self) -> "PurchaseSettingsIn":
        valid = {
            "savings-plan-1yr", "savings-plan-3yr", "1yr-ri", "3yr-ri",  # aws / azure
            "committed-use-1yr", "committed-use-3yr",                    # gcp
        }
        for t in self.allowed_commitment_types:
            if t not in valid:
                raise ValueError(
                    f"'{t}' is not a purchasable commitment type. "
                    f"Valid: {sorted(valid)}"
                )
        return self


class ExecuteRequest(BaseModel):
    lookback_days: int = Field(default=90, ge=14, le=365)
    fx_rate_eur_to_usd: float = Field(default=1.08, gt=0.5, lt=5.0)


def _settings_response(s: PurchaseSettings, global_enabled: bool) -> dict:
    return {
        "tenant_id": s.tenant_id,
        "enabled": s.enabled,
        "dry_run": s.dry_run,
        "max_single_purchase_eur": s.max_single_purchase_eur,
        "max_monthly_budget_eur": s.max_monthly_budget_eur,
        "min_confidence_score": s.min_confidence_score,
        "allowed_commitment_types": s.allowed_commitment_types,
        "allowed_services": s.allowed_services,
        "updated_at": s.updated_at,
        # Inform the caller about the global gate so they know why execute
        # might still return 403 even after enabling the tenant flag.
        "global_auto_purchase_enabled": global_enabled,
        "effective_enabled": s.enabled and global_enabled,
    }


def _run_response(run) -> dict:
    def _rec(r) -> dict:
        return {
            "id": r.id,
            "cloud": r.cloud,
            "commitment_type": r.commitment_type,
            "service": r.service,
            "hourly_commitment_usd": r.hourly_commitment_usd,
            "monthly_saving_eur": r.monthly_saving_eur,
            "term_months": r.term_months,
            "dry_run": r.dry_run,
            "status": r.status,
            "aws_commitment_id": r.aws_commitment_id,
            "error": r.error,
            "skip_reason": r.skip_reason,
            "confidence_score": r.confidence_score,
            "purchased_at": r.purchased_at,
        }

    return {
        "tenant_id": run.tenant_id,
        "run_at": run.run_at,
        "dry_run": run.dry_run,
        "total_committed_eur": run.total_committed_eur,
        "purchased": [_rec(r) for r in run.purchased],
        "skipped": [_rec(r) for r in run.skipped],
        "failed": [_rec(r) for r in run.failed],
        "notes": run.notes,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/settings")
async def get_settings_endpoint(tenant_id: str) -> dict:
    """Return the auto-purchase settings for a tenant."""
    try:
        s = await get_purchase_settings(tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return _settings_response(s, get_settings().commitment_auto_purchase_enabled)


@router.put("/{tenant_id}/settings")
async def update_settings_endpoint(tenant_id: str, body: PurchaseSettingsIn) -> dict:
    """
    Update the auto-purchase settings for a tenant.

    Enabling automated purchasing (`enabled=true`) without also setting
    `dry_run=false` will still only simulate purchases — this is intentional
    so teams can review what would be bought before committing real spend.

    The global kill switch (`COMMITMENT_AUTO_PURCHASE_ENABLED` env var) is
    controlled by the platform operator and is not configurable here.
    """
    try:
        s = await get_purchase_settings(tenant_id)
        s.enabled = body.enabled
        s.dry_run = body.dry_run
        s.max_single_purchase_eur = body.max_single_purchase_eur
        s.max_monthly_budget_eur = body.max_monthly_budget_eur
        s.min_confidence_score = body.min_confidence_score
        s.allowed_commitment_types = body.allowed_commitment_types
        s.allowed_services = body.allowed_services
        s = await save_purchase_settings(s)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    log.info(
        "commitment_purchaser.settings_updated",
        tenant_id=tenant_id, enabled=s.enabled, dry_run=s.dry_run,
    )
    return _settings_response(s, get_settings().commitment_auto_purchase_enabled)


@router.post("/{tenant_id}/execute")
async def execute_purchase(tenant_id: str, body: ExecuteRequest) -> dict:
    """
    Run an automated commitment purchase cycle for the tenant.

    1. Fetches the latest Commitment Advisor recommendations.
    2. Filters to advisories eligible for auto-purchase (timing=commit_now,
       confidence ≥ min_confidence_score, purchasable type for the cloud,
       within budget caps).
    3. Purchases the filtered advisories via the matching provider SDK
       (AWS Savings Plans / RIs, Azure Compute Savings Plans, GCP CUDs).

    Returns HTTP 403 if either safety gate (global or per-tenant) is closed.
    Returns a full run report including purchased, skipped, and failed items.

    **Always start with `dry_run=true` (the default) to preview what would be
    bought before flipping to live execution.**
    """
    try:
        # Build advisory list from the advisor service
        report = await generate_advisories(tenant_id, lookback_days=body.lookback_days)
        advisories = [
            {
                "service": a.service,
                "cloud": a.cloud,
                "recommended_type": a.recommended_type,
                "on_demand_monthly_eur": a.on_demand_monthly_eur,
                "commitment_horizon_months": a.commitment_horizon_months,
                "confidence_score": a.confidence_score,
                "saving_pct": a.saving_pct,
                "timing": a.timing,
                "wait_months": a.wait_months,
            }
            for a in report.advisories
        ]
        run = await run_purchase(
            tenant_id=tenant_id,
            advisories=advisories,
            fx_rate_eur_to_usd=body.fx_rate_eur_to_usd,
        )
    except CommitmentAutoDisabledError as exc:
        raise HTTPException(status_code=403, detail={"error": "DISABLED", "message": str(exc)})
    except KeyVaultError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "KV_ERROR", "message": f"Could not load AWS credentials: {exc}"},
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    return _run_response(run)


@router.get("/{tenant_id}/history")
async def get_history(
    tenant_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    status: Optional[str] = Query(
        default=None,
        pattern="^(purchased|dry_run|failed|skipped)$",
    ),
) -> dict:
    """
    List the most-recent commitment purchase records for a tenant.
    Optionally filter by status: purchased, dry_run, failed, skipped.
    """
    try:
        records = await get_purchase_history(tenant_id, limit=limit, status_filter=status)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {"tenant_id": tenant_id, "count": len(records), "records": records}
