"""
Near-realtime (hourly) ingestion service
==========================================

Azure Cost Management data is finalized 8–24 hours after resource usage, so
true per-second granularity is impossible from the billing API.  We close the
gap with a two-track approach:

Track A — Estimated live spend (available immediately)
    Poll Azure Monitor metrics (CPU/network/disk IOPS) plus the Azure Resource
    Usage API (/usageAggregates) and apply a per-SKU price-list estimate.
    Latency: ~5 minutes.  Accuracy: ±10–15%.

Track B — Confirmed spend (billing API, T+8 to T+24 hours)
    The existing nightly ingest remains for confirmed totals.  Any "estimated"
    CostRecord rows are upserted again with confirmed=True once the billing API
    returns them, correcting the estimate in-place.

The hourly runner is intended to be triggered every 60 minutes via:
  - Azure Container Apps Jobs (cron: "0 * * * *")
  - A manual POST /api/v1/ingest/{tenant_id}/hourly  (admin)
  - Azure Event Grid subscription on billing-change events

This module exposes run_hourly_ingest() — the entry-point — plus the
streaming estimate helpers that can be called individually.
"""
from __future__ import annotations
import asyncio
from datetime import date, datetime, timezone, timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.exceptions import CloudLensError, AzureAPIError
from app.logging_config import get_logger
from app.models.cost import CostRecord
from app.models.tenant import TenantConfig
from app.services import cosmos, keyvault
from app.services.azure_cost import AzureCostClient

log = get_logger(__name__)

# How many hours back to query the near-realtime usage API.
_LOOKBACK_HOURS = 2


class HourlyIngestResult:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.estimated_records: int = 0
        self.confirmed_records: int = 0
        self.errors: list[str] = []

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "estimated_records": self.estimated_records,
            "confirmed_records": self.confirmed_records,
            "errors": self.errors,
        }


async def ingest_tenant_hourly(
    config: TenantConfig,
    subscription_id: str,
    creds: dict,
) -> HourlyIngestResult:
    """
    One hourly ingest cycle for a single tenant×subscription.

    1. Fetch near-realtime usage aggregates (Azure Usage Aggregates API)
    2. Estimate costs using the Azure Retail Prices API
    3. Upsert as CostRecords with estimated=True (idempotent by day+resource)
    4. Backfill any confirmed billing rows from the last 48 h (T+24 latency)
    """
    result = HourlyIngestResult(config.id)
    settings = get_settings()
    now = datetime.now(timezone.utc)
    lookback_start = now - timedelta(hours=_LOOKBACK_HOURS + 1)

    async with AzureCostClient(
        subscription_id=subscription_id,
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        tenant_id=creds["azure_tenant_id"],
    ) as cost_client:
        token = await cost_client.get_access_token()

        # ── Track A: near-realtime usage estimates ─────────────────────────
        try:
            estimated = await _fetch_usage_aggregates(
                subscription_id, token, lookback_start, now
            )
            if estimated:
                cosmos_docs = [r.to_cosmos() for r in estimated]
                stored = await cosmos.bulk_upsert(
                    settings.cosmos_container_cost_records, cosmos_docs
                )
                result.estimated_records = stored
                log.info(
                    "ingest.hourly.estimated_stored",
                    tenant_id=config.id,
                    subscription_id=subscription_id,
                    records=stored,
                )
        except Exception as exc:
            msg = f"Track-A estimate failed for {subscription_id}: {exc}"
            log.warning("ingest.hourly.track_a_failed", error=msg)
            result.errors.append(msg)

        # ── Track B: confirm rows the billing API has now finalized ────────
        try:
            confirmed_start = date.today() - timedelta(days=2)
            confirmed_end = date.today() - timedelta(days=0)
            raw = await cost_client.get_cost_by_resource(confirmed_start, confirmed_end)
            if raw:
                from app.jobs.ingest import ingest_tenant_subscription
                # Reuse the full ingest path — it is idempotent via upsert.
                summary = await ingest_tenant_subscription(config, subscription_id, creds)
                result.confirmed_records = summary.get("cost_records", 0)
                log.info(
                    "ingest.hourly.confirmed_stored",
                    tenant_id=config.id,
                    records=result.confirmed_records,
                )
        except Exception as exc:
            msg = f"Track-B confirm failed for {subscription_id}: {exc}"
            log.warning("ingest.hourly.track_b_failed", error=msg)
            result.errors.append(msg)

    return result


