"""
Bot webhook router — /api/v1/bots

Endpoints
─────────
POST /api/v1/bots/{tenant_id}/slack/events
    Handles Slack Events API payloads:
    • url_verification challenge (no auth — Slack owns the handshake)
    • app_mention / message events → interactive command dispatch

POST /api/v1/bots/{tenant_id}/slack/command
    Handles Slack slash-command payloads (application/x-www-form-urlencoded).
    Signature-verified using the tenant's Slack signing secret from Key Vault.
    Returns an ephemeral Block Kit response immediately (< 3 s Slack timeout).

POST /api/v1/bots/{tenant_id}/teams/message
    Handles Teams Bot Framework Activity payloads.
    • Echoes a help card for unrecognised commands.
    • Routes "spend", "budget", "status" to CloudLens data + responds inline.

POST /api/v1/bots/{tenant_id}/notify/budget
    Push a budget breach notification to all configured bot channels.
    Requires API key. Called by the alert engine.

POST /api/v1/bots/{tenant_id}/notify/spend
    Push a spend summary to all configured bot channels.
    Requires API key.

GET  /api/v1/bots/{tenant_id}/channels
    Return which channels (slack / teams) are configured for a tenant.
    Requires API key.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services import cosmos
from app.services.bot_notifications import (
    build_slash_response,
    notify_budget_breach,
    notify_spend_spike,
    notify_spend_summary,
    send_slack,
    send_teams,
    verify_slack_signature,
    _get_webhook_url,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/bots",
    tags=["bots"],
    dependencies=[Depends(rate_limit_tenant)],
)


# ── Request / response models ─────────────────────────────────────────────────

class BudgetNotifyRequest(BaseModel):
    budget_name: str
    spent_eur: float = Field(..., ge=0)
    budget_eur: float = Field(..., gt=0)


class SpendNotifyRequest(BaseModel):
    total_eur: float = Field(..., ge=0)
    by_service: list[dict] = Field(default_factory=list)
    period: str = Field(default="today")


class SpikeNotifyRequest(BaseModel):
    service: str
    current_eur: float = Field(..., ge=0)
    baseline_eur: float = Field(..., ge=0)


class ChannelStatus(BaseModel):
    slack_configured: bool
    teams_configured: bool


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _today_spend(tenant_id: str) -> tuple[float, list[dict]]:
    """Fetch today's spend from Cosmos for slash-command responses."""
    settings = get_settings()
    from datetime import date
    today = date.today().isoformat()
    try:
        rows = await cosmos.query_items(
            settings.cosmos_container_cost_records,
            """SELECT c.service_name, SUM(c.cost_eur) AS total_eur
               FROM c
               WHERE c.tenant_id = @tid AND c.record_date = @today
               GROUP BY c.service_name""",
            parameters=[
                {"name": "@tid", "value": tenant_id},
                {"name": "@today", "value": today},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        return 0.0, []

    by_service = [
        {"service": r.get("service_name", "Unknown"), "cost_eur": float(r.get("total_eur") or 0)}
        for r in rows
    ]
    total = sum(r["cost_eur"] for r in by_service)
    return total, by_service


async def _scheduler_status() -> dict:
    """Return a brief scheduler status block."""
    from app.services.realtime_ingest import get_poll_state
    settings = get_settings()
    return {
        "poll_enabled": settings.realtime_poll_enabled,
        "interval_minutes": settings.realtime_poll_interval_minutes,
    }


# ── Slack events ──────────────────────────────────────────────────────────────

@router.post("/{tenant_id}/slack/events", response_model=None)
async def slack_events(
    tenant_id: str,
    request: Request,
) -> JSONResponse:
    """
    Slack Events API endpoint.
    Handles url_verification and app_mention/message events.
    Signature verification is performed for non-challenge requests.
    """
    raw_body = await request.body()

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ── URL verification (Slack ownership challenge — no signature yet) ───
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    # ── Verify Slack signature for all other events ───────────────────────
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    valid = await verify_slack_signature(
        tenant_id,
        timestamp=timestamp,
        signature=signature,
        raw_body=raw_body,
    )
    if not valid:
        log.warning("bots.slack_signature_invalid", tenant_id=tenant_id)
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    event = payload.get("event", {})
    event_type = event.get("type", "")

    if event_type in ("app_mention", "message"):
        text = event.get("text", "").lower().strip()
        tokens = text.split()
        cmd = tokens[1] if len(tokens) > 1 else "help"

        if cmd == "spend":
            total, by_service = await _today_spend(tenant_id)
            resp = build_slash_response(tenant_id, "spend", total_eur=total, by_service=by_service)
        elif cmd == "budget":
            resp = build_slash_response(tenant_id, "budget")
        elif cmd == "status":
            status_data = await _scheduler_status()
            resp = build_slash_response(
                tenant_id, "help",
                error=None,
            )
            resp["text"] = (
                f"*Scheduler:* {'✅ enabled' if status_data['poll_enabled'] else '❌ disabled'} "
                f"(every {status_data['interval_minutes']} min)"
            )
        else:
            resp = build_slash_response(tenant_id, "help")

        # For event API we don't post back inline — caller should use chat.postMessage.
        # Return 200 immediately per Slack requirements.
        log.info("bots.slack_event_handled", tenant_id=tenant_id, cmd=cmd)

    return JSONResponse({"ok": True})


# ── Slack slash command ───────────────────────────────────────────────────────

@router.post("/{tenant_id}/slack/command", response_model=None)
async def slack_command(
    tenant_id: str,
    request: Request,
) -> JSONResponse:
    """
    Slack slash-command endpoint (/cloudlens …).
    Verifies the Slack signing secret, then returns an ephemeral Block Kit
    response within Slack's 3-second timeout window.
    """
    raw_body = await request.body()

    # Signature verification
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    valid = await verify_slack_signature(
        tenant_id,
        timestamp=timestamp,
        signature=signature,
        raw_body=raw_body,
    )
    if not valid:
        log.warning("bots.slack_command_sig_invalid", tenant_id=tenant_id)
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Parse form body
    from urllib.parse import parse_qs
    form = parse_qs(raw_body.decode("utf-8", errors="replace"))

    def _field(key: str) -> str:
        vals = form.get(key, [""])
        return vals[0] if vals else ""

    text = _field("text").strip()
    tokens = text.split()
    cmd = tokens[0].lower() if tokens else "help"
    arg_tenant = tokens[1] if len(tokens) > 1 else tenant_id

    if cmd == "spend":
        total, by_service = await _today_spend(arg_tenant)
        resp = build_slash_response(arg_tenant, "spend", total_eur=total, by_service=by_service)
    elif cmd == "budget":
        resp = build_slash_response(arg_tenant, "budget")
    elif cmd == "status":
        status_data = await _scheduler_status()
        resp = {
            "response_type": "ephemeral",
            "text": (
                f"*CloudLens Scheduler*\n"
                f"Status: {'✅ enabled' if status_data['poll_enabled'] else '❌ disabled'}\n"
                f"Interval: every {status_data['interval_minutes']} min"
            ),
        }
    else:
        resp = build_slash_response(tenant_id, "help")

    log.info("bots.slack_command", tenant_id=tenant_id, cmd=cmd)
    return JSONResponse(resp)


# ── Teams message ─────────────────────────────────────────────────────────────

@router.post("/{tenant_id}/teams/message", response_model=None)
async def teams_message(
    tenant_id: str,
    request: Request,
) -> JSONResponse:
    """
    Microsoft Teams Bot Framework Activity handler.
    Accepts conversationUpdate and message activities.
    Responds with an Adaptive Card inline (Teams bot reply).
    """
    try:
        activity = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    activity_type = activity.get("type", "")

    if activity_type == "conversationUpdate":
        return JSONResponse({"type": "message", "text": "👋 CloudLens bot connected. Try: spend, budget, status"})

    if activity_type != "message":
        return JSONResponse({})

    text = (activity.get("text") or "").strip().lower()
    tokens = text.split()
    cmd = tokens[0] if tokens else "help"
    arg_tenant = tokens[1] if len(tokens) > 1 else tenant_id

    if cmd == "spend":
        total, by_service = await _today_spend(arg_tenant)
        from app.services.bot_notifications import _teams_spend_card
        card = _teams_spend_card(arg_tenant, total, by_service)
        return JSONResponse(card)

    if cmd == "status":
        status_data = await _scheduler_status()
        state_text = "enabled" if status_data["poll_enabled"] else "disabled"
        return JSONResponse({
            "type": "message",
            "text": f"**CloudLens Scheduler** — {state_text}, interval: {status_data['interval_minutes']} min",
        })

    # Help / unrecognised
    return JSONResponse({
        "type": "message",
        "text": "**CloudLens commands:** `spend [tenant]` · `budget [tenant]` · `status`",
    })


# ── Push notification endpoints ───────────────────────────────────────────────

@router.post("/{tenant_id}/notify/budget", dependencies=[Depends(require_api_key)])
async def notify_budget(
    tenant_id: str,
    payload: BudgetNotifyRequest,
) -> dict:
    """Push a budget breach alert to all configured bot channels."""
    results = await notify_budget_breach(
        tenant_id,
        budget_name=payload.budget_name,
        spent_eur=payload.spent_eur,
        budget_eur=payload.budget_eur,
    )
    return {"tenant_id": tenant_id, "delivered": results}


@router.post("/{tenant_id}/notify/spend", dependencies=[Depends(require_api_key)])
async def notify_spend(
    tenant_id: str,
    payload: SpendNotifyRequest,
) -> dict:
    """Push a spend summary to all configured bot channels."""
    results = await notify_spend_summary(
        tenant_id,
        total_eur=payload.total_eur,
        by_service=payload.by_service,
        period=payload.period,
    )
    return {"tenant_id": tenant_id, "delivered": results}


@router.post("/{tenant_id}/notify/spike", dependencies=[Depends(require_api_key)])
async def notify_spike(
    tenant_id: str,
    payload: SpikeNotifyRequest,
) -> dict:
    """Push a spend spike alert to all configured bot channels."""
    results = await notify_spend_spike(
        tenant_id,
        service=payload.service,
        current_eur=payload.current_eur,
        baseline_eur=payload.baseline_eur,
    )
    return {"tenant_id": tenant_id, "delivered": results}


@router.get("/{tenant_id}/channels", dependencies=[Depends(require_api_key)])
async def get_channels(tenant_id: str) -> ChannelStatus:
    """Return which bot channels are configured for a tenant."""
    slack_url = await _get_webhook_url(f"slack-webhook-{tenant_id}")
    teams_url = await _get_webhook_url(f"teams-webhook-{tenant_id}")
    return ChannelStatus(
        slack_configured=bool(slack_url),
        teams_configured=bool(teams_url),
    )
