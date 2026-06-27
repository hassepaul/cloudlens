"""
Real-time cost streaming — /api/v1/costs/{tenant_id}/stream
===========================================================

Server-Sent Events (SSE) endpoint that delivers a live feed of cost data to
the dashboard without requiring the client to poll.

Event types
-----------
``snapshot``
    Full today-so-far cost breakdown by service, sent immediately on connect
    and on every poll cycle where spend has changed by ≥ €0.01.

``heartbeat``
    Keep-alive ping sent every ``sse_keepalive_seconds`` seconds while the
    client is connected and spend has not changed. Prevents proxies and
    load-balancers from dropping idle SSE connections.

``error``
    Sent if the upstream Azure Cost Management query fails; the stream stays
    open so the client can reconnect gracefully.

Protocol
--------
The endpoint uses the W3C EventSource / text/event-stream protocol so any
browser ``EventSource`` or Node/Python SSE client can consume it:

    const es = new EventSource("/api/v1/costs/tenant123/stream?interval=30");
    es.addEventListener("snapshot", e => console.log(JSON.parse(e.data)));

Query parameters
----------------
``interval``
    Seconds between Azure Cost Management polls (default: sse_poll_interval_seconds
    from config, typically 60s). Minimum 15s, maximum 300s.

``lookback_hours``
    How many hours back "today" means for the live query (default 24, min 1, max 48).
    This window is passed as the time range to the Cost Management query so the
    snapshot always shows an accurate rolling picture.

Azure Cost Management notes
---------------------------
The Cost Management query uses "ActualCost" with a time range of today minus
``lookback_hours`` to now. The API has a 1-minute data latency on new charges;
CloudLens therefore polls at a minimum of 15s but in practice 60s is fine and
avoids rate-limit pressure (429s).
"""
from __future__ import annotations
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Query, Depends
from fastapi.responses import StreamingResponse

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services import cosmos

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/costs",
    tags=["streaming"],
    dependencies=[Depends(rate_limit_tenant)],
)

# Absolute floor on the client-requested poll interval (seconds)
_MIN_INTERVAL = 15
_MAX_INTERVAL = 300


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sse_event(event_type: str, data: dict) -> str:
    """Format a single SSE frame."""
    payload = json.dumps(data, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _sse_comment(text: str = "") -> str:
    """SSE keep-alive comment frame (prevents proxy timeouts)."""
    return f": {text}\n\n"


async def _fetch_today_snapshot(tenant_id: str, lookback_hours: int) -> dict:
    """
    Query Cosmos for today's cost-so-far grouped by service.

    Note: this queries the already-ingested cost_records from Cosmos rather
    than hitting Azure Cost Management directly from the stream handler —
    keeping the stream handler stateless and avoiding per-stream Azure auth
    complexity.  The nightly ingest job (or an on-demand trigger) keeps
    Cosmos current.  For true sub-minute freshness, trigger an on-demand
    ingest from ``POST /api/v1/ingest/{tenant_id}`` before opening the stream.
    """
    settings = get_settings()
    container = settings.cosmos_container_cost_records

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).date().isoformat()
    today = date.today().isoformat()

    try:
        rows = await cosmos.query_items(
            container,
            """SELECT c.service_name,
                      SUM(c.cost_eur) AS total_eur,
                      COUNT(1)        AS record_count
               FROM c
               WHERE c.tenant_id = @tid
                 AND c.record_date >= @cutoff
                 AND c.record_date <= @today
               GROUP BY c.service_name""",
            parameters=[
                {"name": "@tid",    "value": tenant_id},
                {"name": "@cutoff", "value": cutoff},
                {"name": "@today",  "value": today},
            ],
            partition_key=tenant_id,
        )
    except CosmosError as exc:
        raise RuntimeError(f"Cosmos query failed: {exc.message}") from exc

    by_service = [
        {
            "service": r.get("service_name", "Unknown"),
            "cost_eur": round(float(r.get("total_eur") or 0.0), 4),
            "records": int(r.get("record_count") or 0),
        }
        for r in rows
    ]
    by_service.sort(key=lambda x: x["cost_eur"], reverse=True)

    total = round(sum(s["cost_eur"] for s in by_service), 4)
    return {
        "tenant_id": tenant_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "total_eur": total,
        "by_service": by_service,
    }


# ── SSE generator ────────────────────────────────────────────────────────────

async def _cost_event_stream(
    tenant_id: str,
    interval: int,
    lookback_hours: int,
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings indefinitely until the
    client disconnects.
    """
    settings = get_settings()
    keepalive_secs = settings.sse_keepalive_seconds
    last_total: float = -1.0
    last_keepalive = time.monotonic()

    while True:
        # ── poll ──────────────────────────────────────────────────────────
        try:
            snapshot = await _fetch_today_snapshot(tenant_id, lookback_hours)
            current_total = snapshot["total_eur"]

            # Emit a snapshot when spend has changed by ≥ €0.01 since last push
            if abs(current_total - last_total) >= 0.01:
                yield _sse_event("snapshot", snapshot)
                last_total = current_total
                last_keepalive = time.monotonic()
            else:
                # No change — emit heartbeat if keepalive window has elapsed
                if time.monotonic() - last_keepalive >= keepalive_secs:
                    yield _sse_comment("heartbeat")
                    last_keepalive = time.monotonic()

        except Exception as exc:
            log.warning(
                "stream.poll_error",
                tenant_id=tenant_id,
                error=str(exc),
            )
            yield _sse_event(
                "error",
                {
                    "tenant_id": tenant_id,
                    "message": "Upstream query failed — retrying next interval",
                    "detail": str(exc)[:200],
                },
            )

        # ── wait for next poll, yielding keep-alive ticks every ~5s ───────
        remaining = interval
        while remaining > 0:
            sleep_chunk = min(5, remaining)
            await asyncio.sleep(sleep_chunk)
            remaining -= sleep_chunk
            if time.monotonic() - last_keepalive >= keepalive_secs:
                yield _sse_comment("heartbeat")
                last_keepalive = time.monotonic()


# ── Route ────────────────────────────────────────────────────────────────────

@router.get(
    "/{tenant_id}/stream",
    response_class=StreamingResponse,
    summary="Real-time cost stream (Server-Sent Events)",
    description=(
        "Opens an SSE stream that pushes live cost snapshots as spend changes. "
        "Connect with ``EventSource`` in the browser or any SSE client. "
        "The stream stays open until the client closes the connection."
    ),
)
async def cost_stream(
    tenant_id: str,
    interval: int = Query(
        default=0,
        ge=0,
        le=_MAX_INTERVAL,
        description=(
            f"Seconds between Azure Cost Management polls "
            f"(0 = use server default from config; min {_MIN_INTERVAL}s, max {_MAX_INTERVAL}s)"
        ),
    ),
    lookback_hours: int = Query(
        default=24,
        ge=1,
        le=48,
        description="Rolling lookback window in hours for the live cost snapshot",
    ),
) -> StreamingResponse:
    settings = get_settings()
    effective_interval = max(_MIN_INTERVAL, interval or settings.sse_poll_interval_seconds)
    effective_interval = min(effective_interval, _MAX_INTERVAL)

    log.info(
        "stream.connected",
        tenant_id=tenant_id,
        interval=effective_interval,
        lookback_hours=lookback_hours,
    )

    return StreamingResponse(
        _cost_event_stream(tenant_id, effective_interval, lookback_hours),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx proxy buffering
            "Connection": "keep-alive",
        },
    )
