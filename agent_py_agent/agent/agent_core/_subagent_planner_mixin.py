"""_ParentPlannerMixin and RunParentPlannerParams for parent planner LLM orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import ParentPlannerRecord
from ..subagents.services.dispatch_params import ParentPlannerRecordParams
from .planner import (
    PARENT_PLANNER_READ_TOOLS,
    _build_parent_planner_prompt,
    _build_parent_planner_state,
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
        """运行一轮父代理 LLM planner，并写出审计报告.

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