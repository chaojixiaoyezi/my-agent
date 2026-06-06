
from __future__ import annotations

"""Refs-first recovery strategy for failed or stalled subagent runs."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....runtime_errors import runtime_error_report
from ...models import SubAgentTask
from ...protocol import build_task_address, build_task_envelope
from ...role_templates import role_template_snapshot_for_task
from ..task_attribute_reader import task_int, task_list, task_role, task_status, task_text
from .instructions import runner_instruction
from .modes import (
    CLOSED,
    LEADERSHIP_RECOVERY,
    MANUAL_REVIEW_MISSING_REFS,
    NO_PROGRESS_LIMIT_REACHED,
    RERUN_FROM_CHECKPOINT,
    RERUN_FROM_CONTINUE_PACKET,
    TAKEOVER_FROM_CHECKPOINT,
    TAKEOVER_FROM_CONTINUE_PACKET,
    action_for_recovery_mode,
    mode_uses_continue_packet,
)

_PACKET_SCHEMA_VERSION = "subagent_continue_packet.v1"
_RECOVERABLE_STATUSES = {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}
_DEAD_STATUSES = {"TIMEOUT", "CHANNEL_ERROR"}
_CLOSED_STATUSES = {"DONE", "ABANDONED", "TAKEN_OVER"}


@dataclass(frozen=True)
class SubagentRecoveryStrategyRequest:
    task: SubAgentTask
    all_tasks: list[SubAgentTask] = field(default_factory=list)
    now: float = 0.0
    packet_max_age_seconds: float = 0.0
    no_progress_attempt_limit: int = 4


@dataclass(frozen=True)
class SubagentRecoveryStrategy:
    run_id: str
    status: str
    role: str
    recommended_action: str
    recovery_mode: str
    packet_status: str
    packet_ref: str = ""
    uses_continue_packet: bool = False
    memory_scope: str = "task_local"
    recovery_refs: list[str] = field(default_factory=list)
    takeover_refs: list[str] = field(default_factory=list)
    child_run_ids: list[str] = field(default_factory=list)
    address: dict[str, object] = field(default_factory=dict)
    task_envelope: dict[str, object] = field(default_factory=dict)
    leadership_recovery: bool = False
    no_progress_fuse: bool = False
    blocked_by: list[str] = field(default_factory=list)
    runner_instruction: str = ""
    packet_load_error: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "role": self.role,
            "recommended_action": self.recommended_action,
            "recovery_mode": self.recovery_mode,
            "packet_status": self.packet_status,
            "packet_ref": self.packet_ref,
            "uses_continue_packet": self.uses_continue_packet,
            "memory_scope": self.memory_scope,
            "recovery_refs": list(self.recovery_refs),
            "takeover_refs": list(self.takeover_refs),
            "child_run_ids": list(self.child_run_ids),
            "address": dict(self.address),
            "task_envelope": dict(self.task_envelope),
            "leadership_recovery": self.leadership_recovery,
            "no_progress_fuse": self.no_progress_fuse,
            "blocked_by": list(self.blocked_by),
            "runner_instruction": self.runner_instruction,
            "packet_load_error": dict(self.packet_load_error),
        }


@dataclass(frozen=True)
class _PacketState:
    status: str
    ref: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    blocked_by: list[str] = field(default_factory=list)
    load_error: dict[str, object] = field(default_factory=dict)


def build_subagent_recovery_strategy(request: SubagentRecoveryStrategyRequest) -> SubagentRecoveryStrategy:
    task = request.task
    packet = _read_packet_state(request)
    recovery_refs = _recovery_refs(task)
    no_progress_fuse = _no_progress_fuse(task, request.no_progress_attempt_limit)
    recovery_mode = _recovery_mode(task, packet, recovery_refs, no_progress_fuse)
    action = action_for_recovery_mode(recovery_mode)
    return SubagentRecoveryStrategy(
        run_id=task_text(task, "id"),
        status=task_status(task),
        role=task_role(task),
        recommended_action=action,
        recovery_mode=recovery_mode,
        packet_status=packet.status,
        packet_ref=packet.ref,
        uses_continue_packet=packet.status == "ready" and mode_uses_continue_packet(recovery_mode),
        recovery_refs=recovery_refs,
        takeover_refs=_takeover_refs(task),
        child_run_ids=task_list(task, "child_ids"),
        address=build_task_address(task, all_tasks=request.all_tasks).to_dict(),
        task_envelope=build_task_envelope(task, all_tasks=request.all_tasks).to_dict(),
        leadership_recovery=recovery_mode == LEADERSHIP_RECOVERY,
        no_progress_fuse=no_progress_fuse,
        blocked_by=packet.blocked_by,
        runner_instruction=runner_instruction(task, packet, recovery_refs, recovery_mode),
        packet_load_error=packet.load_error,
    )


def _read_packet_state(request: SubagentRecoveryStrategyRequest) -> _PacketState:
    ref = _packet_ref(request.task)
    if not ref:
        return _PacketState(status="missing")
    path = Path(ref)
    if not path.exists():
        return _PacketState(status="missing", ref=str(path))
    if _packet_is_stale(path, request):
        return _PacketState(status="stale", ref=str(path), blocked_by=["packet_stale"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _PacketState(
            status="corrupt",
            ref=str(path),
            blocked_by=["packet_corrupt"],
            load_error=_packet_load_error(path, exc),
        )
    return _validated_packet_state(request.task, path, payload)


def _validated_packet_state(task: SubAgentTask, path: Path, payload: object) -> _PacketState:
    if not isinstance(payload, dict):
        exc = ValueError(f"JSON root is {type(payload).__name__}, expected object")
        return _PacketState(
            status="corrupt",
            ref=str(path),
            blocked_by=["packet_not_object"],
            load_error=_packet_load_error(path, exc),
        )
    blockers = _packet_blockers(task, payload)
    if blockers:
        return _PacketState(status="invalid", ref=str(path), payload=payload, blocked_by=blockers)
    if not bool(payload.get("ready_to_continue", False)):
        return _PacketState(status="closed", ref=str(path), payload=payload)
    return _PacketState(status="ready", ref=str(path), payload=payload)


def _packet_load_error(path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="subagent_recovery_strategy.continue_packet")
    report["path"] = str(path)
    return report


def _packet_blockers(task: SubAgentTask, payload: dict[str, Any]) -> list[str]:
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    blockers: list[str] = []
    if payload.get("schema_version") != _PACKET_SCHEMA_VERSION:
        blockers.append("schema_mismatch")
    task_id = task_text(task, "id")
    if str(payload.get("run_id") or "") != task_id:
        blockers.append("run_id_mismatch")
    if str(owner.get("owner_id") or "") != task_id:
        blockers.append("owner_mismatch")
    if payload.get("memory_scope") != "task_local":
        blockers.append("memory_scope_not_task_local")
    if bool(payload.get("writes_main_memory", True)):
        blockers.append("writes_main_memory")
    return blockers


def _recovery_mode(
    task: SubAgentTask,
    packet: _PacketState,
    recovery_refs: list[str],
    no_progress_fuse: bool,
) -> str:
    if no_progress_fuse:
        return NO_PROGRESS_LIMIT_REACHED
    if _needs_leadership_recovery(task):
        return LEADERSHIP_RECOVERY
    if _needs_takeover(task):
        return _takeover_mode(packet)
    if packet.status == "ready" and _is_recoverable(task):
        return RERUN_FROM_CONTINUE_PACKET
    if recovery_refs and _is_recoverable(task):
        return RERUN_FROM_CHECKPOINT
    if _is_closed(task):
        return CLOSED
    return MANUAL_REVIEW_MISSING_REFS


def _packet_ref(task: SubAgentTask) -> str:
    explicit = task_text(task, "agent_run_latest_session_continue_packet_json")
    if explicit:
        return explicit
    compactions_dir = task_text(task, "agent_run_compactions_dir")
    if not compactions_dir:
        return ""
    return str(Path(compactions_dir) / "session" / "latest_continue_packet.json")


def _packet_is_stale(path: Path, request: SubagentRecoveryStrategyRequest) -> bool:
    if request.packet_max_age_seconds <= 0:
        return False
    now = request.now or time.time()
    try:
        return path.stat().st_mtime < now - request.packet_max_age_seconds
    except OSError:
        return False


def _recovery_refs(task: SubAgentTask) -> list[str]:
    values = [
        task_text(task, "agent_run_checkpoint_json"),
        task_text(task, "agent_run_summary_md"),
        task_text(task, "agent_run_task_md"),
        task_text(task, "failure_handoff_json"),
        task_text(task, "takeover_readiness_json"),
        task_text(task, "output_json"),
        task_text(task, "runner_result_json"),
    ]
    return _existing_refs(values)


def _takeover_refs(task: SubAgentTask) -> list[str]:
    return _existing_refs(
        [
            task_text(task, "task_dir"),
            task_text(task, "agent_run_workspace_dir"),
            task_text(task, "agent_run_artifacts_dir"),
            task_text(task, "task_workspace_artifacts_dir"),
            task_text(task, "task_workspace_shared_dir"),
        ]
    )


def _existing_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and Path(text).exists() and text not in refs:
            refs.append(text)
    return refs


def _needs_leadership_recovery(task: SubAgentTask) -> bool:
    return (
        bool(task_list(task, "child_ids"))
        and bool(role_template_snapshot_for_task(task).get("can_spawn_children"))
        and _is_dead(task)
    )


def _needs_takeover(task: SubAgentTask) -> bool:
    return _is_dead(task) and not _needs_leadership_recovery(task)


def _is_dead(task: SubAgentTask) -> bool:
    status = task_status(task)
    failure_type = task_text(task, "failure_type").lower()
    return status in _DEAD_STATUSES or failure_type in {"runner_timeout", "channel_error", "runner_channel_failed"}


def _is_recoverable(task: SubAgentTask) -> bool:
    return task_status(task) in _RECOVERABLE_STATUSES


def _is_closed(task: SubAgentTask) -> bool:
    return task_status(task) in _CLOSED_STATUSES


def _no_progress_fuse(task: SubAgentTask, attempt_limit: int) -> bool:
    if attempt_limit <= 0:
        return False
    return task_int(task, "runner_attempts") >= attempt_limit and _is_recoverable(task)


def _takeover_mode(packet: _PacketState) -> str:
    if packet.status == "ready":
        return TAKEOVER_FROM_CONTINUE_PACKET
    return TAKEOVER_FROM_CHECKPOINT


__all__ = [
    "SubagentRecoveryStrategy",
    "SubagentRecoveryStrategyRequest",
    "build_subagent_recovery_strategy",
]
