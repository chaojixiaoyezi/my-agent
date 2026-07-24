from __future__ import annotations

"""会话运行时 model tools for one persisted thread goal."""

import json
from typing import TYPE_CHECKING, Any

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ..core import SimpleAgent


def _goal_context(agent: SimpleAgent) -> tuple[str, str, dict[str, object] | None]:
    """Return authority from the current structured run, never from prompt text."""
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    if not isinstance(attrs, dict):
        return "", "", None
    return (
        str(attrs.get("conversation_thread_id") or "").strip(),
        str(attrs.get("conversation_task_id") or "").strip(),
        attrs,
    )


def _error(tool: str, message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool,
        False,
        json.dumps({"ok": False, "error": message, "error_code": code}, ensure_ascii=False),
        error_code=code,
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
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = ToolSpec(
            name="get_goal",
            category="goal",
            description=(
                "Get the current goal for this thread, including status, budgets, token and "
                "elapsed-time usage, and remaining token budget."
            ),
            use_cases=["Read the current persisted thread goal"],
            avoid_when=[],
            keywords=["goal", "status", "budget"],
            parameters={},
            effect="read_only",
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        del params
        thread_id, _, _ = _goal_context(self.agent)
        if not thread_id:
            return _error("get_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED")
        goal = self.agent.conversation_store.load_goal(thread_id)
        return ToolExecutionResult("get_goal", True, _goal_response(goal))


class CreateGoalTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = ToolSpec(
            name="create_goal",
            category="goal",
            description=(
                "Create a goal only when explicitly requested by the user or system/developer "
                "instructions; do not infer goals from ordinary tasks.\n"
                "Set token_budget only when an explicit token budget is requested. Fails if an "
                "unfinished goal exists; use update_goal only for status."
            ),
            use_cases=["The user or system explicitly requests a persistent goal"],
            avoid_when=["Ordinary tasks that did not explicitly request a goal"],
            keywords=["goal", "persistent objective"],
            parameters={
                "objective": (
                    "Required. The concrete objective to start pursuing. This starts a new active "
                    "goal when no goal exists or replaces the current goal when it is complete."
                ),
                "token_budget": "Positive token budget for the new goal. Omit unless explicitly requested.",
            },
            parameter_schema={
                "objective": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 1},
            },
            required_parameters=["objective"],
            effect="mutating",
            idempotency_scope="operation",
            promotes_task=True,
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        objective = str(params.get("objective") or "").strip()
        thread_id, task_id, attrs = _goal_context(self.agent)
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
        store = self.agent.conversation_store
        try:
            with store.goal_transition_guard(thread_id):
                goal = store.create_goal(request)
                store.update_task_goal({"task_id": task_id, "goal": goal.objective})
                self.agent.local_store.task_registry.register_task(
                    task_id, status="running", goal=goal.objective
                )
        except ValueError as exc:
            return _error("create_goal", str(exc), "GOAL_INVALID_REQUEST")
        attrs["thread_goal_id"] = goal.goal_id
        attrs["thread_goal_activation_pending"] = True
        return ToolExecutionResult("create_goal", True, _goal_response(goal))


class UpdateGoalTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = ToolSpec(
            name="update_goal",
            category="goal",
            description=(
                "Update the existing goal.\n"
                "Use this tool only to mark the goal achieved or genuinely blocked.\n"
                "Set status to `complete` only when the objective has actually been achieved and "
                "no required work remains.\n"
                "Set status to `blocked` only when the same blocking condition has repeated for "
                "at least three consecutive goal turns, counting the original/user-triggered turn "
                "and any automatic continuations, and the agent cannot make meaningful progress "
                "without user input or an external-state change.\n"
                "If the user resumes a goal that was previously marked `blocked`, treat the resumed "
                "run as a fresh blocked audit. If the same blocking condition then repeats for at "
                "least three consecutive resumed goal turns, set status to `blocked` again.\n"
                "Once the blocked threshold is satisfied, do not keep reporting that you are still "
                "blocked while leaving the goal active; set status to `blocked`.\n"
                "Do not use `blocked` merely because the work is hard, slow, uncertain, incomplete, "
                "or would benefit from clarification.\n"
                "Do not mark a goal complete merely because its budget is nearly exhausted or "
                "because you are stopping work.\n"
                "You cannot use this tool to pause, resume, budget-limit, or usage-limit a goal; "
                "those status changes are controlled by the user or system.\n"
                "When marking a budgeted goal achieved with status `complete`, report the final "
                "token usage from the tool result to the user."
            ),
            use_cases=["Mark an existing goal complete or genuinely blocked"],
            avoid_when=["The goal still has useful work available"],
            keywords=["goal", "complete", "blocked"],
            parameters={
                "status": (
                    "Required. Set to `complete` only when the objective is achieved and no "
                    "required work remains. Set to `blocked` only after the same blocking "
                    "condition has recurred for at least three consecutive goal turns and the "
                    "agent is at an impasse."
                )
            },
            parameter_schema={"status": {"type": "string", "enum": ["complete", "blocked"]}},
            required_parameters=["status"],
            effect="mutating",
            idempotency_scope="operation",
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        status = str(params.get("status") or "").strip().lower()
        if status not in {"complete", "blocked"}:
            return _error(
                "update_goal",
                "update_goal can only mark the existing goal complete or blocked",
                "TOOL_INVALID_ARGUMENTS",
            )
        thread_id, task_id, _ = _goal_context(self.agent)
        if not thread_id:
            return _error("update_goal", "current thread context is unavailable", "GOAL_CONTEXT_REQUIRED")
        store = self.agent.conversation_store
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id)
            if goal is None:
                return _error("update_goal", "this thread has no goal", "GOAL_NOT_FOUND")
            if task_id and goal.task_id != task_id:
                return _error("update_goal", "the active run is bound to another task", "GOAL_STATE_CONFLICT")
            updated = store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "expected_status": goal.status,
                }
            )
            if updated is None:
                return _error("update_goal", "the goal changed concurrently", "GOAL_STATE_CONFLICT")
            task_status = "completed" if status == "complete" else "interrupted"
            store.update_task_status({"task_id": goal.task_id, "status": task_status})
            registry_status = "done" if status == "complete" else "blocked"
            self.agent.local_store.task_registry.register_task(
                goal.task_id, status=registry_status, goal=updated.objective
            )
        return ToolExecutionResult(
            "update_goal",
            True,
            _goal_response(updated, completion_report=status == "complete"),
        )


__all__ = ["CreateGoalTool", "GetGoalTool", "UpdateGoalTool"]
