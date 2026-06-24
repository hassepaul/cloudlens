"""Azure Service Bus — async producer and consumer for cost-ingest queue."""
from __future__ import annotations
import json
from typing import Callable, Awaitable
from uuid import uuid4

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.exceptions import ServiceBusError
from app.logging_config import get_logger

log = get_logger(__name__)

_sender = None
_receiver = None


def _get_service_bus_client():
    try:
        from azure.servicebus.aio import ServiceBusClient
        from azure.identity.aio import ManagedIdentityCredential
        settings = get_settings()
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        return ServiceBusClient(
            fully_qualified_namespace=settings.service_bus_namespace,
            credential=credential,
        )
    except ImportError as exc:
        raise ServiceBusError(f"Azure Service Bus SDK not available: {exc}")


@retry(
    retry=retry_if_exception_type(ServiceBusError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def send_ingest_message(tenant_id: str, subscription_id: str, extra: dict | None = None) -> str:
    """Enqueue a cost-ingest message for a specific tenant subscription."""
    settings = get_settings()
    message_id = str(uuid4())
    payload = {
        "message_id": message_id,
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        **(extra or {}),
    }
    try:
        client = _get_service_bus_client()
        async with client:
            sender = client.get_queue_sender(queue_name=settings.service_bus_queue_ingest)
            async with sender:
                from azure.servicebus import ServiceBusMessage
                msg = ServiceBusMessage(
                    body=json.dumps(payload),
                    message_id=message_id,
                    subject="cost-ingest",
                    application_properties={"tenant_id": tenant_id},
                )
                await sender.send_messages(msg)
        log.info("bus.message_sent", queue=settings.service_bus_queue_ingest,
                 tenant_id=tenant_id, message_id=message_id)
        return message_id
    except ServiceBusError:
        raise
    except Exception as exc:
        log.error("bus.send_failed", tenant_id=tenant_id, error=str(exc))
        raise ServiceBusError(f"Failed to enqueue ingest message for {tenant_id}", detail=str(exc)) from exc


async def process_queue(
    handler: Callable[[dict], Awaitable[None]],
    max_messages: int | None = None,
) -> None:
    """
    Consume messages from the ingest queue and call handler for each.
    handler receives the parsed JSON payload dict.
    Raises ServiceBusError on connection failure.
    """
    settings = get_settings()
    processed = 0
    try:
        client = _get_service_bus_client()
        async with client:
            receiver = client.get_queue_receiver(
                queue_name=settings.service_bus_queue_ingest,
                max_wait_time=5,
            )
            async with receiver:
                async for msg in receiver:
                    try:
                        raw = b"".join(msg.body) if hasattr(msg.body, "__iter__") else msg.body
                        payload = json.loads(raw)
                        log.info("bus.message_received", message_id=str(msg.message_id),
                                 tenant_id=payload.get("tenant_id"))
                        await handler(payload)
                        await receiver.complete_message(msg)
                        processed += 1
                        if max_messages and processed >= max_messages:
                            break
                    except Exception as exc:
                        log.error("bus.message_handler_failed", error=str(exc))
                        await receiver.abandon_message(msg)
    except ServiceBusError:
        raise
    except Exception as exc:
        log.error("bus.receive_failed", error=str(exc))
        raise ServiceBusError("Failed to process Service Bus queue", detail=str(exc)) from exc

    log.info("bus.queue_processed", count=processed)
