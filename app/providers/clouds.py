"""
GCP, Alibaba Cloud, and OCI provider adapters.

Each normalizes its native billing export shape into FOCUS. Live fetch paths are
documented inline; normalize() is pure and unit-tested against the documented
shapes (must be validated against real billing data on first ingest).

GCP Key Vault secret format (JSON) -- the full service account key JSON with one
extra field added:
    {
        "type": "service_account",
        "project_id": "my-gcp-project",
        "billing_export_table": "my-project.billing_export.gcp_billing_export_v1_XXXXXX",
        "private_key_id": "...",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\\n...",
        "client_email": "cloudlens@my-project.iam.gserviceaccount.com",
        ...
    }

The service account needs the BigQuery Data Viewer + BigQuery Job User roles on
the billing export dataset.
"""
from __future__ import annotations
import asyncio
from datetime import date

from app.logging_config import get_logger
from app.providers.base import CloudProvider, register, classify_service
from app.models.focus import (
    FocusRecord, ProviderName, CommitmentDiscountType,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Google Cloud — BigQuery billing export
# ══════════════════════════════════════════════════════════════════════════════
@register("gcp")
class GCPProvider(CloudProvider):
    provider_name = ProviderName.GCP

    def __init__(self, project_id: str = "", billing_export_table: str = "", sa_key: dict | None = None):
        self.project_id = project_id
        # billing_export_table may also be embedded in sa_key dict; extract it.
        if not billing_export_table and sa_key:
            billing_export_table = sa_key.pop("billing_export_table", "")
        self.billing_export_table = billing_export_table
        self.sa_key = sa_key or {}

    # cost data

    def _fetch_cost_sync(self, start: date, end: date) -> list[dict]:
        """
        BigQuery query against the standard GCP billing export table.
        Uses parameterised queries to prevent SQL injection.
        Requires BigQuery Data Viewer + BigQuery Job User on the export dataset.
        """
        from google.cloud import bigquery
        from google.oauth2 import service_account

        if not self.billing_export_table:
            raise ValueError(
                "billing_export_table must be set for GCP provider "
                "(embed it in the Key Vault secret as 'billing_export_table')"
            )

        credentials = service_account.Credentials.from_service_account_info(
            self.sa_key,
            scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
        )
        client = bigquery.Client(project=self.project_id, credentials=credentials)

        # Use backtick-quoted table ref; parameters carry the date values safely.
        query = f"""
            SELECT
                service.description           AS service_description,
                sku.description               AS sku_description,
                project.id                    AS project_id,
                location.region               AS region,
                DATE(usage_start_time)        AS day,
                SUM(cost)                     AS cost,
                SUM(IFNULL(
                    (SELECT SUM(c.amount) FROM UNNEST(credits) c), 0
                ))                            AS credits,
                SUM(usage.amount)             AS usage_amount,
                MAX(usage.unit)               AS usage_unit
            FROM `{self.billing_export_table}`
            WHERE DATE(usage_start_time) >= @start
              AND DATE(usage_start_time) <= @end
            GROUP BY 1, 2, 3, 4, 5
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start", "DATE", start.isoformat()),
                bigquery.ScalarQueryParameter("end",   "DATE", end.isoformat()),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        result = [dict(r) for r in rows]
        log.info(
            "gcp.cost_fetched",
            project_id=self.project_id,
            rows=len(result),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return result

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """Async wrapper -- runs the sync BigQuery call in the default thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_cost_sync, start, end)

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        out = []
        for r in raw_rows:
            cost = float(r.get("cost", 0.0))
            credits = float(r.get("credits", 0.0))   # negative for CUD/discounts
            effective = cost + credits
            cdt = CommitmentDiscountType.CUD if credits < 0 else CommitmentDiscountType.NONE
            out.append(FocusRecord(
                tenant_id=tenant_id,
                provider_name=ProviderName.GCP,
                sub_account_id=str(r.get("project_id", "")),
                charge_period_start=str(r.get("day", "")),
                billed_cost=cost,
                effective_cost=effective,
                list_cost=cost,
                service_name=r.get("service_description", "Unknown"),
                service_category=classify_service(r.get("service_description", "")),
                charge_description=r.get("sku_description", ""),
                consumed_quantity=float(r.get("usage_amount", 0.0)),
                consumed_unit=r.get("usage_unit", ""),
                commitment_discount_type=cdt,
                region_id=r.get("region", ""),
            ))
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Alibaba Cloud — BSS OpenAPI (DescribeInstanceBill)
# ══════════════════════════════════════════════════════════════════════════════
@register("alibaba")
class AlibabaProvider(CloudProvider):
    provider_name = ProviderName.ALIBABA

    def __init__(self, access_key_id: str = "", access_key_secret: str = "", region: str = "eu-central-1"):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = region

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """
        Live: BSS OpenAPI DescribeInstanceBill (BillingCycle=YYYY-MM, Granularity=DAILY).
        Returns Data.Items[] with PretaxAmount, ProductName, InstanceID, BillingDate,
        SubscriptionType (Subscription => reserved-style commitment), Usage, etc.
        """
        return []

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        out = []
        for r in raw_rows:
            pretax = float(r.get("PretaxAmount", 0.0))
            sub_type = r.get("SubscriptionType", "PayAsYouGo")
            cdt = (CommitmentDiscountType.RESERVED
                   if sub_type == "Subscription" else CommitmentDiscountType.NONE)
            out.append(FocusRecord(
                tenant_id=tenant_id,
                provider_name=ProviderName.ALIBABA,
                sub_account_id=str(r.get("OwnerID", "")),
                charge_period_start=str(r.get("BillingDate", "")),
                billed_cost=pretax,
                effective_cost=pretax,
                list_cost=float(r.get("PretaxGrossAmount", pretax)),
                service_name=r.get("ProductName", "Unknown"),
                service_category=classify_service(r.get("ProductName", "")),
                resource_id=r.get("InstanceID", ""),
                consumed_quantity=float(r.get("Usage", 0.0) or 0.0),
                consumed_unit=r.get("UsageUnit", ""),
                commitment_discount_type=cdt,
                region_id=r.get("Region", self.region),
                billing_currency=r.get("Currency", "USD"),
            ))
        return out


