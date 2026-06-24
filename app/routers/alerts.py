"""Alerts router — /api/v1/alerts (rule CRUD, event log, evaluate)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, status

from app.config import get_settings
from app.exceptions import NotFoundError, CosmosError
from app.logging_config import get_logger
from app.models.alert import AlertRule, AlertRuleCreate, AlertRuleUpdate, AlertEvent
from app.rate_limit import rate_limit_tenant
from app.services import cosmos

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/alerts", tags=["alerts"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _c() -> str:
    # alert rules + events live in the waste_items container, type-discriminated
    return get_settings().cosmos_container_waste_items


# ── Rule CRUD ────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/rules", response_model=list[AlertRule])
async def list_rules(tenant_id: str) -> list[AlertRule]:
    try:
        docs = await cosmos.query_items(
            _c(), "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='alert_rule'",
            parameters=[{"name": "@t", "value": tenant_id}], partition_key=tenant_id)
        return [AlertRule.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/rules", response_model=AlertRule, status_code=status.HTTP_201_CREATED)
async def create_rule(tenant_id: str, payload: AlertRuleCreate) -> AlertRule:
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=422, detail={
            "error": "VALIDATION_ERROR", "message": "Body tenant_id must match path"})
    try:
        rule = AlertRule(**payload.model_dump())
        await cosmos.upsert_item(_c(), rule.to_cosmos())
        log.info("alert_rule.created", tenant_id=tenant_id, rule_id=rule.id,
                 alert_type=rule.alert_type.value)
        return rule
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{tenant_id}/rules/{rule_id}", response_model=AlertRule)
async def update_rule(tenant_id: str, rule_id: str, payload: AlertRuleUpdate) -> AlertRule:
    try:
        doc = await cosmos.get_item(_c(), rule_id, tenant_id)
        if doc.get("type") != "alert_rule":
            raise NotFoundError(f"Alert rule {rule_id} not found")
        rule = AlertRule.from_cosmos(doc)
        updated = rule.model_copy(update=payload.model_dump(exclude_unset=True))
        await cosmos.upsert_item(_c(), updated.to_cosmos())
        return updated
    except NotFoundError:
        raise HTTPException(status_code=404, detail={
            "error": "NOT_FOUND", "message": f"Alert rule {rule_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_rule(tenant_id: str, rule_id: str) -> None:
    try:
        doc = await cosmos.get_item(_c(), rule_id, tenant_id)
        if doc.get("type") != "alert_rule":
            raise NotFoundError(f"Alert rule {rule_id} not found")
        await cosmos.delete_item(_c(), rule_id, tenant_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail={
            "error": "NOT_FOUND", "message": f"Alert rule {rule_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ── Event log ────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/events", response_model=list[AlertEvent])
async def list_events(tenant_id: str, limit: int = 50) -> list[AlertEvent]:
    """Most-recent alert events first."""
    try:
        docs = await cosmos.query_items(
            _c(),
            "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='alert_event' "
            "ORDER BY c.triggered_at DESC OFFSET 0 LIMIT @lim",
            parameters=[{"name": "@t", "value": tenant_id},
                        {"name": "@lim", "value": min(limit, 200)}],
            partition_key=tenant_id)
        return [AlertEvent.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/events/{event_id}/acknowledge", response_model=AlertEvent)
async def acknowledge_event(tenant_id: str, event_id: str) -> AlertEvent:
    try:
        doc = await cosmos.get_item(_c(), event_id, tenant_id)
        if doc.get("type") != "alert_event":
            raise NotFoundError(f"Alert event {event_id} not found")
        event = AlertEvent.from_cosmos(doc)
        event.acknowledged = True
        await cosmos.upsert_item(_c(), event.to_cosmos())
        return event
    except NotFoundError:
        raise HTTPException(status_code=404, detail={
            "error": "NOT_FOUND", "message": f"Alert event {event_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
