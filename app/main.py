"""
CloudLens FastAPI application entry point.
Wires up routers, middleware, lifespan events, and global exception handlers.
"""
from __future__ import annotations
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import structlog

from app.config import get_settings
from app.exceptions import CloudLensError
from app.logging_config import configure_logging, get_logger
from app.routers.tenants import router as tenants_router
from app.routers.costs import router as costs_router
from app.routers.waste import router as waste_router
from app.routers.reports import router as reports_router
from app.routers.forecast import router as forecast_router
from app.routers.insights import router as insights_router
from app.routers.budgets import router as budgets_router
from app.routers.multicloud import router as multicloud_router, labels_router
from app.routers.drilldown import router as drilldown_router
from app.routers.alerts import router as alerts_router
from app.routers.optimization import router as optimization_router
from app.routers.admin import router as admin_router
from app.routers.ingest import ingest_router, health_router
from app.routers.fx import router as fx_router
from app.routers.k8s import router as k8s_router, admin_router as k8s_admin_router
from app.routers.unit_economics import router as unit_economics_router
from app.routers.ai_analyst import router as ai_analyst_router
from app.routers.stream import router as stream_router
from app.routers.policies import router as policies_router, admin_router as policies_admin_router
from app.routers.hierarchy import router as hierarchy_router
from app.routers.commitment_advisor import router as commitment_advisor_router
from app.routers.commitment_purchaser import router as commitment_purchaser_router
from app.routers.context_map import router as context_map_router
from app.routers.escalation import router as escalation_router
from app.routers.maturity import router as maturity_router
from app.routers.nl_query import router as nl_query_router
from app.routers.onboarding import router as onboarding_router
from app.routers.cost_estimate import router as cost_estimate_router
from app.routers.bots import router as bots_router
from app.routers.agent import router as agent_router
from app.routers.genai_cost import router as genai_cost_router
from app.routers.terraform_sync import router as terraform_sync_router
from app.routers.sustainability import router as sustainability_router
from app.routers.saml import router as saml_router
from app.routers.scim import router as scim_router
from app.services import cosmos, blob, keyvault

log = get_logger(__name__)


# ── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup + shutdown lifecycle management."""
    settings = get_settings()
    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
    )
    log.info("app.startup", version=settings.app_version, env=settings.environment)
    # Pre-warm FX rate cache so the first response is fast.
    try:
        from app.services.fx import prefetch as fx_prefetch
        await fx_prefetch(settings.fx_prefetch_list)
        log.info("fx.cache_warmed", currencies=settings.fx_prefetch_list)
    except Exception as exc:
        log.warning("fx.cache_warm_failed", error=str(exc))
    # Start the sub-hourly realtime ingest scheduler
    _scheduler_task = None
    if settings.realtime_poll_enabled:
        from app.services.realtime_ingest import run_scheduler
        _scheduler_task = asyncio.create_task(
            run_scheduler(settings.realtime_poll_interval_minutes)
        )
        log.info(
            "realtime_ingest.scheduler_registered",
            interval_minutes=settings.realtime_poll_interval_minutes,
        )
    yield
    # Graceful shutdown
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    log.info("app.shutdown")
    await cosmos.close()
    await blob.close()
    await keyvault.close()


# ── App factory ─────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CloudLens API",
        description="Azure FinOps Managed Service — cost intelligence API",
        version=settings.app_version,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── CORS ────────────────────────────────────────────────────────────────
    # The SPA is served from the storage account's static-website origin, which
    # is configured via the cors_allowed_origins setting (comma-separated).
    allowed_origins = list(settings.cors_allowed_origins_list)
    if not settings.is_production:
        allowed_origins += ["http://localhost:3000", "http://localhost:5173"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    )

    # ── Request ID + timing middleware ──────────────────────────────────────
    @app.middleware("http")
    async def request_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
            log.info(
                "http.response",
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            return response
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            log.error("http.unhandled_error", error=str(exc), elapsed_ms=elapsed_ms)
            raise

    # ── Global exception handlers ───────────────────────────────────────────
    @app.exception_handler(CloudLensError)
    async def cloudlens_error_handler(request: Request, exc: CloudLensError) -> JSONResponse:
        log.warning("app.domain_error", error_code=exc.error_code, message=exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("app.unhandled_exception", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "INTERNAL_ERROR", "message": "An unexpected error occurred"},
        )

    # ── Routers ─────────────────────────────────────────────────────────────
    for r in [tenants_router, costs_router, waste_router, reports_router, forecast_router, insights_router, budgets_router, multicloud_router, labels_router, drilldown_router, alerts_router, optimization_router, admin_router, ingest_router, health_router, fx_router, k8s_router, k8s_admin_router, unit_economics_router, ai_analyst_router, stream_router, policies_router, policies_admin_router, hierarchy_router, commitment_advisor_router, commitment_purchaser_router, escalation_router, context_map_router, maturity_router, nl_query_router, onboarding_router, cost_estimate_router]:
        app.include_router(r)
    app.include_router(bots_router)
    app.include_router(agent_router)
    app.include_router(genai_cost_router)
    app.include_router(terraform_sync_router)
    app.include_router(sustainability_router)
    app.include_router(saml_router)
    app.include_router(scim_router)

    return app


app = create_app()
