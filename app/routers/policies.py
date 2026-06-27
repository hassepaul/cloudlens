"""Policy enforcement router — /api/v1/policies"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query, status

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.models.policy import (
    PolicyRule, PolicyRuleCreate, PolicyRuleUpdate,
    PolicyViolation, PolicyViolationResolve,
)
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.policy_engine import evaluate_tenant_policies

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/policies",
    tags=["policies"],
    dependencies=[Depends(rate_limit_tenant)],
)
admin_router = APIRouter(
    prefix="/api/v1/policies",
    tags=["policies"],
    dependencies=[Depends(require_api_key)],
)


def _c() -> str:
    return get_settings().cosmos_container_policies


# ── Policy rule CRUD ──────────────────────────────────────────────────────────

@router.get("/{tenant_id}/rules", response_model=list[PolicyRule])
async def list_rules(
    tenant_id: str,
    enabled_only: bool = Query(default=False),
) -> list[PolicyRule]:
    """List all policy rules for the tenant."""
    conditions = ["c.tenant_id=@tid", "c.type='policy_rule'"]
    params = [{"name": "@tid", "value": tenant_id}]
    if enabled_only:
        conditions.append("c.enabled=true")
    try:
        docs = await cosmos.query_items(
            _c(),
            f"SELECT * FROM c WHERE {' AND '.join(conditions)} ORDER BY c.created_at DESC",
            params, partition_key=tenant_id,
        )
        return [PolicyRule.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/rules", response_model=PolicyRule, status_code=status.HTTP_201_CREATED)
async def create_rule(tenant_id: str, payload: PolicyRuleCreate) -> PolicyRule:
    """Create a new policy rule."""
    rule = PolicyRule(tenant_id=tenant_id, **payload.model_dump())
    try:
        await cosmos.upsert_item(_c(), rule.to_cosmos())
        log.info("policy.rule_created", tenant_id=tenant_id, rule_id=rule.id, name=rule.name)
        return rule
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/rules/{rule_id}", response_model=PolicyRule)
async def get_rule(tenant_id: str, rule_id: str) -> PolicyRule:
    try:
        doc = await cosmos.get_item(_c(), rule_id, tenant_id)
        if doc.get("type") != "policy_rule":
            raise NotFoundError(f"Policy rule {rule_id} not found")
        return PolicyRule.from_cosmos(doc)
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Rule {rule_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{tenant_id}/rules/{rule_id}", response_model=PolicyRule)
async def update_rule(tenant_id: str, rule_id: str, payload: PolicyRuleUpdate) -> PolicyRule:
    try:
        doc = await cosmos.get_item(_c(), rule_id, tenant_id)
        if doc.get("type") != "policy_rule":
            raise NotFoundError(f"Policy rule {rule_id} not found")
        rule = PolicyRule.from_cosmos(doc)
        rule = rule.model_copy(update=payload.model_dump(exclude_unset=True))
        await cosmos.upsert_item(_c(), rule.to_cosmos())
        log.info("policy.rule_updated", tenant_id=tenant_id, rule_id=rule_id)
        return rule
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Rule {rule_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_rule(tenant_id: str, rule_id: str):
    """Disable and soft-delete a policy rule (sets enabled=false)."""
    try:
        doc = await cosmos.get_item(_c(), rule_id, tenant_id)
        if doc.get("type") != "policy_rule":
            raise NotFoundError(f"Policy rule {rule_id} not found")
        rule = PolicyRule.from_cosmos(doc)
        disabled = rule.model_copy(update={"enabled": False})
        await cosmos.upsert_item(_c(), disabled.to_cosmos())
        log.info("policy.rule_disabled", tenant_id=tenant_id, rule_id=rule_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Rule {rule_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ── On-demand evaluation ──────────────────────────────────────────────────────

@router.post(
    "/{tenant_id}/evaluate",
    status_code=status.HTTP_200_OK,
    summary="Trigger policy evaluation for a tenant on demand",
)
async def evaluate(tenant_id: str) -> dict:
    """
    Run all enabled policies for the tenant immediately.
    Normally called automatically at the end of each nightly ingest cycle.
    """
    try:
        violations = await evaluate_tenant_policies(tenant_id)
        return {
            "tenant_id": tenant_id,
            "violations_triggered": len(violations),
            "violation_ids": [v.id for v in violations],
        }
    except Exception as exc:
        log.error("policy.on_demand_eval_failed", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"error": "EVALUATION_ERROR", "message": str(exc)[:300]},
        )


# ── Violation history ─────────────────────────────────────────────────────────

@router.get("/{tenant_id}/violations", response_model=list[PolicyViolation])
async def list_violations(
    tenant_id: str,
    policy_id: str = Query(default="", description="Filter by specific policy ID"),
    resolved: bool = Query(default=False, description="Include resolved violations"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PolicyViolation]:
    """Return policy violation history for the tenant, newest first."""
    conditions = ["c.tenant_id=@tid", "c.type='policy_violation'"]
    params: list[dict] = [{"name": "@tid", "value": tenant_id}]
    if policy_id:
        conditions.append("c.policy_id=@pid")
        params.append({"name": "@pid", "value": policy_id})
    if not resolved:
        conditions.append("(NOT IS_DEFINED(c.resolved) OR c.resolved = false)")
    query = (
        f"SELECT * FROM c WHERE {' AND '.join(conditions)} "
        f"ORDER BY c.triggered_at DESC OFFSET 0 LIMIT {limit}"
    )
    try:
        docs = await cosmos.query_items(_c(), query, params, partition_key=tenant_id)
        return [PolicyViolation.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch(
    "/{tenant_id}/violations/{violation_id}/resolve",
    response_model=PolicyViolation,
)
async def resolve_violation(
    tenant_id: str, violation_id: str, payload: PolicyViolationResolve
) -> PolicyViolation:
    """Mark a policy violation as resolved."""
    try:
        doc = await cosmos.get_item(_c(), violation_id, tenant_id)
        if doc.get("type") != "policy_violation":
            raise NotFoundError(f"Violation {violation_id} not found")
        v = PolicyViolation.from_cosmos(doc)
        v = v.model_copy(update={
            "resolved": True,
            "resolved_at": datetime.now(timezone.utc),
            "resolved_by": payload.resolved_by,
        })
        await cosmos.upsert_item(_c(), v.to_cosmos())
        log.info("policy.violation_resolved", tenant_id=tenant_id, violation_id=violation_id)
        return v
    except NotFoundError:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"Violation {violation_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
