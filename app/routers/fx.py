"""FX / currency router — GET /api/v1/fx"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.services.fx import get_rate, prefetch, SUPPORTED_CURRENCIES, DEFAULT_CURRENCY

router = APIRouter(prefix="/api/v1/fx", tags=["fx"])


@router.get("/rates")
async def get_rates(
    base: str = Query(default="EUR", description="Base currency (always EUR — others return conversion rate from EUR)"),
    currencies: str = Query(default="USD,GBP,CHF,JPY,CAD,AUD,SEK,PLN", description="Comma-separated target currencies"),
) -> dict:
    """Return current ECB reference rates for requested currencies (base = EUR)."""
    requested = [c.strip().upper() for c in currencies.split(",") if c.strip()]
    invalid = [c for c in requested if c not in SUPPORTED_CURRENCIES]
    if invalid:
        raise HTTPException(status_code=422,
                            detail={"error": "UNSUPPORTED_CURRENCY",
                                    "message": f"Unsupported currencies: {invalid}",
                                    "supported": sorted(SUPPORTED_CURRENCIES)})
    rates = await prefetch(requested)
    return {
        "base": DEFAULT_CURRENCY,
        "source": "European Central Bank (ECB Statistical Data Warehouse)",
        "note": "Rates are refreshed every hour. All CloudLens monetary values are stored in EUR and converted on the way out.",
        "rates": rates,
    }


@router.get("/convert")
async def convert_amount(
    amount: float = Query(..., description="Amount in EUR to convert"),
    currency: str = Query(..., description="Target ISO 4217 currency code"),
) -> dict:
    """Convert an EUR amount to the target currency at the current ECB rate."""
    ccy = currency.upper()
    if ccy not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422,
                            detail={"error": "UNSUPPORTED_CURRENCY",
                                    "message": f"'{ccy}' is not supported",
                                    "supported": sorted(SUPPORTED_CURRENCIES)})
    rate = await get_rate(ccy)
    converted = round(amount * rate, 2)
    return {
        "amount_eur": round(amount, 2),
        "currency": ccy,
        "rate": rate,
        "converted": converted,
    }