# ══════════════════════════════════════════════════════════════════════════════
# OCI — Usage API (RequestSummarizedUsages)
# ══════════════════════════════════════════════════════════════════════════════
@register("oci")
class OCIProvider(CloudProvider):
    provider_name = ProviderName.OCI

    def __init__(self, tenancy_ocid: str = "", config: dict | None = None):
        self.tenancy_ocid = tenancy_ocid
        self.config = config or {}

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """
        Live: OCI UsageapiClient.request_summarized_usages(
            RequestSummarizedUsagesDetails(tenant_id, time_usage_started,
              time_usage_ended, granularity='DAILY', query_type='COST',
              group_by=['service','compartmentName']))
        Returns items[] with computedAmount, service, compartmentName, unit, etc.
        """
        return []

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        out = []
        for r in raw_rows:
            amount = float(r.get("computedAmount", 0.0) or 0.0)
            out.append(FocusRecord(
                tenant_id=tenant_id,
                provider_name=ProviderName.OCI,
                sub_account_id=r.get("compartmentName", ""),
                sub_account_name=r.get("compartmentName", ""),
                charge_period_start=str(r.get("timeUsageStarted", ""))[:10],
                billed_cost=amount,
                effective_cost=amount,
                list_cost=amount,
                service_name=r.get("service", "Unknown"),
                service_category=classify_service(r.get("service", "")),
                consumed_quantity=float(r.get("computedQuantity", 0.0) or 0.0),
                consumed_unit=r.get("unit", ""),
                region_id=r.get("region", ""),
                billing_currency=r.get("currency", "USD"),
            ))
        return out
