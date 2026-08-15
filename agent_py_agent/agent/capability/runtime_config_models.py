
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import CapabilityConfig


@dataclass(frozen=True)
class CapabilityConfigPatch:
    field: str
    value: Any
    reason: str = ""


@dataclass(frozen=True)
class CapabilityConfigPatchRequest:
    config_path: str | Path
    patches: list[CapabilityConfigPatch] = field(default_factory=list)
    apply: bool = False
    expected_version: str = ""
    actor: str = "agent"
    reason: str = ""
    audit_path: str | Path | None = None
    notice_path: str | Path | None = None
    scope: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityConfigPatchResult:
    ok: bool
    applied: bool
    version_before: str
    version_after: str
    config: CapabilityConfig
    changed_fields: list[str] = field(default_factory=list)
    suggestions: list[dict[str, object]] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    audit_path: str = ""
    notice_path: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["config"] = asdict(self.config)
        return payload


@dataclass(frozen=True)
class CapabilityConfigSnapshot:
    path: Path
    config: CapabilityConfig
    version: str
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class CapabilityConfigReloadResult:
    snapshot: CapabilityConfigSnapshot
    changed: bool
    message: str = ""
