# LLM: Subagent leadership recovery planner; keep it dry-run until subset handoff apply is implemented.
# 模块用途: 为失联 coordinator 的孩子生成分批接管计划，不直接修改任务树。

from __future__ import annotations

"""Dry-run batch leadership recovery planner for stale coordinator subtrees."""

import time
from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentDueCheckOptions, SubAgentLeadershipRecoveryPlanOptions, SubAgentTask


# LLM: LeadershipRecoveryAssignment describes one planned child batch for a candidate leader.
# 类用途: 返回一组孩子应该交给哪个 leader；当前只是计划记录，不代表已经执行。
@dataclass(frozen=True)
class LeadershipRecoveryAssignment:
    coordinator_id: str
    leader_id: str
    child_ids: list[str]
    apply_supported: bool = False
    suggested_command: str = ""
    reason: str = ""


# LLM: LeadershipRecoveryUnassigned records child IDs that cannot be safely placed.
# 类用途: 明确列出因为容量、leader 缺失或候选不可用而没有分配出去的孩子。
@dataclass(frozen=True)
class LeadershipRecoveryUnassigned:
    coordinator_id: str
    child_ids: list[str]
    reason: str


# LLM: LeadershipRecoveryPlanReport is the stable JSON shape for dry-run handoff planning.
# 类用途: 汇总批量领导权恢复计划，供 CLI、文档和后续受控 apply 读取。
@dataclass(frozen=True)
class LeadershipRecoveryPlanReport:
    generated_at: float
    root_id: str
    max_children_per_leader: int
    summary: dict[str, int]
    assignments: list[LeadershipRecoveryAssignment] = field(default_factory=list)
    unassigned: list[LeadershipRecoveryUnassigned] = field(default_factory=list)
    candidate_leader_ids: list[str] = field(default_factory=list)


# LLM: _LeaderCapacity keeps assignment state local to one planner call.
# 类用途: 记录每个候选 leader 当前还能接多少个孩子，避免一个 leader 被无限塞任务。
@dataclass
class _LeaderCapacity:
    task: SubAgentTask
    remaining: int


# LLM: _SummaryInput bundles plan summary counters to avoid widening helper signatures.
# 类用途: 汇总 leadership recovery plan 的输入集合，保持 code-size 参数守卫清零。
@dataclass(frozen=True)
class _SummaryInput:
    coordinators: list[SubAgentTask]
    leaders: list[_LeaderCapacity]
    assignments: list[LeadershipRecoveryAssignment]
    unassigned: list[LeadershipRecoveryUnassigned]
    failed_parent_count: int = 0


