"""
Audit logging
=============

An append-only, tamper-evident audit trail of security- and change-relevant
events. This is a core SOC 2 control (CC7.2 monitoring of system components,
CC6.1/6.3 logical access changes, and the change-management criteria). Every
record captures who did what, to which tenant/resource, from where, and when.

Tamper-evidence: each record carries a SHA-256 hash chaining it to the previous
record for the same tenant (like a mini blockchain), so an auditor can verify
the log has not been altered or back-dated. Records have no delete path and a
long TTL.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class AuditAction(str, Enum):
    # access
    LOGIN = "login"
    ACCESS_DENIED = "access_denied"
    TENANT_SCOPE_VIOLATION = "tenant_scope_violation"
    RATE_LIMITED = "rate_limited"
    # change management
    TENANT_CREATED = "tenant_created"
    TENANT_UPDATED = "tenant_updated"
    TENANT_DELETED = "tenant_deleted"
    BUDGET_CREATED = "budget_created"
    BUDGET_UPDATED = "budget_updated"
    BUDGET_DELETED = "budget_deleted"
    ALERT_RULE_CREATED = "alert_rule_created"
    ALERT_RULE_UPDATED = "alert_rule_updated"
    ALERT_RULE_DELETED = "alert_rule_deleted"
    WASTE_RESOLVED = "waste_resolved"
    # data
    INGEST_TRIGGERED = "ingest_triggered"
    REPORT_GENERATED = "report_generated"
    REPORT_DOWNLOADED = "report_downloaded"
    EVIDENCE_EXPORTED = "evidence_exported"
    # identity / SSO / SCIM
    SSO_LOGIN = "sso_login"
    SSO_LOGIN_FAILED = "sso_login_failed"
    SAML_CONFIG_UPDATED = "saml_config_updated"
    SCIM_TOKEN_ROTATED = "scim_token_rotated"
    SCIM_USER_CREATED = "scim_user_created"
    SCIM_USER_UPDATED = "scim_user_updated"
    SCIM_USER_DEACTIVATED = "scim_user_deactivated"
    SCIM_USER_DELETED = "scim_user_deleted"


class AuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="audit_record")
    tenant_id: str                       # partition key ("_system" for platform events)
    action: AuditAction
    actor: str                           # subject (oid/sub) or "system" / "api-key:<name>"
    actor_type: str = "user"             # "user" | "api_key" | "system"
    resource_id: str = ""
    outcome: str = "success"             # "success" | "denied" | "error"
    source_ip: str = ""
    detail: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prev_hash: str = ""
    record_hash: str = ""
    ttl: int = Field(default=63_072_000)   # 2 years (SOC 2 evidence retention)

    def compute_hash(self) -> str:
        payload = {
            "tenant_id": self.tenant_id, "action": self.action.value, "actor": self.actor,
            "resource_id": self.resource_id, "outcome": self.outcome,
            "timestamp": self.timestamp.isoformat(), "detail": self.detail,
            "prev_hash": self.prev_hash,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "AuditRecord":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


def chain_record(record: AuditRecord, prev_hash: str) -> AuditRecord:
    """Link a record to the previous one and seal it with its hash."""
    record.prev_hash = prev_hash or ""
    record.record_hash = record.compute_hash()
    return record


def verify_chain(records: list[AuditRecord]) -> tuple[bool, Optional[str]]:
    """
    Verify an ordered list of audit records forms an intact hash chain.
    Returns (ok, first_broken_id). Used by the compliance evidence export to
    prove the audit trail is tamper-evident.
    """
    prev = ""
    for r in sorted(records, key=lambda x: x.timestamp):
        if r.prev_hash != prev:
            return False, r.id
        if r.compute_hash() != r.record_hash:
            return False, r.id
        prev = r.record_hash
    return True, None
