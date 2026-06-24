from __future__ import annotations
from enum import Enum
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ReportStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class ReportMeta(BaseModel):
    """Report metadata — Cosmos container: reports"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="report")
    tenant_id: str
    period_start: date
    period_end: date
    total_spend_eur: float = 0.0
    total_waste_eur: float = 0.0
    waste_pct: float = 0.0
    waste_items_count: int = 0
    critical_count: int = 0
    high_count: int = 0
    blob_url: Optional[str] = None
    blob_path: Optional[str] = None
    status: ReportStatus = ReportStatus.PENDING
    error_message: Optional[str] = None
    generated_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


    def to_cosmos(self) -> dict:
        data = self.model_dump(mode="json")
        data["_partitionKey"] = self.tenant_id
        return data
