from __future__ import annotations

"""Model-callable lifecycle tools for the current persistent thread goal.

LLM: These tools may affect only the goal bound to the current structured thread/task context. Text in the
prompt cannot select another goal, and only complete/blocked are model-writable terminal statuses.

模块用途: 给持续目标运行轮提供只读查询和终态更新能力，让自动续跑能够明确完成或确实阻塞。
"""

import json
from typing import TYPE_CHECKING, Any

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: Goal authority comes exclusively from current RunParams task attributes.
# 函数用途: 读取本轮绑定的会话线程和持续任务编号。
def _goal_context(agent: SimpleAgent) -> tuple[str, str]:
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    if not isinstance(attrs, dict):
        return "", ""
    return (
        str(attrs.get("conversation_thread_id") or "").strip(),
        str(attrs.get("conversation_task_id") or "").strip(),
    )


# LLM: Goal tool failures use stable structured codes and never mutate fallback state.
# 函数用途: 生成目标工具统一的结构化失败回执。
def _error(tool: str, message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool,
        False,
        json.dumps({"ok": False, "error": message, "error_code": code}, ensure_ascii=False),
        error_code=code,
    )


# LLM: Read-only projection of the exact current goal; it cannot enumerate another user's goals.
# 类用途: 让持续目标运行轮查询自己的目标内容和状态。
class GetGoalTool(BaseTool):
    # LLM: Tool registration declares read-only effect for the shared tool gateway.
    # 函数用途: 注册 get_goal 的说明和参数合同。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = ToolSpec(
            name="get_goal",
            category="goal",
            description="读取当前持续目标、状态和续跑次数。只适用于 /goal 启动的任务。",
            use_cases=["持续目标每轮开始时确认目标与状态"],
            avoid_when=["普通聊天或普通一次性任务"],
            keywords=["goal", "持续目标", "状态"],
            parameters={},
            effect="read_only",
        )

    # LLM: Exact task-id matching is mandatory before returning any goal payload.
    # 函数用途: 读取并返回本轮持续目标；上下文不匹配时明确拒绝。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        del params
        thread_id, task_id = _goal_context(self.agent)
        if not thread_id or not task_id:
            return _error("get_goal", "当前运行不是持续目标。", "GOAL_CONTEXT_REQUIRED")
        goal = self.agent.conversation_store.load_goal(thread_id)
        if goal is None or goal.task_id != task_id:
            return _error("get_goal", "当前持续目标不存在或已切换。", "GOAL_NOT_FOUND")
        return ToolExecutionResult(
            "get_goal", True, json.dumps(goal.to_dict(), ensure_ascii=False, indent=2)
        )


# LLM: Mutates only terminal complete/blocked state under the goal transition guard.
# 类用途: 让持续目标在真正结束或确实无法继续时写入终态。
class UpdateGoalTool(BaseTool):
    # LLM: The mutating effect and idempotency flag must stay aligned with the tool manifest gate.
    # 函数用途: 注册 update_goal 的终态参数合同和副作用级别。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = ToolSpec(
            name="update_goal",
            category="goal",
            description="仅在持续目标真正完成或确实无法继续时，更新其终态。",
            use_cases=["目标全部完成后标记 complete", "目标无法自主推进时标记 blocked"],
            avoid_when=["仍可继续工作", "只完成了一个中间步骤", "普通任务"],
            keywords=["goal", "完成", "阻塞"],
            parameters={"status": "complete 或 blocked"},
            parameter_schema={"status": {"type": "string", "enum": ["complete", "blocked"]}},
            required_parameters=["status"],
            effect="mutating",
            requires_idempotency=True,
        )

    # LLM: CAS current active goal, then synchronize task link and registry; never revive non-active state.
    # 函数用途: 校验本轮身份后完成或阻塞当前持续目标，并同步任务账本。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        status = str(params.get("status") or "").strip().lower()
        if status not in {"complete", "blocked"}:
            return _error(
                "update_goal", "status 只能是 complete 或 blocked。", "TOOL_INVALID_ARGUMENTS"
            )
        thread_id, task_id = _goal_context(self.agent)
        if not thread_id or not task_id:
            return _error("update_goal", "当前运行不是持续目标。", "GOAL_CONTEXT_REQUIRED")
        store = self.agent.conversation_store
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id)
            if goal is None or goal.task_id != task_id:
                return _error("update_goal", "当前持续目标不存在或已切换。", "GOAL_NOT_FOUND")
            if goal.status == status:
                return ToolExecutionResult(
                    "update_goal", True, json.dumps(goal.to_dict(), ensure_ascii=False, indent=2)
                )
            if goal.status != "active":
                return _error(
                    "update_goal", f"当前目标状态为 {goal.status}，不能由本轮覆盖。", "GOAL_STATE_CONFLICT"
                )
            updated = store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "expected_status": "active",
                }
            )
            if updated is None:
                return _error("update_goal", "目标状态刚刚发生变化。", "GOAL_STATE_CONFLICT")
            task_status = "completed" if status == "complete" else "interrupted"
            store.update_task_status(
                {"task_id": task_id, "status": task_status, "expected_status": "active"}
            )
            registry_status = "done" if status == "complete" else "blocked"
            self.agent.local_store.task_registry.register_task(
                task_id,
                status=registry_status,
                goal=updated.objective,
            )
        return ToolExecutionResult(
            "update_goal", True, json.dumps(updated.to_dict(), ensure_ascii=False, indent=2)
        )


__all__ = ["GetGoalTool", "UpdateGoalTool"]
