"""Azure Key Vault service — async secret read/write with retry."""
from __future__ import annotations
import json

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.exceptions import KeyVaultError
from app.logging_config import get_logger

log = get_logger(__name__)


def _get_kv_client():
    try:
        from azure.keyvault.secrets.aio import SecretClient
        from azure.identity.aio import ManagedIdentityCredential
        settings = get_settings()
        credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
        return SecretClient(vault_url=settings.key_vault_uri, credential=credential)
    except ImportError as exc:
        raise KeyVaultError(f"Azure Key Vault SDK not available: {exc}")


_kv_client = None


def get_kv_client():
    global _kv_client
    if _kv_client is None:
        _kv_client = _get_kv_client()
    return _kv_client


@retry(
    retry=retry_if_exception_type(KeyVaultError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    reraise=True,
)
async def get_secret(secret_name: str) -> str:
    """Retrieve a secret value from Key Vault."""
    try:
        client = get_kv_client()
        secret = await client.get_secret(secret_name)
        if secret.value is None:
            raise KeyVaultError(f"Secret '{secret_name}' has no value")
        log.debug("keyvault.secret_retrieved", secret_name=secret_name)
        return secret.value
    except KeyVaultError:
        raise
    except Exception as exc:
        log.error("keyvault.get_secret_failed", secret_name=secret_name, error=str(exc))
        raise KeyVaultError(f"Failed to retrieve secret '{secret_name}'", detail=str(exc)) from exc


async def set_secret(secret_name: str, value: str) -> None:
    """Store or update a secret in Key Vault."""
    try:
        client = get_kv_client()
        await client.set_secret(secret_name, value)
        log.info("keyvault.secret_set", secret_name=secret_name)
    except Exception as exc:
        log.error("keyvault.set_secret_failed", secret_name=secret_name, error=str(exc))
        raise KeyVaultError(f"Failed to set secret '{secret_name}'", detail=str(exc)) from exc


async def get_sp_credentials(tenant_id: str) -> dict:
    """
    Retrieve service principal credentials for a CloudLens tenant.
    Secret format: JSON with keys client_id, client_secret, azure_tenant_id
    """
    secret_name = f"sp-creds-{tenant_id}"
    raw = await get_secret(secret_name)
    try:
        creds = json.loads(raw)
        required = {"client_id", "client_secret", "azure_tenant_id"}
        missing = required - creds.keys()
        if missing:
            raise KeyVaultError(f"SP credentials missing fields: {missing}")
        return creds
    except json.JSONDecodeError as exc:
        raise KeyVaultError(f"SP credentials for {tenant_id} are not valid JSON", detail=str(exc)) from exc


async def store_sp_credentials(
    tenant_id: str, client_id: str, client_secret: str, azure_tenant_id: str
) -> str:
    """Store SP credentials and return the secret name."""
    secret_name = f"sp-creds-{tenant_id}"
    value = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "azure_tenant_id": azure_tenant_id,
    })
    await set_secret(secret_name, value)
    return secret_name


async def close() -> None:
    global _kv_client
    if _kv_client is not None:
        try:
            await _kv_client.close()
        except Exception:
            pass
    _kv_client = None
