# LLM: QA scheduler augmentation keeps role auto-fill out of the core hierarchy scheduler.
# 模块用途: 父任务明确要求 tester/bug_finder/acceptor 时，计算需要补派的 QA child 规格。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentTask
from .qa_role_contract import qa_role_identity_roles, qa_roles_required_by_task

_QA_SCAN_MAX_NODES = 64


# LLM: RequiredQaChildSpec is a scheduler-neutral shape for auto-created QA child tasks.
# 类用途: 保存自动补派 QA 子任务所需字段，避免 helper 反向 import hierarchy scheduler dataclass。
@dataclass(frozen=True)
class RequiredQaChildSpec:
    goal: str
    agent_name: str
    role: str
    acceptance_checks: list[str] = field(default_factory=list)


# LLM: required_qa_child_specs computes missing QA children without mutating the parent or request.
# 函数用途: 对比父任务合同、本轮 child specs 和已存在后代，返回还需要自动补派的 QA 子任务。
def required_qa_child_specs(
    *,
    manager: Any,
    parent: SubAgentTask,
    specs: list[Any],
) -> list[RequiredQaChildSpec]:
    return [_qa_role_child_spec(parent, role) for role in _missing_required_qa_roles(manager, parent, specs)]


# LLM: _missing_required_qa_roles compares parent contract, current request, and persisted descendants.
# 函数用途: 计算还需要补派哪些 QA 角色，保证调度器只做缺口修复。
def _missing_required_qa_roles(manager: Any, parent: SubAgentTask, specs: list[Any]) -> list[str]:
    required = qa_roles_required_by_task(parent)
    if not required:
        return []
    present = _qa_roles_from_specs(specs) | _existing_descendant_qa_roles(manager, parent)
    return [role for role in required if role not in present]


# LLM: _qa_roles_from_specs trusts explicit role identity fields instead of broad goal prose.
# 函数用途: 判断本轮请求里是否已经包含 tester、bug_finder 或 acceptor。
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
# 函数用途: 生成自动补齐的 tester/bug_finder/acceptor 子任务，默认只检查证据和写自己的报告 refs。
def _qa_role_child_spec(parent: SubAgentTask, role: str) -> RequiredQaChildSpec:
    label = _qa_role_zh(role)
    return RequiredQaChildSpec(
        goal=(
            f"补齐父任务要求的 {role}（{label}）QA 角色。"
            f"父任务 run_id={parent.id}；检查当前父任务、已创建下级、证据 refs 和产物 refs；"
            "只写自己的报告/发现/验收 refs，不替 worker 修改业务产物。"
        ),
        agent_name=role,
        role=role,
        acceptance_checks=[
            f"必须以真实 {role} 角色完成独立检查，并记录 evidence_refs/findings_refs。",
            "不得自称替其它 QA 角色完成工作；不得直接修业务产物。",
        ],
    )


# LLM: _qa_role_zh keeps generated auto-QA goals readable for Chinese users and logs.
# 函数用途: 返回 QA 角色中文名；未知角色保留英文，方便后续扩展自定义质量角色。
def _qa_role_zh(role: str) -> str:
    return {
        "tester": "测试子代理",
        "bug_finder": "找茬子代理",
        "acceptor": "验收子代理",
    }.get(role, role)
