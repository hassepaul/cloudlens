"""
Cost estimation router — Terraform plan analysis + CI/CD pipeline run history.

  POST /api/v1/estimate/terraform              Parse plan, return estimate (stateless)
  POST /api/v1/estimate/terraform/record       Parse plan, persist run, return estimate + gate + drift
  GET  /api/v1/estimate/catalog                Supported resource type catalog
  GET  /api/v1/estimate/runs/{tenant_id}       Pipeline run history (most recent first)
  POST /api/v1/estimate/gate                   Budget gate check (pass/fail, no auth)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services.cost_estimator import (
    catalog_summary,
    compute_drift,
    compute_gate,
    estimate_plan,
    list_runs,
    record_run,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/estimate", tags=["cost-estimate"])


# ── Request models ────────────────────────────────────────────────────────────

class TerraformEstimateRequest(BaseModel):
    plan_json: str = Field(
        ...,
        description="Full JSON output of `terraform show -json <plan-file>`",
    )
    label: str = Field(default="", max_length=200)


class RecordRunRequest(BaseModel):
    plan_json: str = Field(..., description="Terraform show -json output")
    tenant_id: str
    label: str = Field(default="", max_length=200)
    ci_system: str = Field(
        default="other",
        description="github_actions | gitlab_ci | azure_devops | other",
    )
    repo: str = Field(default="")
    branch: str = Field(default="")
    commit_sha: str = Field(default="")
    pr_number: Optional[int] = None
    budget_gate_eur: Optional[float] = Field(default=None, ge=0)


class BudgetGateRequest(BaseModel):
    monthly_delta_eur: float
    gate_eur: float = Field(..., ge=0, description="Max allowed net monthly increase in EUR")
    label: str = Field(default="")


# ── Stateless estimate ────────────────────────────────────────────────────────

@router.post("/terraform")
async def estimate_terraform(body: TerraformEstimateRequest) -> dict:
    """Parse a Terraform plan JSON and return a per-resource cost estimate (not persisted)."""
    try:
        result = estimate_plan(body.plan_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid Terraform plan: {exc}")
    except Exception as exc:
        log.error("cost_estimate.parse_error", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to parse plan JSON")

    out = result.to_dict()
    if body.label:
        out["label"] = body.label
    log.info(
        "cost_estimate.terraform",
        label=body.label,
        resources=result.total_resources_analyzed,
        delta_eur=round(result.total_monthly_delta_eur, 2),
    )
    return out


# ── Estimate + persist ────────────────────────────────────────────────────────

@router.post("/terraform/record", dependencies=[Depends(require_api_key)])
async def estimate_and_record(body: RecordRunRequest) -> dict:
    """
    Parse a Terraform plan, persist the run to pipeline history, and return
    the full estimate with budget gate result and drift vs. the previous run.
    """
    try:
        estimate = estimate_plan(body.plan_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid Terraform plan: {exc}")
    except Exception as exc:
        log.error("cost_estimate.parse_error", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to parse plan JSON")

    drift_pct: Optional[float] = None
    try:
        prev_runs = await list_runs(body.tenant_id, limit=1)
        if prev_runs:
            drift_pct = compute_drift(
                estimate.total_monthly_delta_eur,
                prev_runs[0]["total_monthly_delta_eur"],
            )
    except Exception:
        pass

    try:
        run = await record_run(
            body.tenant_id,
            estimate,
            label=body.label,
            ci_system=body.ci_system,
            repo=body.repo,
            branch=body.branch,
            commit_sha=body.commit_sha,
            pr_number=body.pr_number,
            budget_gate_eur=body.budget_gate_eur,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    out = estimate.to_dict()
    if body.label:
        out["label"] = body.label
    out["run_id"] = run.id
    out["ci_system"] = run.ci_system
    out["gate_passed"] = run.gate_passed
    out["budget_gate_eur"] = run.budget_gate_eur
    out["drift_vs_previous_pct"] = drift_pct

    log.info(
        "cost_estimate.recorded",
        tenant_id=body.tenant_id,
        run_id=run.id,
        delta_eur=round(estimate.total_monthly_delta_eur, 2),
        gate_passed=run.gate_passed,
    )
    return out


# ── Pipeline run history ──────────────────────────────────────────────────────

@router.get("/runs/{tenant_id}", dependencies=[Depends(require_api_key)])
async def get_run_history(
    tenant_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Return the CI/CD pipeline run history for a tenant (most recent first)."""
    try:
        runs = await list_runs(tenant_id, limit=limit)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    for i, run in enumerate(runs):
        if i + 1 < len(runs):
            run["drift_vs_previous_pct"] = compute_drift(
                run["total_monthly_delta_eur"],
                runs[i + 1]["total_monthly_delta_eur"],
            )
        else:
            run["drift_vs_previous_pct"] = None

    return {"tenant_id": tenant_id, "total": len(runs), "runs": runs}


# ── Budget gate ───────────────────────────────────────────────────────────────

@router.post("/gate")
async def budget_gate(body: BudgetGateRequest) -> dict:
    """
    Evaluate a cost budget gate. Returns pass/fail + exit_code (0/1).
    No auth required so pipelines can use it without storing a key.
    """
    passed = compute_gate(body.monthly_delta_eur, body.gate_eur)
    sign = "+" if body.monthly_delta_eur >= 0 else ""
    return {
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "monthly_delta_eur": round(body.monthly_delta_eur, 2),
        "gate_eur": body.gate_eur,
        "label": body.label,
        "message": (
            f"PASS: {sign}\u20ac{body.monthly_delta_eur:,.2f}/month is within the "
            f"\u20ac{body.gate_eur:,.2f}/month gate."
            if passed else
            f"FAIL: {sign}\u20ac{body.monthly_delta_eur:,.2f}/month exceeds the "
            f"\u20ac{body.gate_eur:,.2f}/month budget gate."
        ),
    }


# ── Catalog ───────────────────────────────────────────────────────────────────

@router.get("/catalog")
async def get_catalog() -> dict:
    """Return the full pricing catalog with all supported resource types and sizes."""
    return catalog_summary()
