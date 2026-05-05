"""Dataclasses for effective memory config and fallback warnings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MemorySettings:
    """Effective memory config after validation and fallback normalization."""
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_hook_archive_level: int = 3
    memory_hook_retention_days: int = 7
    memory_rule_routing_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_rule_receipt_enabled: bool = True
    memory_resume_auto_context_enabled: bool = False
    memory_resume_auto_context_mode: str = "trigger"
    memory_resume_auto_context_limit: int = 5


@dataclass(frozen=True)
class MemoryConfigWarning:
    """Structured warning emitted when a memory config value falls back to default."""
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable warning payload for doctor/log output."""
        return asdict(self)
