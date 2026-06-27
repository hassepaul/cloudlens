"""
AWS provider adapter.

Two ingestion paths (both produce FOCUS records):
  1. Cost Explorer GetCostAndUsage (default; near-real-time, grouped by service +
     linked account, with amortized + unblended metrics).
  2. CUR 2.0 / FOCUS 1.0 export in S3 (for customers who enable the native FOCUS
     export -- richest data, includes commitment fields).

Live calls assume a read-only cross-account IAM role via STS. The role must have:
  - ce:GetCostAndUsage
  - ce:GetSavingsPlansUtilization
  - ce:GetReservationUtilization

Key Vault secret format (JSON):
    {
        "role_arn":    "arn:aws:iam::123456789012:role/CloudLensReadOnly",
        "external_id": "cloudlens-<tenant_id>",
        "region":      "us-east-1"
    }

boto3 is synchronous -- all calls run in a thread-pool executor so they do not
block the event loop.
"""
from __future__ import annotations
import asyncio
from datetime import date, timedelta

from app.logging_config import get_logger
from app.providers.base import CloudProvider, register, classify_service
from app.models.focus import (
    FocusRecord, Commitment, ProviderName, ChargeCategory, CommitmentDiscountType,
)

log = get_logger(__name__)


@register("aws")
class AWSProvider(CloudProvider):
    provider_name = ProviderName.AWS

    def __init__(self, role_arn: str = "", external_id: str = "", region: str = "us-east-1"):
        self.role_arn = role_arn
        self.external_id = external_id
        self.region = region

    # credential helpers

    def _assume_role(self, session_name: str = "cloudlens-ingest") -> dict:
        """STS AssumeRole -- return temporary credentials dict."""
        import boto3
        sts = boto3.client("sts")
        kwargs: dict = {
            "RoleArn": self.role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": 3600,
        }
        if self.external_id:
            kwargs["ExternalId"] = self.external_id
        assumed = sts.assume_role(**kwargs)
        return assumed["Credentials"]

    def _ce_client(self, creds: dict):
        """Return a boto3 Cost Explorer client using assumed-role credentials."""
        import boto3
        return boto3.client(
            "ce",
            region_name=self.region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    # cost data

    def _fetch_cost_sync(self, start: date, end: date) -> list[dict]:
        """Paginated Cost Explorer GetCostAndUsage. Returns raw ResultsByTime list."""
        creds = self._assume_role()
        ce = self._ce_client(creds)
        all_periods: list[dict] = []
        kwargs: dict = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "AmortizedCost", "UsageQuantity"],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
            ],
        }
        while True:
            resp = ce.get_cost_and_usage(**kwargs)
            all_periods.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
        log.info(
            "aws.cost_fetched",
            role_arn=self.role_arn,
            periods=len(all_periods),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return all_periods

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """Async wrapper -- runs the sync boto3 call in the default thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_cost_sync, start, end)

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        """Map Cost Explorer ResultsByTime to FOCUS records."""
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
                if amortized < unblended * 0.99:  # 1% tolerance for float noise
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

    # commitments

    def _fetch_commitments_sync(self) -> list[dict]:
        """Savings Plans + Reserved Instance utilization for the trailing 30 days."""
        end = date.today()
        start = end - timedelta(days=30)
        period = {"Start": start.isoformat(), "End": end.isoformat()}
        creds = self._assume_role(session_name="cloudlens-commitments")
        ce = self._ce_client(creds)
        results: list[dict] = []

        # Savings Plans
        try:
            sp_resp = ce.get_savings_plans_utilization(TimePeriod=period)
            for entry in sp_resp.get("SavingsPlansUtilizationsByTime", []):
                util = entry.get("Utilization", {})
                savings = entry.get("Savings", {})
                total_commitment = float(util.get("TotalCommitment", {}).get("Amount", 0.0))
                results.append({
                    "type": "Savings Plan",
                    "service": "Compute",
                    "term_months": 12,
                    "hourly_commitment": total_commitment / (30 * 24) if total_commitment > 0 else 0.0,
                    "utilization_pct": float(util.get("UtilizationPercentage", 0.0)),
                    "coverage_eligible": float(
                        savings.get("OnDemandCostEquivalent", {}).get("Amount", 0.0)
                    ),
                })
        except Exception as exc:
            log.warning("aws.savings_plans_fetch_failed", error=str(exc))

        # Reserved Instances
        try:
            ri_resp = ce.get_reservation_utilization(TimePeriod=period)
            for entry in ri_resp.get("UtilizationsByTime", []):
                total = entry.get("Total", {})
                amortized_fee = float(total.get("TotalAmortizedFee", {}).get("Amount", 0.0))
                results.append({
                    "type": "Reserved",
                    "service": "EC2",
                    "term_months": 12,
                    "hourly_commitment": amortized_fee / (30 * 24) if amortized_fee > 0 else 0.0,
                    "utilization_pct": float(total.get("UtilizationPercentage", 0.0)),
                    "coverage_eligible": float(total.get("NetRISavings", {}).get("Amount", 0.0)),
                })
        except Exception as exc:
            log.warning("aws.ri_utilization_fetch_failed", error=str(exc))

        log.info("aws.commitments_fetched", role_arn=self.role_arn, count=len(results))
        return results

    async def fetch_commitments(self) -> list[dict]:
        """Async wrapper for commitment utilization fetch."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_commitments_sync)

    def normalize_commitments(self, tenant_id: str, raw: list[dict]) -> list[Commitment]:
        out = []
        for c in raw:
            out.append(Commitment(
                tenant_id=tenant_id,
                provider_name=ProviderName.AWS,
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
