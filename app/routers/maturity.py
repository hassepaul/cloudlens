"""FinOps Maturity Score router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.rate_limit import rate_limit_tenant
from app.services.maturity import compute_maturity_score, _VALID_VERTICALS
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/maturity",
    tags=["maturity"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _dimension_dict(d) -> dict:
    return {
        "dimension": d.dimension,
        "label": d.label,
        "weight": d.weight,
        "score": d.score,
        "percentile": d.percentile,
        "cohort_median": d.cohort_median,
        "cohort_p75": d.cohort_p75,
        "cohort_p90": d.cohort_p90,
        "cohort_context": d.cohort_context,
        "evidence": d.evidence,
        "recommended_action": d.recommended_action,
    }


@router.get("/{tenant_id}/score")
async def get_maturity_score(
    tenant_id: str,
    vertical: str = Query(
        default="enterprise",
        description="Industry vertical for benchmark comparison",
    ),
) -> dict:
    """
    Compute a FinOps maturity score across 6 dimensions and benchmark against
    anonymised industry cohorts.

    Valid verticals: saas, ecommerce, enterprise, startup.
    Defaults to 'enterprise' if an unrecognised value is supplied.
    """
    score = await compute_maturity_score(tenant_id, vertical=vertical)
    return {
        "tenant_id": score.tenant_id,
        "vertical": score.vertical,
        "overall_score": score.overall_score,
        "overall_percentile": score.overall_percentile,
        "overall_label": score.overall_label,
        "top_recommendation": score.top_recommendation,
        "generated_at": score.generated_at,
        "dimensions": [_dimension_dict(d) for d in score.dimensions],
        "available_verticals": sorted(_VALID_VERTICALS),
    }
