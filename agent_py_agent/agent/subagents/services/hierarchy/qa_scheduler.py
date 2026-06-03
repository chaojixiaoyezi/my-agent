
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...models import SubAgentTask
from ..qa_role_contract import qa_role_identity_roles, qa_roles_required_by_task
from .qa_ready_refs import (
    flatten_ready_ref_values,
    has_ready_implementation_child,
    ready_implementation_refs,
)
from .write_policy import inherited_extra_write_roots

_QA_SCAN_MAX_NODES = 64


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


@dataclass(frozen=True)
class QaOrchestrationAdvice:
    phase: str
    llm_next_step: str
    guardrails: list[str] = field(default_factory=list)
    suggested_roles: list[str] = field(default_factory=list)
    suggested_children: list[RequiredQaChildSpec] = field(default_factory=list)
    ready_work_refs: list[dict[str, object]] = field(default_factory=list)


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


def _qa_autofill_deferred_until_implementation_ready(manager: Any, parent: SubAgentTask) -> bool:
    if not inherited_extra_write_roots(parent):
        return False
    return not has_ready_implementation_child(manager, parent)


def _missing_required_qa_roles(manager: Any, parent: SubAgentTask, specs: list[Any]) -> list[str]:
    required = qa_roles_required_by_task(parent)
    if not required:
        return []
    present = _qa_roles_from_specs(specs) | _existing_descendant_qa_roles(manager, parent)
    return [role for role in required if role not in present]


def _qa_roles_from_specs(specs: list[Any]) -> set[str]:
    roles: set[str] = set()
    for spec in specs:
        roles.update(qa_role_identity_roles(role=str(getattr(spec, "role", "")), agent_name=str(getattr(spec, "agent_name", ""))))
    return roles


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


def _load_child_for_qa_scan(manager: Any, run_id: str) -> SubAgentTask | None:
    try:
        return manager.load(run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


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

def _qa_role_zh(role: str) -> str:
    return {
        "tester": "测试子代理",
        "bug_finder": "找茬子代理",
    }.get(role, role)


def _quality_guardrails() -> list[str]:
    return [
        "没有可测试产物或 ready work refs 时，不要创建/执行 QA。",
        "QA 失败、QA 工具失败、产品失败必须分开记录。",
        "QA 通过只表示对应 scope 已被检查，不代表整个任务自动完成。",
        "repair 必须基于失败 refs，不能覆盖无关产物。",
    ]
