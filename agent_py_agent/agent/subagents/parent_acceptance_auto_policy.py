# LLM: Parent acceptance auto-policy gate; dry-run only, no command execution.
# 模块用途: 把 next-action 映射成自动策略判断和审计文件，为后续自动执行层预留安全闸门。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .models import SubAgentTask
from .parent_acceptance_next_action import (
    ParentAcceptanceNextAction,
    build_parent_acceptance_next_action,
)


# LLM: ParentAcceptanceAutoPolicy is an advisory policy decision, not an executor.
# 类用途: 记录父级验收自动策略 dry-run 结果；它不会执行命令，也不会改变 task 状态。
@dataclass(frozen=True)
class ParentAcceptanceAutoPolicy:
    """Dry-run auto-policy decision for a parent acceptance next action."""

    __test__: ClassVar[bool] = False

    run_id: str
    action: str
    decision: str
    reason: str
    command: str = ""
    dry_run: bool = True
    would_execute: bool = False
    executed: bool = False
    execution_mode: str = "manual_only"
    automatic_execution_allowed: bool = False
    recommended_command: str = ""
    mutates_task_state: bool = False
    requires_human_confirmation: bool = False
    next_action_ref: str = ""
    decision_ref: str = ""
    apply_ref: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps policy JSON stable for CLI and later schedulers.
    # 函数用途: 转换自动策略结果为 JSON 友好字典；不展开引用文件正文。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: build_parent_acceptance_auto_policy evaluates whether an action could be automated later.
# 函数用途: 生成 dry-run 自动策略判断并写审计文件；当前不执行建议命令、不修改任务状态。
def build_parent_acceptance_auto_policy(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
    allowed_actions: set[str] | None = None,
) -> ParentAcceptanceAutoPolicy:
    action = build_parent_acceptance_next_action(task, workspace_root=workspace_root)
    allowed = allowed_actions or {"run_tests"}
    policy = _policy_for_action(task, action, allowed)
    write_parent_acceptance_auto_policy_file(task, policy)
    return policy


# LLM: write_parent_acceptance_auto_policy_file persists dry-run policy audit only.
# 函数用途: 写入 `parent_acceptance_auto_policy.json`；文件只保存策略、refs 和预留字段。
def write_parent_acceptance_auto_policy_file(
    task: SubAgentTask,
    policy: ParentAcceptanceAutoPolicy,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    payload = {
        "schema": "parent_acceptance_auto_policy.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "dry_run": True,
        "run_id": task.id,
        "policy": policy.to_dict(),
        "reserved": {
            "refs_only": True,
            "executes_command": False,
            "mutates_task_state": False,
            "future_apply_supported": True,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _policy_for_action keeps the first policy slice conservative.
# 函数用途: 把 next-action 转成 allow/blocked；当前只允许 run_tests 进入未来自动处理候选。
def _policy_for_action(
    task: SubAgentTask,
    action: ParentAcceptanceNextAction,
    allowed_actions: set[str],
) -> ParentAcceptanceAutoPolicy:
    is_allowed = action.action in allowed_actions and not action.requires_human_confirmation
    decision = "allow" if is_allowed else "blocked"
    reason = "action is in auto-policy allowlist" if is_allowed else _blocked_reason(action)
    recommended_command = action.command if is_allowed and action.command else ""
    return ParentAcceptanceAutoPolicy(
        run_id=task.id,
        action=action.action,
        decision=decision,
        reason=reason,
        command=action.command,
        would_execute=is_allowed and bool(action.command),
        executed=False,
        execution_mode="manual_only",
        automatic_execution_allowed=False,
        recommended_command=recommended_command,
        mutates_task_state=False,
        requires_human_confirmation=action.requires_human_confirmation,
        next_action_ref="inline",
        decision_ref=action.decision_ref,
        apply_ref=action.apply_ref,
        reserved={
            "allowed_actions": sorted(allowed_actions),
            "semi_auto_plan": _semi_auto_plan(is_allowed=is_allowed, recommended_command=recommended_command),
            "source_mutates_task_state": action.mutates_task_state,
            "source_action_reason": action.reason,
        },
    )


# LLM: _semi_auto_plan makes the future executor contract explicit while still denying execution.
# 函数用途: 生成半自动计划预留字段；只记录人工可执行命令和硬边界，不触发任何执行。
def _semi_auto_plan(*, is_allowed: bool, recommended_command: str) -> dict[str, Any]:
    return {
        "stage": "ready_for_manual_confirmation" if is_allowed and recommended_command else "blocked",
        "execution_mode": "manual_only",
        "automatic_execution_allowed": False,
        "recommended_command": recommended_command,
        "requires_manual_confirmation": True,
        "safety_boundaries": [
            "dry_run_only",
            "no_process_execution",
            "no_task_state_mutation",
        ],
    }


# LLM: _blocked_reason explains why policy stops without consulting external state.
# 函数用途: 给 human/rescue/apply 等不自动化的动作生成可审计阻断原因。
def _blocked_reason(action: ParentAcceptanceNextAction) -> str:
    if action.requires_human_confirmation:
        return "action requires human confirmation"
    if action.action == "plan_rescue":
        return "rescue planning is not automated in v1"
    if action.mutates_task_state:
        return "state-mutating action is not automated in v1"
    return "action is not in auto-policy allowlist"
