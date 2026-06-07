
from __future__ import annotations

"""Hierarchy recovery decisions using canonical TaskStatus facts."""

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...models import (
    SUBAGENT_FAILURE_STATUSES,
    SubAgentTask,
    TaskStatus,
    task_has_status,
    task_is_handled_after_parent_timeout,
    task_status_in,
)
from ...policies import _is_active
from ..recovery.strategy import SubagentRecoveryStrategyRequest, build_subagent_recovery_strategy

RECOVERY_STATUSES = SUBAGENT_FAILURE_STATUSES


@dataclass(frozen=True)
class HierarchyRecoveryRequest:
    root_run_id: str
    requested_by: str = "parent"
    include_healthy: bool = True
    max_nodes: int = 200
    heartbeat_timeout: float = 0.0
    run_timeout: float = 0.0
    now: float = 0.0


@dataclass(frozen=True)
class HierarchyRecoveryNode:
    run_id: str
    parent_id: str
    root_id: str
    depth: int
    role: str
    agent_name: str
    status: str
    current_step: str
    latest_summary: str
    child_ids: list[str]
    needs_recovery: bool
    recovery_reason: str
    takeover_readiness_ref: str = ""
    failure_handoff_ref: str = ""
    checkpoint_ref: str = ""
    context_bundle_ref: str = ""
    parent_context_bundle_ref: str = ""
    recommended_command: str = ""
    recovery_action: str = ""
    recovery_mode: str = ""
    continue_packet_ref: str = ""
    continue_packet_status: str = ""
    no_progress_fuse: bool = False
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HierarchyRecoveryResult:
    generated_at: float
    root_run_id: str
    requested_by: str
    node_count: int
    recovery_candidate_count: int
    omitted_healthy_count: int
    truncated: bool
    nodes: list[HierarchyRecoveryNode]
    recovery_candidates: list[HierarchyRecoveryNode]
    reads_artifact_bodies: bool = False
    auto_takeover: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SubAgentHierarchyRecoveryService:
    """Build refs-only recovery packets for a nested subagent tree."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def build_packet(self, request: HierarchyRecoveryRequest) -> HierarchyRecoveryResult:
        scanned = _scan_tree(self.manager, request)
        candidates = [node for node in scanned if node.needs_recovery]
        visible = _visible_nodes(scanned, candidates, request)
        return HierarchyRecoveryResult(
            generated_at=time.time(),
            root_run_id=request.root_run_id,
            requested_by=request.requested_by,
            node_count=len(scanned),
            recovery_candidate_count=len(candidates),
            omitted_healthy_count=max(0, len(scanned) - len(visible)),
            truncated=len(scanned) >= max(1, request.max_nodes),
            nodes=visible,
            recovery_candidates=candidates,
            reads_artifact_bodies=False,
            auto_takeover=False,
        )


def _scan_tree(manager: Any, request: HierarchyRecoveryRequest) -> list[HierarchyRecoveryNode]:
    tasks = _load_tree_tasks(manager, request)
    task_index = {task.id: task for task in tasks}
    return [_node_from_task(task, request, task_index=task_index) for task in tasks]


def _load_tree_tasks(manager: Any, request: HierarchyRecoveryRequest) -> list[SubAgentTask]:
    queue = [request.root_run_id]
    seen: set[str] = set()
    tasks: list[SubAgentTask] = []
    limit = max(1, int(request.max_nodes or 1))
    while queue and len(tasks) < limit:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            continue
        tasks.append(task)
        queue.extend(child_id for child_id in task.child_ids if child_id not in seen)
    return tasks


def _visible_nodes(
    scanned: list[HierarchyRecoveryNode],
    candidates: list[HierarchyRecoveryNode],
    request: HierarchyRecoveryRequest,
) -> list[HierarchyRecoveryNode]:
    if request.include_healthy:
        return scanned
    candidate_ids = {node.run_id for node in candidates}
    return [node for node in scanned if node.run_id == request.root_run_id or node.run_id in candidate_ids]


def _node_from_task(
    task: SubAgentTask,
    request: HierarchyRecoveryRequest,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> HierarchyRecoveryNode:
    reason = _recovery_reason(task, request, task_index=task_index)
    strategy = _recovery_strategy_for_task(task, reason)
    return HierarchyRecoveryNode(
        run_id=task.id,
        parent_id=task.parent_id,
        root_id=task.root_id or task.id,
        depth=int(task.depth or 0),
        role=task.role,
        agent_name=task.agent_name,
        status=task.status,
        current_step=task.current_step or task.status,
        latest_summary=task.latest_summary,
        child_ids=list(task.child_ids),
        needs_recovery=bool(reason),
        recovery_reason=reason,
        takeover_readiness_ref=task.takeover_readiness_json,
        failure_handoff_ref=task.failure_handoff_json,
        checkpoint_ref=task.agent_run_checkpoint_json or task.checkpoint_json or task.checkpoint_ref,
        context_bundle_ref=_context_bundle_ref(task),
        parent_context_bundle_ref=_parent_context_bundle_ref(task, task_index),
        recommended_command=_recommended_command(task, reason),
        recovery_action=strategy.recommended_action if strategy else "",
        recovery_mode=strategy.recovery_mode if strategy else "",
        continue_packet_ref=strategy.packet_ref if strategy else "",
        continue_packet_status=strategy.packet_status if strategy else "",
        no_progress_fuse=bool(strategy.no_progress_fuse) if strategy else False,
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
    )


def _recovery_strategy_for_task(task: SubAgentTask, reason: str):
    if not reason:
        return None
    return build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))


def _context_bundle_ref(task: SubAgentTask) -> str:
    if task.agent_run_workspace_dir:
        return str(Path(task.agent_run_workspace_dir) / "context_bundle.json")
    if task.task_dir:
        return str(Path(task.task_dir) / "context_bundle.json")
    return ""


def _parent_context_bundle_ref(
    task: SubAgentTask,
    task_index: dict[str, SubAgentTask] | None,
) -> str:
    parent_id = str(task.parent_id or "")
    if not parent_id:
        return ""
    parent = (task_index or {}).get(parent_id)
    if parent is not None:
        return _context_bundle_ref(parent)
    if task.task_workspace_dir:
        return str(Path(task.task_workspace_dir) / "work" / "agents" / parent_id / "context_bundle.json")
    return ""


def _recovery_reason(
    task: SubAgentTask,
    request: HierarchyRecoveryRequest,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> str:
    status = str(task.status or "").strip()
    if status in RECOVERY_STATUSES:
        return f"status:{status}"
    if task.failure_type:
        return f"failure_type:{task.failure_type}"
    if task.blockers:
        return "blockers_present"
    parent_timeout = _parent_timeout_child_reason(task, task_index)
    if parent_timeout:
        return parent_timeout
    stale_reasons = _active_stale_reasons(task, request)
    if stale_reasons:
        return f"due:{','.join(stale_reasons)}"
    return ""


def _parent_timeout_child_reason(
    task: SubAgentTask,
    task_index: dict[str, SubAgentTask] | None,
) -> str:
    if task_is_handled_after_parent_timeout(task):
        return ""
    parent = (task_index or {}).get(str(task.parent_id or ""))
    if parent is None or not task_has_status(parent, TaskStatus.TIMEOUT):
        return ""
    return f"parent_timeout_unfinished_child:{parent.id}"


def _recommended_command(task: SubAgentTask, reason: str) -> str:
    if not reason:
        return ""
    if reason.startswith("parent_timeout_unfinished_child:"):
        root_id = task.root_id or task.id
        return f"my-agent subagents-recovery-tree {root_id} --hide-healthy"
    if reason.startswith("due:"):
        return (
            "my-agent subagents-apply-actions --apply --action takeover_or_reassign "
            f"--run-id {task.id} --take-over-by <agent>"
        )
    return f"my-agent subagents-tests-plan {task.id} --followup"


def _active_stale_reasons(task: SubAgentTask, request: HierarchyRecoveryRequest) -> list[str]:
    status = str(task.status or "").strip()
    has_active_attempt = bool(str(task.runner_active_attempt_id or "").strip())
    if not (_is_active(status) and (task_status_in(status, {TaskStatus.RUNNING.value}) or has_active_attempt)):
        return []
    now = float(request.now or time.time())
    reasons: list[str] = []
    stale_seconds = max(0.0, now - float(task.heartbeat_at or task.updated_at or now))
    age_seconds = max(0.0, now - float(task.created_at or now))
    if request.heartbeat_timeout > 0 and stale_seconds > request.heartbeat_timeout:
        reasons.append("heartbeat_stale")
    if request.run_timeout > 0 and age_seconds > request.run_timeout:
        reasons.append("run_timeout")
    return reasons
