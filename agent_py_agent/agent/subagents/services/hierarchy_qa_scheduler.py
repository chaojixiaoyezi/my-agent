# LLM: QA scheduler augmentation keeps role auto-fill out of the core hierarchy scheduler.
# 模块用途: 父任务明确要求 tester/bug_finder 时，计算需要补派的 QA child 规格。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentTask
from .hierarchy_qa_ready_refs import (
    flatten_ready_ref_values,
    has_ready_implementation_child,
    ready_implementation_refs,
)
from .hierarchy_write_policy import inherited_extra_write_roots
from .qa_role_contract import qa_role_identity_roles, qa_roles_required_by_task

_QA_SCAN_MAX_NODES = 64


# LLM: RequiredQaChildSpec is a scheduler-neutral shape for LLM-suggested QA child tasks.
# 类用途: 保存候选 QA 子任务所需字段，作为 quality_advice 输出给模型参考。
@dataclass(frozen=True)
class RequiredQaChildSpec:
    goal: str
    agent_name: str
    role: str
    acceptance_checks: list[str] = field(default_factory=list)
    source_run_ids: list[str] = field(default_factory=list)
    source_artifact_refs: list[str] = field(default_factory=list)
    source_output_refs: list[str] = field(default_factory=list)
    quality_scope: str = "ready_work_refs"


# LLM: QaOrchestrationAdvice is a refs-only decision packet for the model, not an execution order.
# 类用途: 告诉 LLM 当前 QA/验收/返修的可选方向和红线，让模型自己按上下文选择下一步。
@dataclass(frozen=True)
class QaOrchestrationAdvice:
    phase: str
    llm_next_step: str
    guardrails: list[str] = field(default_factory=list)
    suggested_roles: list[str] = field(default_factory=list)
    suggested_children: list[RequiredQaChildSpec] = field(default_factory=list)
    ready_work_refs: list[dict[str, object]] = field(default_factory=list)


# LLM: qa_orchestration_advice exposes quality planning facts without creating child runs.
# 函数用途: 根据父任务合同、现有 QA 子任务和实现进度生成给 LLM 的 QA 决策包；不自动补派任何子代理。
def qa_orchestration_advice(
    *,
    manager: Any,
    parent: SubAgentTask,
    specs: list[Any],
) -> QaOrchestrationAdvice | None:
    missing = _missing_required_qa_roles(manager, parent, specs)
    if not missing:
        return None
    if _qa_autofill_deferred_until_implementation_ready(manager, parent):
        return QaOrchestrationAdvice(
            phase="implementation_first",
            llm_next_step=(
                "先创建或继续 dispatch worker/writer/leaf_worker，等至少一个实现节点有可测试产物后，"
                "再由 LLM 选择局部 QA、整体检查或 repair 组合。"
            ),
            guardrails=_quality_guardrails(),
            suggested_roles=missing,
        )
    ready_refs = ready_implementation_refs(manager, parent)
    return QaOrchestrationAdvice(
        phase="quality_wave_ready",
        llm_next_step=(
            "根据 ready work refs、依赖组和风险，选择 tester/bug_finder 的数量、scope 和顺序；"
            "系统只校验红线，不固定工作流。"
        ),
        guardrails=_quality_guardrails(),
        suggested_roles=missing,
        suggested_children=[_qa_role_child_spec(parent, role, ready_refs) for role in missing],
        ready_work_refs=ready_refs,
    )


# LLM: _qa_autofill_deferred_until_implementation_ready prevents empty-build QA children.
# 函数用途: 有产物根的父任务先等 worker/leaf 有可检查状态，再建议 tester/bug_finder。
def _qa_autofill_deferred_until_implementation_ready(manager: Any, parent: SubAgentTask) -> bool:
    if not inherited_extra_write_roots(parent):
        return False
    return not has_ready_implementation_child(manager, parent)


# LLM: _missing_required_qa_roles compares parent contract, current request, and persisted descendants.
# 函数用途: 计算还需要补派哪些 QA 角色，保证调度器只做缺口修复。
def _missing_required_qa_roles(manager: Any, parent: SubAgentTask, specs: list[Any]) -> list[str]:
    required = qa_roles_required_by_task(parent)
    if not required:
        return []
    present = _qa_roles_from_specs(specs) | _existing_descendant_qa_roles(manager, parent)
    return [role for role in required if role not in present]


