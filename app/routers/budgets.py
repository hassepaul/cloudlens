"""Budget management router — /api/v1/budgets

Budgets are low-volume per-tenant configuration objects. To keep infra cheap we
store them in the existing waste_items container, discriminated by type='budget'
and partitioned by tenant_id (same pattern as waste items). No extra Cosmos
container is provisioned.
"""
from __future__ import annotations
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Depends, status

from app.config import get_settings
from app.exceptions import NotFoundError, CosmosError
from app.logging_config import get_logger
from app.models.budget import Budget, BudgetCreate, BudgetUpdate, BudgetStatus
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services import forecast as fc_svc

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/budgets", tags=["budgets"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _container() -> str:
    # budgets live alongside waste items, discriminated by type
    return get_settings().cosmos_container_waste_items


def _cr() -> str:
    return get_settings().cosmos_container_cost_records


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}", response_model=list[Budget])
async def list_budgets(tenant_id: str) -> list[Budget]:
    """List all budgets for a tenant."""
    try:
        docs = await cosmos.query_items(
            _container(),
            "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='budget'",
            parameters=[{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
        return [Budget.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}", response_model=Budget, status_code=status.HTTP_201_CREATED)
async def create_budget(tenant_id: str, payload: BudgetCreate) -> Budget:
    """Create a budget (tenant-wide or scoped to a tag dimension+value)."""
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=422,
                            detail={"error": "VALIDATION_ERROR",
                                    "message": "Body tenant_id must match path tenant_id"})
    if (payload.scope_dimension is None) != (payload.scope_value is None):
        raise HTTPException(status_code=422,
                            detail={"error": "VALIDATION_ERROR",
                                    "message": "scope_dimension and scope_value must both be set or both omitted"})
    try:
        budget = Budget(**payload.model_dump())
        await cosmos.upsert_item(_container(), budget.to_cosmos())
        log.info("budget.created", tenant_id=tenant_id, budget_id=budget.id)
        return budget
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/{budget_id}", response_model=Budget)
async def get_budget(tenant_id: str, budget_id: str) -> Budget:
    try:
        doc = await cosmos.get_item(_container(), budget_id, tenant_id)
        if doc.get("type") != "budget":
            raise NotFoundError(f"Budget {budget_id} not found")
        return Budget.from_cosmos(doc)
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Budget {budget_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{tenant_id}/{budget_id}", response_model=Budget)
async def update_budget(tenant_id: str, budget_id: str, payload: BudgetUpdate) -> Budget:
    try:
        doc = await cosmos.get_item(_container(), budget_id, tenant_id)
        if doc.get("type") != "budget":
            raise NotFoundError(f"Budget {budget_id} not found")
        budget = Budget.from_cosmos(doc)
        updates = payload.model_dump(exclude_unset=True)
        updated = budget.model_copy(update=updates)
        await cosmos.upsert_item(_container(), updated.to_cosmos())
        log.info("budget.updated", tenant_id=tenant_id, budget_id=budget_id)
        return updated
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Budget {budget_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(tenant_id: str, budget_id: str) -> None:
    try:
        doc = await cosmos.get_item(_container(), budget_id, tenant_id)
        if doc.get("type") != "budget":
            raise NotFoundError(f"Budget {budget_id} not found")
        await cosmos.delete_item(_container(), budget_id, tenant_id)
        log.info("budget.deleted", tenant_id=tenant_id, budget_id=budget_id)
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Budget {budget_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


# ── Status (spend-to-date + forecast projection) ──────────────────────────────

async def _month_to_date_spend(tenant_id: str, budget: Budget) -> tuple[float, list[dict]]:
    """Return (MTD spend for the budget's scope, daily series for projection)."""
    today = date.today()
    month_start = today.replace(day=1)

    # daily series across the whole tenant for projection
    series_start = today - timedelta(days=89)
    daily_rows = await cosmos.query_items(
        _cr(),
        """SELECT c.record_date, SUM(c.cost_eur) AS daily_cost
           FROM c WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e
           GROUP BY c.record_date""",
        parameters=[{"name": "@t", "value": tenant_id},
                    {"name": "@s", "value": series_start.isoformat()},
                    {"name": "@e", "value": today.isoformat()}],
        partition_key=tenant_id,
    )
    daily_rows.sort(key=lambda r: r.get("record_date", ""))
    daily = [{"date": r["record_date"], "cost_eur": round(r["daily_cost"], 2)} for r in daily_rows]

    # MTD spend for the scope
    if budget.scope_dimension and budget.scope_value:
        rows = await cosmos.query_items(
            _cr(),
            """SELECT c.cost_eur, c.tags FROM c
               WHERE c.tenant_id=@t AND c.record_date>=@s AND c.record_date<=@e""",
            parameters=[{"name": "@t", "value": tenant_id},
                        {"name": "@s", "value": month_start.isoformat()},
                        {"name": "@e", "value": today.isoformat()}],
            partition_key=tenant_id,
        )
        mtd = 0.0
        for r in rows:
            tags = r.get("tags") or {}
            for k, v in tags.items():
                if k.lower() == budget.scope_dimension.lower() and str(v) == budget.scope_value:
                    mtd += float(r.get("cost_eur", 0.0))
                    break
    else:
        mtd = sum(d["cost_eur"] for d in daily
                  if d["date"] >= month_start.isoformat())

    return round(mtd, 2), daily


@router.get("/{tenant_id}/{budget_id}/status", response_model=BudgetStatus)
async def budget_status(tenant_id: str, budget_id: str) -> BudgetStatus:
    """Live budget status: spend-to-date, forecast month-end projection, and state."""
    try:
        doc = await cosmos.get_item(_container(), budget_id, tenant_id)
        if doc.get("type") != "budget":
            raise NotFoundError(f"Budget {budget_id} not found")
        budget = Budget.from_cosmos(doc)

        mtd, daily = await _month_to_date_spend(tenant_id, budget)
        consumed_pct = round(mtd / budget.amount_eur * 100, 1) if budget.amount_eur else 0.0

        # project month-end via forecast (tenant-wide scope only; scoped budgets
        # use a simple run-rate extrapolation to stay accurate to the scope)
        today = date.today()
        if today.month == 12:
            next_month = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month = today.replace(month=today.month + 1, day=1)
        days_elapsed = today.day
        days_in_month = (next_month - today.replace(day=1)).days

        projected = None
        breach_date = None
        if budget.scope_dimension:
            # scope-accurate linear run-rate
            if days_elapsed > 0:
                projected = round(mtd / days_elapsed * days_in_month, 2)
        else:
            fc = fc_svc.forecast_spend(daily, horizon_days=days_in_month)
            projected = fc.month_end_projection
            # breach date: cumulative MTD + forecast crosses the budget
            cum = mtd
            for p in fc.points:
                if p.day < next_month.isoformat():
                    cum += p.value
                    if cum > budget.amount_eur:
                        breach_date = p.day
                        break

        projected_pct = (round(projected / budget.amount_eur * 100, 1)
                         if projected and budget.amount_eur else None)

        if consumed_pct >= 100:
            state = "breach"
        elif projected_pct and projected_pct >= 100:
            state = "projected_breach"
        elif consumed_pct >= budget.warning_threshold_pct:
            state = "warning"
        else:
            state = "ok"

        return BudgetStatus(
            budget_id=budget.id, name=budget.name, amount_eur=budget.amount_eur,
            spend_to_date_eur=mtd, consumed_pct=consumed_pct,
            projected_month_end_eur=projected, projected_consumed_pct=projected_pct,
            status=state, breach_date=breach_date,
            scope=(f"{budget.scope_dimension}={budget.scope_value}"
                   if budget.scope_dimension else "tenant"),
            headroom_eur=round(budget.amount_eur - mtd, 2),
        )
    except NotFoundError:
        raise HTTPException(status_code=404,
                            detail={"error": "NOT_FOUND", "message": f"Budget {budget_id} not found"})
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
