"""
AWS provider adapter.

Two ingestion paths (both produce FOCUS records):
  1. Cost Explorer GetCostAndUsage (default; near-real-time, grouped by service +
     linked account, with amortized + unblended metrics).
  2. CUR 2.0 / FOCUS 1.0 export in S3 (for customers who enable the native FOCUS
     export — richest data, includes commitment fields).

This adapter normalizes the Cost Explorer response shape. Live calls use boto3
with an assumed read-only role (CostExplorerReadOnly + ce:GetCostAndUsage). The
normalize() function is pure and unit-tested against the documented response
shape; it must be validated against a real account on first ingest.
"""
from __future__ import annotations
from datetime import date

from app.providers.base import CloudProvider, register, classify_service
from app.models.focus import (
    FocusRecord, Commitment, ProviderName, ChargeCategory, CommitmentDiscountType,
)


@register("aws")
class AWSProvider(CloudProvider):
    provider_name = ProviderName.AWS

    def __init__(self, role_arn: str = "", external_id: str = "", region: str = "eu-west-1"):
        self.role_arn = role_arn
        self.external_id = external_id
        self.region = region

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """
        Live: boto3 ce.get_cost_and_usage(
            TimePeriod={Start, End}, Granularity='DAILY',
            Metrics=['UnblendedCost','AmortizedCost','UsageQuantity'],
            GroupBy=[{Type:'DIMENSION',Key:'SERVICE'},
                     {Type:'DIMENSION',Key:'LINKED_ACCOUNT'}])
        Returns ResultsByTime[].Groups[]. Stubbed here (no live creds in sandbox).
        """
        return []

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        """
        Map Cost Explorer ResultsByTime → FOCUS.
        Each raw_row is one ResultsByTime entry: {TimePeriod, Groups[], Total}.
        """
        out: list[FocusRecord] = []
        for period in raw_rows:
            day = period.get("TimePeriod", {}).get("Start", "")
            for grp in period.get("Groups", []):
                keys = grp.get("Keys", [])
                service = keys[0] if len(keys) > 0 else "Unknown"
                account = keys[1] if len(keys) > 1 else ""
                metrics = grp.get("Metrics", {})
                unblended = float(metrics.get("UnblendedCost", {}).get("Amount", 0.0))
                amortized = float(metrics.get("AmortizedCost", {}).get("Amount", unblended))
                qty = float(metrics.get("UsageQuantity", {}).get("Amount", 0.0))
                # commitment detection: amortized < unblended implies RI/SP discount
                cdt = CommitmentDiscountType.NONE
                if amortized < unblended:
                    cdt = CommitmentDiscountType.SAVINGS_PLAN
                out.append(FocusRecord(
                    tenant_id=tenant_id,
                    provider_name=ProviderName.AWS,
                    sub_account_id=account,
                    charge_period_start=day,
                    billed_cost=unblended,
                    effective_cost=amortized,
                    list_cost=unblended,
                    service_name=service,
                    service_category=classify_service(service),
                    charge_category=ChargeCategory.USAGE,
                    consumed_quantity=qty,
                    commitment_discount_type=cdt,
                    region_id=self.region,
                ))
        return out

    async def fetch_commitments(self) -> list[dict]:
        """Live: ce.get_savings_plans_utilization + describe_reserved_instances."""
        return []

    def normalize_commitments(self, tenant_id: str, raw: list[dict]) -> list[Commitment]:
        out = []
        for c in raw:
            out.append(Commitment(
                tenant_id=tenant_id, provider_name=ProviderName.AWS,
                commitment_type=CommitmentDiscountType(c.get("type", "Savings Plan")),
                service=c.get("service", "Compute"),
                term_months=int(c.get("term_months", 12)),
                hourly_commitment_eur=float(c.get("hourly_commitment", 0.0)),
                utilization_pct=float(c.get("utilization_pct", 0.0)),
                coverage_eligible_eur=float(c.get("coverage_eligible", 0.0)),
                start_date=c.get("start_date"),
                end_date=c.get("end_date"),
            ))
        return out
