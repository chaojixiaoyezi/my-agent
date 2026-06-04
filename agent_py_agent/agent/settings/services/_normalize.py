"""Domain-specific normalize services for config fields."""


from __future__ import annotations

from ..defaults import default_agent_config
from ._normalize_core_fields import DaemonFieldsService, GatewayFieldsService, ModelFieldsService
from ._normalize_home_fields import HomeLayoutFieldsService
from ._normalize_identity_fields import AdapterFieldsService, UserFieldsService
from ._normalize_operational_fields import RuntimeBoolFieldsService
from ._normalize_runtime_fields import (
    SubagentAdvancedFieldsService,
    SubagentBasicFieldsService,
    ToolFieldsService,
)
from ._normalize_timeout_fields import TimeoutFieldsService

_NORMALIZE_SERVICES = (
    ModelFieldsService,
    GatewayFieldsService,
    DaemonFieldsService,
    ToolFieldsService,
    SubagentBasicFieldsService,
    AdapterFieldsService,
    HomeLayoutFieldsService,
    UserFieldsService,
    RuntimeBoolFieldsService,
    TimeoutFieldsService,
    SubagentAdvancedFieldsService,
)


class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe defaults."""
        warnings: list[str] = []
        out: dict[str, object] = dict(data)
        defaults = default_agent_config()

        for service in _NORMALIZE_SERVICES:
            out, service_warnings = service.normalize(out, defaults)
            warnings.extend(service_warnings)

        return out, warnings
