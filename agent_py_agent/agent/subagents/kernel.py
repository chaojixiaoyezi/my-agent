# LLM: Subagent kernel read model; keep it refs-first and free of orchestration side effects.
# 模块用途: 汇总子代理 run/session/task 的核心状态，给父级、接管和收口链路提供统一读取入口。

from __future__ import annotations

"""Subagent kernel facade.

给人看的解释：
这里不是新的调度器，也不是新的事实源。它只是把旧 task.json、run workspace、
control-plane 已有字段整理成一个稳定快照，后续恢复、QA、验收、E2E 都先读这里，
减少“每个模块自己猜状态”的问题。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import SubAgentTask
from .protocol import build_task_address, build_task_envelope

_RUNNING_STATUSES = {"RUNNING"}
_COMPLETED_STATUSES = {"DONE", "COMPLETED", "ACCEPTED", "VERIFIED"}
_FAILED_STATUSES = {"FAILED", "ERROR", "TIMEOUT"}
_BLOCKED_STATUSES = {"BLOCKED"}
_TAKEOVER_CANDIDATE_STATUSES = _FAILED_STATUSES | _BLOCKED_STATUSES


# LLM: SubagentKernelQuery is the bundle-shaped read contract for kernel snapshots.
# 类用途: 描述调用方要读取 root tree、某个子树，还是全部可见 run；默认只读，不产生副作用。
@dataclass(frozen=True)
class SubagentKernelQuery:

    root_id: str = ""
    run_id: str = ""
    scope: str = "root_tree"
    include_refs: bool = True
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: SubagentKernelRun is a compact refs-first status row for one subagent run.
# 类用途: 保存单个子代理 run 的身份、状态、父子关系、工作区引用和恢复引用，供父级稳定读取。
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
    evidence_refs: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: SubagentKernelSnapshot is the stable boundary between orchestration and status consumers.
# 类用途: 汇总一棵任务树或子树的状态桶和 run 列表，后续恢复、QA 和验收链路先读它。
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
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: SubagentKernel builds read-only snapshots from SubAgentManager's current file facts.
# 类用途: 为 SubAgentManager 提供内核读取门面，不调度、不恢复、不验收，只整理当前状态。
class SubagentKernel:

    # LLM: __init__ keeps the kernel facade attached to one manager instance.
    # 函数用途: 保存 manager 依赖，后续所有读取仍通过 manager 的 load/list_runs 事实源完成。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: snapshot returns a refs-first status view without mutating task files.
    # 函数用途: 按 query 读取 root tree、own subtree 或全部 run，并生成统一状态快照。
    def snapshot(self, query: SubagentKernelQuery | None = None) -> SubagentKernelSnapshot:
        query = query or SubagentKernelQuery()
        all_tasks = self._safe_list_runs()
        selected, warnings = _select_tasks(all_tasks, query)
        rows = [_task_to_kernel_run(task, include_refs=query.include_refs, all_tasks=selected) for task in selected]
        root_id = _snapshot_root_id(selected, query)
        return SubagentKernelSnapshot(
            schema_version="subagent_kernel_snapshot.v1",
            root_id=root_id,
            scope=query.scope,
            runs=rows,
            running_run_ids=[row.run_id for row in rows if row.status in _RUNNING_STATUSES],
            blocked_run_ids=[row.run_id for row in rows if row.status in _BLOCKED_STATUSES],
            completed_run_ids=[row.run_id for row in rows if row.status in _COMPLETED_STATUSES],
            failed_run_ids=[row.run_id for row in rows if row.status in _FAILED_STATUSES],
            takeover_candidate_run_ids=[
                row.run_id for row in rows if row.status in _TAKEOVER_CANDIDATE_STATUSES
            ],
            source_refs=_snapshot_source_refs(selected),
            warnings=warnings,
            reserved={"query": _query_reserved(query)},
        )

    # LLM: _safe_list_runs isolates missing/corrupt task records from the public snapshot call.
    # 函数用途: 读取全部 run；如果 workspace 还没初始化，返回空列表而不是让状态查询卡死。
    def _safe_list_runs(self) -> list[SubAgentTask]:
        try:
            return list(self.manager.list_runs())
        except FileNotFoundError:
            return []


# LLM: SubagentKernelMixin exposes kernel_snapshot on the public SubAgentManager facade.
# 类用途: 给 manager 增加稳定内核读取入口，调用方不需要自己实例化 SubagentKernel。
class SubagentKernelMixin:

    # LLM: kernel_snapshot is the public read boundary for subagent kernel status.
    # 函数用途: 返回当前子代理任务树/子树快照；只读，不触发调度、恢复或验收。
    def kernel_snapshot(self, query: SubagentKernelQuery | None = None) -> SubagentKernelSnapshot:
        return SubagentKernel(self).snapshot(query)


# LLM: _select_tasks keeps scope rules explicit and separate from snapshot rendering.
# 函数用途: 根据 root_id、run_id 和 scope 选择需要返回的任务集合。
def _select_tasks(
    tasks: list[SubAgentTask],
    query: SubagentKernelQuery,
) -> tuple[list[SubAgentTask], list[str]]:
    by_id = {task.id: task for task in tasks}
    warnings: list[str] = []
    if query.scope in {"own_subtree", "subtree"} and query.run_id:
        return _ordered_subtree(tasks, query.run_id), warnings
    root_id = query.root_id or _root_for_run(by_id.get(query.run_id))
    if root_id:
        return [task for task in _stable_tasks(tasks) if task.id == root_id or task.root_id == root_id], warnings
    if query.run_id and query.run_id not in by_id:
        warnings.append("run_not_found")
    if query.run_id and query.run_id in by_id:
        return [by_id[query.run_id]], warnings
    return _stable_tasks(tasks), warnings


# LLM: _ordered_subtree selects one run and descendants using persisted child edges.
# 函数用途: 按 child_ids 递归返回子树，避免把 sibling 的正文或状态混进接管视图。
def _ordered_subtree(tasks: list[SubAgentTask], run_id: str) -> list[SubAgentTask]:
    by_id = {task.id: task for task in tasks}
    selected: list[SubAgentTask] = []

    # LLM: visit walks persisted child links without loading artifact bodies.
    # 函数用途: 递归收集当前 run 及其后代；遇到缺失 child 或重复节点时安全跳过。
    def visit(current_id: str) -> None:
        task = by_id.get(current_id)
        if task is None or task in selected:
            return
        selected.append(task)
        for child_id in task.child_ids:
            visit(child_id)

    visit(run_id)
    return selected


# LLM: _task_to_kernel_run maps a full task record to a compact kernel row.
# 函数用途: 保留状态、关系和 refs；不读取 artifact 正文，避免父级上下文膨胀。
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
        workspace_refs=_workspace_refs(task) if include_refs else {},
        recovery_refs=_recovery_refs(task) if include_refs else {},
        tool_contract=_tool_contract(task) if include_refs else {},
        artifact_refs=list(task.artifact_refs),
        evidence_refs=list(task.evidence_refs),
        blockers=list(task.blockers),
        reserved={"task_dir": task.task_dir} if include_refs and task.task_dir else {},
    )


# LLM: _workspace_refs centralizes task/run workspace refs used by upper agents.
# 函数用途: 返回任务目录、runtime task workspace 和 agent run workspace 路径引用。
def _workspace_refs(task: SubAgentTask) -> dict[str, str]:
    refs = {
        "task_dir": task.task_dir,
        "task_workspace": task.task_workspace_dir,
        "agent_run_workspace": task.agent_run_workspace_dir,
        "shared_blackboard": task.task_workspace_shared_blackboard,
        "inbox": task.agent_run_inbox_dir,
        "outbox": task.agent_run_outbox_dir,
        "final_report": task.agent_run_final_report_md,
    }
    return {key: value for key, value in refs.items() if value}


# LLM: _recovery_refs groups checkpoint, compact, handoff, and continue packet refs.
# 函数用途: 给接管/恢复链路提供稳定入口，只返回路径和摘要，不读取正文。
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


# LLM: _tool_contract exposes tool readiness as machine fields without granting new capabilities.
# 函数用途: 汇总允许工具、已用工具和能力缺口，供父级/接管者判断是否需要工具网关或授权。
def _tool_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_tools": list(task.allowed_tools),
        "used_tools": list(task.used_tools),
        "open_request_count": len(_open_capability_requests(task)),
        "grant_count": len(task.capability_grants),
        "gap_count": len(task.capability_gaps),
        "controlled_exec_grant_ids": [
            grant.id for grant in task.capability_grants if "controlled_exec" in list(getattr(grant, "tools", []) or [])
        ],
    }


# LLM: _open_capability_requests normalizes old and new request status fields.
# 函数用途: 统计仍需要父级/工具网关处理的能力申请，关闭态不再算缺口。
def _open_capability_requests(task: SubAgentTask) -> list[object]:
    closed = {"CLOSED", "RESOLVED", "REJECTED", "APPROVED", "GRANTED"}
    return [item for item in task.capability_requests if str(getattr(item, "status", "OPEN") or "OPEN").upper() not in closed]


# LLM: _continue_packet_ref derives the latest task-local continue packet path from compaction refs.
# 函数用途: 当子代理 compact 后，给父级一个固定读取 latest_continue_packet.json 的位置。
def _continue_packet_ref(task: SubAgentTask) -> str:
    if not task.agent_run_compactions_dir:
        return ""
    return str(Path(task.agent_run_compactions_dir) / "session" / "latest_continue_packet.json")


# LLM: _stable_tasks gives deterministic root-before-child ordering for snapshots.
# 函数用途: 按 depth、created_at 和 run id 排序，让测试、日志和父级读取结果稳定。
def _stable_tasks(tasks: list[SubAgentTask]) -> list[SubAgentTask]:
    return sorted(tasks, key=lambda item: (int(item.depth or 0), float(item.created_at or 0.0), item.id))


# LLM: _agent_kind is a structural projection, not a role/prompt classifier.
# 函数用途: 根据 parent/depth 给状态树一个稳定层级标签，让父级查看时不用猜 child/grandchild。
def _agent_kind(task: SubAgentTask) -> str:
    if int(task.depth or 0) <= 0 and not task.parent_id:
        return "root_agent"
    if int(task.depth or 0) <= 1:
        return "child_agent"
    return "grandchild_agent"


# LLM: _root_for_run normalizes legacy empty root_id to the run itself.
# 函数用途: root 任务通常 root_id 为空；快照里统一显示自己的 id。
def _root_for_run(task: SubAgentTask | None) -> str:
    if task is None:
        return ""
    return task.root_id or task.id


# LLM: _snapshot_root_id chooses the most useful root id for the returned snapshot.
# 函数用途: 优先 query，再从首个 run 推导 root，保持空工作区可安全返回。
def _snapshot_root_id(tasks: list[SubAgentTask], query: SubagentKernelQuery) -> str:
    if query.root_id:
        return query.root_id
    if tasks:
        return _root_for_run(tasks[0])
    return ""


# LLM: _snapshot_source_refs reports where the kernel view came from.
# 函数用途: 给调试和后续接管说明当前快照基于哪些 workspace 文件。
def _snapshot_source_refs(tasks: list[SubAgentTask]) -> dict[str, str]:
    if not tasks:
        return {}
    root = tasks[0]
    return {
        key: value
        for key, value in {
            "root_task_dir": root.task_dir,
            "root_task_workspace": root.task_workspace_dir,
            "root_agent_run_workspace": root.agent_run_workspace_dir,
        }.items()
        if value
    }


# LLM: _query_reserved stores non-control query details without growing the public schema.
# 函数用途: 把查询参数放入 reserved，方便调试且不影响主字段稳定性。
def _query_reserved(query: SubagentKernelQuery) -> dict[str, object]:
    return {
        "root_id": query.root_id,
        "run_id": query.run_id,
        "include_refs": query.include_refs,
        **dict(query.reserved),
    }
