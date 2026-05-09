# LLM: Controlled leadership recovery apply service; only moves explicitly listed child subtrees.
# 模块用途: 安全执行批量恢复计划中的单个 child 子集重挂，并生成审计报告。

from __future__ import annotations

"""Controlled subset apply for subagent leadership recovery."""

import time
from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentLeadershipRecoveryApplyOptions, SubAgentTask
from .leadership_recovery import _leader_is_available, _remaining_capacity


# LLM: LeadershipRecoveryApplyRecord records one dry-run or apply attempt for a child subset.
# 类用途: 记录一次分批 leadership recovery apply 的校验、移动结果和证据路径。
@dataclass(frozen=True)
class LeadershipRecoveryApplyRecord:
    coordinator_id: str
    leader_id: str
    requested_child_ids: list[str]
    moved_child_ids: list[str]
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    blocked_by: list[str] = field(default_factory=list)
    before_remaining_child_ids: list[str] = field(default_factory=list)
    after_remaining_child_ids: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: LeadershipRecoveryApplyReport keeps subset apply output stable for CLI and dispatch refs.
# 类用途: 汇总 leadership recovery apply 结果；summary 固定暴露 total/ok/applied/moved_children。
@dataclass(frozen=True)
class LeadershipRecoveryApplyReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[LeadershipRecoveryApplyRecord]


# LLM: _ApplyContext carries loaded task snapshots through validation and mutation.
# 类用途: 保存一次 apply 所需的 coordinator、leader、child 快照，避免 helper 散传参数。
@dataclass(frozen=True)
class _ApplyContext:
    coordinator: SubAgentTask
    leader: SubAgentTask
    children: list[SubAgentTask]
    blocked_by: list[str]


# LLM: _ScopeValidationInput bundles root-scope checks for the code-size guard.
# 类用途: 校验 root 一致性所需上下文，避免 helper 参数继续扩张。
@dataclass(frozen=True)
class _ScopeValidationInput:
    request: SubAgentLeadershipRecoveryApplyOptions
    coordinator: SubAgentTask
    leader: SubAgentTask
    blocked: list[str]


# LLM: _ApplyMutationInput bundles mutation dependencies for the final apply path.
# 类用途: 携带 manager、请求、快照和时间戳，保持 apply helper 参数稳定。
@dataclass(frozen=True)
class _ApplyMutationInput:
    manager: Any
    request: SubAgentLeadershipRecoveryApplyOptions
    ctx: _ApplyContext
    now: float


# LLM: SubAgentLeadershipRecoveryApplier owns validation, subtree reparenting, and audit records.
# 类用途: 执行或预演指定 child 子集的领导权恢复，保持所有副作用集中在一个服务中。
class SubAgentLeadershipRecoveryApplier:
    """Apply one controlled subset handoff from an old coordinator to a new leader."""

    # LLM: __init__ stores the manager facade used for task persistence and takeover audit.
    # 函数用途: 初始化 apply 服务依赖；本身不修改任务。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: apply validates first, then optionally reparents children when request.apply is true.
    # 函数用途: 预演或执行单个分批接管动作；失败时不产生部分移动。
    def apply(self, request: SubAgentLeadershipRecoveryApplyOptions) -> LeadershipRecoveryApplyReport:
        now = time.time()
        ctx = _load_apply_context(self.manager, request)
        if ctx.blocked_by:
            record = _blocked_record(request, ctx, now)
            return _report([record], dry_run=not request.apply, generated_at=now)
        if not request.apply:
            record = _dry_run_record(request, ctx, now)
            return _report([record], dry_run=True, generated_at=now)
        record = _apply_record(_ApplyMutationInput(self.manager, request, ctx, now))
        return _report([record], dry_run=False, generated_at=now)


# LLM: _load_apply_context performs every preflight check before mutation can happen.
# 函数用途: 读取 coordinator/leader/children，并检查 root、leader 健康、直接孩子关系和容量。
def _load_apply_context(manager: Any, request: SubAgentLeadershipRecoveryApplyOptions) -> _ApplyContext:
    blocked: list[str] = []
    coordinator = _try_load(manager, request.coordinator_id, blocked, "coordinator_missing")
    leader = _try_load(manager, request.leader_id, blocked, "leader_missing")
    children = _load_children(manager, request.child_ids, blocked)
    if coordinator and leader:
        _validate_scope(_ScopeValidationInput(request, coordinator, leader, blocked))
        _validate_leader(leader, request, blocked)
    if coordinator:
        _validate_children(coordinator, children, request.child_ids, blocked)
    return _ApplyContext(
        coordinator=coordinator or _empty_task(request.coordinator_id),
        leader=leader or _empty_task(request.leader_id),
        children=children,
        blocked_by=blocked,
    )


