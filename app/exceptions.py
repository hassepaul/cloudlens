"""CloudLens exception hierarchy — all domain errors derive from CloudLensError."""
from __future__ import annotations
from typing import Optional


class CloudLensError(Exception):
    """Base class for all CloudLens application errors."""
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "error": self.error_code,
            "message": self.message,
            "detail": self.detail,
        }


# ── 4xx Client errors ──────────────────────────────────────────────────────

class NotFoundError(CloudLensError):
    status_code = 404
    error_code = "NOT_FOUND"


class ValidationError(CloudLensError):
    status_code = 422
    error_code = "VALIDATION_ERROR"


class ConflictError(CloudLensError):
    status_code = 409
    error_code = "CONFLICT"


class UnauthorizedError(CloudLensError):
    status_code = 401
    error_code = "UNAUTHORIZED"


class ForbiddenError(CloudLensError):
    status_code = 403
    error_code = "FORBIDDEN"


class RateLimitError(CloudLensError):
    status_code = 429
    error_code = "RATE_LIMITED"


# ── 5xx Server / external errors ──────────────────────────────────────────

class AzureAPIError(CloudLensError):
    """Raised when Azure Cost Management or Advisor API returns an error."""
    status_code = 502
    error_code = "AZURE_API_ERROR"


class CosmosError(CloudLensError):
    """Cosmos DB operation failure."""
    status_code = 503
    error_code = "COSMOS_ERROR"


class StorageError(CloudLensError):
    """Azure Blob Storage operation failure."""
    status_code = 503
    error_code = "STORAGE_ERROR"


class ServiceBusError(CloudLensError):
    """Service Bus send/receive failure."""
    status_code = 503
    error_code = "SERVICE_BUS_ERROR"


class KeyVaultError(CloudLensError):
    """Key Vault secret retrieval failure."""
    status_code = 503
    error_code = "KEY_VAULT_ERROR"


class IngestError(CloudLensError):
    """Ingestion job failure — typically wraps AzureAPIError or CosmosError."""
    status_code = 500
    error_code = "INGEST_ERROR"


class ReportGenerationError(CloudLensError):
    """Report generation or upload failure."""
    status_code = 500
    error_code = "REPORT_ERROR"
