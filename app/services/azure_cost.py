"""Azure Cost Management API client — async, with retry and structured error handling."""
from __future__ import annotations
import asyncio
import time
from datetime import date, timedelta
from typing import Any, Optional
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


from app.exceptions import AzureAPIError
from app.logging_config import get_logger

log = get_logger(__name__)

COST_MGMT_BASE = "https://management.azure.com"
COST_MGMT_API_VERSION = "2023-03-01"
ADVISOR_API_VERSION = "2023-01-01"
TOKEN_SCOPE = "https://management.azure.com/.default"


class AzureCostClient:
    """
    Authenticated client for Azure Cost Management + Advisor APIs.
    One instance per tenant — holds the tenant's service principal token.
    """

    def __init__(
        self,
        subscription_id: str,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        timeout_seconds: int = 30,
    ) -> None:
        self._subscription_id = subscription_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._timeout = httpx.Timeout(timeout_seconds)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._http: Optional[httpx.AsyncClient] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "AzureCostClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        await self._refresh_token()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()

    # ── authentication ───────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return
        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": TOKEN_SCOPE,
        }
        try:
            resp = await self._http.post(url, data=data)
            resp.raise_for_status()
            body = resp.json()
            self._access_token = body["access_token"]
            self._token_expires_at = time.time() + body.get("expires_in", 3600)
            log.debug("azure.token_refreshed", tenant_id=self._tenant_id)
        except httpx.HTTPStatusError as exc:
            raise AzureAPIError(
                f"Token refresh failed for tenant {self._tenant_id}",
                detail=exc.response.text,
            ) from exc
        except Exception as exc:
            raise AzureAPIError(f"Token refresh error: {exc}") from exc

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed. Reused by the
        Resource Graph collector so it doesn't authenticate a second time."""
        await self._refresh_token()
        return self._access_token or ""

    # ── Cost Management API ──────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(AzureAPIError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def get_cost_by_resource(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """
        Fetch daily cost grouped by resource from Cost Management API.
        Returns list of raw row dicts with columns mapped by name.
        """
        await self._refresh_token()
        url = (
            f"{COST_MGMT_BASE}/subscriptions/{self._subscription_id}"
            f"/providers/Microsoft.CostManagement/query"
            f"?api-version={COST_MGMT_API_VERSION}"
        )
        payload = {
            "type": "ActualCost",
            "dataSet": {
                "granularity": "Daily",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                    "totalQuantity": {"name": "UsageQuantity", "function": "Sum"},
                },
                "grouping": [
                    {"type": "Dimension", "name": "ResourceId"},
                    {"type": "Dimension", "name": "ResourceGroupName"},
                    {"type": "Dimension", "name": "ServiceName"},
                    {"type": "Dimension", "name": "ResourceType"},
                    {"type": "Dimension", "name": "ResourceLocation"},
                    {"type": "Dimension", "name": "MeterCategory"},
                    {"type": "Dimension", "name": "MeterSubCategory"},
                    {"type": "Dimension", "name": "UnitOfMeasure"},
                    {"type": "Dimension", "name": "Currency"},
                ],
            },
            "timePeriod": {
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
            },
        }
        try:
            resp = await self._http.post(url, json=payload, headers=self._auth_headers)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                log.warning("azure.cost_rate_limited", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                raise AzureAPIError("Cost Management rate limited")
            resp.raise_for_status()
            body = resp.json()
            return self._parse_cost_response(body)
        except AzureAPIError:
            raise
        except httpx.HTTPStatusError as exc:
            raise AzureAPIError(
                f"Cost Management API error: {exc.response.status_code}",
                detail=exc.response.text[:500],
            ) from exc
        except Exception as exc:
            raise AzureAPIError(f"Unexpected error querying costs: {exc}") from exc

    # Cost Management returns columns by name, but the *order* varies by query
    # and the cost/quantity columns carry different names depending on the
    # dataset (ActualCost vs AmortizedCost) and whether tax is included. We map
    # each logical field to the set of column names Azure may use, and look up
    # strictly by name — never by position — so a reordered response can't
    # silently shift values into the wrong field.
    _COL_ALIASES = {
        "cost":              ("Cost", "CostUSD", "PreTaxCost", "PreTaxCostUSD"),
        "quantity":          ("UsageQuantity", "Quantity"),
        "resource_id":       ("ResourceId",),
        "resource_group":    ("ResourceGroupName", "ResourceGroup"),
        "service_name":      ("ServiceName",),
        "resource_type":     ("ResourceType",),
        "location":          ("ResourceLocation", "Location"),
        "meter_category":    ("MeterCategory",),
        "meter_sub_category":("MeterSubCategory",),
        "unit_of_measure":   ("UnitOfMeasure",),
        "currency":          ("Currency", "BillingCurrency", "BillingCurrencyCode"),
        "date":              ("UsageDate", "Date", "BillingMonth"),
    }

    def _parse_cost_response(self, body: dict) -> list[dict]:
        """
        Map a columnar Cost Management query response to a list of dicts.

        Strictly name-based: every value is resolved by matching the column
        name against a known alias set. Columns that are absent yield a safe
        default (0.0 for numerics, "" for strings) rather than a misaligned
        value pulled from the wrong position. If the mandatory Cost or date
        columns are missing entirely, the response shape is unexpected and we
        raise AzureAPIError so the ingest surfaces the problem instead of
        persisting garbage.
        """
        props = body.get("properties", {})
        columns = [c.get("name", "") for c in props.get("columns", [])]
        rows = props.get("rows", []) or []
        name_to_idx = {name: i for i, name in enumerate(columns)}

        def idx_for(field: str):
            for alias in self._COL_ALIASES[field]:
                if alias in name_to_idx:
                    return name_to_idx[alias]
            return None

        resolved = {field: idx_for(field) for field in self._COL_ALIASES}

        # Sanity-check the response shape up front.
        if resolved["cost"] is None or resolved["date"] is None:
            raise AzureAPIError(
                "Unexpected Cost Management response shape",
                detail=f"Could not locate Cost/date columns. Columns seen: {columns}",
            )

        def get_num(row, field):
            i = resolved[field]
            if i is None or i >= len(row) or row[i] is None:
                return 0.0
            try:
                return float(row[i])
            except (TypeError, ValueError):
                return 0.0

        def get_str(row, field, lower=False):
            i = resolved[field]
            if i is None or i >= len(row) or row[i] is None:
                return ""
            val = str(row[i])
            return val.lower() if lower else val

        results = []
        for row in rows:
            results.append({
                "cost":               get_num(row, "cost"),
                "quantity":           get_num(row, "quantity"),
                "resource_id":        get_str(row, "resource_id", lower=True),
                "resource_group":     get_str(row, "resource_group", lower=True),
                "service_name":       get_str(row, "service_name"),
                "resource_type":      get_str(row, "resource_type"),
                "location":           get_str(row, "location"),
                "meter_category":     get_str(row, "meter_category"),
                "meter_sub_category": get_str(row, "meter_sub_category"),
                "unit_of_measure":    get_str(row, "unit_of_measure"),
                "currency":           get_str(row, "currency") or "EUR",
                "date":               get_str(row, "date"),
            })
        return results

    async def get_resource_tags(self, resource_id: str) -> dict[str, str]:
        """Fetch tags for a specific ARM resource."""
        await self._refresh_token()
        url = f"{COST_MGMT_BASE}{resource_id}?api-version=2021-04-01"
        try:
            resp = await self._http.get(url, headers=self._auth_headers)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json().get("tags") or {}
        except Exception as exc:
            log.warning("azure.tags_fetch_failed", resource_id=resource_id, error=str(exc))
            return {}

    # ── Advisor API ──────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(AzureAPIError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def get_advisor_recommendations(self, category: str = "Cost") -> list[dict]:
        """Fetch Advisor cost recommendations for the subscription."""
        await self._refresh_token()
        url = (
            f"{COST_MGMT_BASE}/subscriptions/{self._subscription_id}"
            f"/providers/Microsoft.Advisor/recommendations"
            f"?api-version={ADVISOR_API_VERSION}&$filter=Category eq '{category}'"
        )
        try:
            resp = await self._http.get(url, headers=self._auth_headers)
            resp.raise_for_status()
            body = resp.json()
            return body.get("value", [])
        except httpx.HTTPStatusError as exc:
            raise AzureAPIError(
                f"Advisor API error: {exc.response.status_code}",
                detail=exc.response.text[:500],
            ) from exc
        except Exception as exc:
            raise AzureAPIError(f"Advisor API unexpected error: {exc}") from exc

    async def get_vm_metrics(self, resource_id: str, days: int = 14) -> dict:
        """Fetch CPU average % for a VM over the last N days."""
        await self._refresh_token()
        from datetime import datetime, timezone
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        url = (
            f"{COST_MGMT_BASE}{resource_id}/providers/microsoft.insights/metrics"
            f"?api-version=2018-01-01"
            f"&metricnames=Percentage CPU"
            f"&aggregation=Average"
            f"&interval=P1D"
            f"&timespan={start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        try:
            resp = await self._http.get(url, headers=self._auth_headers)
            resp.raise_for_status()
            body = resp.json()
            values = body.get("value", [{}])
            timeseries = values[0].get("timeseries", [{}]) if values else [{}]
            data = timeseries[0].get("data", []) if timeseries else []
            averages = [d.get("average", 0.0) or 0.0 for d in data]
            cpu_avg = sum(averages) / len(averages) if averages else 0.0
            return {"cpu_avg_pct": round(cpu_avg, 2), "samples": len(averages)}
        except AzureAPIError:
            raise
        except Exception as exc:
            log.warning("azure.vm_metrics_failed", resource_id=resource_id, error=str(exc))
            return {"cpu_avg_pct": None, "samples": 0}
