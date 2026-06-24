"""Tenants CRUD router — /api/v1/tenants"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status, Depends

from app.auth import require_api_key
from app.config import get_settings
from app.exceptions import NotFoundError, ConflictError, CosmosError, KeyVaultError
from app.logging_config import get_logger
from app.models.tenant import TenantConfig, TenantCreate, TenantUpdate
from app.services import cosmos, keyvault

log = get_logger(__name__)
# Tenant management is admin-only — protected by the internal API key.
router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"], dependencies=[Depends(require_api_key)])


def _container() -> str:
    return get_settings().cosmos_container_tenants


@router.get("/", response_model=list[TenantConfig])
async def list_tenants() -> list[TenantConfig]:
    """List all tenant configurations."""
    try:
        docs = await cosmos.query_items(
            _container(),
            "SELECT * FROM c WHERE c.type = 'tenant' ORDER BY c.tenant_name",
        )
        return [TenantConfig.from_cosmos(d) for d in docs]
    except CosmosError as exc:
        log.error("tenants.list_failed", error=str(exc))
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.post("/", response_model=TenantConfig, status_code=status.HTTP_201_CREATED)
async def create_tenant(payload: TenantCreate) -> TenantConfig:
    """
    Create a new tenant configuration.
    SP credentials are stored in Key Vault; only the secret reference is persisted.
    """
    try:
        # Check for duplicate name
        existing = await cosmos.query_items(
            _container(),
            "SELECT c.id FROM c WHERE c.tenant_name = @name",
            parameters=[{"name": "@name", "value": payload.tenant_name}],
        )
        if existing:
            raise ConflictError(f"Tenant '{payload.tenant_name}' already exists")

        # Generate the tenant ID up front so the Key Vault secret name and the
        # Cosmos document id are guaranteed to match.
        tenant_id = str(uuid4())
        secret_ref = await keyvault.store_sp_credentials(
            tenant_id=tenant_id,
            client_id=payload.sp_client_id,
            client_secret=payload.sp_client_secret,
            azure_tenant_id=payload.sp_tenant_id,
        )

        # Build and persist TenantConfig
        config = TenantConfig(
            id=tenant_id,
            tenant_name=payload.tenant_name,
            subscription_ids=payload.subscription_ids,
            plan_tier=payload.plan_tier,
            alert_email=payload.alert_email,
            active=payload.active,
            sp_secret_ref=secret_ref,
        )
        await cosmos.upsert_item(_container(), config.to_cosmos())
        log.info("tenants.created", tenant_id=config.id, name=config.tenant_name)
        return config

    except (ConflictError, NotFoundError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except KeyVaultError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.get("/{tenant_id}", response_model=TenantConfig)
async def get_tenant(tenant_id: str) -> TenantConfig:
    try:
        doc = await cosmos.get_item(_container(), tenant_id, tenant_id)
        return TenantConfig.from_cosmos(doc)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.patch("/{tenant_id}", response_model=TenantConfig)
async def update_tenant(tenant_id: str, payload: TenantUpdate) -> TenantConfig:
    try:
        doc = await cosmos.get_item(_container(), tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
        update_data = payload.model_dump(exclude_none=True)
        updated = config.model_copy(update={**update_data, "updated_at": datetime.now(timezone.utc)})
        await cosmos.upsert_item(_container(), updated.to_cosmos())
        log.info("tenants.updated", tenant_id=tenant_id, fields=list(update_data.keys()))
        return updated
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(tenant_id: str) -> None:
    """Soft-delete: sets active=False. Does not remove data."""
    try:
        doc = await cosmos.get_item(_container(), tenant_id, tenant_id)
        config = TenantConfig.from_cosmos(doc)
        updated = config.model_copy(update={"active": False, "updated_at": datetime.now(timezone.utc)})
        await cosmos.upsert_item(_container(), updated.to_cosmos())
        log.info("tenants.soft_deleted", tenant_id=tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except CosmosError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
