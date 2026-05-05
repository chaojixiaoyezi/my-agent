"""Shared dispatch parameter dataclasses for dispatch_mixin."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DispatchParams:
    """Bundle of all dispatch_subagents parameters into a single object."""

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None


@dataclass
class WatchParams:
    """Bundle of all dispatch_subagents parameters into a single object."""

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    interval: float = 30.0
    max_cycles: int = 0
    force_lock: bool = False
    stop_file: str | Path | None = None


@dataclass
class DispatchContext:
    """Internal context bundle for dispatch helpers."""

    cfg: Any
    normalized_workflow_mode: str
    apply: bool
    planner: bool
    runner_instruction: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    take_over_by: str
    locked_files: list[str] | None
    router: Any  # CapabilityRouter – forward ref to avoid circular import at module level
    records: list = field(default_factory=list)


@dataclass
class RunnerBatchContext:
    """Bundle of parameters for _run_*_batch helper methods."""

    pending_runner_jobs: list
    runner_concurrency: int
    runner_timeout_seconds: float
    effective_runner_instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    records: list