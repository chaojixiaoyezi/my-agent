from __future__ import annotations

"""LLM: implements SimpleAgent subagent spawning, runner execution, and parent-planner turns.

给人看的解释：
这个文件只管"主代理怎么和子代理互动"。
包括创建工单、跑一个子代理 runner、以及让父代理 planner 做一次完整模型判断。
"""

import logging
from dataclasses import dataclass

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import (
    CompressionSnapshotInput,
    RecoverySnapshotInput,
)
from ..subagent import (
    ParentPlannerRecord,
    RecordRunnerResultParams,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_parent_planner_output,
    parse_subagent_runner_output,
)
from ..subagents.services.dispatch_params import ParentPlannerRecordParams
from .automation_guard import SubagentAutomationGuard
from .planner import (
    PARENT_PLANNER_READ_TOOLS,
    _build_parent_planner_prompt,
    _build_parent_planner_state,
)
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


# ---------------------------------------------------------------------------
# Internal mixin classes – each ≤ 250 lines
# ---------------------------------------------------------------------------


class _SubagentLifecycleMixin:
    """Internal: subagent spawning and runner execution (spawn, run, failure, finalize)."""

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:
        """生成子任务记录。"""

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")

        # 如果调用者显式指定了数量，优先使用调用者的意图
        # 只有当 count 为 None 时才使用复杂度预估判断
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
        """按执行上下文运行一个子代理入口。

        第一版 runner 不负责并行调度，只负责把"上下文 -> 模型执行 -> 工单回写"
        这条最小链路打通。默认 dry-run，避免误触真实模型接口。
        """
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
        from .runner_prompts import (
            _append_runner_repair_failure,
            _append_runner_repair_prompt,
            _append_runner_repair_response,
            _build_subagent_runner_repair_prompt,
        )

        structured = parse_subagent_runner_output(result.response)
        prompt_for_log = result.prompt
        response_for_log = result.response
        backend_name = result.backend
        message = "runner 已完成模型调用，等待独立验收。"
        # Initialize repair tracking variables upfront so they're always defined
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


class _SubagentRepairMixin:
    """Internal: structured output repair and recovery snapshot writing."""

    def _handle_subagent_repair(
        self,
        context,
        result,
        structured,
        prompt_for_log,
        response_for_log,
        backend_name,
        message,
    ):
        """Handle structured output repair when initial parse fails."""
        from .runner_prompts import (
            _append_runner_repair_failure,
            _append_runner_repair_prompt,
            _append_runner_repair_response,
            _build_subagent_runner_repair_prompt,
        )

        structured_repair_attempted = True
        structured_repair_ok = False
        structured_repair_error = ""

        repair_prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt=result.prompt,
            original_response=result.response,
            parse_error=structured.parse_error,
        )
        try:
            repair_response = self.backend.generate(repair_prompt)
        except Exception as exc:
            structured_repair_error = str(exc)
            response_for_log = _append_runner_repair_failure(result.response, exc)
            return (
                structured,
                structured_repair_ok,
                structured_repair_error,
                backend_name,
                prompt_for_log,
                response_for_log,
                message,
            )

        repaired = parse_subagent_runner_output(repair_response.text)
        prompt_for_log = _append_runner_repair_prompt(result.prompt, repair_prompt)
        response_for_log = _append_runner_repair_response(result.response, repair_response.text)
        backend_name = repair_response.backend or result.backend

        if repaired.found and repaired.ok:
            structured = repaired
            structured_repair_ok = True
            message = "runner 已完成模型调用，并已修复结构化结果，等待独立验收。"
        elif not structured.found and repaired.found:
            structured = repaired
            structured_repair_error = repaired.parse_error
        else:
            structured_repair_error = (
                repaired.parse_error or "repair response still missing structured output"
            )

        return (
            structured,
            structured_repair_ok,
            structured_repair_error,
            backend_name,
            prompt_for_log,
            response_for_log,
            message,
        )

    def _write_subagent_recovery_snapshot(
        self,
        run_id: str,
        *,
        user_prompt: str,
        response_text: str,
        backend: str,
        status: str,
        error_code: str,
        tool_calls: list[dict[str, object]],
    ) -> None:
        """LLM: write a best-effort run_id recovery snapshot after subagent-run finishes.

        给人看的解释：
        子代理 runner 用 `save=False`，避免把大 prompt 写进普通对话记忆。
        但任务恢复需要一个小锚点，所以 runner 结果写回后单独写 hook，并附上任务目录里的权威文件路径。
        """

        if not bool(getattr(self.config, "memory_hook_enabled", True)):
            return
        try:
            task = self.subagents.load(run_id)
        except (FileNotFoundError, TypeError):
            task = None
        content_paths = []
        next_actions = [
            "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
        ]
        if task is not None:
            content_paths = [
                task.status_file,
                task.work_log_file,
                task.runner_result_file,
                task.runner_result_json,
                task.output_json,
                task.handoff_file,
            ]
        write_recovery_snapshot(
            self.root,
            params=RecoverySnapshotInput(
                session_id=getattr(self, "session_id", self.config.agent_name),
                user_prompt=user_prompt,
                response_text=response_text,
                backend=backend,
                source="subagent_run",
                request_id=f"subagent-run:{run_id}",
                run_id=run_id,
                task_id=run_id,
                status=status.lower() or "unknown",
                error_code=error_code,
                tool_calls=tool_calls,
                task_refs=[run_id],
                content_paths=content_paths,
                next_actions=next_actions,
                archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
            ),
        )


