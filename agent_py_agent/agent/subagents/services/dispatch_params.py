"""Dispatch record params dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchRecordParams:
    """Bundle of make_dispatch_record parameters."""
    step: str
    action: str
    run_id: str = ""
    dry_run: bool = True
    applied: bool = False
    ok: bool = True
    message: str = ""
    before_status: str = ""
    after_status: str = ""
    before_verification_status: str = ""
    after_verification_status: str = ""
    evidence_paths: list[str] | None = None


@dataclass(frozen=True)
class DispatchWatchRecordParams:
    """Bundle of make_dispatch_watch_record parameters."""
    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] | None = None


@dataclass(frozen=True)
class DispatchWatchHeartbeatParams:
    """Bundle of write_dispatch_watch_heartbeat parameters."""
    cycle: int
    status: str
    lock_path: str
    pid: int
    message: str = ""


@dataclass(frozen=True)
class ParentPlannerRecordParams:
    """Bundle of make_parent_planner_record parameters."""
    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] | None = None
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] | None = None
    blockers: list[str] | None = None
    risks: list[str] | None = None
    notes: list[str] | None = None
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] | None = None