# LLM: SubAgentLeadershipRecoveryPlanner owns read-only batch leader assignment rules.
# 类用途: 根据 due-check 发现的失联 coordinator，按 leader 容量生成分批接管计划。
class SubAgentLeadershipRecoveryPlanner:
    """Plan batch coordinator leadership handoffs without mutating task records."""

    # LLM: __init__ stores the manager facade used for loading task refs and due-check issues.
    # 函数用途: 初始化规划器依赖；本身不读写文件。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: plan uses existing due-check facts and explicit leader IDs to build a deterministic dry-run.
    # 函数用途: 生成批量 leadership recovery 分配结果，所有输出都是 refs-only。
    def plan(self, request: SubAgentLeadershipRecoveryPlanOptions) -> LeadershipRecoveryPlanReport:
        stale_coordinators = self._stale_coordinators(request)
        failed_parents = self._failed_parent_sources(request, stale_coordinators)
        coordinators = _unique_sources([*stale_coordinators, *failed_parents])
        leaders = self._candidate_leaders(request)
        assignments, unassigned = _assign_coordinator_children(coordinators, leaders)
        return LeadershipRecoveryPlanReport(
            generated_at=time.time(),
            root_id=request.root_id,
            max_children_per_leader=max(0, int(request.max_children_per_leader or 0)),
            summary=_summary(
                _SummaryInput(
                    coordinators=coordinators,
                    leaders=leaders,
                    assignments=assignments,
                    unassigned=unassigned,
                    failed_parent_count=len(failed_parents),
                )
            ),
            assignments=assignments,
            unassigned=unassigned,
            candidate_leader_ids=[leader.task.id for leader in leaders],
        )

    # LLM: _stale_coordinators preserves the existing coordinator_heartbeat_stale policy as the source of truth.
    # 函数用途: 从 due-check 报告中找出需要恢复领导权的 coordinator，再读取其任务快照。
    def _stale_coordinators(self, request: SubAgentLeadershipRecoveryPlanOptions) -> list[SubAgentTask]:
        due_report = self.manager.due_check(
            params=SubAgentDueCheckOptions(config=request.config, root_id=request.root_id)
        )
        coordinators: list[SubAgentTask] = []
        for issue in due_report.issues:
            if issue.kind != "coordinator_heartbeat_stale":
                continue
            try:
                coordinators.append(self.manager.load(issue.run_id))
            except FileNotFoundError:
                continue
        return coordinators

    # LLM: _failed_parent_sources lets a replacement leader that later fails become a fresh handoff source.
    # 函数用途: 找出同 root 下已经失败/超时/断通道且仍有孩子的父节点，支持二次接管计划。
    def _failed_parent_sources(
        self,
        request: SubAgentLeadershipRecoveryPlanOptions,
        stale_coordinators: list[SubAgentTask],
    ) -> list[SubAgentTask]:
        stale_ids = {task.id for task in stale_coordinators}
        failed: list[SubAgentTask] = []
        for task in self.manager.list_runs():
            if task.id in stale_ids or not task.child_ids:
                continue
            if not _leader_matches_scope(task, request.root_id):
                continue
            if _parent_is_failed_recovery_source(task):
                failed.append(task)
        return failed

    # LLM: _candidate_leaders accepts only explicit same-root healthy leader refs in this first dry-run slice.
    # 函数用途: 校验候选 leader 是否存在、同 root 且不是明显不可接管状态。
    def _candidate_leaders(self, request: SubAgentLeadershipRecoveryPlanOptions) -> list[_LeaderCapacity]:
        leaders: list[_LeaderCapacity] = []
        max_children = int(request.max_children_per_leader or 0)
        for leader_id in request.leader_ids:
            try:
                leader = self.manager.load(leader_id)
            except FileNotFoundError:
                continue
            if not _leader_matches_scope(leader, request.root_id):
                continue
            if not _leader_is_available(leader):
                continue
            remaining = _remaining_capacity(leader, max_children)
            if remaining <= 0:
                continue
            leaders.append(_LeaderCapacity(task=leader, remaining=remaining))
        return leaders


# LLM: _assign_coordinator_children greedily fills leaders while preserving child order.
# 函数用途: 将每个失联 coordinator 的直接孩子按 leader 剩余容量分批分配，分不出去的明确列入 unassigned。
def _assign_coordinator_children(
    coordinators: list[SubAgentTask],
    leaders: list[_LeaderCapacity],
) -> tuple[list[LeadershipRecoveryAssignment], list[LeadershipRecoveryUnassigned]]:
    assignments: list[LeadershipRecoveryAssignment] = []
    unassigned: list[LeadershipRecoveryUnassigned] = []
    leader_ids = {leader.task.id for leader in leaders}
    for coordinator in coordinators:
        pending = [child_id for child_id in coordinator.child_ids if child_id and child_id not in leader_ids]
        if not pending:
            continue
        pending = _assign_pending_children(coordinator, pending, leaders, assignments)
        if pending:
            unassigned.append(
                LeadershipRecoveryUnassigned(
                    coordinator_id=coordinator.id,
                    child_ids=pending,
                    reason="leader_capacity_exhausted" if leaders else "no_candidate_leaders",
                )
            )
    return assignments, unassigned


# LLM: _unique_sources keeps source order deterministic when a node matches more than one recovery rule.
# 函数用途: 合并 stale coordinator 和 failed parent 列表，避免重复生成分配批次。
def _unique_sources(tasks: list[SubAgentTask]) -> list[SubAgentTask]:
    seen: set[str] = set()
    unique: list[SubAgentTask] = []
    for task in tasks:
        if task.id in seen:
            continue
        seen.add(task.id)
        unique.append(task)
    return unique


