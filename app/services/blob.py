"""Azure Blob Storage service — report upload + SAS URL generation."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.exceptions import StorageError
from app.logging_config import get_logger

log = get_logger(__name__)

_blob_service = None


def _get_blob_service():
    try:
        from azure.storage.blob.aio import BlobServiceClient
        from azure.identity.aio import ManagedIdentityCredential
        settings = get_settings()
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        url = f"https://{settings.storage_account_name}.blob.core.windows.net"
        return BlobServiceClient(account_url=url, credential=credential)
    except ImportError as exc:
        raise StorageError(f"Azure Blob SDK not available: {exc}")


def get_blob_service():
    global _blob_service
    if _blob_service is None:
        _blob_service = _get_blob_service()
    return _blob_service


@retry(
    retry=retry_if_exception_type(StorageError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def upload_report(
    tenant_id: str,
    report_id: str,
    content: bytes,
    content_type: str = "application/pdf",
    file_ext: str = "pdf",
) -> str:
    """
    Upload a report file to Blob Storage.
    Returns the blob path (not a SAS URL — call get_download_url for that).
    """
    settings = get_settings()
    blob_path = f"{tenant_id}/{report_id}.{file_ext}"
    try:
        service = get_blob_service()
        container = service.get_container_client(settings.storage_container_reports)
        blob_client = container.get_blob_client(blob_path)
        from azure.storage.blob import ContentSettings
        await blob_client.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            metadata={"tenant_id": tenant_id, "report_id": report_id},
        )
        log.info("blob.upload_ok", blob_path=blob_path, size_bytes=len(content))
        return blob_path
    except StorageError:
        raise
    except Exception as exc:
        log.error("blob.upload_failed", blob_path=blob_path, error=str(exc))
        raise StorageError(f"Failed to upload report {report_id}", detail=str(exc)) from exc


async def get_download_url(blob_path: str) -> str:
    """
    Generate a time-limited SAS URL for a report blob.
    Expiry controlled by settings.blob_sas_expiry_hours.

    The azure-storage-blob SAS helpers are synchronous, so the blocking work
    (fetching a user-delegation key and signing the token) is offloaded to a
    worker thread to avoid stalling the event loop.
    """
    settings = get_settings()

    def _build_sas() -> str:
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions
        from azure.storage.blob import BlobServiceClient as SyncBlobServiceClient
        from azure.identity import ManagedIdentityCredential as SyncMICredential

        credential = SyncMICredential(client_id=settings.azure_client_id)
        sync_service = SyncBlobServiceClient(
            account_url=f"https://{settings.storage_account_name}.blob.core.windows.net",
            credential=credential,
        )
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(hours=settings.blob_sas_expiry_hours)
        delegation_key = sync_service.get_user_delegation_key(
            key_start_time=now,
            key_expiry_time=expiry,
        )
        sas_token = generate_blob_sas(
            account_name=settings.storage_account_name,
            container_name=settings.storage_container_reports,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return (
            f"https://{settings.storage_account_name}.blob.core.windows.net"
            f"/{settings.storage_container_reports}/{blob_path}?{sas_token}"
        )

    try:
        url = await asyncio.to_thread(_build_sas)
        log.info("blob.sas_generated", blob_path=blob_path, expiry_hours=settings.blob_sas_expiry_hours)
        return url
    except Exception as exc:
        log.error("blob.sas_failed", blob_path=blob_path, error=str(exc))
        raise StorageError(f"Failed to generate SAS URL for {blob_path}", detail=str(exc)) from exc


async def close() -> None:
    global _blob_service
    if _blob_service is not None:
        try:
            await _blob_service.close()
        except Exception:
            pass
    _blob_service = None
