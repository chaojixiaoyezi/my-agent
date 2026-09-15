# LLM: Goal tools bind to structured current-thread authority. Pre-update rejection is
# side-effect-free; generic create/persistence failures must not acquire replay authority.
# 模块用途: 提供会话目标读写工具；不存在或归属冲突交回模型处理，真实写入异常保留保护。
from __future__ import annotations

"""会话运行时 model tools for one persisted thread goal."""

import json
from typing import TYPE_CHECKING, Any

from ..agent_core.runner.context import current_subagent_run_id
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
from .goal_binding import goal_binding, goal_tool_target

if TYPE_CHECKING:
    from ..core import SimpleAgent


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


# LLM: 输出真实目标及执行归属；提示供模型理解，不解析文案来激活或停止运行。
# 函数用途: 生成当前代理目标及历史记录回执，不把展示名称解释成后台派工。
def _goal_response(
    goal: object | None, *, completion_report: bool = False, execution: dict | None = None,
    goals: list[object] | None = None,
) -> str:
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
            **({"execution": execution} if execution is not None else {}),
            **({"goals": [item.public_dict() for item in goals]} if goals is not None else {}),
        },
        ensure_ascii=False,
        indent=2,
    )


# LLM: 只读当前会话的权威目标，冲突返回完整公开列表供精确控制，不猜测当前目标。
# 类用途: 查询真实 Goal 和使用情况，避免把 Todo 或模型口头承诺当成目标。
class GetGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="get_goal",
        description="Get this agent's current goal and retained history, including status, budgets, token and elapsed-time usage.",
        input_schema={"type": "object", "properties": {"target_run_id": {"type": "string", "description": "Optional exact direct-child run ID; omit for your own Goal."}}, "additionalProperties": False},
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

    # LLM: get 不改变目标生命周期；旧共享任务歧义可读但必须暴露结构化冲突。
    # 函数用途: 返回当前目标或候选列表，目标记录损坏仍交给存储层报错。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            thread_id, task_id, goal_id, _ = goal_tool_target(self.agent, str(params.get("target_run_id") or ""))
        except (PermissionError, FileNotFoundError) as exc:
            return _error("get_goal", str(exc), "GOAL_STATE_CONFLICT", effect_outcome="not_started")
        if not thread_id:
            return _error("get_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED")
        store = self.agent.conversation_store
        try:
            goal = store.load_goal(thread_id, goal_id=goal_id, task_id=task_id)
        except ValueError:
            return ToolHandlerOutcome(
                "get_goal", False,
                json.dumps({"goal": None, "goals": [item.public_dict() for item in store.load_goals(thread_id)],
                            "error_code": "GOAL_STATE_CONFLICT",
                            "message": "目标绑定存在歧义，请使用 /goal 目标编号 resume 精确恢复。"}, ensure_ascii=False),
                error_code="GOAL_STATE_CONFLICT", effect_outcome="not_started", failure_stage="validation",
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
        return ToolHandlerOutcome("get_goal", True, _goal_response(goal, goals=store.load_goals(thread_id)))


# LLM: 创建只绑定当前代理会话，名字不授权额外执行器；子代理身份来自 scoped runner。
# 类用途: 把明确要求的持续目标落盘，向用户返回真实目标状态而不是仅写任务清单。
class CreateGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="create_goal",
        description=(
            "Create a goal only when explicitly requested by the user or system/developer instructions; "
            "do not infer goals from ordinary tasks. Omit token_budget and duration_seconds unless the user "
            "explicitly supplies a limit; do not invent a timeout. Omission means no time or token cap. "
            "Each agent has at most one unfinished goal. A different name does not create a second executor. "
            "Use update_goal to revise the current objective. Delegated agents have their own goals and "
            "optional task_progress Todo; a Todo is not required to begin or finish work."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "Concrete persisted objective."},
                "token_budget": {"type": "integer", "minimum": 1, "description": "Explicitly requested positive token budget."},
                "name": {"type": "string", "minLength": 1, "description": "Optional display name; never starts another agent."},
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

    # LLM: Goal lock covers uniqueness and create; retain current task and execution lane. No implicit background dispatch.
    # 函数用途: 保存当前代理的一个目标，已有未完成目标明确拒绝，避免悄悄重复开工。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        objective = str(params.get("objective") or "").strip()
        thread_id, task_id, _goal_id, attrs = goal_binding(self.agent)
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
        try:
            with store.goal_transition_guard(thread_id):
                current_link = store.load_task_link(task_id)
                existing_goals = store.load_goals(thread_id)
                if any(item.status != "complete" for item in existing_goals):
                    return _error(
                        "create_goal", "this agent already has an unfinished goal; update it explicitly",
                        "GOAL_STATE_CONFLICT", effect_outcome="not_started",
                    )
                goal = store.create_goal(request)
                if not current_subagent_run_id(self.agent):
                    store.bind_task(
                        {
                            "thread_id": thread_id,
                            "task_id": goal.task_id,
                            "goal": goal.objective,
                            "status": "active",
                            "work_kind": "goal",
                            "work_name": goal.name,
                            "duration_seconds": goal.duration_seconds,
                            "cancellation_scope": str(getattr(current_link, "cancellation_scope", "") or "foreground"),
                        }
                    )
                    self.agent.local_store.task_registry.register_task(
                        goal.task_id, status="running", goal=goal.objective
                    )
        except ValueError as exc:
            return _error("create_goal", str(exc), "GOAL_INVALID_REQUEST")
        attrs["thread_goal_id"] = goal.goal_id
        attrs["thread_goal_revision"] = goal.revision
        execution = {
            "mode": "current_turn",
            "task_id": goal.task_id,
            "current_task_id": task_id,
            "current_goal_id": str(attrs.get("thread_goal_id") or _goal_id),
            "guidance": "此目标由当前代理负责；Todo 可选，子代理目标独立，不会另开执行器。",
        }
        current_goal = store.load_goal(thread_id, goal_id=str(execution["current_goal_id"]), task_id=task_id)
        if current_goal is not None:
            from .goal_prompting import goal_execution_scope

            execution.update(goal_execution_scope(current_goal, tuple(store.load_goals(thread_id))))
        return ToolHandlerOutcome("create_goal", True, _goal_response(goal, execution=execution))


# LLM: Goal mutation requires exact current thread/task identity; only structured
# preconditions are recoverable failures. Keep post-write persistence protected.
# 类用途: 由模型标记当前目标完成或阻塞，不创建目标，也不把无目标的普通任务误停成未知副作用。
class UpdateGoalTool(BaseTool):
    model_spec = ToolModelSpec(
        name="update_goal",
        description=(
            "Update this agent's existing goal. Supply objective to revise it when the user or parent "
            "changes the requested outcome; do not shrink the scope merely to declare success. "
            "An objective-only edit preserves status, task identity and usage. Supply status only "
            "to mark the goal achieved or genuinely blocked. "
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
                    "description": "Optional terminal status under the goal lifecycle rules.",
                },
                "objective": {"type": "string", "minLength": 1, "maxLength": 4000},
                "expected_revision": {"type": "integer", "minimum": 1},
                "target_run_id": {"type": "string", "description": "Optional exact direct-child run ID; omit for your own Goal. A child objective edit preserves its lifecycle."},
            },
            "anyOf": [{"required": ["status"]}, {"required": ["objective"]}],
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

    # LLM: CAS 前验证 exact Goal/task；完成 Goal 不能提前关闭仍在执行的 turn/task，执行终态由 finalization 提交。
    # 函数用途: 更新目标并返回真实分工；完成后仍可交付回复，避免中途将子代理所属任务误标为结束。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        status = str(params.get("status") or "").strip().lower()
        objective = str(params.get("objective") or "").strip()
        if (status and status not in {"complete", "blocked"}) or not (status or objective):
            return _error(
                "update_goal",
                "update_goal can only mark the existing goal complete or blocked",
                "TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
            )
        try:
            thread_id, task_id, goal_id, attrs = goal_tool_target(self.agent, str(params.get("target_run_id") or ""))
        except (PermissionError, FileNotFoundError) as exc:
            return _error("update_goal", str(exc), "GOAL_STATE_CONFLICT", effect_outcome="not_started")
        if not thread_id:
            return _error(
                "update_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED",
                effect_outcome="not_started",
            )
        store = self.agent.conversation_store
        with store.goal_transition_guard(thread_id):
            goals = store.load_goals(thread_id)
            if sum(item.task_id == task_id and item.status != "complete" for item in goals) > 1:
                return _error(
                    "update_goal", "shared task has conflicting goals; resume the exact goal before changing status",
                    "GOAL_STATE_CONFLICT", effect_outcome="not_started",
                )
            goal = store.load_goal(thread_id, goal_id=goal_id, task_id=task_id)
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
            expected_revision = params.get("expected_revision", (attrs or {}).get("thread_goal_revision", goal.revision))
            if params.get("target_run_id") and params.get("expected_revision") is None:
                return _error("update_goal", "read the child get_goal and provide its expected_revision", "TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
            updated = store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "objective": objective,
                    "expected_status": goal.status,
                    "expected_revision": expected_revision,
                }
            )
            if updated is None:
                return _error(
                    "update_goal", "the goal changed concurrently", "GOAL_STATE_CONFLICT",
                    effect_outcome="not_started",
                )
            if objective:
                from .goal_prompting import objective_updated_prompt

                if not params.get("target_run_id") and not current_subagent_run_id(self.agent):
                    store.update_task_goal({"task_id": updated.task_id, "goal": updated.objective})
                    self.agent.local_store.task_registry.update_task_description(updated.task_id, updated.objective)
                # Goal 版本 CAS 已防重；这是持久要求变更，不预绑正在运行的用户插话轮次。
                store.append_guidance({
                    "target_type": "agent_run" if params.get("target_run_id") or current_subagent_run_id(self.agent) else "task",
                    "target_id": updated.task_id, "message": objective_updated_prompt(updated),
                    "sender": "goal", "metadata": {"goal_id": updated.goal_id, "revision": updated.revision},
                })
            # blocked 是明确的可恢复停止；complete 只是目标已达成，仍有当前回复和子树需要安全收尾。
            if status == "blocked" and not params.get("target_run_id") and not current_subagent_run_id(self.agent):
                store.update_task_status({"task_id": goal.task_id, "status": "interrupted"})
                self.agent.local_store.task_registry.register_task(
                    goal.task_id, status="blocked", goal=updated.objective
                )
            from .goal_prompting import goal_execution_scope

            execution = goal_execution_scope(updated, tuple(store.load_goals(thread_id)))
        return ToolHandlerOutcome(
            "update_goal",
            True,
            _goal_response(updated, completion_report=status == "complete", execution=execution),
        )


__all__ = ["CreateGoalTool", "GetGoalTool", "UpdateGoalTool"]
