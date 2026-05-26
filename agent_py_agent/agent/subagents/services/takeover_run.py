# LLM: Takeover run service creates one replacement runner without losing source task refs.
# 模块用途: 原子化创建接管 run、记录旧 run 被接管，并防止同一失败任务无限扩容。

from __future__ import annotations

"""Create idempotent takeover runs for dead subagent tasks."""

from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentTask
from .takeover_refs import (
    default_takeover_plan,
    source_refs,
    structured_payload,
    takeover_agent_name,
    takeover_attributes,
    takeover_chain_depth,
    takeover_chain_limit,
    takeover_lineage_root,
    takeover_write_roots,
    unique_strings,
    unique_structured,
)


# LLM: TakeoverRunRequest bundles source run and creation overrides for future tool/CLI use.
# 类用途: 描述要接管哪个 run、为什么接管，以及可选的新 agent 名/角色/计划。
@dataclass(frozen=True)
class TakeoverRunRequest:
    source_run_id: str
    reason: str
    agent_name: str = ""
    role: str = ""
    plan: list[str] = field(default_factory=list)


# LLM: TakeoverRunResult is a compact audit result for tests, CLI, and parent dispatch payloads.
# 类用途: 返回旧 run、新接管 run、是否新建、接管 refs 和消息，不展开 artifacts 正文。
@dataclass(frozen=True)
class TakeoverRunResult:
    source_run_id: str
    takeover_run_id: str
    created: bool
    applied: bool
    message: str
    takeover_refs: dict[str, str] = field(default_factory=dict)

    # LLM: to_dict preserves a JSON-friendly response shape for future orchestration tools.
    # 函数用途: 把接管结果转成稳定字典，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, object]:
        return {
            "source_run_id": self.source_run_id,
            "takeover_run_id": self.takeover_run_id,
            "created": self.created,
            "applied": self.applied,
            "message": self.message,
            "takeover_refs": dict(self.takeover_refs),
        }


# LLM: SubAgentTakeoverRunService owns replacement creation and source takeover marking.
# 类用途: 集中处理接管 run 创建、refs 继承、幂等检查和旧 run 状态记录。
class SubAgentTakeoverRunService:
    """Create one takeover run for a source task, or return the existing one."""

    # LLM: __init__ stores the manager facade used for persistence and hierarchy links.
    # 函数用途: 初始化接管服务依赖；本身不读写任务。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: create creates a same-scope takeover run and then records takeover on the source.
    # 函数用途: 幂等地创建接管 run；如果旧 run 已有 takeover_by，则复用已有 run。
    def create(self, request: TakeoverRunRequest) -> TakeoverRunResult:
        source = self.manager.load(request.source_run_id)
        existing = _existing_takeover(self.manager, source)
        if existing:
            _ensure_existing_takeover_handoff(self.manager, source, existing)
            return _existing_result(source, existing)
        chain_limit = takeover_chain_limit(self.manager)
        if chain_limit > 0 and takeover_chain_depth(source) >= chain_limit:
            return _chain_exhausted_result(self.manager, source, chain_limit)
        takeover = _create_takeover_task(self.manager, source, request)
        self.manager.record_takeover(source.id, take_over_by=takeover.id, reason=request.reason, locked_files=[])
        return TakeoverRunResult(
            source_run_id=source.id,
            takeover_run_id=takeover.id,
            created=True,
            applied=True,
            message=f"created takeover run {takeover.id} for {source.id}",
            takeover_refs=source_refs(source),
        )


# LLM: _existing_takeover prevents repeated recovery from spawning endless replacements.
# 函数用途: 如果 source.takeover_by 指向仍存在的 run，直接复用它。
def _existing_takeover(manager: Any, source: SubAgentTask) -> SubAgentTask | None:
    takeover_id = str(source.takeover_by or "").strip()
    if not takeover_id:
        return _existing_takeover_by_source_ref(manager, source.id)
    try:
        return manager.load(takeover_id)
    except FileNotFoundError:
        return _existing_takeover_by_source_ref(manager, source.id)


