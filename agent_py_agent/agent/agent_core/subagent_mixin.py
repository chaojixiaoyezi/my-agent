"""LLM: implements SimpleAgent subagent spawning, runner execution, and parent-planner turns.

给人看的解释：
这个文件只管"主代理怎么和子代理互动"。
包括创建工单、跑一个子代理 runner、以及让父代理 planner 做一次完整模型判断。
"""

from __future__ import annotations

import logging

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import (
    RecordRunnerResultParams,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_subagent_runner_output,
)
from ._subagent_planner_mixin import RunParentPlannerParams, _ParentPlannerMixin
from ._subagent_repair_mixin import _SubagentRepairMixin
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


def _config_workflow_dispatch_mode(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"


class _SubagentLifecycleMixin:
    """Internal: subagent spawning and runner execution (spawn, run, failure, finalize)."""

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:
        """生成子任务记录。"""

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
        *,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
        retry_reason: str = "",
        attempt_id: str = "",
    ) -> SubAgentRunnerResult:
        """按执行上下文运行一个子代理入口。"""
        active_attempt_id = str(attempt_id or "").strip()
        if not dry_run and not active_attempt_id:
            prepared = self.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)
            active_attempt_id = prepared.runner_active_attempt_id

        context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        if dry_run:
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

        if probe:
            probe_result = self.subagents.probe_channel(run_id)
            if probe_result.channel_status == "BROKEN":
                context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
                prompt = _build_subagent_runner_prompt(context, instruction)
                return self.subagents.record_runner_result(
                    RecordRunnerResultParams(
                        run_id=run_id,
                        attempt_id=active_attempt_id,
                        dry_run=False,
                        ok=False,
                        message="通道健康检查为 BROKEN，未启动模型执行。",
                        prompt=prompt,
                        status="CHANNEL_ERROR",
                        verification_status="UNVERIFIED",
                        failure_type="channel",
                    )
                )
            context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
            prompt = _build_subagent_runner_prompt(context, instruction)

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
                run_id, active_attempt_id, exc, context, prompt
            )

        return self._finalize_subagent_run(run_id, active_attempt_id, result, context, prompt)

    def _handle_subagent_run_failure(self, run_id, active_attempt_id, exc, context, prompt):
        """Handle subagent run failure by recording error result."""
        failed_result = self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                attempt_id=active_attempt_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {exc}",
                prompt=prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_error",
            )
        )
        self._write_subagent_recovery_snapshot(
            run_id,
            user_prompt=context.goal,
            response_text=failed_result.message,
            backend="",
            status=failed_result.status,
            error_code=failed_result.runner_last_error or "runner_error",
            tool_calls=[],
        )
        return failed_result

    def _finalize_subagent_run(self, run_id, active_attempt_id, result, context, prompt):
        """Finalize subagent run: parse output, repair if needed, record result."""
        structured = parse_subagent_runner_output(result.response)
        prompt_for_log = result.prompt
        response_for_log = result.response
        backend_name = result.backend
        message = "runner 已完成模型调用，等待独立验收。"
        structured_repair_attempted = False
        structured_repair_ok = False
        structured_repair_error = ""

        if not (structured.found and structured.ok):
            (
                structured,
                structured_repair_ok,
                structured_repair_error,
                backend_name,
                prompt_for_log,
                response_for_log,
                message,
            ) = self._handle_subagent_repair(
                context, result, structured, prompt_for_log, response_for_log, backend_name, message
            )
            structured_repair_attempted = True
        else:
            structured_repair_attempted = False

        runner_result = self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                attempt_id=active_attempt_id,
                dry_run=False,
                ok=structured.ok if structured.found else True,
                message=message,
                prompt=prompt_for_log,
                response=response_for_log,
                backend=backend_name,
                tool_rounds=result.tool_rounds,
                status="" if structured.found else "AWAITING_ACCEPTANCE",
                verification_status="" if structured.found else "NEEDS_ACCEPTANCE",
                structured_output=structured,
                actual_tools=result.executed_tools or [],
                structured_repair_attempted=structured_repair_attempted,
                structured_repair_ok=structured_repair_ok,
                structured_repair_error=structured_repair_error,
            )
        )
        self._write_subagent_recovery_snapshot(
            run_id,
            user_prompt=context.goal,
            response_text=message,
            backend=backend_name,
            status=runner_result.status,
            error_code=runner_result.runner_last_error,
            tool_calls=[
                {"tool": tool_name, "id": f"{run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(result.executed_tools or [], start=1)
            ],
        )
        return runner_result


class SimpleAgentSubagentMixin(
    _SubagentLifecycleMixin,
    _SubagentRepairMixin,
    _ParentPlannerMixin,
):
    """LLM: mixin for subagent lifecycle orchestration reachable from SimpleAgent.

    给人看的解释：
    这些方法不直接写文件细节，而是调用 SubAgentManager，让主代理保留"编排者"的角色。
    """


# Re-export for backward compatibility
from ._subagent_planner_mixin import RunParentPlannerParams  # noqa: E402