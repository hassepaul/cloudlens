"""
GenAI Cost Tracking
===================

First-class cost visibility for LLM and AI API spend — the fastest-growing
cloud cost category in 2026.

Tracks and analyses spend across:
  * OpenAI          — GPT-4o, GPT-4o-mini, o1, o3-mini, embeddings, DALL·E,
                      Whisper, TTS
  * Azure OpenAI    — same models on enterprise billing
  * AWS Bedrock     — Claude 3.x, Llama 3.x, Mistral, Amazon Nova, Titan
  * GCP Vertex AI   — Gemini 1.5/2.0 Pro/Flash
  * Custom          — bring-your-own pricing for self-hosted models

Key outputs:
  * Cost per 1 M tokens by model (blended input/output rate)
  * Daily spend trends by provider/model/app
  * Budget monitoring with warning (≥80%) and breach (≥100%) alerts
  * Model comparison: quantify the saving from switching GPT-4o → GPT-4o-mini
    for a portion of traffic

Data ingestion paths:
  * POST /api/v1/genai/{tenant_id}/usage  — per-request or batch
  * Native SDK middleware (Python/TypeScript) calls the same endpoint inline

All costs stored in USD (source-of-truth) and converted to EUR via the live
ECB FX rate at ingest time.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta, datetime, timezone
from typing import Optional
from uuid import uuid4

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)

# ── Pricing table (USD per 1 M tokens, unless noted) ─────────────────────────
# All prices as of mid-2026. Operators may inject overrides via the custom
# pricing endpoint; the table is the default fallback.

_PRICING: dict[str, dict] = {
    # ── OpenAI ────────────────────────────────────────────────────────────
    "openai/gpt-4o":                        {"input": 2.50,  "output": 10.00},
    "openai/gpt-4o-mini":                   {"input": 0.15,  "output": 0.60},
    "openai/gpt-4-turbo":                   {"input": 10.00, "output": 30.00},
    "openai/gpt-4":                         {"input": 30.00, "output": 60.00},
    "openai/gpt-3.5-turbo":                 {"input": 0.50,  "output": 1.50},
    "openai/o1":                            {"input": 15.00, "output": 60.00},
    "openai/o1-mini":                       {"input": 3.00,  "output": 12.00},
    "openai/o3-mini":                       {"input": 1.10,  "output": 4.40},
    "openai/text-embedding-3-small":        {"input": 0.02,  "output": 0.00},
    "openai/text-embedding-3-large":        {"input": 0.13,  "output": 0.00},
    "openai/text-embedding-ada-002":        {"input": 0.10,  "output": 0.00},
    "openai/dall-e-3":                      {"per_image": 0.040},   # standard 1024×1024
    "openai/dall-e-3-hd":                   {"per_image": 0.080},
    "openai/tts-1":                         {"per_1m_chars": 15.00},
    "openai/tts-1-hd":                      {"per_1m_chars": 30.00},
    "openai/whisper-1":                     {"per_minute": 0.006},
    # ── Azure OpenAI (same models, enterprise SKU pricing) ────────────────
    "azure_openai/gpt-4o":                  {"input": 2.50,  "output": 10.00},
    "azure_openai/gpt-4o-mini":             {"input": 0.165, "output": 0.66},
    "azure_openai/gpt-4-turbo":             {"input": 10.00, "output": 30.00},
    "azure_openai/gpt-35-turbo":            {"input": 0.50,  "output": 1.50},
    "azure_openai/text-embedding-3-small":  {"input": 0.02,  "output": 0.00},
    "azure_openai/text-embedding-3-large":  {"input": 0.13,  "output": 0.00},
    # ── AWS Bedrock ───────────────────────────────────────────────────────
    "bedrock/claude-3-5-sonnet":            {"input": 3.00,  "output": 15.00},
    "bedrock/claude-3-5-haiku":             {"input": 0.80,  "output": 4.00},
    "bedrock/claude-3-opus":                {"input": 15.00, "output": 75.00},
    "bedrock/claude-3-haiku":               {"input": 0.25,  "output": 1.25},
    "bedrock/llama-3-1-405b-instruct":      {"input": 5.32,  "output": 16.00},
    "bedrock/llama-3-1-70b-instruct":       {"input": 0.99,  "output": 0.99},
    "bedrock/llama-3-1-8b-instruct":        {"input": 0.22,  "output": 0.22},
    "bedrock/llama-3-70b-instruct":         {"input": 0.99,  "output": 0.99},
    "bedrock/mistral-7b-instruct":          {"input": 0.15,  "output": 0.20},
    "bedrock/mistral-large":                {"input": 4.00,  "output": 12.00},
    "bedrock/titan-text-lite":              {"input": 0.30,  "output": 0.40},
    "bedrock/amazon-nova-pro":              {"input": 0.80,  "output": 3.20},
    "bedrock/amazon-nova-lite":             {"input": 0.06,  "output": 0.24},
    "bedrock/amazon-nova-micro":            {"input": 0.035, "output": 0.14},
    # ── GCP Vertex AI ─────────────────────────────────────────────────────
    "vertex_ai/gemini-2.0-flash":           {"input": 0.10,  "output": 0.40},
    "vertex_ai/gemini-1.5-pro":             {"input": 3.50,  "output": 10.50},
    "vertex_ai/gemini-1.5-flash":           {"input": 0.075, "output": 0.30},
    "vertex_ai/gemini-1.0-pro":             {"input": 0.50,  "output": 1.50},
    "vertex_ai/text-embedding-004":         {"input": 0.025, "output": 0.00},
}

# Providers we recognise
_KNOWN_PROVIDERS = frozenset({"openai", "azure_openai", "bedrock", "vertex_ai", "custom"})

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class GenAIUsageRecord:
    id: str
    tenant_id: str
    provider: str
    model: str
    deployment_name: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    total_cost_eur: float
    request_type: str        # "chat" | "embedding" | "image" | "speech" | "fine_tune"
    quantity: int            # images: N images; other: 1
    duration_seconds: float  # audio models: clip length in seconds
    app_name: str
    environment: str
    user_id: str
    tags: dict
    latency_ms: int
    period_date: str         # YYYY-MM-DD (for daily rollup queries)
    recorded_at: str

    def to_cosmos(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "_partitionKey": self.tenant_id,
            "type": "genai_usage",
            "provider": self.provider,
            "model": self.model,
            "deployment_name": self.deployment_name,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "total_cost_eur": self.total_cost_eur,
            "request_type": self.request_type,
            "quantity": self.quantity,
            "duration_seconds": self.duration_seconds,
            "app_name": self.app_name,
            "environment": self.environment,
            "user_id": self.user_id,
            "tags": self.tags,
            "latency_ms": self.latency_ms,
            "period_date": self.period_date,
            "recorded_at": self.recorded_at,
        }

    @staticmethod
    def from_cosmos(doc: dict) -> "GenAIUsageRecord":
        return GenAIUsageRecord(
            id=doc["id"],
            tenant_id=doc["tenant_id"],
            provider=doc.get("provider", ""),
            model=doc.get("model", ""),
            deployment_name=doc.get("deployment_name", ""),
            input_tokens=int(doc.get("input_tokens") or 0),
            output_tokens=int(doc.get("output_tokens") or 0),
            total_tokens=int(doc.get("total_tokens") or 0),
            input_cost_usd=float(doc.get("input_cost_usd") or 0),
            output_cost_usd=float(doc.get("output_cost_usd") or 0),
            total_cost_usd=float(doc.get("total_cost_usd") or 0),
            total_cost_eur=float(doc.get("total_cost_eur") or 0),
            request_type=doc.get("request_type", "chat"),
            quantity=int(doc.get("quantity") or 1),
            duration_seconds=float(doc.get("duration_seconds") or 0),
            app_name=doc.get("app_name", ""),
            environment=doc.get("environment", ""),
            user_id=doc.get("user_id", ""),
            tags=doc.get("tags") or {},
            latency_ms=int(doc.get("latency_ms") or 0),
            period_date=doc.get("period_date", ""),
            recorded_at=doc.get("recorded_at", ""),
        )


@dataclass
class GenAIBudget:
    id: str
    tenant_id: str
    name: str
    monthly_limit_usd: float
    model_filter: str = ""       # empty = all models
    provider_filter: str = ""    # empty = all providers
    app_filter: str = ""         # empty = all apps
    alert_threshold_pct: float = 80.0
    created_at: str = ""
    updated_at: str = ""

    def to_cosmos(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "_partitionKey": self.tenant_id,
            "type": "genai_budget",
            "name": self.name,
            "monthly_limit_usd": self.monthly_limit_usd,
            "model_filter": self.model_filter,
            "provider_filter": self.provider_filter,
            "app_filter": self.app_filter,
            "alert_threshold_pct": self.alert_threshold_pct,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_cosmos(doc: dict) -> "GenAIBudget":
        return GenAIBudget(
            id=doc["id"],
            tenant_id=doc["tenant_id"],
            name=doc.get("name", ""),
            monthly_limit_usd=float(doc.get("monthly_limit_usd") or 0),
            model_filter=doc.get("model_filter", ""),
            provider_filter=doc.get("provider_filter", ""),
            app_filter=doc.get("app_filter", ""),
            alert_threshold_pct=float(doc.get("alert_threshold_pct") or 80),
            created_at=doc.get("created_at", ""),
            updated_at=doc.get("updated_at", ""),
        )


@dataclass
class ModelStats:
    provider: str
    model: str
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: float
    total_cost_eur: float
    avg_cost_per_request_usd: float
    avg_tokens_per_request: float
    blended_cost_per_1m_tokens_usd: float
    cost_trend_pct: float = 0.0        # % change vs previous equal period


@dataclass
class BudgetUtilisation:
    budget: GenAIBudget
    current_spend_usd: float
    utilisation_pct: float
    status: str                        # "ok" | "warning" | "breach"
    days_elapsed: int
    days_in_month: int
    projected_monthly_usd: float


@dataclass
class ModelComparison:
    current_model: str
    current_provider: str
    alternative_model: str
    alternative_provider: str
    current_cost_usd: float
    alternative_cost_usd: float
    saving_usd: float
    saving_pct: float
    total_tokens: int
    caveat: str


@dataclass
class GenAISummary:
    tenant_id: str
    period_days: int
    total_cost_usd: float
    total_cost_eur: float
    total_requests: int
    total_tokens: int
    by_provider: list[dict]
    by_model: list[ModelStats]
    daily_trend: list[dict]
    top_model: str
    cost_per_1m_tokens_usd: float
    comparisons: list[ModelComparison]


# ── Pricing helpers ───────────────────────────────────────────────────────────

def _normalize_model(model: str) -> str:
    """Strip date/version suffixes so 'gpt-4o-2024-11-20' → 'gpt-4o'."""
    m = model.lower().strip()
    m = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", m)   # -2024-11-20
    m = re.sub(r"-\d{8}$", "", m)                # -20241022
    return m


def lookup_pricing(provider: str, model: str) -> dict:
    """Return pricing dict for the model, or {} if unknown."""
    prov = provider.lower().replace("-", "_")
    key = f"{prov}/{_normalize_model(model)}"
    return _PRICING.get(key, {})


def calculate_cost(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    quantity: int = 1,
    duration_seconds: float = 0.0,
    custom_input_price: float = 0.0,   # USD per 1M tokens (custom models)
    custom_output_price: float = 0.0,
) -> tuple[float, float]:
    """Return (input_cost_usd, output_cost_usd).

    Handles three billing models:
      token-based  — most LLMs: per 1M input + output tokens
      per_image    — DALL·E: quantity × per_image rate
      per_minute   — Whisper: duration_seconds / 60 × per_minute rate
      per_1m_chars — TTS: input_tokens (reused for char count) × rate
    """
    if custom_input_price or custom_output_price:
        return (
            custom_input_price * input_tokens / 1_000_000,
            custom_output_price * output_tokens / 1_000_000,
        )
    pricing = lookup_pricing(provider, model)
    if not pricing:
        return 0.0, 0.0
    if "per_image" in pricing:
        return round(pricing["per_image"] * max(quantity, 1), 6), 0.0
    if "per_minute" in pricing:
        return round(pricing["per_minute"] * (duration_seconds / 60.0), 6), 0.0
    if "per_1m_chars" in pricing:
        return round(pricing["per_1m_chars"] * input_tokens / 1_000_000, 6), 0.0
    in_cost = pricing.get("input", 0.0) * input_tokens / 1_000_000
    out_cost = pricing.get("output", 0.0) * output_tokens / 1_000_000
    return round(in_cost, 6), round(out_cost, 6)


def _eur_rate() -> float:
    """Best-effort USD→EUR rate (live FX or fallback)."""
    try:
        from app.services.fx import get_rate_sync
        rate = get_rate_sync("USD")  # EUR per USD
        return float(rate) if rate else 0.92
    except Exception:
        return float(__import__("os").environ.get("USD_TO_EUR", "0.92"))


# ── Container helpers ─────────────────────────────────────────────────────────

def _usage_container() -> str:
    return get_settings().cosmos_container_genai_usage


def _budget_container() -> str:
    return get_settings().cosmos_container_genai_budgets


# ── Ingest ────────────────────────────────────────────────────────────────────

def _build_record(tenant_id: str, payload: dict) -> GenAIUsageRecord:
    provider = payload.get("provider", "custom").lower().replace("-", "_")
    model = payload.get("model", "unknown")
    input_tok = int(payload.get("input_tokens") or 0)
    output_tok = int(payload.get("output_tokens") or 0)
    qty = int(payload.get("quantity") or 1)
    dur = float(payload.get("duration_seconds") or 0)

    # Prefer caller-supplied cost; fall back to pricing table
    if payload.get("total_cost_usd") is not None:
        total_usd = float(payload["total_cost_usd"])
        in_cost = float(payload.get("input_cost_usd") or total_usd)
        out_cost = float(payload.get("output_cost_usd") or 0)
    else:
        in_cost, out_cost = calculate_cost(
            provider, model, input_tok, output_tok, qty, dur,
            float(payload.get("custom_input_price") or 0),
            float(payload.get("custom_output_price") or 0),
        )
        total_usd = in_cost + out_cost

    fx = _eur_rate()
    now = datetime.now(timezone.utc)
    return GenAIUsageRecord(
        id=payload.get("id") or str(uuid4()),
        tenant_id=tenant_id,
        provider=provider,
        model=model,
        deployment_name=payload.get("deployment_name", ""),
        input_tokens=input_tok,
        output_tokens=output_tok,
        total_tokens=input_tok + output_tok,
        input_cost_usd=in_cost,
        output_cost_usd=out_cost,
        total_cost_usd=round(total_usd, 6),
        total_cost_eur=round(total_usd * fx, 6),
        request_type=payload.get("request_type", "chat"),
        quantity=qty,
        duration_seconds=dur,
        app_name=payload.get("app_name", ""),
        environment=payload.get("environment", ""),
        user_id=payload.get("user_id", ""),
        tags=payload.get("tags") or {},
        latency_ms=int(payload.get("latency_ms") or 0),
        period_date=payload.get("period_date") or now.date().isoformat(),
        recorded_at=payload.get("recorded_at") or now.isoformat(),
    )


async def ingest_usage(tenant_id: str, payload: dict) -> GenAIUsageRecord:
    """Ingest a single GenAI usage record."""
    record = _build_record(tenant_id, payload)
    await cosmos.upsert_item(_usage_container(), record.to_cosmos())
    log.info("genai.usage_ingested", tenant_id=tenant_id, model=record.model, cost_usd=record.total_cost_usd)
    return record


async def ingest_batch(tenant_id: str, records: list[dict]) -> dict:
    """Ingest a list of GenAI usage records. Returns {ingested, failed}."""
    ingested, failed = 0, 0
    for payload in records:
        try:
            await ingest_usage(tenant_id, payload)
            ingested += 1
        except Exception as exc:
            log.warning("genai.batch_ingest_item_failed", tenant_id=tenant_id, error=str(exc))
            failed += 1
    return {"ingested": ingested, "failed": failed, "total": len(records)}


# ── Aggregation helpers ───────────────────────────────────────────────────────

def _date_range(period_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=period_days)
    return start.isoformat(), end.isoformat()


async def _fetch_usage(tenant_id: str, period_days: int, extra_where: str = "", extra_params: list = None) -> list[dict]:
    start, end = _date_range(period_days)
    params = [
        {"name": "@t", "value": tenant_id},
        {"name": "@s", "value": start},
        {"name": "@e", "value": end},
    ]
    if extra_params:
        params.extend(extra_params)
    sql = (
        f"SELECT * FROM c WHERE c.tenant_id=@t AND c.type='genai_usage' "
        f"AND c.period_date>=@s AND c.period_date<=@e {extra_where}"
    )
    try:
        return await cosmos.query_items(_usage_container(), sql, params, partition_key=tenant_id)
    except CosmosError:
        return []


def _model_key(provider: str, model: str) -> str:
    return f"{provider}/{_normalize_model(model)}"


def _compute_model_stats(rows: list[dict]) -> list[ModelStats]:
    buckets: dict[str, dict] = {}
    for r in rows:
        key = _model_key(r.get("provider", ""), r.get("model", ""))
        if key not in buckets:
            buckets[key] = {
                "provider": r.get("provider", ""),
                "model": r.get("model", ""),
                "requests": 0, "input_tok": 0, "output_tok": 0,
                "cost_usd": 0.0, "cost_eur": 0.0,
            }
        b = buckets[key]
        b["requests"] += 1
        b["input_tok"] += int(r.get("input_tokens") or 0)
        b["output_tok"] += int(r.get("output_tokens") or 0)
        b["cost_usd"] += float(r.get("total_cost_usd") or 0)
        b["cost_eur"] += float(r.get("total_cost_eur") or 0)

    stats: list[ModelStats] = []
    for b in buckets.values():
        total_tok = b["input_tok"] + b["output_tok"]
        avg_rq = b["cost_usd"] / b["requests"] if b["requests"] else 0
        avg_tok = total_tok / b["requests"] if b["requests"] else 0
        blended = b["cost_usd"] / total_tok * 1_000_000 if total_tok else 0
        stats.append(ModelStats(
            provider=b["provider"],
            model=b["model"],
            total_requests=b["requests"],
            total_input_tokens=b["input_tok"],
            total_output_tokens=b["output_tok"],
            total_tokens=total_tok,
            total_cost_usd=round(b["cost_usd"], 4),
            total_cost_eur=round(b["cost_eur"], 4),
            avg_cost_per_request_usd=round(avg_rq, 6),
            avg_tokens_per_request=round(avg_tok, 1),
            blended_cost_per_1m_tokens_usd=round(blended, 4),
        ))
    return sorted(stats, key=lambda s: -s.total_cost_usd)


def _build_comparisons(model_stats: list[ModelStats]) -> list[ModelComparison]:
    """For each expensive model, find the cheapest capable alternative."""
    comparisons: list[ModelComparison] = []
    # Define upgrade/downgrade pairs (current → alternative)
    alternatives: dict[str, tuple[str, str]] = {
        "openai/gpt-4o":       ("openai", "gpt-4o-mini"),
        "openai/gpt-4-turbo":  ("openai", "gpt-4o"),
        "openai/o1":           ("openai", "gpt-4o"),
        "bedrock/claude-3-opus": ("bedrock", "claude-3-5-sonnet"),
        "bedrock/claude-3-5-sonnet": ("bedrock", "claude-3-5-haiku"),
        "vertex_ai/gemini-1.5-pro": ("vertex_ai", "gemini-1.5-flash"),
        "azure_openai/gpt-4-turbo": ("azure_openai", "gpt-4o"),
        "azure_openai/gpt-4o": ("azure_openai", "gpt-4o-mini"),
    }
    for ms in model_stats:
        key = _model_key(ms.provider, ms.model)
        alt = alternatives.get(key)
        if not alt or ms.total_cost_usd < 1.0:
            continue
        alt_prov, alt_mod = alt
        alt_in, alt_out = calculate_cost(alt_prov, alt_mod, ms.total_input_tokens, ms.total_output_tokens)
        alt_cost = alt_in + alt_out
        saving = ms.total_cost_usd - alt_cost
        if saving <= 0:
            continue
        comparisons.append(ModelComparison(
            current_model=ms.model,
            current_provider=ms.provider,
            alternative_model=alt_mod,
            alternative_provider=alt_prov,
            current_cost_usd=round(ms.total_cost_usd, 2),
            alternative_cost_usd=round(alt_cost, 2),
            saving_usd=round(saving, 2),
            saving_pct=round(saving / ms.total_cost_usd * 100, 1),
            total_tokens=ms.total_tokens,
            caveat="Quality may differ — validate on your workload before migrating.",
        ))
    return sorted(comparisons, key=lambda c: -c.saving_usd)


# ── Public query API ──────────────────────────────────────────────────────────

async def get_summary(tenant_id: str, period_days: int = 30) -> GenAISummary:
    rows = await _fetch_usage(tenant_id, period_days)
    model_stats = _compute_model_stats(rows)

    total_cost_usd = sum(ms.total_cost_usd for ms in model_stats)
    total_cost_eur = sum(ms.total_cost_eur for ms in model_stats)
    total_requests = sum(ms.total_requests for ms in model_stats)
    total_tokens = sum(ms.total_tokens for ms in model_stats)

    # By provider
    prov_buckets: dict[str, dict] = {}
    for ms in model_stats:
        p = ms.provider
        if p not in prov_buckets:
            prov_buckets[p] = {"provider": p, "cost_usd": 0.0, "cost_eur": 0.0, "requests": 0}
        prov_buckets[p]["cost_usd"] += ms.total_cost_usd
        prov_buckets[p]["cost_eur"] += ms.total_cost_eur
        prov_buckets[p]["requests"] += ms.total_requests
    by_provider = sorted(
        [{"provider": v["provider"], "cost_usd": round(v["cost_usd"], 4), "cost_eur": round(v["cost_eur"], 4), "requests": v["requests"]} for v in prov_buckets.values()],
        key=lambda x: -x["cost_usd"],
    )

    # Daily trend (last 14 days from the rows)
    day_buckets: dict[str, float] = {}
    for r in rows:
        day = r.get("period_date", "")[:10]
        if day:
            day_buckets[day] = day_buckets.get(day, 0.0) + float(r.get("total_cost_usd") or 0)
    daily_trend = [{"date": d, "cost_usd": round(v, 4)} for d, v in sorted(day_buckets.items())[-14:]]

    top_model = model_stats[0].model if model_stats else ""
    blended_per_1m = total_cost_usd / total_tokens * 1_000_000 if total_tokens else 0

    return GenAISummary(
        tenant_id=tenant_id,
        period_days=period_days,
        total_cost_usd=round(total_cost_usd, 4),
        total_cost_eur=round(total_cost_eur, 4),
        total_requests=total_requests,
        total_tokens=total_tokens,
        by_provider=by_provider,
        by_model=model_stats,
        daily_trend=daily_trend,
        top_model=top_model,
        cost_per_1m_tokens_usd=round(blended_per_1m, 4),
        comparisons=_build_comparisons(model_stats),
    )


async def get_model_breakdown(tenant_id: str, period_days: int = 30) -> list[ModelStats]:
    rows = await _fetch_usage(tenant_id, period_days)
    return _compute_model_stats(rows)


async def get_daily_trends(tenant_id: str, period_days: int = 30, group_by: str = "model") -> list[dict]:
    rows = await _fetch_usage(tenant_id, period_days)
    buckets: dict[tuple, dict] = {}
    for r in rows:
        day = (r.get("period_date") or "")[:10]
        if group_by == "model":
            grp = r.get("model", "")
        elif group_by == "provider":
            grp = r.get("provider", "")
        elif group_by == "app":
            grp = r.get("app_name", "") or "unknown"
        else:
            grp = "total"
        key = (day, grp)
        if key not in buckets:
            buckets[key] = {"date": day, "group": grp, "cost_usd": 0.0, "requests": 0, "tokens": 0}
        b = buckets[key]
        b["cost_usd"] += float(r.get("total_cost_usd") or 0)
        b["requests"] += 1
        b["tokens"] += int(r.get("total_tokens") or 0)

    return sorted(
        [{"date": v["date"], "group": v["group"], "cost_usd": round(v["cost_usd"], 4), "requests": v["requests"], "tokens": v["tokens"]} for v in buckets.values()],
        key=lambda x: (x["date"], x["group"]),
    )


async def get_top_apps(tenant_id: str, period_days: int = 30) -> list[dict]:
    rows = await _fetch_usage(tenant_id, period_days)
    app_buckets: dict[str, dict] = {}
    for r in rows:
        app = r.get("app_name") or "unknown"
        if app not in app_buckets:
            app_buckets[app] = {"app_name": app, "cost_usd": 0.0, "requests": 0, "tokens": 0}
        app_buckets[app]["cost_usd"] += float(r.get("total_cost_usd") or 0)
        app_buckets[app]["requests"] += 1
        app_buckets[app]["tokens"] += int(r.get("total_tokens") or 0)
    total = sum(v["cost_usd"] for v in app_buckets.values()) or 1
    result = sorted(
        [{"app_name": v["app_name"], "cost_usd": round(v["cost_usd"], 4), "requests": v["requests"], "tokens": v["tokens"], "pct": round(v["cost_usd"] / total * 100, 1)} for v in app_buckets.values()],
        key=lambda x: -x["cost_usd"],
    )
    return result[:20]


# ── Budget management ─────────────────────────────────────────────────────────

async def create_budget(tenant_id: str, payload: dict) -> GenAIBudget:
    now = datetime.now(timezone.utc).isoformat()
    budget = GenAIBudget(
        id=payload.get("id") or str(uuid4()),
        tenant_id=tenant_id,
        name=payload["name"],
        monthly_limit_usd=float(payload["monthly_limit_usd"]),
        model_filter=payload.get("model_filter", ""),
        provider_filter=payload.get("provider_filter", ""),
        app_filter=payload.get("app_filter", ""),
        alert_threshold_pct=float(payload.get("alert_threshold_pct") or 80),
        created_at=now,
        updated_at=now,
    )
    await cosmos.upsert_item(_budget_container(), budget.to_cosmos())
    return budget


async def list_budgets(tenant_id: str) -> list[GenAIBudget]:
    sql = "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='genai_budget'"
    params = [{"name": "@t", "value": tenant_id}]
    try:
        docs = await cosmos.query_items(_budget_container(), sql, params, partition_key=tenant_id)
        return [GenAIBudget.from_cosmos(d) for d in docs]
    except CosmosError:
        return []


async def delete_budget(tenant_id: str, budget_id: str) -> bool:
    try:
        await cosmos.delete_item(_budget_container(), budget_id, tenant_id)
        return True
    except Exception:
        return False


async def check_budget_alerts(tenant_id: str) -> list[BudgetUtilisation]:
    budgets = await list_budgets(tenant_id)
    if not budgets:
        return []

    today = date.today()
    # Days elapsed in current calendar month
    days_elapsed = today.day
    import calendar
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Fetch current-month usage
    month_start = today.replace(day=1).isoformat()
    sql = "SELECT * FROM c WHERE c.tenant_id=@t AND c.type='genai_usage' AND c.period_date>=@s"
    params = [{"name": "@t", "value": tenant_id}, {"name": "@s", "value": month_start}]
    try:
        rows = await cosmos.query_items(_usage_container(), sql, params, partition_key=tenant_id)
    except CosmosError:
        rows = []

    alerts: list[BudgetUtilisation] = []
    for budget in budgets:
        # Filter rows according to budget scope
        scoped = [
            r for r in rows
            if (not budget.model_filter or r.get("model", "") == budget.model_filter)
            and (not budget.provider_filter or r.get("provider", "") == budget.provider_filter)
            and (not budget.app_filter or r.get("app_name", "") == budget.app_filter)
        ]
        current_spend = sum(float(r.get("total_cost_usd") or 0) for r in scoped)
        pct = current_spend / budget.monthly_limit_usd * 100 if budget.monthly_limit_usd else 0
        projected = current_spend / days_elapsed * days_in_month if days_elapsed else 0
        status = "breach" if pct >= 100 else "warning" if pct >= budget.alert_threshold_pct else "ok"
        alerts.append(BudgetUtilisation(
            budget=budget,
            current_spend_usd=round(current_spend, 4),
            utilisation_pct=round(pct, 1),
            status=status,
            days_elapsed=days_elapsed,
            days_in_month=days_in_month,
            projected_monthly_usd=round(projected, 2),
        ))
    return alerts
