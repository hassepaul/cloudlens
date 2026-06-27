"""Ingest trigger and health check routers."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import NotFoundError, CosmosError, CloudLensError
from app.logging_config import get_logger
from app.models.tenant import TenantConfig
from app.services import cosmos, keyvault

log = get_logger(__name__)

# Manual ingest is an admin action — protected by the internal API key.
ingest_router = APIRouter(
    prefix="/api/v1/ingest", tags=["ingest"], dependencies=[Depends(require_api_key)]
)
health_router = APIRouter(prefix="/api/v1/health", tags=["health"])


async def _run_ingest_inline(tenant_id: str) -> None:
    """
    Run a full ingest for one tenant inline (no queue).

    At low-to-moderate tenant counts this is cheaper and simpler than a
    Service Bus round-trip: the manual trigger reuses exactly the same code
    path as the nightly Container Apps Job.
    """
    from app.jobs.ingest import ingest_tenant_subscription

    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
        creds = await keyvault.get_sp_credentials(config.id)
        for sub_id in config.subscription_ids:
            try:
                await ingest_tenant_subscription(config, sub_id, creds)
            except CloudLensError as exc:
                log.error(
                    "ingest.manual_subscription_failed",
                    tenant_id=tenant_id, subscription_id=sub_id, error=str(exc),
                )
    except Exception as exc:
        log.error("ingest.manual_failed", tenant_id=tenant_id, error=str(exc))


@ingest_router.post("/{tenant_id}", status_code=202)
async def trigger_ingest(tenant_id: str, background_tasks: BackgroundTasks) -> dict:
    """
    Manually trigger a cost ingestion run for a tenant.
    Runs inline as a background task — returns 202 immediately.
    """
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        subscription_ids: list[str] = doc.get("subscription_ids", [])
        if not subscription_ids:
            raise HTTPException(status_code=422, detail="Tenant has no subscription IDs configured")

        background_tasks.add_task(_run_ingest_inline, tenant_id)
        log.info("ingest.triggered", tenant_id=tenant_id, subscriptions=len(subscription_ids))
        return {
            "tenant_id": tenant_id,
            "status": "accepted",
            "subscriptions": len(subscription_ids),
        }

    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@ingest_router.post("/{tenant_id}/hourly", status_code=202)
async def trigger_hourly_ingest(tenant_id: str, background_tasks: BackgroundTasks) -> dict:
    """
    Trigger a near-realtime (hourly) ingest for one tenant.
    Returns 202 immediately; runs two tracks in the background:
      - Track A: near-realtime estimated spend via Azure Usage Aggregates API
      - Track B: confirmed billing rows for the last 48 hours
    """
    settings = get_settings()
    try:
        doc = await cosmos.get_item(settings.cosmos_container_tenants, tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
        if not config.subscription_ids:
            raise HTTPException(status_code=422, detail="Tenant has no subscription IDs configured")
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())

    async def _run():
        from app.jobs.ingest_hourly import ingest_tenant_hourly
        try:
            creds = await keyvault.get_sp_credentials(tenant_id)
            for sub_id in config.subscription_ids:
                await ingest_tenant_hourly(config, sub_id, creds)
        except Exception as exc:
            log.error("ingest.hourly_failed", tenant_id=tenant_id, error=str(exc))

    background_tasks.add_task(_run)
    log.info("ingest.hourly_triggered", tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "status": "accepted",
        "mode": "hourly",
        "tracks": ["near_realtime_estimate", "billing_confirm_48h"],
    }


@ingest_router.get("/{tenant_id}/lag")
async def get_data_lag(tenant_id: str) -> dict:
    """
    Return the current data lag for a tenant: when it was last polled,
    how many records were pulled, and the lag in minutes.
    """
    from app.services.realtime_ingest import get_poll_state
    state = await get_poll_state(tenant_id)
    return {
        "tenant_id": tenant_id,
        "last_polled_at": state.get("last_polled_at"),
        "last_success_at": state.get("last_success_at"),
        "lag_minutes": state.get("lag_minutes"),
        "records_last_pull": state.get("records_last_pull", 0),
        "total_records_pulled": state.get("total_records_pulled", 0),
        "consecutive_errors": state.get("consecutive_errors", 0),
        "last_error": state.get("last_error"),
    }


@ingest_router.post("/poll-all", status_code=202)
async def trigger_poll_all(background_tasks: BackgroundTasks) -> dict:
    """
    Admin: immediately trigger a delta pull for all active tenants.
    Normally driven by the background scheduler; this allows manual forcing.
    """
    from app.services.realtime_ingest import poll_all_active_tenants
    background_tasks.add_task(poll_all_active_tenants)
    return {"status": "accepted", "message": "Delta pull queued for all active tenants"}


@health_router.get("/")
async def health_check() -> dict:
    """Liveness + dependency health check."""
    checks: dict[str, str] = {}

    # Cosmos
    try:
        settings = get_settings()
        await cosmos.query_items(
            settings.cosmos_container_tenants,
            "SELECT VALUE COUNT(1) FROM c WHERE c.type = 'tenant'",
        )
        checks["cosmos"] = "ok"
    except Exception as exc:
        checks["cosmos"] = f"error: {str(exc)[:100]}"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "version": get_settings().app_version,
        "checks": checks,
    }
