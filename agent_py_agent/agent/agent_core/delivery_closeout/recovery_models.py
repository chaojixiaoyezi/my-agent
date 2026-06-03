
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RecoveryActionLedger:
    actions: list[dict[str, object]]
    seen: set[str]


@dataclass(frozen=True)
class StagingActionContext:
    ledger: RecoveryActionLedger
    staging: dict[str, Any]
    artifact_exists: bool
    builder_tool: str
    source_ref: str
    output_ref: str
    source_path: Path | None
    required_columns: list[str]
    required_sheets_min: int
    source_shape_hint: str
    workspace_root: Path


@dataclass(frozen=True)
class CheckpointQualityActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    checkpoint_path: Path
    required_columns: list[str] | None = None
    required_sheets_min: int = 0
    checkpoint_shape_hint: str = ""
    validation_contract: dict[str, object] | None = None


@dataclass(frozen=True)
class StagedEvidenceActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    workspace_root: Path
    validation_contract: dict[str, object]


__all__ = [
    "CheckpointQualityActionRequest",
    "RecoveryActionLedger",
    "StagedEvidenceActionRequest",
    "StagingActionContext",
]
