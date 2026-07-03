
from __future__ import annotations

"""Subagent kernel projection.

这里不是新的调度器，也不是新的事实源。它只是把 task.json、run workspace、
control-plane 已有字段整理成一个稳定快照，后续恢复、QA、验收、E2E 都先读这里，
减少“每个模块自己猜状态”的问题。source_refs 指向最新可见 workspace，避免同 slug
旧任务把父级带回过期 task_root。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model_visible_refs import has_placeholder_path_segment
from .context_bundle_refs import workspace_refs as model_workspace_refs
from .model_capabilities import capability_request_counts_as_open
from .models import (
    SUBAGENT_BLOCKED_STATUSES,
    SUBAGENT_FAILED_RESULT_STATUSES,
    SubAgentTask,
    TaskStatus,
    task_status_in,
)
from .protocol import build_task_address, build_task_envelope


@dataclass(frozen=True)
class SubagentKernelQuery:
    root_id: str = ""
    run_id: str = ""
    scope: str = "root_tree"
    include_refs: bool = True
    task_workspace_dir: str = ""


@dataclass(frozen=True)
class SubagentKernelRun:
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    root_id: str = ""
    root_run_id: str = ""
    parent_task_id: str = ""
    parent_id: str = ""
    parent_run_id: str = ""
    depth: int = 0
    agent_kind: str = ""
    role: str = ""
    agent_name: str = ""
    # 派工 goal 摘要(T3 整合覆盖度):整合轮只看得到 role+摘要时无从核对"计划 vs 实交",
    # dispatch 交付深度因此缩水;全量 goal 在 canonical/task_registry,这里只投影短摘要。
    goal_digest: str = ""
    status: str = ""
    verification_status: str = ""
    failure_type: str = ""
    progress: float = 0.0
    current_step: str = ""
    current_tool: str = ""
    heartbeat_at: float = 0.0
    updated_at: float = 0.0
    last_progress_at: float = 0.0
    last_progress_summary: str = ""
    latest_summary: str = ""
    child_ids: list[str] = field(default_factory=list)
    address: dict[str, object] = field(default_factory=dict)
    task_envelope: dict[str, object] = field(default_factory=dict)
    workspace_refs: dict[str, str] = field(default_factory=dict)
    recovery_refs: dict[str, str] = field(default_factory=dict)
    tool_contract: dict[str, object] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    artifact_registry_refs: list[dict[str, object]] = field(default_factory=list)
    declared_output_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    needs_capability: list[str] = field(default_factory=list)
    recent_tool_trace: list[dict[str, object]] = field(default_factory=list)
    background_start: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentKernelSnapshot:
    schema_version: str
    root_id: str = ""
    scope: str = ""
    runs: list[SubagentKernelRun] = field(default_factory=list)
    running_run_ids: list[str] = field(default_factory=list)
    blocked_run_ids: list[str] = field(default_factory=list)
    completed_run_ids: list[str] = field(default_factory=list)
    failed_run_ids: list[str] = field(default_factory=list)
    takeover_candidate_run_ids: list[str] = field(default_factory=list)
    source_refs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    query_root_id: str = ""
    query_run_id: str = ""
    query_include_refs: bool = True
    query_task_workspace_dir: str = ""
    load_errors: list[dict[str, object]] = field(default_factory=list)


class SubagentKernel:

    def __init__(self, manager: Any):
        self.manager = manager

    def snapshot(self, query: SubagentKernelQuery | None = None) -> SubagentKernelSnapshot:
        query = query or SubagentKernelQuery()
        all_tasks, load_errors = self._safe_list_runs_report()
        selected, warnings = _select_tasks(all_tasks, query)
        if load_errors:
            warnings.extend(_load_error_warning(error) for error in load_errors)
        rows = [_task_to_kernel_run(task, include_refs=query.include_refs, all_tasks=selected) for task in selected]
        root_id = _snapshot_root_id(selected, query)
        return SubagentKernelSnapshot(
            schema_version="subagent_kernel_snapshot.v1",
            root_id=root_id,
            scope=query.scope,
            runs=rows,
            # Snapshot buckets use the shared TaskStatus sets; display text never drives lifecycle facts.
            running_run_ids=[row.run_id for row in rows if task_status_in(row.status, {TaskStatus.RUNNING.value})],
            blocked_run_ids=[row.run_id for row in rows if task_status_in(row.status, SUBAGENT_BLOCKED_STATUSES)],
            completed_run_ids=[row.run_id for row in rows if task_status_in(row.status, {TaskStatus.DONE.value})],
            failed_run_ids=[row.run_id for row in rows if task_status_in(row.status, SUBAGENT_FAILED_RESULT_STATUSES)],
            takeover_candidate_run_ids=[
                row.run_id
                for row in rows
                if task_status_in(row.status, SUBAGENT_FAILED_RESULT_STATUSES | SUBAGENT_BLOCKED_STATUSES)
            ],
            source_refs=_snapshot_source_refs(selected),
            warnings=warnings,
            query_root_id=query.root_id,
            query_run_id=query.run_id,
            query_include_refs=query.include_refs,
            query_task_workspace_dir=query.task_workspace_dir,
            load_errors=load_errors,
        )

    def _safe_list_runs_report(self) -> tuple[list[SubAgentTask], list[dict[str, object]]]:
        try:
            report_method = getattr(self.manager, "list_runs_report", None)
            if callable(report_method):
                report = report_method()
                return list(report.runs), list(report.load_errors)
            return list(self.manager.list_runs()), []
        except FileNotFoundError:
            return [], []


def _load_error_warning(error: dict[str, object]) -> str:
    run_id = str(error.get("run_id") or "").strip()
    error_type = str(error.get("error_type") or "").strip()
    suffix = f":{run_id}" if run_id else ""
    if error_type:
        suffix = f"{suffix}:{error_type}"
    return f"subagent_load_error{suffix}"


class SubagentKernelMixin:

    def kernel_snapshot(self, query: SubagentKernelQuery | None = None) -> SubagentKernelSnapshot:
        return SubagentKernel(self).snapshot(query)


def _select_tasks(
    tasks: list[SubAgentTask],
    query: SubagentKernelQuery,
) -> tuple[list[SubAgentTask], list[str]]:
    by_id = {task.id: task for task in tasks}
    warnings: list[str] = []
    if query.scope in {"own_subtree", "subtree"} and query.run_id:
        return _ordered_subtree(tasks, query.run_id), warnings
    if query.scope == "task_workspace":
        return _select_task_workspace(tasks, query)
    root_id = query.root_id or _root_for_run(by_id.get(query.run_id))
    if root_id:
        return [task for task in _stable_tasks(tasks) if task.id == root_id or task.root_id == root_id], warnings
    if query.run_id and query.run_id not in by_id:
        warnings.append("run_not_found")
    if query.run_id and query.run_id in by_id:
        return [by_id[query.run_id]], warnings
    return _stable_tasks(tasks), warnings


def _select_task_workspace(
    tasks: list[SubAgentTask],
    query: SubagentKernelQuery,
) -> tuple[list[SubAgentTask], list[str]]:
    task_workspace = str(query.task_workspace_dir or "").strip()
    if not task_workspace:
        return _stable_tasks(tasks), ["task_workspace_scope_missing_workspace"]
    selected = [task for task in _stable_tasks(tasks) if str(task.task_workspace_dir or "").strip() == task_workspace]
    return selected, [] if selected else ["task_workspace_scope_had_no_subagent_rows"]


def _ordered_subtree(tasks: list[SubAgentTask], run_id: str) -> list[SubAgentTask]:
    by_id = {task.id: task for task in tasks}
    selected: list[SubAgentTask] = []

    def visit(current_id: str) -> None:
        task = by_id.get(current_id)
        if task is None or task in selected:
            return
        selected.append(task)
        for child_id in task.child_ids:
            visit(child_id)

    visit(run_id)
    return selected


def _task_to_kernel_run(
    task: SubAgentTask,
    *,
    include_refs: bool,
    all_tasks: list[SubAgentTask],
) -> SubagentKernelRun:
    return SubagentKernelRun(
        task_id=task.id,
        run_id=task.id,
        session_id=task.subagent_session_id,
        thread_id=task.agent_thread_id,
        root_id=_root_for_run(task),
        root_run_id=_root_for_run(task),
        parent_task_id=task.parent_id,
        parent_id=task.parent_id,
        parent_run_id=task.parent_id,
        depth=int(task.depth or 0),
        agent_kind=_agent_kind(task),
        role=task.role,
        agent_name=task.agent_name,
        goal_digest=" ".join(str(task.goal or "").split())[:200],
        status=task.status,
        verification_status=task.verification_status,
        failure_type=task.failure_type,
        progress=float(task.progress or 0.0),
        current_step=task.current_step,
        current_tool=str(getattr(task, "current_tool", "") or ""),
        heartbeat_at=float(task.heartbeat_at or 0.0),
        updated_at=float(task.updated_at or 0.0),
        last_progress_at=float(getattr(task, "last_progress_at", 0.0) or 0.0),
        last_progress_summary=str(getattr(task, "last_progress_summary", "") or ""),
        latest_summary=task.latest_summary,
        child_ids=list(task.child_ids),
        address=build_task_address(task, all_tasks=all_tasks).to_dict() if include_refs else {},
        task_envelope=build_task_envelope(task, all_tasks=all_tasks).to_dict() if include_refs else {},
        workspace_refs=model_workspace_refs(task) if include_refs else {},
        recovery_refs=_recovery_refs(task) if include_refs else {},
        tool_contract=_tool_contract(task) if include_refs else {},
        artifact_refs=list(task.artifact_refs),
        artifact_registry_refs=_artifact_registry_refs(task),
        declared_output_refs=_declared_output_refs(task),
        evidence_refs=list(task.evidence_refs),
        blockers=list(task.blockers),
        needs_capability=_needs_capability(task) if include_refs else [],
        recent_tool_trace=_recent_tool_trace(task) if include_refs else [],
        background_start=_background_start(task) if include_refs else {},
    )


def _recovery_refs(task: SubAgentTask) -> dict[str, str]:
    refs = {
        "checkpoint": task.agent_run_checkpoint_json or task.checkpoint_ref,
        "summary": task.agent_run_summary_md,
        "latest_compaction_summary": task.agent_run_latest_compaction_summary_md,
        "latest_compaction_metadata": task.agent_run_latest_compaction_metadata_json,
        "latest_session_compaction_summary": task.agent_run_latest_session_compaction_summary_md,
        "latest_session_compaction_metadata": task.agent_run_latest_session_compaction_metadata_json,
        "session_continue_packet": _continue_packet_ref(task),
        "failure_warning": task.failure_handoff.warning,
        "failure_recommended_next_action": task.failure_handoff.recommended_next_action,
    }
    return {key: value for key, value in refs.items() if value}


def _tool_contract(task: SubAgentTask) -> dict[str, object]:
    open_requests = _open_capability_requests(task)
    contract: dict[str, object] = {
        "allowed_tools": list(task.allowed_tools),
        "used_tools": list(task.used_tools),
        "open_request_count": len(open_requests),
        "grant_count": len(task.capability_grants),
        "gap_count": len(task.capability_gaps),
        "controlled_exec_grant_ids": [
            grant.id for grant in task.capability_grants if "controlled_exec" in list(getattr(grant, "tools", []) or [])
        ],
    }
    # P4-1 引导前移(R5 三案实锤:A3 引导只挂 closeout finding,不提交 closeout 就
    # 永远看不到)。运行中树快照存在 OPEN capreq 时直接带结构化引导,主代理在
    # inspect_agent_tree/watch 里即可照着调用,不必等 closeout。软引导,不拦路。
    if open_requests:
        contract["recommended_tool"] = "resolve_capability_requests"
        contract["open_capability_request_ids"] = [
            str(getattr(item, "id", "") or "") for item in open_requests[:20]
        ]
    return contract


def _artifact_registry_refs(task: SubAgentTask, *, limit: int = 12) -> list[dict[str, object]]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    value = attrs.get("artifact_registry_refs")
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        artifact_id = str(row.get("artifact_id") or "").strip()
        path = str(row.get("path") or "").strip()
        key = artifact_id or path
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    refs: list[str] = []
    for key in ("output_files", "output_refs", "artifact_refs"):
        value = attrs.get(key)
        if isinstance(value, list):
            refs.extend(str(item).strip() for item in value if str(item or "").strip())
    return list(dict.fromkeys(ref for ref in refs if not has_placeholder_path_segment(ref)))


def _recent_tool_trace(task: SubAgentTask) -> list[dict[str, object]]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    value = attrs.get("recent_tool_trace")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-5:] if isinstance(item, dict)]


def _needs_capability(task: SubAgentTask) -> list[str]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    value = attrs.get("needs_capability")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _background_start(task: SubAgentTask) -> dict[str, object]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    value = attrs.get("background_start")
    return dict(value) if isinstance(value, dict) else {}


def _open_capability_requests(task: SubAgentTask) -> list[object]:
    return [item for item in task.capability_requests if capability_request_counts_as_open(getattr(item, "status", "OPEN"))]


def _continue_packet_ref(task: SubAgentTask) -> str:
    if not task.agent_run_compactions_dir:
        return ""
    return str(Path(task.agent_run_compactions_dir) / "session" / "latest_continue_packet.json")


def _stable_tasks(tasks: list[SubAgentTask]) -> list[SubAgentTask]:
    return sorted(tasks, key=lambda item: (int(item.depth or 0), float(item.created_at or 0.0), item.id))


def _agent_kind(task: SubAgentTask) -> str:
    if int(task.depth or 0) <= 0 and not task.parent_id:
        return "root_agent"
    if int(task.depth or 0) <= 1:
        return "child_agent"
    return "grandchild_agent"


def _root_for_run(task: SubAgentTask | None) -> str:
    if task is None:
        return ""
    return task.root_id or task.id


def _snapshot_root_id(tasks: list[SubAgentTask], query: SubagentKernelQuery) -> str:
    if query.root_id:
        return query.root_id
    if tasks:
        return _root_for_run(tasks[0])
    return ""


def _snapshot_source_refs(tasks: list[SubAgentTask]) -> dict[str, str]:
    if not tasks:
        return {}
    # Prefer the freshest workspace so same-slug historical task dirs cannot pollute source refs.
    root = _latest_workspace_task(tasks)
    return {
        key: value
        for key, value in {
            "root_task": root.task_workspace_dir,
            "root_work": str(Path(root.task_workspace_dir) / "work") if root.task_workspace_dir else "",
            "root_output": str(Path(root.task_workspace_dir) / "output") if root.task_workspace_dir else "",
            "root_agent_work": root.agent_run_workspace_dir,
        }.items()
        if value
    }


def _latest_workspace_task(tasks: list[SubAgentTask]) -> SubAgentTask:
    with_workspace = [task for task in tasks if str(task.task_workspace_dir or "").strip()]
    candidates = with_workspace or tasks
    return max(candidates, key=lambda task: (float(task.updated_at or 0.0), float(task.created_at or 0.0), task.id))