# LLM: _assign_pending_children consumes leader capacity and returns only leftovers.
# 函数用途: 为单个 coordinator 的孩子生成一个或多个 leader 批次，保持算法可预测。
def _assign_pending_children(
    coordinator: SubAgentTask,
    pending: list[str],
    leaders: list[_LeaderCapacity],
    assignments: list[LeadershipRecoveryAssignment],
) -> list[str]:
    for leader in leaders:
        if not pending:
            break
        if leader.remaining <= 0:
            continue
        batch = pending[: leader.remaining]
        pending = pending[leader.remaining :]
        leader.remaining -= len(batch)
        assignments.append(
            LeadershipRecoveryAssignment(
                coordinator_id=coordinator.id,
                leader_id=leader.task.id,
                child_ids=batch,
                suggested_command=_future_subset_apply_command(coordinator.id, leader.task.id, batch),
                reason="dry_run_split_handoff_requires_subset_apply",
            )
        )
    return pending


# LLM: _remaining_capacity treats 0 or negative max as no practical cap while keeping finite math.
# 函数用途: 计算 leader 本轮还能接多少直接孩子；已有 child_ids 会占用容量。
def _remaining_capacity(leader: SubAgentTask, max_children: int) -> int:
    if max_children <= 0:
        return 10**9
    return max(0, max_children - len(leader.child_ids))


# LLM: _leader_matches_scope prevents plans from crossing root task boundaries.
# 函数用途: 校验候选 leader 是否属于本次 root 范围，避免 shared workspace 互相污染。
def _leader_matches_scope(leader: SubAgentTask, root_id: str) -> bool:
    normalized = str(root_id or "").strip()
    return not normalized or (leader.root_id or leader.id) == normalized


# LLM: _leader_is_available rejects terminal or broken candidates before planning child load.
# 函数用途: 过滤明显不能接管的 leader，避免把孩子交给失败、超时、损坏或已被接管的 run。
def _leader_is_available(leader: SubAgentTask) -> bool:
    status = str(leader.status or "").upper()
    channel = str(leader.channel_status or "").upper()
    return status not in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER", "ABANDONED"} and channel != "BROKEN"


# LLM: _parent_is_failed_recovery_source identifies failed leaders whose child subtrees need a new owner.
# 函数用途: 判断一个已有 child_ids 的父节点是否已经不可继续领导，用于二次恢复计划。
def _parent_is_failed_recovery_source(task: SubAgentTask) -> bool:
    status = str(task.status or "").upper()
    channel = str(task.channel_status or "").upper()
    return status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "ABANDONED"} or channel == "BROKEN"


# LLM: _future_subset_apply_command documents the intended next apply command without executing it.
# 函数用途: 给报告显示未来分批 apply 的命令形状；当前第一片不提供真正执行入口。
def _future_subset_apply_command(coordinator_id: str, leader_id: str, child_ids: list[str]) -> str:
    child_flags = " ".join(f"--child-run-id {child_id}" for child_id in child_ids)
    return (
        "my-agent subagents-leadership-recovery-apply "
        f"--coordinator {coordinator_id} --leader {leader_id} {child_flags} --apply"
    )


# LLM: _summary keeps CLI/report counters machine-readable and stable.
# 函数用途: 汇总 coordinator、leader、分配和未分配孩子数量。
def _summary(data: _SummaryInput) -> dict[str, int]:
    assigned_children = sum(len(item.child_ids) for item in data.assignments)
    unassigned_children = sum(len(item.child_ids) for item in data.unassigned)
    return {
        "stale_coordinators": len(data.coordinators) - data.failed_parent_count,
        "candidate_leaders": len(data.leaders),
        "assignments": len(data.assignments),
        "assigned_children": assigned_children,
        "unassigned_children": unassigned_children,
        "failed_parent_nodes": data.failed_parent_count,
    }
