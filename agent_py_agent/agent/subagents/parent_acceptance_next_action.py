# LLM: Parent acceptance next-action mapper; keep recommendations refs-only and non-mutating.
# 模块用途: 把父级验收决策或 apply 审计转成下一步显式动作建议，供上级代理/调度器读取。
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from .models import SubAgentTask
from .parent_acceptance_controller import build_parent_acceptance_decision


# LLM: ParentAcceptanceNextAction is advisory, not an executor.
# 类用途: 记录父级下一动作建议和相关审计引用；它本身不执行命令、不改变 task 状态。
@dataclass(frozen=True)
class ParentAcceptanceNextAction:
    """Advisory next action for parent-controlled acceptance."""

    __test__: ClassVar[bool] = False

    run_id: str
    action: str
    reason: str
    decision: str = ""
    command: str = ""
    decision_ref: str = ""
    apply_ref: str = ""
    test_execution_ref: str = ""
    failure_handoff_ref: str = ""
    takeover_readiness_ref: str = ""
    requires_human_confirmation: bool = False
    mutates_task_state: bool = False

    # LLM: to_dict keeps CLI JSON and future scheduler reads stable.
    # 函数用途: 把下一动作建议转成 JSON 字典；不展开引用文件正文。
    def to_dict(self) -> dict:
        return asdict(self)


# LLM: build_parent_acceptance_next_action only maps facts to a recommendation.
# 函数用途: 根据当前父级验收决策和已有审计文件生成下一步建议；不执行 tests、不 rescue、不写状态。
def build_parent_acceptance_next_action(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
) -> ParentAcceptanceNextAction:
    decision = build_parent_acceptance_decision(task, workspace_root=workspace_root)
    if task.status == "DONE" and task.verification_status == "VERIFIED":
        return _next_action(task, decision, {"action": "none", "reason": "task already accepted"})
    if decision.decision == "execute_tests":
        return _run_tests_next_action(task, decision)
    if decision.decision == "request_human":
        return _human_next_action(task, decision)
    if decision.decision == "rescue":
        return _rescue_next_action(task, decision)
    return _apply_next_action(task, decision)


# LLM: _run_tests_next_action recommends explicit test execution without starting it.
# 函数用途: 为 execute_tests 决策生成上级可执行但当前不执行的 tests 命令建议。
def _run_tests_next_action(task: SubAgentTask, decision) -> ParentAcceptanceNextAction:
    return _next_action(
        task,
        decision,
        {"action": "run_tests", "reason": "blocked apply requires explicit test execution"},
        {"command": f"subagents-tests {task.id} --re-run"},
    )


# LLM: _human_next_action preserves the human-confirmation stop.
# 函数用途: 为危险命令或安全边界不明确的决策生成请求人工确认建议。
def _human_next_action(task: SubAgentTask, decision) -> ParentAcceptanceNextAction:
    return _next_action(
        task,
        decision,
        {"action": "request_human_confirmation", "reason": decision.reason},
        {"requires_human_confirmation": True},
    )


# LLM: _rescue_next_action keeps rescue advisory refs visible without taking over.
# 函数用途: 为失败/阻塞决策生成救援规划建议，并携带 handoff/readiness refs。
def _rescue_next_action(task: SubAgentTask, decision) -> ParentAcceptanceNextAction:
    return _next_action(
        task,
        decision,
        {"action": "plan_rescue", "reason": decision.reason},
        {
            "failure_handoff_ref": decision.failure_handoff_ref,
            "takeover_readiness_ref": decision.takeover_readiness_ref,
        },
    )


# LLM: _apply_next_action points to the explicit low-risk apply bridge.
# 函数用途: 为 inspect_only 决策生成显式 apply 建议；只是建议，不在本函数内写回状态。
def _apply_next_action(task: SubAgentTask, decision) -> ParentAcceptanceNextAction:
    return _next_action(
        task,
        decision,
        {"action": "apply_acceptance", "reason": "inspect_only decision can be explicitly applied"},
        {
            "command": f"subagents-acceptance-plan {task.id} --apply",
            "mutates_task_state": True,
            "test_execution_ref": decision.test_execution_ref,
        },
    )


# LLM: _next_action centralizes the stable advisory record shape.
# 函数用途: 构造下一动作建议并补齐决策、审计引用和安全边界字段。
def _next_action(
    task: SubAgentTask,
    decision,
    base: dict[str, object],
    overrides: dict[str, object] | None = None,
) -> ParentAcceptanceNextAction:
    refs = _audit_refs(task)
    payload = {
        "run_id": task.id,
        "decision": decision.decision,
        "decision_ref": refs["decision_ref"],
        "apply_ref": refs["apply_ref"],
    }
    payload.update(base)
    payload.update(overrides or {})
    return ParentAcceptanceNextAction(**payload)


# LLM: _audit_refs reports only existing audit file paths.
# 函数用途: 收集父级验收决策和 apply 审计引用；不存在时返回空字符串，避免误导调用方。
def _audit_refs(task: SubAgentTask) -> dict[str, str]:
    decision_path = Path(task.reports_dir) / "parent_acceptance_decision.json"
    apply_path = Path(task.reports_dir) / "parent_acceptance_apply.json"
    return {
        "decision_ref": str(decision_path) if decision_path.exists() else "",
        "apply_ref": str(apply_path) if apply_path.exists() else "",
    }