# LLM: _try_load records a stable blocker instead of raising from CLI flows.
# 函数用途: 尝试读取任务；缺失时把阻断原因写入 blocked_by。
def _try_load(manager: Any, run_id: str, blocked: list[str], missing_prefix: str):
    if not run_id:
        blocked.append(f"{missing_prefix}:")
        return None
    try:
        return manager.load(run_id)
    except FileNotFoundError:
        blocked.append(f"{missing_prefix}:{run_id}")
        return None


# LLM: _load_children preserves requested child order and reports every missing child.
# 函数用途: 按请求顺序读取 child 任务，确保移动顺序和报告顺序可预测。
def _load_children(manager: Any, child_ids: list[str], blocked: list[str]) -> list[SubAgentTask]:
    children: list[SubAgentTask] = []
    seen: set[str] = set()
    for child_id in child_ids:
        if not child_id or child_id in seen:
            continue
        seen.add(child_id)
        try:
            children.append(manager.load(child_id))
        except FileNotFoundError:
            blocked.append(f"child_missing:{child_id}")
    if not children:
        blocked.append("no_child_ids")
    return children


# LLM: _validate_scope prevents cross-root hierarchy mutation.
# 函数用途: 校验 coordinator、leader 和请求 root 是否一致，避免 shared workspace 串树。
def _validate_scope(data: _ScopeValidationInput) -> None:
    expected_root = data.request.root_id or data.coordinator.root_id or data.coordinator.id
    if (data.coordinator.root_id or data.coordinator.id) != expected_root:
        data.blocked.append(f"coordinator_root_mismatch:{data.coordinator.id}")
    if (data.leader.root_id or data.leader.id) != expected_root:
        data.blocked.append(f"leader_root_mismatch:{data.leader.id}")


# LLM: _validate_leader rejects unhealthy leaders and optional direct-child capacity overflow.
# 函数用途: 校验接管者是否可用，并在用户传容量上限时阻断过载接管。
def _validate_leader(
    leader: SubAgentTask,
    request: SubAgentLeadershipRecoveryApplyOptions,
    blocked: list[str],
) -> None:
    if not _leader_is_available(leader):
        blocked.append(f"leader_unavailable:{leader.id}")
    remaining = _remaining_capacity(leader, int(request.max_children_per_leader or 0))
    if request.max_children_per_leader and remaining < len(set(request.child_ids)):
        blocked.append(f"leader_capacity_exceeded:{leader.id}")


# LLM: _validate_children guarantees this apply only moves direct children of the named coordinator.
# 函数用途: 阻断旧计划、重复计划或错误 child id 导致的半树乱搬。
def _validate_children(
    coordinator: SubAgentTask,
    children: list[SubAgentTask],
    requested_child_ids: list[str],
    blocked: list[str],
) -> None:
    direct = set(coordinator.child_ids)
    loaded = {child.id for child in children}
    for child_id in requested_child_ids:
        if child_id and child_id not in direct:
            blocked.append(f"child_not_direct:{child_id}")
    for child in children:
        if child.id in direct and child.parent_id != coordinator.id:
            blocked.append(f"child_parent_mismatch:{child.id}")
    if not loaded.issubset(direct):
        missing = sorted(loaded - direct)
        blocked.extend(f"child_not_direct:{child_id}" for child_id in missing)


# LLM: _apply_record performs the only mutating path after validation succeeds.
# 函数用途: 重挂 child 子树、更新 leader/coordinator child_ids，并在旧 coordinator 清空后标记接管完成。
def _apply_record(data: _ApplyMutationInput) -> LeadershipRecoveryApplyRecord:
    moved = _rehang_children(data.manager, data.ctx.children, data.ctx.leader, data.now)
    leader = data.manager.load(data.ctx.leader.id)
    leader.child_ids = _unique_refs([*leader.child_ids, *moved])
    leader.updated_at = data.now
    data.manager.save_hierarchy_links(leader)
    coordinator = data.manager.load(data.ctx.coordinator.id)
    before_remaining = list(coordinator.child_ids)
    coordinator.child_ids = [child_id for child_id in coordinator.child_ids if child_id not in set(moved)]
    coordinator.updated_at = data.now
    if coordinator.child_ids:
        data.manager.save_hierarchy_links(coordinator)
    else:
        data.manager.record_takeover(
            coordinator.id,
            take_over_by=leader.id,
            reason="leadership_recovery_apply_all_children_moved",
            locked_files=[],
        )
        coordinator = data.manager.load(coordinator.id)
        coordinator.child_ids = []
        data.manager.save_hierarchy_links(coordinator)
    return LeadershipRecoveryApplyRecord(
        coordinator_id=coordinator.id,
        leader_id=leader.id,
        requested_child_ids=_unique_refs(data.request.child_ids),
        moved_child_ids=moved,
        dry_run=False,
        applied=True,
        ok=True,
        message=f"moved {len(moved)} child subtrees to {leader.id}",
        before_remaining_child_ids=before_remaining,
        after_remaining_child_ids=list(coordinator.child_ids),
        evidence_paths=_evidence_paths(data.manager, coordinator.id, leader.id, moved),
        created_at=data.now,
    )


