"""
CloudLens Nightly Ingestion Job
Azure Container Apps Job entrypoint — runs at 02:00 UTC daily.
Iterates all active tenants, fetches cost data, stores to Cosmos, runs waste engine.
"""
from __future__ import annotations
import asyncio
import sys
from datetime import date, timedelta, datetime, timezone

from app.config import get_settings
from app.exceptions import CloudLensError
from app.logging_config import configure_logging, get_logger
from app.models.cost import CostRecord
from app.models.tenant import TenantConfig, CloudProvider
from app.services import cosmos, keyvault
from app.services.azure_cost import AzureCostClient
from app.services.waste_engine import run_all_rules

log = get_logger(__name__)


async def ingest_tenant_subscription(
    config: TenantConfig,
    subscription_id: str,
    creds: dict,
) -> dict:
    """
    Full ingest cycle for one tenant × subscription:
    1. Fetch cost data from Azure Cost Management API
    2. Persist CostRecords to Cosmos
    3. Run waste detection rules
    4. Persist WasteItems to Cosmos
    Returns a summary dict.
    """
    settings = get_settings()
    lookback = settings.ingest_lookback_days
    end_date = date.today() - timedelta(days=1)  # yesterday (billing settled)
    start_date = end_date - timedelta(days=lookback - 1)

    log.info(
        "ingest.subscription_start",
        tenant_id=config.id,
        subscription_id=subscription_id,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
    )

    async with AzureCostClient(
        subscription_id=subscription_id,
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        tenant_id=creds["azure_tenant_id"],
    ) as cost_client:

        # ── 1. Fetch raw cost data ─────────────────────────────────────────
        raw_rows = await cost_client.get_cost_by_resource(start_date, end_date)
        log.info("ingest.rows_fetched", tenant_id=config.id, rows=len(raw_rows))

        # ── 2. Collect live resource state via Azure Resource Graph ─────────
        # Done up front so cost records can be enriched with resource tags from
        # the same bulk query (no per-resource ARM GET, keeps cost/latency low).
        from app.services.resource_graph import ResourceGraphCollector
        sp_token = await cost_client.get_access_token()
        rg_context: dict = {}
        try:
            collector = ResourceGraphCollector(subscription_id, sp_token)
            rg_context = await collector.collect_all()
        except Exception as exc:
            log.warning("ingest.resource_graph_failed", tenant_id=config.id, error=str(exc))
        resource_tags: dict = rg_context.get("resource_tags", {})

        # ── 3. Convert to CostRecord and bulk upsert ───────────────────────
        cost_records: list[dict] = []
        cosmos_docs: list[dict] = []
        for row in raw_rows:
            try:
                record_date_raw = str(row.get("date", ""))
                if len(record_date_raw) == 8:  # YYYYMMDD
                    record_date = date(int(record_date_raw[:4]), int(record_date_raw[4:6]), int(record_date_raw[6:8]))
                else:
                    record_date = date.fromisoformat(record_date_raw[:10])
            except (ValueError, TypeError):
                record_date = end_date

            rid = row.get("resource_id", "")
            rec = CostRecord(
                tenant_id=config.id,
                subscription_id=subscription_id,
                record_date=record_date,
                service_name=row.get("service_name", "Unknown"),
                resource_id=rid,
                resource_group=row.get("resource_group", ""),
                resource_name=rid.split("/")[-1],
                location=row.get("location", ""),
                cost_eur=float(row.get("cost", 0.0)),
                currency=row.get("currency", "EUR"),
                quantity=float(row.get("quantity", 0.0)),
                unit_of_measure=row.get("unit_of_measure", ""),
                meter_category=row.get("meter_category", ""),
                meter_sub_category=row.get("meter_sub_category", ""),
                tags=resource_tags.get(rid.lower(), {}),
            )
            cost_records.append(row)
            cosmos_docs.append(rec.to_cosmos())

        upserted = await cosmos.bulk_upsert(settings.cosmos_container_cost_records, cosmos_docs)
        log.info("ingest.records_stored", tenant_id=config.id, stored=upserted)

        # ── 4. Fetch remaining supporting context for waste rules ──────────
        advisor_recs = await cost_client.get_advisor_recommendations()

        async def metrics_fetcher(resource_id: str) -> dict:
            return await cost_client.get_vm_metrics(resource_id, days=14)

        # rg_context was already collected up front (step 2) and reused here.
        context = {
            "metrics_fetcher": metrics_fetcher,
            "advisor_recommendations": advisor_recs,
            "disk_states": rg_context.get("disk_states", {}),
            "ip_associations": rg_context.get("ip_associations", {}),
            "snapshot_ages": rg_context.get("snapshot_ages", {}),
            "lb_backend_counts": rg_context.get("lb_backend_counts", {}),
            "storage_access_tiers": rg_context.get("storage_access_tiers", {}),
            "cert_expiries": rg_context.get("cert_expiries", {}),
            "backup_policy_counts": rg_context.get("backup_policy_counts", {}),
            "vm_power_states": rg_context.get("vm_power_states", {}),
            "subscription_offer_type": "",
            "env_tag_value": "",
            "vm_uptime_days": {},
            "app_service_metrics": {},
        }

        # ── 5. Run waste engine ────────────────────────────────────────────
        waste_items = await run_all_rules(config.id, subscription_id, cost_records, context)

        waste_docs = [w.to_cosmos() for w in waste_items]
        waste_stored = await cosmos.bulk_upsert(settings.cosmos_container_waste_items, waste_docs)
        log.info(
            "ingest.waste_stored",
            tenant_id=config.id,
            waste_items=waste_stored,
            total_saving_eur=round(sum(w.saving_eur for w in waste_items), 2),
        )

    return {
        "subscription_id": subscription_id,
        "cost_records": upserted,
        "waste_items": waste_stored,
    }


