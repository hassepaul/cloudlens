"""Unit economics router — /api/v1/unit-economics"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger
from app.services.unit_economics import (
    create_metric,
    list_metrics,
    get_metric,
    delete_metric,
    upsert_datapoints,
    list_datapoints,
    compute_cost_per_unit,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/unit-economics", tags=["unit-economics"])


# ── Request / response models ────────────────────────────────────────────────

class MetricCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120,
                      description="Human-readable metric name, e.g. 'Active users'")
    unit_label: str = Field(..., min_length=1, max_length=40,
                            description="Label for one unit, e.g. 'user', 'API call', 'order'")
    scope: Optional[dict] = Field(
        default=None,
        description=(
            "Optional filter for the cost numerator. "
            "Keys: resource_group | service_name | tag (dict). "
            "Omit to use full tenant cost."
        ),
    )


class DatapointBatch(BaseModel):
    """Batch upload of daily unit counts."""
    data: list[dict] = Field(
        ...,
        description='Array of {"date": "YYYY-MM-DD", "count": <number>}',
        min_length=1,
        max_length=366,
    )

    @field_validator("data")
    @classmethod
    def validate_entries(cls, v: list[dict]) -> list[dict]:
        for entry in v:
            if "date" not in entry:
                raise ValueError("Each entry must have a 'date' field")
            if "count" not in entry:
                raise ValueError("Each entry must have a 'count' field")
            try:
                date.fromisoformat(str(entry["date"]))
            except ValueError:
                raise ValueError(f"Invalid date format: {entry['date']} — use YYYY-MM-DD")
            if float(entry["count"]) < 0:
                raise ValueError("count must be >= 0")
        return v


# ── Metric CRUD ──────────────────────────────────────────────────────────────

@router.post("/{tenant_id}/metrics", status_code=201)
async def define_metric(tenant_id: str, body: MetricCreate) -> dict:
    """Create a new unit economics metric."""
    try:
        return await create_metric(
            tenant_id=tenant_id,
            name=body.name,
            unit_label=body.unit_label,
            scope=body.scope,
        )
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}/metrics")
async def get_metrics(tenant_id: str) -> dict:
    """List all unit metrics for a tenant."""
    try:
        metrics = await list_metrics(tenant_id)
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {"tenant_id": tenant_id, "metrics": metrics, "count": len(metrics)}


@router.get("/{tenant_id}/metrics/{metric_id}")
async def get_one_metric(tenant_id: str, metric_id: str) -> dict:
    try:
        return await get_metric(tenant_id, metric_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}/metrics/{metric_id}", status_code=200)
async def remove_metric(tenant_id: str, metric_id: str) -> dict:
    try:
        await delete_metric(tenant_id, metric_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {"tenant_id": tenant_id, "metric_id": metric_id, "deleted": True}


# ── Data point ingestion ─────────────────────────────────────────────────────

@router.post("/{tenant_id}/metrics/{metric_id}/data", status_code=200)
async def push_data(tenant_id: str, metric_id: str, body: DatapointBatch) -> dict:
    """
    Upload daily unit counts for a metric.

    Accepts a batch of up to 366 date+count pairs. Existing records for the
    same date are overwritten (idempotent).
    """
    try:
        saved = await upsert_datapoints(tenant_id, metric_id, body.data)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {"tenant_id": tenant_id, "metric_id": metric_id, "saved": saved}


@router.get("/{tenant_id}/metrics/{metric_id}/data")
async def get_data(
    tenant_id: str,
    metric_id: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """List raw daily unit counts for a metric."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    try:
        points = await list_datapoints(tenant_id, metric_id, start, end)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {
        "tenant_id": tenant_id,
        "metric_id": metric_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "data": points,
        "count": len(points),
    }


# ── Cost-per-unit analytics ──────────────────────────────────────────────────

@router.get("/{tenant_id}/cost-per-unit")
async def cost_per_unit(
    tenant_id: str,
    metric_id: str = Query(..., description="ID of the metric to analyse"),
    days: int = Query(default=30, ge=7, le=365),
) -> dict:
    """
    Return a daily cost-per-unit time series and trend summary.

    Example response shape:
    ```json
    {
      "metric_name": "Active users",
      "unit_label": "user",
      "average_cost_per_unit_eur": 0.000342,
      "trend": "decreasing",
      "series": [
        {"date": "2024-03-01", "cost_eur": 342.10, "unit_count": 1000000, "cost_per_unit_eur": 0.000342},
        ...
      ]
    }
    ```
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    try:
        return await compute_cost_per_unit(tenant_id, metric_id, start, end)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
