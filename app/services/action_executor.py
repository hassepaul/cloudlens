"""
CloudLens Action Executor
=========================

Translates optimization recommendations into actual control-plane operations.
Currently supports Azure VMs (deallocate / start) and Azure App Services
(stop / start) via the Azure Resource Management REST API. AWS EC2 stop/start
is stubbed and activates when the AWS ingest path is wired with live credentials.

SAFETY GATE
-----------
Execution is only permitted when ``action_execution_enabled = true`` is set in
Settings (env var ``ACTION_EXECUTION_ENABLED=true``). The default is ``false``.
Operators must explicitly opt in, and the required IAM role must be granted on
the customer subscription:

  Required Azure RBAC on the customer subscription service principal:
    Microsoft.Compute/virtualMachines/deallocate/action
    Microsoft.Compute/virtualMachines/start/action
    Microsoft.Web/sites/stop/action            (App Service)
    Microsoft.Web/sites/start/action           (App Service)

  Equivalent built-in role: "Virtual Machine Contributor" covers VMs.
  For App Service add "Website Contributor".

ARM long-running operations return HTTP 202 Accepted; the executor polls the
Location / Azure-AsyncOperation header until the operation completes or times out.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.exceptions import AzureAPIError
from app.logging_config import get_logger
from app.models.action import ActionRecord, ActionType, ActionStatus
from app.services import cosmos

log = get_logger(__name__)

COMPUTE_API_VERSION = "2024-03-01"
WEB_API_VERSION = "2023-12-01"
ARM_BASE = "https://management.azure.com"

# Maximum seconds to wait for an ARM async operation to complete
_ASYNC_OP_TIMEOUT = 300
_ASYNC_OP_POLL_INTERVAL = 5


class ActionExecutionDisabledError(Exception):
    """Raised when action_execution_enabled is False."""


# ── ARM helpers ──────────────────────────────────────────────────────────────

async def _arm_post(url: str, access_token: str, timeout: int = 60) -> Optional[str]:
    """
    POST to an ARM action endpoint.  Returns the async-operation polling URL
    (from Location or Azure-AsyncOperation header) if the response is 202,
    otherwise None for immediate 200.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Length": "0",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await client.post(url, headers=headers, content=b"")
        if resp.status_code == 200:
            return None
        if resp.status_code == 202:
            # Long-running operation — caller should poll
            return (
                resp.headers.get("Azure-AsyncOperation")
                or resp.headers.get("Location")
            )
        raise AzureAPIError(
            f"ARM action returned unexpected status {resp.status_code}",
            detail=resp.text[:500],
        )


