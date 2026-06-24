"""Cosmos DB async service layer with structured error handling and retries."""
from __future__ import annotations
import asyncio
from typing import Any, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.exceptions import CosmosError, NotFoundError
from app.logging_config import get_logger

log = get_logger(__name__)

# ── Lazy import Azure SDK to avoid import errors when mocking in tests ──────
def _get_cosmos_client():
    try:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import ManagedIdentityCredential
        settings = get_settings()
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        return CosmosClient(url=settings.cosmos_endpoint, credential=credential)
    except ImportError as e:
        raise CosmosError(f"Azure SDK not available: {e}")


_client: Any = None
_database: Any = None
_containers: dict[str, Any] = {}


async def get_container(name: str) -> Any:
    """Return (and cache) a Cosmos container client."""
    global _client, _database
    if name not in _containers:
        if _database is None:
            if _client is None:
                _client = _get_cosmos_client()
            settings = get_settings()
            _database = _client.get_database_client(settings.cosmos_database)
        _containers[name] = _database.get_container_client(name)
    return _containers[name]


async def close() -> None:
    """Close the Cosmos client — call on app shutdown."""
    global _client, _database, _containers
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
    _client = None
    _database = None
    _containers = {}


# ── CRUD helpers ────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(CosmosError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def upsert_item(container_name: str, item: dict) -> dict:
    """Upsert a document. Raises CosmosError on failure."""
    try:
        container = await get_container(container_name)
        result = await container.upsert_item(body=item)
        log.debug("cosmos.upsert_ok", container=container_name, id=item.get("id"))
        return result
    except CosmosError:
        raise
    except Exception as exc:
        log.error("cosmos.upsert_failed", container=container_name, error=str(exc))
        raise CosmosError(f"Failed to upsert item in {container_name}", detail=str(exc)) from exc


@retry(
    retry=retry_if_exception_type(CosmosError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def get_item(container_name: str, item_id: str, partition_key: str) -> dict:
    """Fetch a single document by id + partition key. Raises NotFoundError if absent."""
    try:
        container = await get_container(container_name)
        result = await container.read_item(item=item_id, partition_key=partition_key)
        return dict(result)
    except Exception as exc:
        err_str = str(exc).lower()
        if "404" in err_str or "notfound" in err_str or "resource not found" in err_str:
            raise NotFoundError(f"Item {item_id} not found in {container_name}")
        log.error("cosmos.get_failed", container=container_name, id=item_id, error=str(exc))
        raise CosmosError(f"Failed to read item {item_id} from {container_name}", detail=str(exc)) from exc


async def delete_item(container_name: str, item_id: str, partition_key: str) -> None:
    """Delete a document. Raises NotFoundError if not present."""
    try:
        container = await get_container(container_name)
        await container.delete_item(item=item_id, partition_key=partition_key)
        log.debug("cosmos.delete_ok", container=container_name, id=item_id)
    except Exception as exc:
        err_str = str(exc).lower()
        if "404" in err_str or "notfound" in err_str:
            raise NotFoundError(f"Item {item_id} not found in {container_name}")
        raise CosmosError(f"Failed to delete item {item_id}", detail=str(exc)) from exc


async def query_items(
    container_name: str,
    query: str,
    parameters: Optional[list[dict]] = None,
    partition_key: Optional[str] = None,
    max_item_count: int = 100,
) -> list[dict]:
    """Execute a SQL query and return all matching documents."""
    try:
        container = await get_container(container_name)
        kwargs: dict[str, Any] = {
            "query": query,
            "max_item_count": max_item_count,
        }
        if parameters:
            kwargs["parameters"] = parameters
        if partition_key:
            kwargs["partition_key"] = partition_key

        results = []
        async for item in container.query_items(**kwargs):
            results.append(dict(item))

        log.debug("cosmos.query_ok", container=container_name, count=len(results))
        return results

    except CosmosError:
        raise
    except NotFoundError:
        raise
    except Exception as exc:
        log.error("cosmos.query_failed", container=container_name, query=query, error=str(exc))
        raise CosmosError(f"Query failed on {container_name}", detail=str(exc)) from exc


async def count_items(container_name: str, query: str, parameters: Optional[list[dict]] = None) -> int:
    """
    Return count of documents matching a query.
    `query` must be a full SELECT that returns the documents/values to be counted;
    callers should pass a COUNT query directly when possible. This helper executes
    the query and returns the length of the result set.
    """
    results = await query_items(container_name, query, parameters)
    # If the query was itself a `SELECT VALUE COUNT(1)`, unwrap the scalar.
    if len(results) == 1 and isinstance(results[0], int):
        return int(results[0])
    return len(results)


async def bulk_upsert(container_name: str, items: list[dict], batch_size: int = 50) -> int:
    """Upsert a list of documents in concurrent batches. Returns count upserted."""
    if not items:
        return 0

    total = 0
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        tasks = [upsert_item(container_name, item) for item in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            log.warning("cosmos.bulk_upsert_partial_failure", errors=len(errors), batch_size=len(batch))
        total += len(batch) - len(errors)

    log.info("cosmos.bulk_upsert_complete", container=container_name, total=total)
    return total
