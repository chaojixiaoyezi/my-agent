from __future__ import annotations

"""LLM: implements SimpleAgent subagent spawning, runner execution, and parent-planner turns.

给人看的解释：
这个文件只管“主代理怎么和子代理互动”。
包括创建工单、跑一个子代理 runner、以及让父代理 planner 做一次完整模型判断。
"""

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..memory_archive import write_recovery_snapshot
from ..subagent import (
    ParentPlannerRecord,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_parent_planner_output,
    parse_subagent_runner_output,
)
from .planner import PARENT_PLANNER_READ_TOOLS, _build_parent_planner_prompt, _build_parent_planner_state
from .runner_prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_prompt,
    _build_subagent_runner_repair_prompt,
)


class SimpleAgentSubagentMixin:
    """LLM: mixin for subagent lifecycle orchestration reachable from SimpleAgent.

    给人看的解释：
    这些方法不直接写文件细节，而是调用 SubAgentManager，让主代理保留“编排者”的角色。
    """

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:
        """生成子任务记录。"""

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")
        n = min(count or self.config.max_subagents, self.config.max_subagents)
        return self.subagents.split(goal, n)

    def run_subagent(
        self,
        run_id: str,
        *,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
        retry_reason: str = "",
    ) -> SubAgentRunnerResult:
        """按执行上下文运行一个子代理入口。

        第一版 runner 不负责并行调度，只负责把“上下文 -> 模型执行 -> 工单回写”
        这条最小链路打通。默认 dry-run，避免误触真实模型接口。
        """

        if not dry_run:
            self.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)

        context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        if dry_run:
            return self.subagents.record_runner_result(
                run_id,
                dry_run=True,
                ok=True,
                message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
                prompt=prompt,
            )

        if probe:
            probe_result = self.subagents.probe_channel(run_id)
            if probe_result.channel_status == "BROKEN":
                context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
                prompt = _build_subagent_runner_prompt(context, instruction)
                return self.subagents.record_runner_result(
                    run_id,
                    dry_run=False,
                    ok=False,
                    message="通道健康检查为 BROKEN，未启动模型执行。",
                    prompt=prompt,
                    status="CHANNEL_ERROR",
                    verification_status="UNVERIFIED",
                    failure_type="channel",
                )
            context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
            prompt = _build_subagent_runner_prompt(context, instruction)

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
            failed_result = self.subagents.record_runner_result(
                run_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {exc}",
                prompt=prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_error",
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

        structured = parse_subagent_runner_output(result.response)
        prompt_for_log = result.prompt
        response_for_log = result.response
        backend_name = result.backend
        message = "runner 已完成模型调用，等待独立验收。"
        structured_repair_attempted = False
        structured_repair_ok = False
        structured_repair_error = ""
        if not (structured.found and structured.ok):
            structured_repair_attempted = True
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
            else:
                repaired = parse_subagent_runner_output(repair_response.text)
                prompt_for_log = _append_runner_repair_prompt(result.prompt, repair_prompt)
                response_for_log = _append_runner_repair_response(
                    result.response,
                    repair_response.text,
                )
                backend_name = repair_response.backend or result.backend
                if repaired.found and repaired.ok:
                    structured = repaired
                    structured_repair_ok = True
                    message = "runner 已完成模型调用，并已修复结构化结果，等待独立验收。"
                elif not structured.found and repaired.found:
                    structured = repaired
                    structured_repair_error = repaired.parse_error
                else:
                    structured_repair_error = repaired.parse_error or "repair response still missing structured output"
        runner_result = self.subagents.record_runner_result(
            run_id,
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
        next_actions = ["先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"]
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
            session_id=getattr(self, "session_id", self.config.agent_name),
            request_id=f"subagent-run:{run_id}",
            run_id=run_id,
            task_id=run_id,
            user_prompt=user_prompt,
            response_text=response_text,
            backend=backend,
            source="subagent_run",
            status=status.lower() or "unknown",
            error_code=error_code,
            tool_calls=tool_calls,
            task_refs=[run_id],
            content_paths=content_paths,
            next_actions=next_actions,
            archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
        )

    def run_parent_planner(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        execute_runners: bool = False,
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
    ) -> ParentPlannerRecord:
        """运行一轮父代理 LLM planner，并写出审计报告。

        planner 不是 heartbeat 的浅层 OK，而是一个完整模型 turn。只有状态门禁发现
        有 active/pending/stalled/needs-intervention 事项时，才真正调用模型；如果模型
        在有事时只回 HEARTBEAT_OK，会被标记为失败。
        """

        cfg = capability_config or CapabilityConfig()
        state = _build_parent_planner_state(
            self,
            cfg,
            max_runners=max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
        )
        gate = state["gate"]
        gate_summary = {key: int(value) for key, value in gate.items() if isinstance(value, int)}
        if not gate.get("needs_planner", 0):
            record = self.subagents.make_parent_planner_record(
                dry_run=not apply,
                triggered=False,
                ok=True,
                decision="HEARTBEAT_OK",
                message="planner gate 确认无 active/pending/stalled/needs-intervention 事项，允许 HEARTBEAT_OK。",
                gate_summary=gate_summary,
                summary="no work",
            )
            report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
            self.subagents.write_parent_planner_report(report, append_log=apply)
            return record

        prompt = _build_parent_planner_prompt(
            state,
            apply=apply,
            execute_runners=execute_runners,
            max_runners=max_runners,
            runner_instruction=runner_instruction,
        )
        prompt_path, response_path = self.subagents.write_parent_planner_exchange(prompt)
        try:
            result = self.run(
                prompt,
                save=False,
                allowed_tools=PARENT_PLANNER_READ_TOOLS,
            )
        except Exception as exc:
            record = self.subagents.make_parent_planner_record(
                dry_run=not apply,
                triggered=True,
                ok=False,
                decision="PLANNER_ERROR",
                message=f"父代理 planner 调用失败: {exc}",
                gate_summary=gate_summary,
                prompt_path=prompt_path,
                response_path=response_path,
                evidence_paths=[prompt_path],
            )
            report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
            self.subagents.write_parent_planner_report(report, append_log=apply)
            return record

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
        if parsed.decision == "HEARTBEAT_OK" and gate.get("needs_planner", 0):
            ok = False
            parse_error = parse_error or "planner gate blocked HEARTBEAT_OK"
            message = "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。"

        record = self.subagents.make_parent_planner_record(
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
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
        self.subagents.write_parent_planner_report(report, append_log=apply)
        return record
