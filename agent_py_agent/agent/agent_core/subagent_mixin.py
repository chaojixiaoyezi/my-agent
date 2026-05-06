
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import (
    RecordRunnerResultParams,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_subagent_runner_output,
)
from ._subagent_planner_mixin import RunParentPlannerParams, _ParentPlannerMixin
from ._subagent_repair_mixin import SubagentRepairParams, _SubagentRepairMixin
from .automation_guard import SubagentAutomationGuard
from .planner import _build_parent_planner_state
from .runner_prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_prompt,
    _build_subagent_runner_repair_prompt,
)
from .task_complexity import TaskComplexityEstimate, estimate_task_complexity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubagentProbeParams:
    run_id: str
    active_attempt_id: str
    max_cards: int
    instruction: str
    probe: bool


@dataclass(frozen=True)
class SubagentRunFailureParams:
    run_id: str
    active_attempt_id: str
    exc: Exception
    context: object
    prompt: str


@dataclass(frozen=True)
class SubagentFinalizeParams:
    run_id: str
    active_attempt_id: str
    result: object
    context: object
    prompt: str


def _config_workflow_dispatch_mode(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"


def _initial_repair_state(result) -> dict[str, object]:
    return {
        "prompt_for_log": result.prompt,
        "response_for_log": result.response,
        "backend_name": result.backend,
        "message": "runner 已完成模型调用，等待独立验收。",
        "attempted": False,
        "ok": False,
        "error": "",
    }


def _tuple_repair_state(value: tuple) -> dict[str, object]:
    structured, ok, error, backend_name, prompt_for_log, response_for_log, message = value
    return {
        "structured": structured,
        "prompt_for_log": prompt_for_log,
        "response_for_log": response_for_log,
        "backend_name": backend_name,
        "message": message,
        "attempted": True,
        "ok": ok,
        "error": error,
    }


class _SubagentLifecycleBase:

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")

        if count is None:
            allowed_tools = getattr(self.config, "subagent_allowed_tools", [])
            complexity = estimate_task_complexity(goal, plan=[], allowed_tools=allowed_tools)
            guard = SubagentAutomationGuard(self.config)
            should_delegate = guard.should_delegate(complexity)
            delegated = False

            if should_delegate:
                n = self.config.max_subagents
                tasks = self.subagents.split(
                    goal,
                    n,
                    workflow_mode=_config_workflow_dispatch_mode(
                        self.config.subagent_workflow_mode
                    ),
                )
                delegated = len(tasks) > 0
            else:
                tasks = []

            guard.warn_if_not_delegating(complexity, delegated)
            return tasks
        else:
            n = min(count, self.config.max_subagents)
            return self.subagents.split(
                goal,
                n,
                workflow_mode=_config_workflow_dispatch_mode(self.config.subagent_workflow_mode),
            )

    def run_subagent(
        self,
        run_id: str,
        **kwargs,
    ) -> SubAgentRunnerResult:
        instruction = str(kwargs.get("instruction", ""))
        dry_run = bool(kwargs.get("dry_run", True))
        max_cards = int(kwargs.get("max_cards", 0))
        probe = bool(kwargs.get("probe", True))
        retry_reason = str(kwargs.get("retry_reason", ""))
        attempt_id = str(kwargs.get("attempt_id", ""))
        active_attempt_id = str(attempt_id or "").strip()
        active_attempt_id = self._prepare_subagent_attempt(
            run_id, dry_run=dry_run, active_attempt_id=active_attempt_id, retry_reason=retry_reason
        )
        context, prompt = self._build_subagent_prompt(run_id, max_cards, instruction)
        if dry_run:
            return self._record_subagent_dry_run(run_id, active_attempt_id, prompt)

        probe_blocked = self._probe_subagent_channel(
            SubagentProbeParams(run_id, active_attempt_id, max_cards, instruction, probe)
        )
        if probe_blocked is not None:
            return probe_blocked

        context, prompt = self._build_subagent_prompt(run_id, max_cards, instruction)
        task_for_attrs = self.subagents.load(run_id)
        self._current_task_attributes = task_for_attrs.attributes

        try:
            result = self.run(
                prompt,
                save=False,
                allowed_tools=context.allowed_tools,
                write_boundary=context.write_boundary,
                source="subagent_run_model_turn",
                recovery_snapshot=False,
            )
        except Exception as exc:
            return self._handle_subagent_run_failure(
                SubagentRunFailureParams(run_id, active_attempt_id, exc, context, prompt)
            )

        return self._finalize_subagent_run(
            SubagentFinalizeParams(run_id, active_attempt_id, result, context, prompt)
        )

    def _prepare_subagent_attempt(self, run_id, *, dry_run, active_attempt_id, retry_reason):
        if dry_run or active_attempt_id:
            return active_attempt_id
        prepared = self.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)
        return prepared.runner_active_attempt_id

    def _build_subagent_prompt(self, run_id, max_cards, instruction):
        context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        return context, prompt

    def _record_subagent_dry_run(self, run_id, active_attempt_id, prompt):
        return self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                attempt_id=active_attempt_id,
                dry_run=True,
                ok=True,
                message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
                prompt=prompt,
            )
        )

    def _probe_subagent_channel(self, params: SubagentProbeParams):
        if not params.probe:
            return None
        probe_result = self.subagents.probe_channel(params.run_id)
        if probe_result.channel_status != "BROKEN":
            return None
        context, prompt = self._build_subagent_prompt(
            params.run_id, params.max_cards, params.instruction
        )
        return self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=False,
                message="通道健康检查为 BROKEN，未启动模型执行。",
                prompt=prompt,
                status="CHANNEL_ERROR",
                verification_status="UNVERIFIED",
                failure_type="channel",
            )
        )

    def _handle_subagent_run_failure(self, params: SubagentRunFailureParams):
        failed_result = self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {params.exc}",
                prompt=params.prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_error",
            )
        )
        self._write_subagent_recovery_snapshot(
            params.run_id,
            user_prompt=params.context.goal,
            response_text=failed_result.message,
            backend="",
            status=failed_result.status,
            error_code=failed_result.runner_last_error or "runner_error",
            tool_calls=[],
        )
        return failed_result

    def _finalize_subagent_run(self, params: SubagentFinalizeParams):
        structured = parse_subagent_runner_output(params.result.response)
        repair_state = _initial_repair_state(params.result)
        if not (structured.found and structured.ok):
            repair_state = self._handle_subagent_repair(
                SubagentRepairParams(
                    context=params.context,
                    result=params.result,
                    structured=structured,
                    prompt_for_log=repair_state["prompt_for_log"],
                    response_for_log=repair_state["response_for_log"],
                    backend_name=repair_state["backend_name"],
                    message=repair_state["message"],
                )
            )
            structured = repair_state[0]
            repair_state = _tuple_repair_state(repair_state)

        runner_result = self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=structured.ok if structured.found else True,
                message=repair_state["message"],
                prompt=repair_state["prompt_for_log"],
                response=repair_state["response_for_log"],
                backend=repair_state["backend_name"],
                tool_rounds=params.result.tool_rounds,
                status="" if structured.found else "AWAITING_ACCEPTANCE",
                verification_status="" if structured.found else "NEEDS_ACCEPTANCE",
                structured_output=structured,
                actual_tools=params.result.executed_tools or [],
                structured_repair_attempted=repair_state["attempted"],
                structured_repair_ok=repair_state["ok"],
                structured_repair_error=repair_state["error"],
            )
        )
        self._write_subagent_recovery_snapshot(
            params.run_id,
            user_prompt=params.context.goal,
            response_text=repair_state["message"],
            backend=repair_state["backend_name"],
            status=runner_result.status,
            error_code=runner_result.runner_last_error,
            tool_calls=[
                {"tool": tool_name, "id": f"{params.run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(params.result.executed_tools or [], start=1)
            ],
        )
        return runner_result


class SimpleAgentSubagentMixin(
    _SubagentLifecycleBase,
    _SubagentRepairMixin,
    _ParentPlannerMixin,
):
    pass


# Re-export for backward compatibility
from ._subagent_planner_mixin import RunParentPlannerParams  # noqa: E402
