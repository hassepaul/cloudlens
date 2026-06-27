"""Policy enforcement models — rules, conditions, actions, violations."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid

from pydantic import BaseModel, Field


# ── Condition types ───────────────────────────────────────────────────────────

class ConditionType(str, Enum):
    SPEND_THRESHOLD    = "spend_threshold"    # spend > threshold_eur in period
    SPEND_ANOMALY      = "spend_anomaly"      # Holt-Winters z-score above min
    RESOURCE_IDLE      = "resource_idle"      # CPU < pct for N days (any resource)
    MISSING_TAG        = "missing_tag"        # resources missing a required tag key
    UNBUDGETED_SPEND   = "unbudgeted_spend"   # spend with no matching budget > min
    RI_UTILIZATION_LOW = "ri_low"             # commitment utilization < pct
    REGION_NOT_ALLOWED = "region_blocked"     # resources deployed in disallowed regions
    WASTE_THRESHOLD    = "waste_threshold"    # open recoverable waste > threshold_eur


class PolicyCondition(BaseModel):
    """
    Flat condition model — only the fields relevant to condition_type are used.
    All fields have safe defaults so unused ones don't cause validation errors.
    """
    condition_type: ConditionType

    # SPEND_THRESHOLD
    threshold_eur: float = 0.0
    period: str = Field(default="daily", pattern="^(daily|weekly|monthly)$")

    # SPEND_ANOMALY
    min_z_score: float = 2.0
    min_excess_eur: float = 0.0

    # RESOURCE_IDLE
    cpu_threshold_pct: float = 5.0
    lookback_days: int = Field(default=14, ge=1, le=90)

    # MISSING_TAG
    required_tag_key: str = ""

    # UNBUDGETED_SPEND
    min_unbudgeted_eur: float = 100.0

    # RI_UTILIZATION_LOW
    ri_threshold_pct: float = 80.0

    # REGION_NOT_ALLOWED
    allowed_regions: list[str] = Field(default_factory=list)

    # WASTE_THRESHOLD
    waste_threshold_eur: float = 500.0

    # Scope filters (apply to any condition type)
    cloud_filter: str = ""     # empty = all clouds; e.g. "aws", "gcp", "azure"
    service_filter: str = ""   # partial match on service_name (case-insensitive)


# ── Action types ──────────────────────────────────────────────────────────────

class PolicyActionType(str, Enum):
    SEND_ALERT        = "send_alert"        # create an AlertEvent in-app
    AUTOSTOP_RESOURCE = "autostop_resource" # deallocate offending resources
    WEBHOOK           = "webhook"           # HTTP POST (Slack / Teams / PagerDuty)
    TAG_RESOURCE      = "tag_resource"      # apply enforcement tags to resources


class PolicyAction(BaseModel):
    action_type: PolicyActionType

    # SEND_ALERT
    severity: str = Field(default="medium", pattern="^(critical|high|medium|info)$")
    message_template: str = ""   # {{policy_name}}, {{tenant_id}}, {{evidence}} placeholders

    # WEBHOOK
    webhook_url: str = ""
    webhook_secret: str = ""     # HMAC-SHA256 signing secret (stored encrypted in KV)

    # TAG_RESOURCE
    enforce_tags: dict = Field(default_factory=dict)   # tags to apply to offending resources
    tag_cloud: str = "azure"     # which cloud's ARM/API to use for tagging


# ── Policy rule ───────────────────────────────────────────────────────────────

class PolicyRule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=160)
    description: str = ""

    conditions: list[PolicyCondition] = Field(..., min_length=1)
    condition_logic: str = Field(default="AND", pattern="^(AND|OR)$")
    actions: list[PolicyAction] = Field(..., min_length=1)

    # Scope: which clouds / resource tags this policy applies to.
    # Empty lists = unrestricted.
    scope_clouds: list[str] = Field(default_factory=list)
    scope_tag_filters: dict = Field(default_factory=dict)

    enabled: bool = True
    severity: str = Field(default="medium", pattern="^(critical|high|medium|info)$")

    # Cooldown prevents re-triggering the same policy within N hours.
    cooldown_hours: int = Field(default=24, ge=0, le=720)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_triggered_at: Optional[datetime] = None
    trigger_count: int = 0

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["type"] = "policy_rule"
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "PolicyRule":
        d = {k: v for k, v in doc.items()
             if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        d.pop("type", None)
        return cls(**d)


class PolicyRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    description: str = ""
    conditions: list[PolicyCondition]
    condition_logic: str = Field(default="AND", pattern="^(AND|OR)$")
    actions: list[PolicyAction]
    scope_clouds: list[str] = Field(default_factory=list)
    scope_tag_filters: dict = Field(default_factory=dict)
    enabled: bool = True
    severity: str = Field(default="medium", pattern="^(critical|high|medium|info)$")
    cooldown_hours: int = Field(default=24, ge=0, le=720)


class PolicyRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    conditions: Optional[list[PolicyCondition]] = None
    condition_logic: Optional[str] = None
    actions: Optional[list[PolicyAction]] = None
    scope_clouds: Optional[list[str]] = None
    scope_tag_filters: Optional[dict] = None
    enabled: Optional[bool] = None
    severity: Optional[str] = None
    cooldown_hours: Optional[int] = None


# ── Policy violation ──────────────────────────────────────────────────────────

class PolicyViolation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    policy_id: str
    policy_name: str
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    conditions_met: list[str] = Field(default_factory=list)  # condition_type values
    resource_ids: list[str] = Field(default_factory=list)    # offending resources
    evidence: dict = Field(default_factory=dict)             # actual values that fired
    actions_taken: list[str] = Field(default_factory=list)   # action_type values executed
    cloud: str = ""
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    resolved_by: str = ""

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["type"] = "policy_violation"
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "PolicyViolation":
        d = {k: v for k, v in doc.items()
             if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        d.pop("type", None)
        return cls(**d)


class PolicyViolationResolve(BaseModel):
    resolved_by: str = Field(..., min_length=1)
