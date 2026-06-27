"""
Real-time (sub-hourly) ingest scheduler and poll-state management.

Architecture
────────────
  ┌─────────────────────────────────────────────────────────────────┐
  │  Background asyncio task (started in app lifespan)              │
  │  Wakes every REALTIME_POLL_INTERVAL_MINUTES minutes             │
  │  → poll_all_active_tenants()                                    │
  │    → for each active tenant: run_delta_pull(tenant_id)          │
  │      → calls existing ingest_tenant_hourly() per provider       │
  │      → updates poll_state document in Cosmos                    │
  └─────────────────────────────────────────────────────────────────┘

Poll state document (Cosmos container: poll_state):
  {
    "id": "pollstate-{tenant_id}",
    "type": "poll_state",
    "tenant_id": "...",
    "last_polled_at": "2026-06-26T10:30:00+00:00",
    "last_success_at": "2026-06-26T10:30:00+00:00",
    "records_last_pull": 142,
    "total_records_pulled": 14820,
    "lag_minutes": 28,
    "consecutive_errors": 0,
    "last_error": null
  }
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get_settings
from app.exceptions import CloudLensError, CosmosError
from app.logging_config import get_logger
from app.services import cosmos, keyvault

log = get_logger(__name__)

_POLL_STATE_DOC_TYPE = "poll_state"

# Maximum consecutive errors before a tenant is skipped from auto-polling
_MAX_CONSECUTIVE_ERRORS = 5


def _state_id(tenant_id: str) -> str:
    return f"pollstate-{tenant_id}"


# ── Poll state helpers ────────────────────────────────────────────────────────

async def get_poll_state(tenant_id: str) -> dict:
    """Return the current poll state for a tenant, or a default if none exists."""
    settings = get_settings()
    try:
        doc = await cosmos.get_item(
            settings.cosmos_container_poll_state,
            _state_id(tenant_id),
            tenant_id,
        )
        return doc
    except Exception:
        return _default_state(tenant_id)


def _default_state(tenant_id: str) -> dict:
    return {
        "id": _state_id(tenant_id),
        "type": _POLL_STATE_DOC_TYPE,
        "tenant_id": tenant_id,
        "last_polled_at": None,
        "last_success_at": None,
        "records_last_pull": 0,
        "total_records_pulled": 0,
        "lag_minutes": None,
        "consecutive_errors": 0,
        "last_error": None,
    }


async def _save_poll_state(state: dict) -> None:
    settings = get_settings()
    try:
        await cosmos.upsert_item(settings.cosmos_container_poll_state, state)
    except CosmosError as exc:
        log.warning("realtime_ingest.state_save_failed", error=str(exc))


# ── Delta pull ────────────────────────────────────────────────────────────────

async def run_delta_pull(tenant_id: str) -> dict:
    """
    Execute a near-realtime delta pull for one tenant.

    Calls the existing hourly ingest infrastructure for each enabled provider
    and updates the poll state document in Cosmos.

    Returns a summary dict with lag_minutes, records_added, duration_ms.
    """
    from app.models.tenant import TenantConfig
    from app.services import keyvault

    settings = get_settings()
    start = datetime.now(timezone.utc)
    state = await get_poll_state(tenant_id)

    try:
        # Load tenant config
        doc = await cosmos.get_item(
            settings.cosmos_container_tenants, tenant_id, tenant_id
        )
        config = TenantConfig.from_cosmos(doc)

        if not config.active:
            return {"tenant_id": tenant_id, "skipped": True, "reason": "inactive"}

        total_records = 0
        errors: list[str] = []

        # Azure (primary provider)
        if "azure" in (config.enabled_clouds or []):
            try:
                from app.jobs.ingest_hourly import ingest_tenant_hourly
                creds = await keyvault.get_sp_credentials(tenant_id)
                for sub_id in config.subscription_ids:
                    result = await ingest_tenant_hourly(config, sub_id, creds)
                    total_records += result.estimated_records + result.confirmed_records
            except CloudLensError as exc:
                errors.append(f"azure: {exc.message}")
            except Exception as exc:
                errors.append(f"azure: {exc!s}")

        # AWS delta (via Cost Explorer if boto3 available, else skip gracefully)
        if "aws" in (config.enabled_clouds or []):
            try:
                aws_records = await _delta_pull_aws(tenant_id, config)
                total_records += aws_records
            except Exception as exc:
                errors.append(f"aws: {exc!s}")

        # GCP delta (via BigQuery if client available, else skip gracefully)
        if "gcp" in (config.enabled_clouds or []):
            try:
                gcp_records = await _delta_pull_gcp(tenant_id, config)
                total_records += gcp_records
            except Exception as exc:
                errors.append(f"gcp: {exc!s}")

        end = datetime.now(timezone.utc)
        lag_minutes = _compute_lag_minutes(state.get("last_success_at"), end)
        duration_ms = int((end - start).total_seconds() * 1000)

        # Update state
        state.update({
            "last_polled_at": end.isoformat(),
            "last_success_at": end.isoformat() if not errors else state.get("last_success_at"),
            "records_last_pull": total_records,
            "total_records_pulled": (state.get("total_records_pulled") or 0) + total_records,
            "lag_minutes": lag_minutes,
            "consecutive_errors": 0 if not errors else (state.get("consecutive_errors") or 0) + 1,
            "last_error": "; ".join(errors) if errors else None,
        })
        await _save_poll_state(state)

        log.info(
            "realtime_ingest.pull_complete",
            tenant_id=tenant_id,
            records=total_records,
            lag_minutes=lag_minutes,
            duration_ms=duration_ms,
            errors=len(errors),
        )
        return {
            "tenant_id": tenant_id,
            "records_added": total_records,
            "lag_minutes": lag_minutes,
            "duration_ms": duration_ms,
            "errors": errors,
        }

    except Exception as exc:
        end = datetime.now(timezone.utc)
        state.update({
            "last_polled_at": end.isoformat(),
            "consecutive_errors": (state.get("consecutive_errors") or 0) + 1,
            "last_error": str(exc),
        })
        await _save_poll_state(state)
        log.error("realtime_ingest.pull_failed", tenant_id=tenant_id, error=str(exc))
        return {"tenant_id": tenant_id, "error": str(exc), "records_added": 0}


def _compute_lag_minutes(
    last_success_at: Optional[str],
    now: datetime,
) -> Optional[int]:
    """Minutes since the last successful pull, or None if never polled."""
    if not last_success_at:
        return None
    try:
        last = datetime.fromisoformat(last_success_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0, int((now - last).total_seconds() / 60))
    except (ValueError, TypeError):
        return None


# ── Provider-specific delta pulls ────────────────────────────────────────────

async def _delta_pull_aws(tenant_id: str, config) -> int:
    """
    Pull AWS Cost Explorer data for the last 2 hours.
    Returns number of records upserted. Requires boto3 in the runtime image.
    """
    try:
        import boto3  # type: ignore[import]
    except ImportError:
        log.debug("realtime_ingest.aws_boto3_unavailable", tenant_id=tenant_id)
        return 0

    from app.services import keyvault
    settings = get_settings()

    try:
        creds = await keyvault.get_sp_credentials(f"{tenant_id}-aws")
    except Exception:
        return 0

    role_arn = creds.get("client_id", "")
    external_id = creds.get("client_secret", "")
    if not role_arn:
        return 0

    now = datetime.now(timezone.utc)
    start_str = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
    end_str = now.strftime("%Y-%m-%dT%H:00:00Z")

    # Assume role
    sts = boto3.client("sts")
    assume = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="CloudLensDelta",
        ExternalId=external_id,
    )
    temp = assume["Credentials"]
    ce = boto3.client(
        "ce",
        aws_access_key_id=temp["AccessKeyId"],
        aws_secret_access_key=temp["SecretAccessKey"],
        aws_session_token=temp["SessionToken"],
    )

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start_str[:10], "End": end_str[:10]},
        Granularity="HOURLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    records = 0
    container = settings.cosmos_container_cost_records
    for group_result in resp.get("ResultsByTime", []):
        for group in group_result.get("Groups", []):
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount <= 0:
                continue
            doc = {
                "id": f"{tenant_id}-aws-{service}-{group_result['TimePeriod']['Start']}",
                "type": "focus_record",
                "tenant_id": tenant_id,
                "provider_name": "aws",
                "service_name": service,
                "effective_cost": amount,
                "charge_period_start": group_result["TimePeriod"]["Start"],
                "estimated": True,
                "source": "cost_explorer_delta",
                "upserted_at": now.isoformat(),
            }
            await cosmos.upsert_item(container, doc)
            records += 1

    return records


async def _delta_pull_gcp(tenant_id: str, config) -> int:
    """
    Pull GCP Billing export data via BigQuery for the last 2 hours.
    Returns number of records upserted. Requires google-cloud-bigquery.
    """
    try:
        from google.cloud import bigquery  # type: ignore[import]
        from google.oauth2 import service_account  # type: ignore[import]
    except ImportError:
        log.debug("realtime_ingest.gcp_bigquery_unavailable", tenant_id=tenant_id)
        return 0

    from app.services import keyvault
    settings = get_settings()

    try:
        creds_doc = await keyvault.get_sp_credentials(f"{tenant_id}-gcp")
        sa_json = creds_doc.get("client_secret", "")
        if not sa_json:
            return 0
        import json as _json
        sa_info = _json.loads(sa_json)
        credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
        )
    except Exception:
        return 0

    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(hours=2)).isoformat()

    # GCP billing export table: project.dataset.gcp_billing_export_v1_*
    project_ids = (config.cloud_accounts or {}).get("gcp", [])
    if not project_ids:
        return 0

    client = bigquery.Client(project=project_ids[0], credentials=credentials)
    table = f"`{project_ids[0]}.billing_export.gcp_billing_export_v1_*`"
    query = f"""
        SELECT
            service.description AS service_name,
            project.id AS project_id,
            DATE(usage_start_time) AS charge_period_start,
            SUM(cost) AS effective_cost
        FROM {table}
        WHERE export_time >= TIMESTAMP('{since_iso}')
        GROUP BY 1, 2, 3
        HAVING SUM(cost) > 0
    """

    records = 0
    container = settings.cosmos_container_cost_records
    for row in client.query(query).result():
        doc = {
            "id": f"{tenant_id}-gcp-{row.project_id}-{row.service_name}-{row.charge_period_start}",
            "type": "focus_record",
            "tenant_id": tenant_id,
            "provider_name": "gcp",
            "service_name": row.service_name,
            "effective_cost": float(row.effective_cost),
            "charge_period_start": str(row.charge_period_start),
            "estimated": True,
            "source": "bigquery_delta",
            "upserted_at": now.isoformat(),
        }
        await cosmos.upsert_item(container, doc)
        records += 1

    return records


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def poll_all_active_tenants() -> list[dict]:
    """
    Pull delta data for every active tenant.
    Skips tenants that have exceeded the consecutive error threshold.
    Called by the background scheduler task.
    """
    settings = get_settings()
    results: list[dict] = []

    try:
        tenant_docs = await cosmos.query_items(
            settings.cosmos_container_tenants,
            "SELECT c.id, c.active, c.tenant_name FROM c WHERE c.type='tenant' AND c.active=true",
        )
    except CosmosError as exc:
        log.error("realtime_ingest.tenant_list_failed", error=str(exc))
        return []

    for doc in tenant_docs:
        tid = doc.get("id")
        if not tid:
            continue
        state = await get_poll_state(tid)
        if (state.get("consecutive_errors") or 0) >= _MAX_CONSECUTIVE_ERRORS:
            log.warning(
                "realtime_ingest.tenant_skipped_errors",
                tenant_id=tid,
                consecutive_errors=state["consecutive_errors"],
            )
            results.append({"tenant_id": tid, "skipped": True, "reason": "too_many_errors"})
            continue
        result = await run_delta_pull(tid)
        results.append(result)

    log.info(
        "realtime_ingest.scheduler_cycle_done",
        tenants=len(results),
        pulled=sum(1 for r in results if not r.get("skipped") and not r.get("error")),
    )
    return results


async def run_scheduler(interval_minutes: int) -> None:
    """
    Long-running background coroutine. Sleeps for interval_minutes between
    each full poll cycle. Designed to run inside the FastAPI lifespan.
    """
    log.info("realtime_ingest.scheduler_started", interval_minutes=interval_minutes)
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            await poll_all_active_tenants()
        except Exception as exc:
            log.error("realtime_ingest.scheduler_error", error=str(exc))
