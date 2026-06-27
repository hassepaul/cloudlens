"""Action execution models — AutoStop, AutoStart, schedule tagging."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    AUTOSTOP = "autostop"          # deallocate VM / stop instance
    AUTOSTART = "autostart"        # start a previously stopped resource
    SCHEDULE_TAG = "schedule_tag"  # apply start/stop schedule tags to a resource


class ActionStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"   # e.g. resource already in desired state


class ActionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    action_type: ActionType
    resource_id: str
    resource_name: str = ""
    provider: str = "azure"
    status: ActionStatus = ActionStatus.PENDING
    initiated_by: str = ""          # user identifier or "autostop-engine"
    initiated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: dict = Field(default_factory=dict)

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["type"] = "action_record"
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "ActionRecord":
        d = {k: v for k, v in doc.items()
             if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        d.pop("type", None)
        return cls(**d)


class ActionRequest(BaseModel):
    """Body for submitting a single action via the API."""
    resource_id: str = Field(..., description="Full cloud resource ID to act on")
    resource_name: str = Field(default="", description="Human-readable resource name")
    provider: str = Field(default="azure", pattern="^(azure|aws|gcp)$")
    initiated_by: str = Field(default="", description="Actor identity (user email or service name)")
    metadata: dict = Field(
        default_factory=dict,
        description="Provider-specific extras, e.g. {'region': 'eu-west-1'} for AWS",
    )


class BulkAutostopRequest(BaseModel):
    """Submit autostop actions for all resources matching the given IDs."""
    resource_ids: list[str] = Field(..., min_length=1, max_length=100)
    provider: str = Field(default="azure", pattern="^(azure|aws|gcp)$")
    initiated_by: str = Field(default="")
