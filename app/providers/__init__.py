"""Provider adapters. Importing the package registers every adapter."""
from app.providers import aws, clouds, ai_llm  # noqa: F401  (registration side-effects)
from app.providers.base import (  # noqa: F401
    CloudProvider, get_provider_class, registered_providers, classify_service,
)

__all__ = [
    "CloudProvider", "get_provider_class", "registered_providers", "classify_service",
]
