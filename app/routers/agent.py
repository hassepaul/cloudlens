"""
AI Agent router — /api/v1/agent
================================

POST  /{tenant_id}/chat                 — send a message, get full response
POST  /{tenant_id}/stream               — SSE streaming response
POST  /{tenant_id}/approve/{action_id}  — execute an approved pending action
GET   /{tenant_id}/history              — list all sessions (newest first)
GET   /{tenant_id}/history/{session_id} — full turn history for one session
DELETE/{tenant_id}/history/{session_id} — delete a session
GET   /{tenant_id}/briefing             — on-demand daily briefing
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
import json

from app.auth import require_api_key
from app.logging_config import get_logger
from app.rate_limit import rate_limit_tenant
from app.services.ai_agent import (
    AgentResponse,
    chat,
    chat_stream,
    approve_action,
    get_sessions,
    delete_session,
    _load_session,
)
from app.services.ai_briefing import generate_briefing

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/agent",
    tags=["ai-agent"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_tenant)],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="User message")
    session_id: Optional[str] = Field(default=None, description="Existing session ID to continue")


class ChatResponse(BaseModel):
    session_id: str
    turn_id: str
    reply: str
    chart_data: list[dict] = Field(default_factory=list)
    metric_cards: list[dict] = Field(default_factory=list)
    pending_actions: list[dict] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    fallback: bool = False


class ApproveResponse(BaseModel):
    action_id: str
    tool_name: str = ""
    status: str
    result: Optional[dict] = None
    error: Optional[str] = None


class SessionSummary(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str


class BriefingCardOut(BaseModel):
    category: str
    title: str
    body: str
    metric: str
    severity: str
    action_label: str = ""
    action_url: str = ""


class BriefingOut(BaseModel):
    tenant_id: str
    generated_at: str
    narrative: str
    cards: list[BriefingCardOut]
    top_action: str
    generated_by: str


# ── Chat endpoint ─────────────────────────────────────────────────────────────

@router.post("/{tenant_id}/chat", response_model=ChatResponse)
async def agent_chat(tenant_id: str, body: ChatRequest) -> ChatResponse:
    """Send a message to the CloudLens AI agent and get a full response."""
    resp: AgentResponse = await chat(tenant_id, body.message, body.session_id)
    return ChatResponse(
        session_id=resp.session_id,
        turn_id=resp.turn_id,
        reply=resp.reply,
        chart_data=resp.chart_data,
        metric_cards=resp.metric_cards,
        pending_actions=resp.pending_actions,
        suggestions=resp.suggestions,
        tools_used=resp.tools_used,
        fallback=resp.fallback,
    )


# ── Streaming endpoint ────────────────────────────────────────────────────────

@router.post("/{tenant_id}/stream")
async def agent_stream_endpoint(tenant_id: str, body: ChatRequest) -> StreamingResponse:
    """Stream the agent response as Server-Sent Events (SSE)."""
    async def generate():
        try:
            async for chunk in chat_stream(tenant_id, body.message, body.session_id):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as exc:
            log.error("agent.stream_error", tenant_id=tenant_id, error=str(exc), exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'An internal error occurred. Please try again.'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Action approval ───────────────────────────────────────────────────────────

@router.post("/{tenant_id}/approve/{action_id}", response_model=ApproveResponse)
async def approve_pending_action(tenant_id: str, action_id: str) -> ApproveResponse:
    """Approve and execute a pending write action proposed by the agent."""
    result = await approve_action(tenant_id, session_id="", action_id=action_id)
    if result.get("error") == "action_not_found_or_already_executed":
        raise HTTPException(status_code=404, detail={"error": "ACTION_NOT_FOUND", "message": "Action not found or already executed"})
    if result.get("error") == "session_not_found":
        raise HTTPException(status_code=404, detail={"error": "SESSION_NOT_FOUND", "message": "Session not found"})
    return ApproveResponse(
        action_id=action_id,
        tool_name=result.get("tool_name", ""),
        status=result.get("status", "unknown"),
        result=result.get("result"),
        error=result.get("error"),
    )


@router.post("/{tenant_id}/sessions/{session_id}/approve/{action_id}", response_model=ApproveResponse)
async def approve_session_action(tenant_id: str, session_id: str, action_id: str) -> ApproveResponse:
    """Approve and execute a pending action within a specific session."""
    result = await approve_action(tenant_id, session_id=session_id, action_id=action_id)
    if "error" in result and result.get("status") != "executed":
        error = result["error"]
        if "not_found" in error:
            raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": error})
    return ApproveResponse(
        action_id=action_id,
        tool_name=result.get("tool_name", ""),
        status=result.get("status", "unknown"),
        result=result.get("result"),
        error=result.get("error"),
    )


# ── Session history ───────────────────────────────────────────────────────────

@router.get("/{tenant_id}/history", response_model=list[SessionSummary])
async def list_sessions(tenant_id: str) -> list[SessionSummary]:
    """List all agent conversation sessions for a tenant, newest first."""
    sessions = await get_sessions(tenant_id)
    return [SessionSummary(**s) for s in sessions]


@router.get("/{tenant_id}/history/{session_id}")
async def get_session_detail(tenant_id: str, session_id: str) -> dict:
    """Get the full turn history for a specific session."""
    session = await _load_session(session_id, tenant_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "SESSION_NOT_FOUND", "message": f"Session {session_id} not found"})
    return {
        "session_id": session.session_id,
        "tenant_id": session.tenant_id,
        "title": session.title,
        "turns": [t.to_dict() for t in session.turns],
        "pending_actions": [pa.to_dict() for pa in session.pending_actions],
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


@router.delete("/{tenant_id}/history/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def remove_session(tenant_id: str, session_id: str) -> None:
    """Delete an agent conversation session."""
    deleted = await delete_session(session_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "SESSION_NOT_FOUND", "message": f"Session {session_id} not found"})


# ── Daily briefing ────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/briefing", response_model=BriefingOut)
async def daily_briefing(tenant_id: str) -> BriefingOut:
    """Generate an on-demand AI FinOps briefing for this tenant."""
    result = await generate_briefing(tenant_id)
    return BriefingOut(
        tenant_id=result.tenant_id,
        generated_at=result.generated_at,
        narrative=result.narrative,
        cards=[BriefingCardOut(
            category=c.category,
            title=c.title,
            body=c.body,
            metric=c.metric,
            severity=c.severity,
            action_label=c.action_label,
            action_url=c.action_url,
        ) for c in result.cards],
        top_action=result.top_action,
        generated_by=result.generated_by,
    )