# LLM: source-ref scanning makes takeover creation idempotent even if a stale source snapshot lost takeover_by.
# 函数用途: 根据 takeover task attributes 反查已有接管 run，防止旧 runner/旧父级快照导致重复创建接管分支。
def _existing_takeover_by_source_ref(manager: Any, source_run_id: str) -> SubAgentTask | None:
    try:
        tasks = manager.list_runs()
    except Exception:
        return None
    source_id = str(source_run_id or "").strip()
    candidates: list[SubAgentTask] = []
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if str(attrs.get("takeover_source_run_id") or "").strip() != source_id:
            continue
        if str(getattr(task, "status", "") or "").upper() in {"ABANDONED", "TAKEN_OVER"}:
            continue
        candidates.append(task)
    candidates.sort(key=lambda item: item.created_at or item.updated_at or 0.0)
    return candidates[0] if candidates else None


# LLM: _existing_result reports idempotent reuse with the original source refs.
# 函数用途: 生成“已存在接管 run”的稳定返回，不改动任何任务状态。
def _existing_result(source: SubAgentTask, existing: SubAgentTask) -> TakeoverRunResult:
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id=existing.id,
        created=False,
        applied=False,
        message=f"source {source.id} already taken over by {existing.id}",
        takeover_refs=source_refs(source),
    )


# LLM: _ensure_existing_takeover_handoff repairs old replacement runs created before refs inheritance existed.
# 函数用途: 复用已有 takeover run 时补齐源任务的机器交接字段；只补缺失项，不覆盖接管者已有进展。
def _ensure_existing_takeover_handoff(manager: Any, source: SubAgentTask, existing: SubAgentTask) -> None:
    changed = False
    changed |= _merge_context_manifest(existing, source)
    changed |= _merge_context_packs(existing, source)
    changed |= _merge_attribute_handoff(existing, source)
    merged_artifacts = unique_strings([*source.artifact_refs, *existing.artifact_refs, source.agent_run_artifacts_dir])
    if merged_artifacts != existing.artifact_refs:
        existing.artifact_refs = merged_artifacts
        changed = True
    merged_evidence = unique_strings([*source.evidence_refs, *existing.evidence_refs])
    if merged_evidence != existing.evidence_refs:
        existing.evidence_refs = merged_evidence
        changed = True
    merged_roots = unique_strings([*existing.allowed_write_roots, *takeover_write_roots(source)])
    if merged_roots != existing.allowed_write_roots:
        existing.allowed_write_roots = merged_roots
        changed = True
    if changed:
        manager.save(existing)


# LLM: _merge_context_manifest keeps source refs first and preserves any takeover-local additions.
# 函数用途: 旧接管 run 缺 required_read_paths/hint_read_paths/task_pack_refs 时从源 run 补齐。
def _merge_context_manifest(target: SubAgentTask, source: SubAgentTask) -> bool:
    changed = False
    for field_name in ("task_pack_refs", "required_read_paths", "hint_read_paths", "omitted_context"):
        source_values = list(getattr(source.context_manifest, field_name, []) or [])
        target_values = list(getattr(target.context_manifest, field_name, []) or [])
        merged = unique_strings([*source_values, *target_values])
        if merged != target_values:
            setattr(target.context_manifest, field_name, merged)
            changed = True
    for field_name in ("role_pack", "quality_contract_ref"):
        target_value = str(getattr(target.context_manifest, field_name, "") or "").strip()
        source_value = str(getattr(source.context_manifest, field_name, "") or "").strip()
        if not target_value and source_value:
            setattr(target.context_manifest, field_name, source_value)
            changed = True
    if not int(getattr(target.context_manifest, "token_budget", 0) or 0):
        source_budget = int(getattr(source.context_manifest, "token_budget", 0) or 0)
        if source_budget:
            target.context_manifest.token_budget = source_budget
            changed = True
    return changed