async def _poll_async_operation(poll_url: str, access_token: str) -> None:
    """
    Poll an ARM async operation URL until it reaches a terminal state
    (Succeeded / Failed / Canceled) or the timeout elapses.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    deadline = asyncio.get_event_loop().time() + _ASYNC_OP_TIMEOUT
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(_ASYNC_OP_POLL_INTERVAL)
            resp = await client.get(poll_url, headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                state = (body.get("status") or body.get("provisioningState") or "").lower()
                if state == "succeeded":
                    return
                if state in ("failed", "canceled"):
                    error_info = body.get("error", {})
                    raise AzureAPIError(
                        f"ARM async operation {state}",
                        detail=str(error_info)[:500],
                    )
                # still running — keep polling
    raise AzureAPIError(
        f"ARM async operation did not complete within {_ASYNC_OP_TIMEOUT}s"
    )


# ── Resource-type dispatch ───────────────────────────────────────────────────

def _classify_azure_resource(resource_id: str) -> str:
    """Return a resource type string for routing to the correct action URL."""
    lower = resource_id.lower()
    if "/microsoft.compute/virtualmachines/" in lower:
        return "vm"
    if "/microsoft.web/sites/" in lower:
        return "appservice"
    return "unknown"


async def _execute_azure(record: ActionRecord, access_token: str) -> None:
    """Execute deallocate/start on an Azure VM or App Service."""
    resource_id = record.resource_id.lstrip("/")
    kind = _classify_azure_resource(resource_id)

    if kind == "vm":
        api_version = COMPUTE_API_VERSION
        if record.action_type == ActionType.AUTOSTOP:
            verb = "deallocate"
        elif record.action_type == ActionType.AUTOSTART:
            verb = "start"
        else:
            raise NotImplementedError(f"Azure VM action {record.action_type} not implemented")

    elif kind == "appservice":
        api_version = WEB_API_VERSION
        if record.action_type == ActionType.AUTOSTOP:
            verb = "stop"
        elif record.action_type == ActionType.AUTOSTART:
            verb = "start"
        else:
            raise NotImplementedError(f"App Service action {record.action_type} not implemented")

    else:
        raise NotImplementedError(
            f"Action execution is not yet supported for resource type inferred from: {resource_id}"
        )

    url = f"{ARM_BASE}/{resource_id}/{verb}?api-version={api_version}"
    log.info(
        "action.azure.posting",
        resource_id=resource_id,
        verb=verb,
        kind=kind,
    )
    poll_url = await _arm_post(url, access_token)
    if poll_url:
        await _poll_async_operation(poll_url, access_token)


async def _execute_aws(record: ActionRecord) -> None:
    """
    Stop or start an AWS EC2 instance via boto3.
    Requires live AWS credentials stored on the tenant (secret_ref from Key Vault).

    Uncomment and complete once the AWS ingest path is fully wired:

        import boto3
        region = record.metadata.get("region", "eu-west-1")
        ec2 = boto3.client("ec2", region_name=region,
                           aws_access_key_id=creds["access_key_id"],
                           aws_secret_access_key=creds["secret_access_key"])
        if record.action_type == ActionType.AUTOSTOP:
            ec2.stop_instances(InstanceIds=[record.resource_id])
        elif record.action_type == ActionType.AUTOSTART:
            ec2.start_instances(InstanceIds=[record.resource_id])
    """
    raise NotImplementedError(
        "AWS action execution requires live boto3 credentials — stub only. "
        "Wire the AWS ingest path first."
    )


# ── Public interface ─────────────────────────────────────────────────────────

async def execute_action(
    record: ActionRecord,
    access_token: str,
    *,
    container: str,
) -> ActionRecord:
    """
    Execute a single action and persist the result to Cosmos.
    Returns the updated ActionRecord.

    The caller is responsible for ensuring the tenant's service principal
    has the required IAM permissions before submitting the action.
    """
    settings = get_settings()
    if not settings.action_execution_enabled:
        raise ActionExecutionDisabledError(
            "Action execution is disabled. "
            "Set ACTION_EXECUTION_ENABLED=true to enable."
        )

    record = record.model_copy(update={"status": ActionStatus.EXECUTING})
    await cosmos.upsert_item(container, record.to_cosmos())
    log.info(
        "action.executing",
        action_id=record.id,
        action_type=record.action_type,
        resource_id=record.resource_id,
        provider=record.provider,
    )

    try:
        if record.provider == "azure":
            await _execute_azure(record, access_token)
        elif record.provider == "aws":
            await _execute_aws(record)
        else:
            raise NotImplementedError(
                f"Action execution not yet supported for provider: {record.provider}"
            )

        record = record.model_copy(update={
            "status": ActionStatus.SUCCEEDED,
            "completed_at": datetime.now(timezone.utc),
        })
        log.info(
            "action.succeeded",
            action_id=record.id,
            action_type=record.action_type,
            resource_id=record.resource_id,
        )
    except (ActionExecutionDisabledError, NotImplementedError):
        raise
    except Exception as exc:
        record = record.model_copy(update={
            "status": ActionStatus.FAILED,
            "completed_at": datetime.now(timezone.utc),
            "error": str(exc)[:500],
        })
        log.error(
            "action.failed",
            action_id=record.id,
            action_type=record.action_type,
            resource_id=record.resource_id,
            error=str(exc),
        )

    await cosmos.upsert_item(container, record.to_cosmos())
    return record


async def execute_bulk(
    records: list[ActionRecord],
    access_token: str,
    *,
    container: str,
    max_concurrency: int = 5,
) -> list[ActionRecord]:
    """
    Execute multiple actions with bounded concurrency.
    Returns all updated records (both succeeded and failed).
    """
    sem = asyncio.Semaphore(max_concurrency)

    async def _bounded(r: ActionRecord) -> ActionRecord:
        async with sem:
            return await execute_action(r, access_token, container=container)

    return list(await asyncio.gather(*[_bounded(r) for r in records], return_exceptions=False))
