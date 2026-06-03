from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...artifacts.registry import ArtifactRegistryRecord
from .._runtime_params import ToolLoopExecuteParams


@dataclass(frozen=True)
class DeliveryContractValidationRequest:
    contract: dict[str, Any]
    artifacts: list[dict[str, Any]]
    workspace_root: Path
    params: ToolLoopExecuteParams


@dataclass(frozen=True)
class ArtifactPathFailureRequest:
    item: dict[str, Any]
    raw_path: str
    code: str
    workspace_root: Path | None = None
    locator_findings: list[dict[str, object]] | None = None
    registry_read_errors: list[dict[str, object]] | None = None


@dataclass(frozen=True)
class ArtifactValidationReportRequest:
    item: dict[str, Any]
    path: Path
    workspace_root: Path
    registry_record: ArtifactRegistryRecord | None
    registry_read_errors: list[dict[str, object]]
    archive_tool_calls: list[Any]
    run_id: str


__all__ = [
    "ArtifactPathFailureRequest",
    "ArtifactValidationReportRequest",
    "DeliveryContractValidationRequest",
]
