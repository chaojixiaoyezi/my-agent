"""Domain-specific normalize services for config fields."""

from __future__ import annotations

from ._normalize_core_fields import DaemonFieldsService, GatewayFieldsService, ModelFieldsService
from ._normalize_runtime_fields import (
    AdapterFieldsService,
    SubagentAdvancedFieldsService,
    SubagentBasicFieldsService,
    TimeoutFieldsService,
    ToolFieldsService,
    UserFieldsService,
)

_NORMALIZE_SERVICES = (
    ModelFieldsService,
    GatewayFieldsService,
    DaemonFieldsService,
    ToolFieldsService,
    SubagentBasicFieldsService,
    AdapterFieldsService,
    UserFieldsService,
    TimeoutFieldsService,
    SubagentAdvancedFieldsService,
)


class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe fallbacks."""
        from ..config import AgentConfig

        warnings: list[str] = []
        out: dict[str, object] = dict(data)
        defaults = AgentConfig()

        for service in _NORMALIZE_SERVICES:
            out, service_warnings = service.normalize(out, defaults)
            warnings.extend(service_warnings)

        return out, warnings
