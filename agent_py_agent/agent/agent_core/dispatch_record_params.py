from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowRecordParams:
    agent: Any
    tasks: list
    workflow_mode: str
    limit: int
    apply: bool


@dataclass(frozen=True)
class DryRunWorkflowRecordParams:
    agent: Any
    task: Any
    workflow_mode: str
    preview: dict
    worker_count: int


@dataclass(frozen=True)
class ActionApplyRecordParams:
    agent: Any
    cfg: Any
    apply: bool
    take_over_by: str | None
    locked_files: list[str] | None
    limit: int


@dataclass(frozen=True)
class CapabilityRouteRecordParams:
    agent: Any
    router: Any
    cfg: Any
    apply: bool
    limit: int


@dataclass(frozen=True)
class PatchReviewRecordParams:
    agent: Any
    patch_run_ids: list[str]
    apply: bool
    reviewer: str
    note: str
    limit: int


@dataclass(frozen=True)
class AcceptanceRecordParams:
    agent: Any
    apply: bool
    reviewer: str
    note: str
    limit: int