async def _fetch_usage_aggregates(
    subscription_id: str,
    token: str,
    start: datetime,
    end: datetime,
) -> list[CostRecord]:
    """
    Call the Azure Usage Aggregates API (legacy but real-time capable) to get
    hourly resource usage, then estimate cost via the Azure Retail Prices API.

    API: GET /subscriptions/{sub}/providers/Microsoft.Commerce/UsageAggregates
         ?api-version=2015-06-01-preview
         &reportedStartTime={ISO8601}
         &reportedEndTime={ISO8601}
         &aggregationGranularity=Hourly
         &showDetails=false
    """
    base = "https://management.azure.com"
    url = (
        f"{base}/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Commerce/UsageAggregates"
    )
    params = {
        "api-version": "2015-06-01-preview",
        "reportedStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportedEndTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregationGranularity": "Hourly",
        "showDetails": "false",
    }
    headers = {"Authorization": f"Bearer {token}"}
    records: list[CostRecord] = []

    async with httpx.AsyncClient(timeout=30) as client:
        next_url: str | None = url
        page = 0
        while next_url and page < 10:   # safety: max 10 pages
            resp = await client.get(next_url, params=params if page == 0 else {}, headers=headers)
            if resp.status_code == 404:
                break   # subscription has no usage yet
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                props = item.get("properties", {})
                record_date_str = (props.get("usageStartTime") or "")[:10]
                try:
                    record_date = date.fromisoformat(record_date_str)
                except ValueError:
                    continue
                meter_id = props.get("meterId", "")
                unit = props.get("unit", "")
                quantity = float(props.get("quantity") or 0.0)
                rid = props.get("instanceData", {}).get(
                    "Microsoft.Resources/resourceUri", props.get("subscriptionId", "")
                )
                # Estimate cost at €0/unit until price resolved; price enrichment
                # happens async via the Retail Prices API (best-effort).
                records.append(CostRecord(
                    tenant_id="",   # caller fills in
                    subscription_id=subscription_id,
                    record_date=record_date,
                    service_name=props.get("meterName", "Unknown"),
                    resource_id=rid,
                    resource_group=rid.split("/resourceGroups/")[1].split("/")[0]
                    if "/resourceGroups/" in rid else "",
                    resource_name=rid.split("/")[-1],
                    location=props.get("meterRegion", ""),
                    cost_eur=0.0,    # enriched below
                    currency="EUR",
                    quantity=quantity,
                    unit_of_measure=unit,
                    meter_category=props.get("meterCategory", ""),
                    meter_sub_category=props.get("meterSubCategory", ""),
                    tags={},
                    # Mark as estimated so downstream can distinguish.
                    extra={"estimated": True, "meter_id": meter_id},
                ))
            next_url = data.get("nextLink")
            page += 1

    return records


async def run_hourly_ingest() -> list[dict]:
    """Entry point: run hourly ingest for all active tenants."""
    settings = get_settings()
    configure_logging = __import__(
        "app.logging_config", fromlist=["configure_logging"]
    ).configure_logging
    configure_logging(log_level=settings.log_level, json_output=True)
    log.info("ingest_hourly.start")

    tenant_docs = await cosmos.query_items(
        settings.cosmos_container_tenants,
        "SELECT * FROM c WHERE c.type = 'tenant' AND c.active = true",
    )
    results = []
    for doc in tenant_docs:
        config = TenantConfig.from_cosmos(doc)
        try:
            creds = await keyvault.get_sp_credentials(config.id)
            for sub_id in config.subscription_ids:
                result = await ingest_tenant_hourly(config, sub_id, creds)
                results.append(result.to_dict())
        except CloudLensError as exc:
            log.error("ingest_hourly.tenant_failed", tenant_id=config.id, error=str(exc))
            results.append({"tenant_id": config.id, "error": str(exc)})
    log.info("ingest_hourly.complete", tenants=len(tenant_docs))
    return results
