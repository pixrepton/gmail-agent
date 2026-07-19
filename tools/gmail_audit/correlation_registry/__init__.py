"""P0 Identity + Engagement correlation registry (Node B)."""

from correlation_registry.link_types import LINK_TYPES_P0
from correlation_registry.service import CorrelationRegistryService, build_correlation_registry_service

__all__ = [
    "LINK_TYPES_P0",
    "CorrelationRegistryService",
    "build_correlation_registry_service",
]