# LLM: _rehang_children updates direct children and recursively fixes descendant depths.
# 函数用途: 把指定 child 子树移动到 leader 下，并保持孙级/更深层 depth 连贯。
def _rehang_children(manager: Any, children: list[SubAgentTask], leader: SubAgentTask, now: float) -> list[str]:
    moved: list[str] = []
    for child in children:
        child.parent_id = leader.id
        child.supervisor = leader.id
        child.final_owner = leader.id
        child.depth = max(0, int(leader.depth or 0) + 1)
        child.updated_at = now
        manager.save(child)
        _refresh_descendant_depths(manager, child, now)
        moved.append(child.id)
    return moved


# LLM: _refresh_descendant_depths keeps grandchildren consistent after a subtree is reparented.
# 函数用途: 父节点 depth 改变后，递归刷新所有后代 depth，不改变后代 parent_id。
def _refresh_descendant_depths(manager: Any, parent: SubAgentTask, now: float) -> None:
    for child_id in parent.child_ids:
        try:
            child = manager.load(child_id)
        except FileNotFoundError:
            continue
        child.depth = max(0, int(parent.depth or 0) + 1)
        child.updated_at = now
        manager.save(child)
        _refresh_descendant_depths(manager, child, now)


# LLM: _blocked_record returns a non-mutating audit record for failed preflight.
# 函数用途: 构造阻断报告，确保 CLI 和测试能看到具体失败原因。
def _blocked_record(
    request: SubAgentLeadershipRecoveryApplyOptions,
    ctx: _ApplyContext,
    now: float,
) -> LeadershipRecoveryApplyRecord:
    return LeadershipRecoveryApplyRecord(
        coordinator_id=request.coordinator_id,
        leader_id=request.leader_id,
        requested_child_ids=_unique_refs(request.child_ids),
        moved_child_ids=[],
        dry_run=not request.apply,
        applied=False,
        ok=False,
        message="blocked by preflight checks",
        blocked_by=_unique_refs(ctx.blocked_by),
        before_remaining_child_ids=list(ctx.coordinator.child_ids),
        after_remaining_child_ids=list(ctx.coordinator.child_ids),
        created_at=now,
    )


# LLM: _dry_run_record reports what would move without touching task files.
# 函数用途: 构造 dry-run 成功记录，用于命令预览和调度 refs-only 展示。
def _dry_run_record(
    request: SubAgentLeadershipRecoveryApplyOptions,
    ctx: _ApplyContext,
    now: float,
) -> LeadershipRecoveryApplyRecord:
    return LeadershipRecoveryApplyRecord(
        coordinator_id=ctx.coordinator.id,
        leader_id=ctx.leader.id,
        requested_child_ids=_unique_refs(request.child_ids),
        moved_child_ids=[],
        dry_run=True,
        applied=False,
        ok=True,
        message=f"would move {len(ctx.children)} child subtrees to {ctx.leader.id}",
        before_remaining_child_ids=list(ctx.coordinator.child_ids),
        after_remaining_child_ids=list(ctx.coordinator.child_ids),
        evidence_paths=_evidence_paths(None, ctx.coordinator.id, ctx.leader.id, [child.id for child in ctx.children]),
        created_at=now,
    )


# LLM: _report builds stable summary counters for one-record apply reports.
# 函数用途: 汇总 apply 记录，让 CLI、dispatch 和测试读取统一字段。
def _report(
    records: list[LeadershipRecoveryApplyRecord],
    *,
    dry_run: bool,
    generated_at: float,
) -> LeadershipRecoveryApplyReport:
    return LeadershipRecoveryApplyReport(
        generated_at=generated_at,
        dry_run=dry_run,
        summary={
            "total": len(records),
            "ok": sum(1 for record in records if record.ok),
            "applied": sum(1 for record in records if record.applied),
            "moved_children": sum(len(record.moved_child_ids) for record in records),
        },
        records=records,
    )


# LLM: _evidence_paths resolves task dirs when a manager is available and otherwise returns run IDs.
# 函数用途: 给审计记录提供轻量 refs，不读取任何 artifact 正文。
def _evidence_paths(manager: Any | None, coordinator_id: str, leader_id: str, child_ids: list[str]) -> list[str]:
    refs = [coordinator_id, leader_id, *child_ids]
    if manager is None:
        return refs
    resolved: list[str] = []
    for run_id in refs:
        try:
            resolved.append(manager.load(run_id).task_dir)
        except FileNotFoundError:
            resolved.append(run_id)
    return resolved


# LLM: _empty_task gives blocked records stable fields when a load fails.
# 函数用途: 构造仅含 id 的占位任务，避免错误路径继续抛异常。
def _empty_task(run_id: str) -> SubAgentTask:
    return SubAgentTask(id=run_id, goal="", thought="", plan=[])


# LLM: _unique_refs preserves order while removing blanks and duplicates.
# 函数用途: 统一清理 run id 列表，保证报告和 child_ids 不重复。
def _unique_refs(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
