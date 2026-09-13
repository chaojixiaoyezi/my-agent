# LLM: Goal tools bind to structured current-thread authority. Pre-update rejection is
# side-effect-free; generic create/persistence failures must not acquire replay authority.
# 模块用途: 提供会话目标读写工具；不存在或归属冲突交回模型处理，真实写入异常保留保护。
from __future__ import annotations

"""会话运行时 model tools for one persisted thread goal."""

import json
from typing import TYPE_CHECKING, Any

from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent


def _goal_context(agent: SimpleAgent) -> tuple[str, str, str, dict[str, object] | None]:
    """Return authority from the current structured run, never from prompt text."""
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    if not isinstance(attrs, dict):
        return "", "", "", None
    return (
        str(attrs.get("conversation_thread_id") or "").strip(),
        str(attrs.get("conversation_task_id") or "").strip(),
        str(attrs.get("thread_goal_id") or "").strip(),
        attrs,
    )


# LLM: Only the call site observing a pre-write guard may supply not_started. This
# result crosses ToolOperationCoordinator; do not whitelist codes or parse the message.
# 函数用途: 给目标工具返回结构化失败，明确未写入的错误可供模型纠正，其余保持保守处理。
def _error(
    tool: str, message: str, code: str, *, effect_outcome: str = "",
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        tool,
        False,
        json.dumps({"ok": False, "error": message, "error_code": code}, ensure_ascii=False),
        error_code=code,
        effect_outcome=effect_outcome,
        failure_stage="validation" if effect_outcome == "not_started" else "",
    )


def _goal_response(goal: object | None, *, completion_report: bool = False) -> str:
    remaining = None
    public = None
    report = None
    if goal is not None:
        public = goal.public_dict()
        budget = getattr(goal, "token_budget", None)
        if budget is not None:
            remaining = max(0, int(budget) - int(getattr(goal, "tokens_used", 0) or 0))
        if completion_report and str(getattr(goal, "status", "") or "") == "complete":
            if budget is not None or int(getattr(goal, "time_used_seconds", 0) or 0) > 0:
                report = (
                    "Goal achieved. Report final usage from this tool result's structured goal "
                    "fields. If `goal.tokenBudget` is present, include token usage from "
                    "`goal.tokensUsed` and `goal.tokenBudget`. If `goal.timeUsedSeconds` is "
                    "greater than 0, summarize elapsed time in a concise, human-friendly form "
                    "appropriate to the response language."
                )
    return json.dumps(
        {
            "goal": public,
            "remainingTokens": remaining,
            "completionBudgetReport": report,
        },
        ensure_ascii=False,
        indent=2,
    )


class GetGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="get_goal",
        description="Get the current goal or named goals for this thread, including status, budgets, token and elapsed-time usage.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        hints=ToolModelHints(
            category="goal",
            use_cases=("Read the current persisted thread goal",),
            keywords=("goal", "status", "budget"),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("current_thread_goal",)),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        del params
        thread_id, task_id, goal_id, _ = _goal_context(self.agent)
        if not thread_id:
            return _error("get_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED")
        store = self.agent.conversation_store
        goal = store.load_goal(
            thread_id,
            goal_id=goal_id,
            task_id="" if goal_id else task_id,
        )
        if goal is None:
            goals = [
                item.public_dict()
                for item in store.load_goals(thread_id)
                if str(getattr(item, "status", "") or "") != "complete"
            ]
            return ToolHandlerOutcome(
                "get_goal",
                True,
                json.dumps({"goal": None, "goals": goals}, ensure_ascii=False, indent=2),
            )
        return ToolHandlerOutcome("get_goal", True, _goal_response(goal))


class CreateGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="create_goal",
        description=(
            "Create a goal only when explicitly requested by the user or system/developer instructions; "
            "do not infer goals from ordinary tasks. Set token_budget only when explicitly requested. "
            "Fails if an unfinished unnamed goal exists; named goals may coexist."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "Concrete persisted objective."},
                "token_budget": {"type": "integer", "minimum": 1, "description": "Explicitly requested positive token budget."},
                "name": {"type": "string", "minLength": 1, "description": "Optional exact user-visible goal name."},
                "duration_seconds": {"type": "integer", "minimum": 1, "description": "Optional duration explicitly requested by the user."},
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="goal",
            use_cases=("The user or system explicitly requests a persistent goal",),
            avoid_when=("Ordinary tasks that did not explicitly request a goal",),
            keywords=("goal", "persistent objective"),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("current_thread_goal",)),
        promotes_task=True,
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        objective = str(params.get("objective") or "").strip()
        thread_id, task_id, _goal_id, attrs = _goal_context(self.agent)
        if not thread_id or not task_id or attrs is None:
            return _error(
                "create_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED"
            )
        request: dict[str, object] = {
            "thread_id": thread_id,
            "task_id": task_id,
            "objective": objective,
        }
        if "token_budget" in params and params.get("token_budget") is not None:
            request["token_budget"] = params.get("token_budget")
        if str(params.get("name") or "").strip():
            request["name"] = str(params.get("name") or "").strip()
        if params.get("duration_seconds") is not None:
            request["duration_seconds"] = params.get("duration_seconds")
        store = self.agent.conversation_store
        # 会话运行时 语义：同一身份（thread + task）只允许一个未完成目标。已有未完成目标时
        # 必须由模型显式 update_goal 收口或改写，**不能**隐式再建一条 active（真实事故：
        # 同 task 出现两条 active，导致 get_goal/update_goal 恒 GOAL_STATE_CONFLICT，
        # 且 _matching_goal_status 把该 task 的 goal 当成"不存在"）。
        # 这里不猜、不按 updated_at 取代旧目标，也不自动 supersede：直接拒绝并给出结构化原因。
        try:
            existing_goals = store.load_goals(thread_id)
        except Exception:
            existing_goals = []
        if any(
            str(getattr(item, "task_id", "") or "") == task_id
            and str(getattr(item, "status", "") or "").strip().lower() != "complete"
            for item in existing_goals or []
        ):
            return _error(
                "create_goal",
                (
                    "this task already has an unfinished goal; "
                    "update it explicitly with update_goal (no implicit supersede)"
                ),
                "GOAL_STATE_CONFLICT",
                effect_outcome="not_started",
            )
        try:
            with store.goal_transition_guard(thread_id):
                goal = store.create_goal(request)
                store.bind_task(
                    {
                        "thread_id": thread_id,
                        "task_id": task_id,
                        "goal": goal.objective,
                        "status": "active",
                        "work_kind": "goal",
                        "work_name": goal.name,
                        "duration_seconds": goal.duration_seconds,
                        "cancellation_scope": "detached" if goal.name else "foreground",
                    }
                )
                self.agent.local_store.task_registry.register_task(
                    task_id, status="running", goal=goal.objective
                )
        except ValueError as exc:
            return _error("create_goal", str(exc), "GOAL_INVALID_REQUEST")
        attrs["thread_goal_id"] = goal.goal_id
        attrs["thread_goal_activation_pending"] = True
        return ToolHandlerOutcome("create_goal", True, _goal_response(goal))


# LLM: Goal mutation requires exact current thread/task identity; only structured
# preconditions are recoverable failures. Keep post-write persistence protected.
# 类用途: 由模型标记当前目标完成或阻塞，不创建目标，也不把无目标的普通任务误停成未知副作用。
class UpdateGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="update_goal",
        description=(
            "Update the existing goal. Use this tool only to mark the goal achieved or genuinely blocked. "
            "Set status to complete only when the objective has actually been achieved and no required work remains. "
            "Set status to blocked only when the same blocking condition has repeated for at least three consecutive "
            "goal turns, counting the original user-triggered turn and automatic continuations, and no meaningful "
            "progress is possible without user input or an external-state change. If the user resumes a previously "
            "blocked goal, start a fresh three-turn blocked audit. Do not use blocked merely because work is hard, "
            "slow, uncertain, incomplete, or would benefit from clarification. Do not mark complete because budget "
            "is low or work is stopping. Pause, resume and budget changes remain user/system controlled. When a "
            "budgeted goal completes, report final token usage from the structured tool result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                    "description": "Required terminal status under the goal lifecycle rules.",
                }
            },
            "required": ["status"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="goal",
            use_cases=("Mark an existing goal complete or genuinely blocked",),
            avoid_when=("The goal still has useful work available",),
            keywords=("goal", "complete", "blocked"),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("current_thread_goal",)),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: Validate status, exact task ownership and goal existence before the CAS update;
    # a missing/CAS-rejected goal has no mutation to reconcile. Post-update writes may
    # fail partially and continue through the ordinary unknown-effect path.
    # 函数用途: 修改当前任务目标；没有目标或目标已变更时让模型正常处理，不把未发生的写入当成未知。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        status = str(params.get("status") or "").strip().lower()
        if status not in {"complete", "blocked"}:
            return _error(
                "update_goal",
                "update_goal can only mark the existing goal complete or blocked",
                "TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
            )
        thread_id, task_id, goal_id, _ = _goal_context(self.agent)
        if not thread_id:
            return _error(
                "update_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED",
                effect_outcome="not_started",
            )
        store = self.agent.conversation_store
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(
                thread_id,
                goal_id=goal_id,
                task_id="" if goal_id else task_id,
            )
            if goal is None:
                goals = store.load_goals(thread_id)
                return _error(
                    "update_goal",
                    (
                        "the active run is bound to another task"
                        if goals
                        else "this thread has no goal"
                    ),
                    "GOAL_STATE_CONFLICT" if goals else "GOAL_NOT_FOUND",
                    effect_outcome="not_started",
                )
            if task_id and goal.task_id != task_id:
                return _error(
                    "update_goal", "the active run is bound to another task", "GOAL_STATE_CONFLICT",
                    effect_outcome="not_started",
                )
            updated = store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "expected_status": goal.status,
                }
            )
            if updated is None:
                return _error(
                    "update_goal", "the goal changed concurrently", "GOAL_STATE_CONFLICT",
                    effect_outcome="not_started",
                )
            task_status = "completed" if status == "complete" else "interrupted"
            store.update_task_status({"task_id": goal.task_id, "status": task_status})
            registry_status = "done" if status == "complete" else "blocked"
            self.agent.local_store.task_registry.register_task(
                goal.task_id, status=registry_status, goal=updated.objective
            )
        return ToolHandlerOutcome(
            "update_goal",
            True,
            _goal_response(updated, completion_report=status == "complete"),
        )


__all__ = ["CreateGoalTool", "GetGoalTool", "UpdateGoalTool"]
