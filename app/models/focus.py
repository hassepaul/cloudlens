"""
FOCUS-normalized cost model
===========================

CloudLens normalizes every provider's billing data into a single schema based on
the FinOps Open Cost and Usage Specification (FOCUS) — the FinOps Foundation's
open standard that AWS, Azure, Google Cloud, OCI, and Alibaba Cloud all now
publish native exports for. Normalizing to FOCUS (rather than a bespoke schema)
is what makes cross-cloud allocation, commitment analysis, and unit economics
work uniformly across providers.

This is a pragmatic subset of FOCUS 1.1 — the columns CloudLens actually uses —
plus a CloudLens-internal partition key and TTL for Cosmos.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ProviderName(str, Enum):
    AZURE = "Microsoft Azure"
    AWS = "Amazon Web Services"
    GCP = "Google Cloud"
    ALIBABA = "Alibaba Cloud"
    OCI = "Oracle Cloud Infrastructure"
    ANTHROPIC = "Anthropic"
    OPENAI = "OpenAI"


class ChargeCategory(str, Enum):
    USAGE = "Usage"
    PURCHASE = "Purchase"
    TAX = "Tax"
    CREDIT = "Credit"
    ADJUSTMENT = "Adjustment"


class ServiceCategory(str, Enum):
    COMPUTE = "Compute"
    STORAGE = "Storage"
    DATABASES = "Databases"
    NETWORKING = "Networking"
    AI_ML = "AI and Machine Learning"
    ANALYTICS = "Analytics"
    SECURITY = "Security"
    MANAGEMENT = "Management and Governance"
    OTHER = "Other"


class CommitmentDiscountType(str, Enum):
    NONE = ""
    RESERVED = "Reserved"               # Azure RI, AWS RI, Alibaba/OCI reserved
    SAVINGS_PLAN = "Savings Plan"       # AWS Savings Plans
    CUD = "Committed Use Discount"      # GCP CUDs
    SPOT = "Spot"


class FocusRecord(BaseModel):
    """One normalized line of billing data, FOCUS-aligned."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="focus_record")
    tenant_id: str

    # ── provider / account ──
    provider_name: ProviderName
    billing_account_id: str = ""             # AWS payer, GCP billing account, Azure billing
    sub_account_id: str = ""                 # AWS account, GCP project, Azure subscription, OCI compartment
    sub_account_name: str = ""

    # ── time ──
    charge_period_start: date
    billing_currency: str = "EUR"

    # ── cost (FOCUS core) ──
    billed_cost: float = Field(0.0, description="What appears on the invoice")
    effective_cost: float = Field(0.0, description="Amortized cost incl. commitments")
    list_cost: float = Field(0.0, description="Cost at public on-demand rates")

    # ── service ──
    service_name: str = ""
    service_category: ServiceCategory = ServiceCategory.OTHER
    charge_category: ChargeCategory = ChargeCategory.USAGE
    charge_description: str = ""

    # ── resource ──
    resource_id: str = ""
    resource_name: str = ""
    resource_type: str = ""
    region_id: str = ""

    # ── usage ──
    consumed_quantity: float = 0.0
    consumed_unit: str = ""

    # ── commitments ──
    commitment_discount_id: str = ""
    commitment_discount_type: CommitmentDiscountType = CommitmentDiscountType.NONE

    # ── allocation ──
    tags: dict[str, str] = Field(default_factory=dict)
    allocated_cost_center: str = ""          # filled by the allocation engine

    # ── CloudLens internal ──
    ttl: int = Field(default=7_776_000, description="90 days")
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "FocusRecord":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)


class Commitment(BaseModel):
    """A reservation / savings plan / CUD held by the tenant."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(default="commitment")
    tenant_id: str
    provider_name: ProviderName
    commitment_type: CommitmentDiscountType
    sub_account_id: str = ""
    service: str = ""                        # e.g. "Compute", "EC2", "Cloud SQL"
    region: str = ""
    term_months: int = 12                    # 12 or 36
    payment_option: str = ""                 # "all_upfront" | "partial" | "no_upfront"
    hourly_commitment_eur: float = 0.0       # for savings plans / CUDs
    quantity: float = 0.0                    # for reservations (instance count)
    instance_type: str = ""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    utilization_pct: float = 0.0             # % of the commitment actually used
    coverage_eligible_eur: float = 0.0       # on-demand spend this could have covered

    def to_cosmos(self) -> dict:
        d = self.model_dump(mode="json")
        d["_partitionKey"] = self.tenant_id
        return d

    @classmethod
    def from_cosmos(cls, doc: dict) -> "Commitment":
        for k in ("_partitionKey", "_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return cls(**doc)
