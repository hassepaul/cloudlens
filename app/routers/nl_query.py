"""Natural Language Cost Querying router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.rate_limit import rate_limit_tenant
from app.services.nl_query import answer_question, _MAX_QUESTION_LEN
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/nl-query",
    tags=["nl-query"],
    dependencies=[Depends(rate_limit_tenant)],
)


class NLQueryRequest(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def _check_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        return v[:_MAX_QUESTION_LEN]


@router.post("/{tenant_id}")
async def query(
    tenant_id: str,
    body: NLQueryRequest,
) -> dict:
    """
    Answer a natural-language question about cloud spend for a tenant.

    Uses LLM function-calling (when configured) to select and execute the
    appropriate Cosmos query, then returns structured chart data + a narrative.

    Falls back to rule-based intent matching when no LLM API key is configured.

    Example questions:
    - "Which service cost the most last month?"
    - "Show me the daily spend trend for the past 30 days"
    - "Compare this month vs last month"
    - "What are the top 5 most expensive resources?"
    - "Break down spend by cloud"
    """
    result = await answer_question(body.question, tenant_id)
    return {
        "question": result.question,
        "intent": result.intent,
        "chart_type": result.chart_type,
        "chart_data": result.chart_data,
        "narrative": result.narrative,
        "query_used": result.query_used,
        "confidence": result.confidence,
        "suggestions": result.suggestions,
        "fallback": result.fallback,
    }
