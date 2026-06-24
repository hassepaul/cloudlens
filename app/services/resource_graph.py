"""
Azure Resource Graph collector.

Populates the live-state context the waste engine needs (disk attach state,
public-IP associations, snapshot ages, load-balancer backends, cert expiry,
storage access tiers) using Azure Resource Graph.

Why Resource Graph: it answers "what does every resource of type X look like
right now" in ONE KQL query per type across the whole subscription, instead of
N individual ARM GET calls. That keeps the nightly job fast and the API-call
count (and therefore cost) low.

All queries are read-only and run with the tenant's existing service principal
token — no extra permissions beyond Reader are required.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.exceptions import AzureAPIError
from app.logging_config import get_logger

log = get_logger(__name__)

ARM_BASE = "https://management.azure.com"
RESOURCE_GRAPH_API = "2022-10-01"


class ResourceGraphCollector:
    """Runs Resource Graph KQL queries for one subscription."""

    def __init__(self, subscription_id: str, access_token: str, timeout_seconds: int = 30) -> None:
        self._subscription_id = subscription_id
        self._token = access_token
        self._timeout = httpx.Timeout(timeout_seconds)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @retry(
        retry=retry_if_exception_type(AzureAPIError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _run_query(self, query: str) -> list[dict]:
        """Execute a single Resource Graph KQL query, paging through results."""
        url = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources?api-version={RESOURCE_GRAPH_API}"
        all_rows: list[dict] = []
        skip_token: str | None = None

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                options: dict[str, Any] = {"resultFormat": "objectArray"}
                if skip_token:
                    options["$skipToken"] = skip_token
                payload = {
                    "subscriptions": [self._subscription_id],
                    "query": query,
                    "options": options,
                }
                try:
                    resp = await client.post(url, json=payload, headers=self._headers)
                    if resp.status_code == 429:
                        raise AzureAPIError("Resource Graph rate limited")
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise AzureAPIError(
                        f"Resource Graph error {exc.response.status_code}",
                        detail=exc.response.text[:300],
                    ) from exc
                except AzureAPIError:
                    raise
                except Exception as exc:
                    raise AzureAPIError(f"Resource Graph unexpected error: {exc}") from exc

                body = resp.json()
                all_rows.extend(body.get("data", []))
                skip_token = body.get("$skipToken")
                if not skip_token:
                    break

        return all_rows

    # ── Individual collectors ────────────────────────────────────────────

    async def disk_states(self) -> dict[str, str]:
        """resource_id -> 'Attached' | 'Unattached'"""
        q = (
            "resources "
            "| where type =~ 'microsoft.compute/disks' "
            "| project id = tolower(id), state = tostring(properties.diskState)"
        )
        rows = await self._run_query(q)
        return {
            r["id"]: ("Unattached" if (r.get("state") or "").lower() == "unattached" else "Attached")
            for r in rows if r.get("id")
        }

    async def ip_associations(self) -> dict[str, bool]:
        """resource_id -> is_associated (False means orphan)"""
        q = (
            "resources "
            "| where type =~ 'microsoft.network/publicipaddresses' "
            "| project id = tolower(id), assoc = isnotempty(properties.ipConfiguration)"
        )
        rows = await self._run_query(q)
        return {r["id"]: bool(r.get("assoc")) for r in rows if r.get("id")}

    async def resource_tags(self) -> dict[str, dict]:
        """
        resource_id -> {tag_key: tag_value}

        One bulk query returns tags for every tagged resource in the
        subscription, so cost records can be enriched without an extra ARM GET
        per resource. Untagged resources are simply absent from the map.
        """
        q = (
            "resources "
            "| where isnotempty(tags) "
            "| project id = tolower(id), tags"
        )
        rows = await self._run_query(q)
        return {r["id"]: (r.get("tags") or {}) for r in rows if r.get("id")}

    async def snapshot_ages(self) -> dict[str, int]:
        """resource_id -> age in days"""
        q = (
            "resources "
            "| where type =~ 'microsoft.compute/snapshots' "
            "| project id = tolower(id), created = todatetime(properties.timeCreated)"
        )
        rows = await self._run_query(q)
        now = datetime.now(timezone.utc)
        result: dict[str, int] = {}
        for r in rows:
            rid = r.get("id")
            created = r.get("created")
            if not rid or not created:
                continue
            try:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                result[rid] = max(0, (now - created_dt).days)
            except (ValueError, TypeError):
                continue
        return result

    async def lb_backend_counts(self) -> dict[str, int]:
        """resource_id -> total backend instances across pools"""
        q = (
            "resources "
            "| where type =~ 'microsoft.network/loadbalancers' "
            "| mv-expand pool = properties.backendAddressPools "
            "| extend backends = array_length(pool.properties.backendIPConfigurations) "
            "| summarize total = sum(backends) by id = tolower(id)"
        )
        rows = await self._run_query(q)
        return {r["id"]: int(r.get("total") or 0) for r in rows if r.get("id")}

    async def storage_access_tiers(self) -> dict[str, str]:
        """resource_id -> access tier ('Hot' | 'Cool' | 'Archive')"""
        q = (
            "resources "
            "| where type =~ 'microsoft.storage/storageaccounts' "
            "| project id = tolower(id), tier = tostring(properties.accessTier)"
        )
        rows = await self._run_query(q)
        return {r["id"]: (r.get("tier") or "Hot") for r in rows if r.get("id")}

    async def cert_expiries(self) -> dict[str, int]:
        """resource_id -> days until expiry (Key Vault certificates)"""
        q = (
            "resources "
            "| where type =~ 'microsoft.keyvault/vaults/certificates' "
            "| project id = tolower(id), expires = todatetime(properties.attributes.exp)"
        )
        try:
            rows = await self._run_query(q)
        except AzureAPIError:
            return {}
        now = datetime.now(timezone.utc)
        result: dict[str, int] = {}
        for r in rows:
            rid = r.get("id")
            expires = r.get("expires")
            if not rid or not expires:
                continue
            try:
                exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                result[rid] = (exp_dt - now).days
            except (ValueError, TypeError):
                continue
        return result

    async def vm_power_states(self) -> dict[str, str]:
        """resource_id -> power state ('running' | 'deallocated' | ...)"""
        q = (
            "resources "
            "| where type =~ 'microsoft.compute/virtualmachines' "
            "| extend power = tostring(properties.extended.instanceView.powerState.code) "
            "| project id = tolower(id), power"
        )
        rows = await self._run_query(q)
        out: dict[str, str] = {}
        for r in rows:
            rid = r.get("id")
            power = (r.get("power") or "").lower()
            if not rid:
                continue
            out[rid] = "running" if "running" in power else "deallocated"
        return out

    async def collect_all(self) -> dict[str, Any]:
        """Run every collector and return the full waste-engine context fragment."""
        import asyncio
        log.info("resource_graph.collect_start", subscription_id=self._subscription_id)
        (
            disks, ips, snaps, lbs, tiers, certs, power, tags,
        ) = await asyncio.gather(
            self.disk_states(),
            self.ip_associations(),
            self.snapshot_ages(),
            self.lb_backend_counts(),
            self.storage_access_tiers(),
            self.cert_expiries(),
            self.vm_power_states(),
            self.resource_tags(),
            return_exceptions=True,
        )

        def _safe(value: Any, label: str) -> Any:
            if isinstance(value, Exception):
                log.warning("resource_graph.collector_failed", collector=label, error=str(value))
                return {}
            return value

        context = {
            "disk_states": _safe(disks, "disk_states"),
            "ip_associations": _safe(ips, "ip_associations"),
            "snapshot_ages": _safe(snaps, "snapshot_ages"),
            "lb_backend_counts": _safe(lbs, "lb_backend_counts"),
            "storage_access_tiers": _safe(tiers, "storage_access_tiers"),
            "cert_expiries": _safe(certs, "cert_expiries"),
            "vm_power_states": _safe(power, "vm_power_states"),
            "resource_tags": _safe(tags, "resource_tags"),
        }
        log.info(
            "resource_graph.collect_done",
            subscription_id=self._subscription_id,
            disks=len(context["disk_states"]),
            ips=len(context["ip_associations"]),
            snapshots=len(context["snapshot_ages"]),
        )
        return context
