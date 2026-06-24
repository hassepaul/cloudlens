"""Reports router — /api/v1/reports"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, Depends

from app.config import get_settings
from app.exceptions import NotFoundError, CosmosError, StorageError
from app.logging_config import get_logger
from app.models.report import ReportMeta, ReportStatus
from app.rate_limit import rate_limit_tenant
from app.services import cosmos, blob

log = get_logger(__name__)
router = APIRouter(
    prefix="/api/v1/reports", tags=["reports"],
    dependencies=[Depends(rate_limit_tenant)],
)


def _rpt_container() -> str:
    return get_settings().cosmos_container_reports


def _wi_container() -> str:
    return get_settings().cosmos_container_waste_items


def _cr_container() -> str:
    return get_settings().cosmos_container_cost_records


@router.get("/{tenant_id}", response_model=list[ReportMeta])
async def list_reports(
    tenant_id: str,
    limit: int = Query(12, ge=1, le=50),
) -> list[ReportMeta]:
    try:
        docs = await cosmos.query_items(
            _rpt_container(),
            "SELECT * FROM c WHERE c.tenant_id = @tid ORDER BY c.created_at DESC OFFSET 0 LIMIT @limit",
            parameters=[
                {"name": "@tid", "value": tenant_id},
                {"name": "@limit", "value": limit},
            ],
            partition_key=tenant_id,
        )
        return [ReportMeta.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/{tenant_id}/generate", response_model=ReportMeta, status_code=202)
async def trigger_report(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    period_start: Optional[date] = Query(None),
    period_end: Optional[date] = Query(None),
) -> ReportMeta:
    """Enqueue a report generation job. Returns immediately with status=pending."""
    ps = period_start or (date.today() - timedelta(days=29))
    pe = period_end or date.today()
    meta = ReportMeta(tenant_id=tenant_id, period_start=ps, period_end=pe)
    try:
        await cosmos.upsert_item(_rpt_container(), meta.to_cosmos())
        background_tasks.add_task(_generate_report_background, meta)
        log.info("reports.generation_queued", tenant_id=tenant_id, report_id=meta.id)
        return meta
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


async def _generate_report_background(meta: ReportMeta) -> None:
    """Background task: build PDF report and upload to Blob."""
    from app.services.report_builder import build_pdf_report
    try:
        # Update status to generating
        meta = meta.model_copy(update={"status": ReportStatus.GENERATING})
        await cosmos.upsert_item(_rpt_container(), meta.to_cosmos())

        # Gather data
        waste_docs = await cosmos.query_items(
            _wi_container(),
            "SELECT * FROM c WHERE c.tenant_id = @tid AND (NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null) ORDER BY c.saving_eur DESC",
            parameters=[{"name": "@tid", "value": meta.tenant_id}],
            partition_key=meta.tenant_id,
        )
        cost_rows = await cosmos.query_items(
            _cr_container(),
            "SELECT c.service_name, SUM(c.cost_eur) AS total FROM c WHERE c.tenant_id = @tid AND c.record_date >= @start AND c.record_date <= @end GROUP BY c.service_name",
            parameters=[
                {"name": "@tid", "value": meta.tenant_id},
                {"name": "@start", "value": meta.period_start.isoformat()},
                {"name": "@end", "value": meta.period_end.isoformat()},
            ],
            partition_key=meta.tenant_id,
        )

        total_spend = sum(r.get("total", 0) for r in cost_rows)
        total_waste = sum(w.get("saving_eur", 0) for w in waste_docs)

        # Build PDF
        pdf_bytes = await build_pdf_report(meta, waste_docs, cost_rows)
        blob_path = await blob.upload_report(meta.tenant_id, meta.id, pdf_bytes)
        sas_url = await blob.get_download_url(blob_path)

        # Update report meta
        from datetime import datetime, timezone
        from app.models.waste import Priority
        critical = sum(1 for w in waste_docs if w.get("priority") == Priority.CRITICAL.value)
        high = sum(1 for w in waste_docs if w.get("priority") == Priority.HIGH.value)
        final = meta.model_copy(update={
            "status": ReportStatus.READY,
            "total_spend_eur": round(total_spend, 2),
            "total_waste_eur": round(total_waste, 2),
            "waste_pct": round(total_waste / total_spend * 100, 1) if total_spend > 0 else 0.0,
            "waste_items_count": len(waste_docs),
            "critical_count": critical,
            "high_count": high,
            "blob_url": sas_url,
            "blob_path": blob_path,
            "generated_at": datetime.now(timezone.utc),
        })
        await cosmos.upsert_item(_rpt_container(), final.to_cosmos())
        log.info("reports.generation_complete", report_id=meta.id, tenant_id=meta.tenant_id)

    except Exception as exc:
        log.error("reports.generation_failed", report_id=meta.id, error=str(exc))
        errored = meta.model_copy(update={"status": ReportStatus.FAILED, "error_message": str(exc)[:500]})
        try:
            await cosmos.upsert_item(_rpt_container(), errored.to_cosmos())
        except Exception:
            pass


@router.get("/{report_id}/download")
async def get_download_url(report_id: str, tenant_id: str) -> dict:
    """Return a fresh SAS download URL for a report."""
    try:
        doc = await cosmos.get_item(_rpt_container(), report_id, tenant_id)
        meta = ReportMeta.from_cosmos(doc)
        if meta.status != ReportStatus.READY or not meta.blob_path:
            raise HTTPException(status_code=409, detail={"error": "Report not ready", "status": meta.status})
        url = await blob.get_download_url(meta.blob_path)
        return {"download_url": url, "expires_in_minutes": get_settings().blob_sas_expiry_hours * 60}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