@dataclass(frozen=True)
class RunParentPlannerParams:
    """Bundle of run_parent_planner parameters."""

    router: CapabilityRouter
    capability_config: CapabilityConfig | None
    apply: bool
    execute_runners: bool
    max_runners: int
    limit: int
    reviewer: str
    note: str
    runner_instruction: str


class _ParentPlannerMixin:
    """Internal: parent planner LLM turn orchestration (run, heartbeat, error, build)."""

    def run_parent_planner(self, params: RunParentPlannerParams) -> ParentPlannerRecord:
        """运行一轮父代理 LLM planner，并写出审计报告。

        planner 不是 heartbeat 的浅层 OK，而是一个完整模型 turn。只有状态门禁发现
        有 active/pending/stalled/needs-intervention 事项时，才真正调用模型；如果模型
        在有事时只回 HEARTBEAT_OK，会被标记为失败。
        """
        cfg = params.capability_config or CapabilityConfig()
        state = _build_parent_planner_state(
            self,
            cfg,
            max_runners=params.max_runners,
            limit=params.limit,
            reviewer=params.reviewer,
            note=params.note,
        )
        gate_summary = {
            key: int(value) for key, value in state["gate"].items() if isinstance(value, int)
        }

        if not state["gate"].get("needs_planner", 0):
            return self._make_heartbeat_ok_record(not params.apply, gate_summary)

        result = self._execute_planner_llm(
            state,
            params.apply,
            params.execute_runners,
            params.max_runners,
            params.runner_instruction,
        )
        if result is None:
            return self._make_planner_error_record(
                gate_summary, params.runner_instruction, params.apply
            )
        return self._build_planner_record(
            result, state, gate_summary, params.apply, params.runner_instruction
        )

    def _make_heartbeat_ok_record(self, dry_run, gate_summary):
        """Make heartbeat OK record when no planner needed."""
        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=dry_run,
                triggered=False,
                ok=True,
                decision="HEARTBEAT_OK",
                message="planner gate 确认无 active/pending/stalled/needs-intervention 事项，允许 HEARTBEAT_OK。",
                gate_summary=gate_summary,
                summary="no work",
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=dry_run)
        self.subagents.write_parent_planner_report(report, append_log=False)
        return record

    def _execute_planner_llm(self, state, apply, execute_runners, max_runners, runner_instruction):
        """Execute the parent planner LLM call."""
        from .planner import _build_parent_planner_prompt

        prompt = _build_parent_planner_prompt(
            state,
            apply=apply,
            execute_runners=execute_runners,
            max_runners=max_runners,
            runner_instruction=runner_instruction,
        )
        self.subagents.write_parent_planner_exchange(prompt)
        try:
            return self.run(prompt, save=False, allowed_tools=PARENT_PLANNER_READ_TOOLS)
        except Exception:
            return None

    def _make_planner_error_record(self, gate_summary, runner_instruction, apply):
        """Make planner error record after LLM failure."""
        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not apply,
                triggered=True,
                ok=False,
                decision="PLANNER_ERROR",
                message="父代理 planner 调用失败",
                gate_summary=gate_summary,
                runner_instruction=runner_instruction,
                evidence_paths=[],
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
        self.subagents.write_parent_planner_report(report, append_log=apply)
        return record

    def _build_planner_record(self, result, state, gate_summary, apply, runner_instruction):
        """Build parent planner record from LLM result."""
        from ..subagents.parsing import parse_parent_planner_output

        prompt_path, response_path = self.subagents.write_parent_planner_exchange(
            result.prompt,
            result.response,
        )
        parsed = parse_parent_planner_output(result.response)
        ok = parsed.found and parsed.ok
        decision = parsed.decision or "PARSE_ERROR"
        message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
        parse_error = parsed.parse_error

        if not parsed.found:
            ok = False
            decision = "PARSE_ERROR"
            parse_error = "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。"
            message = "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。"
        if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
            ok = False
            parse_error = parse_error or "planner gate blocked HEARTBEAT_OK"
            message = "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。"

        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not apply,
                triggered=True,
                ok=ok,
                decision=decision,
                message=message,
                gate_summary=gate_summary,
                backend=result.backend,
                tool_rounds=result.tool_rounds,
                parse_error=parse_error,
                summary=parsed.summary,
                actions=parsed.actions,
                blockers=parsed.blockers,
                risks=parsed.risks,
                notes=parsed.notes,
                runner_instruction=parsed.runner_instruction,
                suggested_max_runners=parsed.suggested_max_runners,
                prompt_path=prompt_path,
                response_path=response_path,
                evidence_paths=[prompt_path, response_path],
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
        self.subagents.write_parent_planner_report(report, append_log=apply)
        return record


class SimpleAgentSubagentMixin(
    _SubagentLifecycleMixin,
    _SubagentRepairMixin,
    _ParentPlannerMixin,
):
    """LLM: mixin for subagent lifecycle orchestration reachable from SimpleAgent.

    给人看的解释：
    这些方法不直接写文件细节，而是调用 SubAgentManager，让主代理保留"编排者"的角色。
    """
