
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditPaths:
    root: Path
    log_file: Path


def resolve_audit_paths(config: Any) -> AuditPaths:
    raw_value = getattr(config, "audit_log_path", "data/audit") or "data/audit"
    path = Path(str(raw_value)).expanduser()
    if path.suffix:
        return AuditPaths(root=path.parent, log_file=path)
    return AuditPaths(root=path, log_file=path / "audit.jsonl")