async def ingest_tenant_cloud_provider(
    config: TenantConfig,
    cloud: str,
    provider_creds: dict,
) -> dict:
    """
    Ingest cost data for a non-Azure cloud provider.
    Fetches native billing data, normalises to FOCUS, and stores FocusRecords
    to the cost_records container (discriminated by type='focus_record').
    Returns a summary dict.
    """
    from app.providers.base import _PROVIDERS  # type: ignore
    from app.models.focus import FocusRecord

    settings = get_settings()
    account_ids = config.cloud_accounts.get(cloud, [])
    if not account_ids:
        log.warning("ingest.cloud_no_accounts", tenant_id=config.id, cloud=cloud)
        return {"cloud": cloud, "focus_records": 0}

    provider_cls = _PROVIDERS.get(cloud)
    if provider_cls is None:
        log.error("ingest.cloud_no_provider", tenant_id=config.id, cloud=cloud)
        return {"cloud": cloud, "focus_records": 0, "error": "no_provider_adapter"}

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=settings.ingest_lookback_days - 1)
    total_stored = 0

    for account_id in account_ids:
        try:
            provider = provider_cls(**{_provider_cred_key(cloud): provider_creds,
                                       "project_id": account_id,        # GCP
                                       "access_key_id": provider_creds.get("access_key_id", ""),  # AWS / Alibaba
                                       "access_key_secret": provider_creds.get("access_key_secret", ""),
                                       "region": provider_creds.get("region", "eu-central-1")})
        except TypeError:
            # Provider __init__ may not accept all kwargs — build with only what's needed.
            provider = provider_cls()

        raw = await provider.fetch_cost_data(start_date, end_date)
        focus_records: list[FocusRecord] = provider.normalize(config.id, raw)
        if not focus_records:
            continue

        docs = [r.to_cosmos() for r in focus_records]
        stored = await cosmos.bulk_upsert(settings.cosmos_container_cost_records, docs)
        total_stored += stored
        log.info("ingest.focus_stored", tenant_id=config.id, cloud=cloud,
                 account=account_id, stored=stored)

    return {"cloud": cloud, "focus_records": total_stored}


def _provider_cred_key(cloud: str) -> str:
    """Return the primary credential kwarg name for a provider constructor."""
    return {"gcp": "sa_key", "aws": "role_arn", "alibaba": "access_key_id",
            "oci": "tenancy_ocid"}.get(cloud, "credentials")


async def run_full_ingest() -> None:
    """Entry point: load all active tenants and ingest each one."""
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_output=True)
    log.info("ingest_job.start")

    try:
        tenant_docs = await cosmos.query_items(
            settings.cosmos_container_tenants,
            "SELECT * FROM c WHERE c.type = 'tenant' AND c.active = true",
        )
        log.info("ingest_job.tenants_loaded", count=len(tenant_docs))

        for doc in tenant_docs:
            config = TenantConfig.from_cosmos(doc)
            try:
                creds = await keyvault.get_sp_credentials(config.id)
                results = []
                # ── Azure subscriptions (always enabled) ──────────────────
                for sub_id in config.subscription_ids:
                    try:
                        result = await ingest_tenant_subscription(config, sub_id, creds)
                        results.append(result)
                    except CloudLensError as exc:
                        log.error(
                            "ingest_job.subscription_failed",
                            tenant_id=config.id,
                            subscription_id=sub_id,
                            error=str(exc),
                        )

                # ── Additional enabled clouds (add-on entitlements) ────────
                for cloud in config.enabled_clouds:
                    if cloud == CloudProvider.AZURE:
                        continue  # already handled above
                    secret_name = config.cloud_credential_refs.get(cloud)
                    if not secret_name:
                        log.warning("ingest_job.cloud_no_secret", tenant_id=config.id, cloud=cloud)
                        continue
                    try:
                        cloud_creds = await keyvault.get_secret_json(secret_name)
                        result = await ingest_tenant_cloud_provider(config, cloud, cloud_creds)
                        results.append(result)
                    except CloudLensError as exc:
                        log.error("ingest_job.cloud_failed",
                                  tenant_id=config.id, cloud=cloud, error=str(exc))

                # Update last_ingested_at on success
                updated = config.model_copy(update={
                    "last_ingested_at": datetime.now(timezone.utc),
                    "last_ingest_error": None,
                })
                await cosmos.upsert_item(settings.cosmos_container_tenants, updated.to_cosmos())

            except CloudLensError as exc:
                log.error("ingest_job.tenant_failed", tenant_id=config.id, error=str(exc))
                # Record error on tenant
                errored = config.model_copy(update={"last_ingest_error": str(exc)[:500]})
                try:
                    await cosmos.upsert_item(settings.cosmos_container_tenants, errored.to_cosmos())
                except Exception:
                    pass

    except Exception as exc:
        log.critical("ingest_job.fatal_error", error=str(exc))
        sys.exit(1)
    finally:
        await cosmos.close()
        await keyvault.close()

    log.info("ingest_job.complete")


if __name__ == "__main__":
    asyncio.run(run_full_ingest())
