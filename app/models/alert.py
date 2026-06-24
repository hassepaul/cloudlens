from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from enum import Enum

from pydantic import BaseModel, Field


class AlertType(str, Enum):
    BUDGET_BREACH = "budget_breach"            # a budget crosses warning/breach
    SPEND_SPIKE = "spend_spike"                # tenant daily spend anomaly
    RESOURCE_ANOMALY = "resource_anomaly"      # a specific resource spikes
    WASTE_THRESHOLD = "waste_threshold"        # recoverable spend exceeds a €/% threshold
    COMMITMENT_IDLE = "commitment_idle"        # idle commitment exceeds a threshold


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


class AlertChannel(str, Enum):
    IN_APP = "in_app"        # stored, shown in the console (always on)
    EMAIL = "email"          # requires SMTP/SendGrid/ACS integration
    WEBHOOK = "webhook"      # POST to a URL (Slack/Teams/PagerDuty)


class AlertRuleBase(BaseModel):
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=120)
    alert_type: AlertType
    # threshold semantics depend on type:
    #  budget_breach   → trigger at consumed_pct >= threshold (default 100)
    #  spend_spike     → trigger at z-score >= threshold (default 2.0)
    #  resource_anomaly→ trigger at z-score >= threshold
    #  waste_threshold → trigger at recoverable_eur >= threshold
    #  commitment_idle → trigger at idle_eur >= threshold
    threshold: float = 100.0
    # optional scope filters
    provider: Optional[str] = None
    sub_account_id: Optional[str] = None
    channels: list[AlertChannel] = Field(default_factory=lambda: [AlertChannel.IN_APP])
    webhook_url: Optional[str] = None
    email_to: Optional[str] = None
    enabled: bool = True


class AlertRuleCreate(AlertRuleBase):
    pass


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    threshold: Optional[float] = None
    enabled: Optional[bool] = None
    channels: Optional[list[AlertChannel]] = None
    webhook_url: Optional[str] = None
    email_to: Optional[str] = None


class AlertRule(AlertRuleBase):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="alert_rule")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "AlertRule":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


class AlertEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="alert_event")
    tenant_id: str
    rule_id: str
    rule_name: str
    alert_type: AlertType
    severity: AlertSeverity
    title: str
    title_it: str = ""
    detail: dict = Field(default_factory=dict)     # resource_id, amount, z_score, etc.
    impact_eur: float = 0.0
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delivered_channels: list[str] = Field(default_factory=list)
    acknowledged: bool = False
    # TTL so the event log self-prunes (180 days)
    ttl: int = Field(default=15_552_000)

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "AlertEvent":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)