# LLM: _qa_roles_from_specs trusts explicit role identity fields instead of broad goal prose.
# 函数用途: 判断本轮请求里是否已经包含 tester 或 bug_finder。
def _qa_roles_from_specs(specs: list[Any]) -> set[str]:
    roles: set[str] = set()
    for spec in specs:
        roles.update(qa_role_identity_roles(role=str(getattr(spec, "role", "")), agent_name=str(getattr(spec, "agent_name", ""))))
    return roles


# LLM: _existing_descendant_qa_roles scans exact child ids so repeated scheduling stays idempotent.
# 函数用途: 沿 parent.child_ids 广度优先读取少量已落盘后代，避免重复补派已存在 QA 角色。
def _existing_descendant_qa_roles(manager: Any, parent: SubAgentTask) -> set[str]:
    roles: set[str] = set()
    queue = [str(item) for item in parent.child_ids if item]
    seen: set[str] = set()
    scanned = 0
    while queue and scanned < _QA_SCAN_MAX_NODES:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        scanned += 1
        child = _load_child_for_qa_scan(manager, run_id)
        if child is None:
            continue
        roles.update(qa_role_identity_roles(role=child.role, agent_name=child.agent_name))
        queue.extend(child_id for child_id in child.child_ids if child_id not in seen)
    return roles


# LLM: _load_child_for_qa_scan keeps auto-scheduling tolerant of missing or stale task refs.
# 函数用途: 读取一个后代 run；失败时返回 None，让调度继续保守执行。
def _load_child_for_qa_scan(manager: Any, run_id: str) -> SubAgentTask | None:
    try:
        return manager.load(run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


# LLM: _qa_role_child_spec builds a self-contained checker task while leaving product writes to workers.
# 函数用途: 生成自动补齐的 tester/bug_finder 子任务，默认只检查证据和写自己的报告 refs。
def _qa_role_child_spec(parent: SubAgentTask, role: str, ready_refs: list[dict[str, object]]) -> RequiredQaChildSpec:
    label = _qa_role_zh(role)
    source_run_ids = [str(item.get("run_id") or "") for item in ready_refs if str(item.get("run_id") or "")]
    artifact_refs = flatten_ready_ref_values(ready_refs, "artifact_refs")
    output_refs = flatten_ready_ref_values(ready_refs, "output_refs")
    return RequiredQaChildSpec(
        goal=(
            f"补齐父任务要求的 {role}（{label}）QA 角色。"
            f"父任务 run_id={parent.id}；优先检查 ready source_run_ids={source_run_ids}，"
            "按这些实现节点的产物 refs、output refs 和证据 refs 做局部/整体 QA；"
            "只写自己的报告/发现 refs，不替 worker 修改业务产物。"
        ),
        agent_name=role,
        role=role,
        acceptance_checks=[
            f"必须以真实 {role} 角色完成独立检查，并记录 evidence_refs/findings_refs。",
            "不得自称替其它 QA 角色完成工作；不得直接修业务产物。",
        ],
        source_run_ids=source_run_ids,
        source_artifact_refs=artifact_refs,
        source_output_refs=output_refs,
    )

# LLM: _qa_role_zh keeps generated auto-QA goals readable for Chinese users and logs.
# 函数用途: 返回 QA 角色中文名；未知角色保留英文，方便后续扩展自定义质量角色。
def _qa_role_zh(role: str) -> str:
    return {
        "tester": "测试子代理",
        "bug_finder": "找茬子代理",
    }.get(role, role)


# LLM: _quality_guardrails keeps system behavior as boundaries rather than a rigid workflow.
# 函数用途: 返回给模型看的 QA 红线：挡明显不成立的动作，具体流程留给 LLM 和 workflow。
def _quality_guardrails() -> list[str]:
    return [
        "没有可测试产物或 ready work refs 时，不要创建/执行 QA。",
        "QA 失败、QA 工具失败、产品失败必须分开记录。",
        "QA 通过只表示对应 scope 已被检查，不代表整个任务自动完成。",
        "repair 必须基于失败 refs，不能覆盖无关产物。",
    ]