# LLM: _merge_context_packs restores refs-only packs such as sibling rosters without duplicating them.
# 函数用途: 接管 run 可以继续看到兄弟清单、依赖包和系统派生合同。
def _merge_context_packs(target: SubAgentTask, source: SubAgentTask) -> bool:
    merged = unique_structured([*source.context_packs, *target.context_packs])
    if merged == target.context_packs:
        return False
    target.context_packs = merged
    return True


# LLM: _merge_attribute_handoff fills source-declared output refs while preserving takeover audit keys.
# 函数用途: output_files/output_refs 等机器产物目标不能在接管时丢失；已有同名字段不被覆盖。
def _merge_attribute_handoff(target: SubAgentTask, source: SubAgentTask) -> bool:
    source_attrs = takeover_attributes(source)
    target_attrs = takeover_attributes(target)
    merged = dict(source_attrs)
    merged.update(target_attrs)
    if merged == target.attributes:
        return False
    target.attributes = merged
    return True


# LLM: _chain_exhausted_result turns repeated takeover timeouts into a visible blocker instead of more children.
# 函数用途: 同一任务连续接管超过上限时停止扩容，标记当前 run 为 BLOCKED 并保留 refs 给父级决策。
def _chain_exhausted_result(manager: Any, source: SubAgentTask, chain_limit: int) -> TakeoverRunResult:
    source.status = "BLOCKED"
    source.failure_type = "takeover_chain_exhausted"
    source.blockers = unique_strings(
        [
            *source.blockers,
            f"takeover chain reached max depth {chain_limit}; escalate or adjust timeout/scope before retry",
        ]
    )
    source.current_step = "takeover chain exhausted; waiting for parent decision"
    source.result = source.result or "连续 takeover 仍无进展，已停止继续创建接管 run。"
    manager.save(source)
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id="",
        created=False,
        applied=False,
        message=f"takeover chain exhausted for {source.id}; max_depth={chain_limit}",
        takeover_refs=source_refs(source),
    )


# LLM: _create_takeover_task creates a sibling replacement with source refs in attributes.
# 函数用途: 新建接管 run，并把旧任务目录/artifacts/checkpoint/packet 作为可写或可读引用带过去。
def _create_takeover_task(manager: Any, source: SubAgentTask, request: TakeoverRunRequest) -> SubAgentTask:
    takeover = manager.create_run(
        goal=f"接管 run {source.id}: {source.goal}",
        thought="旧 runner 已超时或断通道；从 task-local refs 接续，不重新理解任务。",
        plan=request.plan or default_takeover_plan(),
        agent_name=request.agent_name or takeover_agent_name(source),
        role=request.role or source.role or "worker",
        parent_id=source.parent_id,
        root_id=source.root_id or source.id,
        depth=source.depth,
        allowed_skills=list(source.allowed_skills),
        allowed_tools=list(source.allowed_tools),
        acceptance_checks=list(source.acceptance_checks),
        quality_contract=structured_payload(source.quality_contract),
        context_manifest=structured_payload(source.context_manifest),
        context_packs=structured_payload(source.context_packs),
        extra_write_roots=takeover_write_roots(source),
        workflow_mode="off",
        attributes=takeover_attributes(source),
    )
    takeover.attributes["takeover_source_run_id"] = source.id
    takeover.attributes["takeover_source_refs"] = source_refs(source)
    takeover.attributes["takeover_lineage_root_run_id"] = takeover_lineage_root(source)
    takeover.attributes["takeover_chain_depth"] = takeover_chain_depth(source) + 1
    takeover.current_step = "读取 takeover_source_refs.latest_continue_packet 或 checkpoint 后接续原任务。"
    takeover.latest_summary = source.latest_summary
    takeover.artifact_refs = unique_strings([*source.artifact_refs, source.agent_run_artifacts_dir])
    takeover.evidence_refs = list(source.evidence_refs)
    manager.save(takeover)
    return takeover

__all__ = [
    "SubAgentTakeoverRunService",
    "TakeoverRunRequest",
    "TakeoverRunResult",
]
