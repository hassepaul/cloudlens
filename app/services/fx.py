"""
FX (foreign-exchange) rate service
===================================

Fetches daily ECB reference rates from the official ECB Statistical Data
Warehouse API and caches them in-process with a 1-hour TTL.

All monetary values are stored internally in EUR.  This service converts them
to any of the ~32 ECB-supported currencies on the way *out* (API responses +
report generation).  Because the ECB publishes rates once per business day
(~16:00 CET), a 1-hour cache is more than adequate; if the fetch fails we
continue serving the most recent rates rather than hard-failing.

ECB SDMX API:
  https://data-api.ecb.europa.eu/service/data/EXR/D.<CCY>.EUR.SP00.A
  Returns 1 CCY per 1 EUR (inverted — we divide, not multiply).
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)

# ECB base URL — no authentication required.
_ECB_BASE = "https://data-api.ecb.europa.eu/service/data/EXR"
_ECB_PARAMS = "?detail=dataonly&lastNObservations=1&format=jsondata"

# Supported ISO 4217 codes (superset of common currencies).
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({
    "EUR", "USD", "GBP", "CHF", "JPY", "CNY", "CAD", "AUD", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "HRK", "ISK", "NZD",
    "SGD", "HKD", "KRW", "BRL", "MXN", "INR", "ZAR", "TRY", "RUB",
    "AED", "SAR", "ILS",
})
DEFAULT_CURRENCY = "EUR"

# ── In-process cache ─────────────────────────────────────────────────────────
# {currency_code: (rate_eur_to_ccy, fetched_at_monotonic)}
_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 3600.0          # 1 hour
_cache_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def get_rate(currency: str) -> float:
    """
    Return the exchange rate: how many *currency* units equal 1 EUR.

    EUR → EUR always returns 1.0.
    On network failure returns the last known rate (or 1.0 as a safe fallback).
    """
    ccy = currency.upper()
    if ccy == DEFAULT_CURRENCY:
        return 1.0
    if ccy not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Currency '{ccy}' is not supported. "
                         f"Supported: {sorted(SUPPORTED_CURRENCIES)}")
    now = time.monotonic()
    # Fast path: cache is warm.
    if ccy in _cache:
        rate, fetched_at = _cache[ccy]
        if now - fetched_at < _CACHE_TTL:
            return rate
    # Slow path: fetch under lock.
    async with _get_lock():
        # Re-check under lock.
        if ccy in _cache:
            rate, fetched_at = _cache[ccy]
            if now - fetched_at < _CACHE_TTL:
                return rate
        rate = await _fetch_rate_from_ecb(ccy)
        _cache[ccy] = (rate, time.monotonic())
    return rate


async def _fetch_rate_from_ecb(ccy: str) -> float:
    """Call the ECB SDMX API and return EUR→CCY rate."""
    # ECB series key: D.<CCY>.EUR.SP00.A = daily, base EUR, spot rate
    url = f"{_ECB_BASE}/D.{ccy}.EUR.SP00.A{_ECB_PARAMS}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Navigate SDMX JSON: dataSets[0].series["0:0:0:0:0"].observations
            series = next(iter(
                data["dataSets"][0]["series"].values()
            ))
            obs = series["observations"]
            # observations is {"0": [rate, status], "1": [...], ...}
            latest_key = str(max(int(k) for k in obs.keys()))
            rate = float(obs[latest_key][0])
            log.info("fx.rate_fetched", currency=ccy, rate=rate)
            return rate
    except Exception as exc:
        # If fetch fails, return last-known or 1.0.
        last = _cache.get(ccy)
        fallback = last[0] if last else 1.0
        log.warning("fx.fetch_failed", currency=ccy, error=str(exc), fallback=fallback)
        return fallback


def convert(amount_eur: float, rate: float, decimals: int = 2) -> float:
    """Convert a EUR amount using a pre-fetched rate."""
    return round(amount_eur * rate, decimals)


async def convert_async(amount_eur: float, currency: str, decimals: int = 2) -> float:
    """Convenience: fetch rate and convert in one call."""
    rate = await get_rate(currency)
    return convert(amount_eur, rate, decimals)


async def prefetch(currencies: list[str]) -> dict[str, float]:
    """
    Warm the cache for a set of currencies in parallel.
    Call at app startup or before building reports.
    """
    results = await asyncio.gather(
        *[get_rate(c) for c in currencies if c.upper() != DEFAULT_CURRENCY],
        return_exceptions=True,
    )
    rates: dict[str, float] = {DEFAULT_CURRENCY: 1.0}
    for ccy, result in zip(currencies, results):
        if isinstance(result, Exception):
            log.warning("fx.prefetch_failed", currency=ccy, error=str(result))
        else:
            rates[ccy.upper()] = result
    return rates
