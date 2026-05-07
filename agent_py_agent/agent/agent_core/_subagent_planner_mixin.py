
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

    router: CapabilityRouter
    capability_config: CapabilityConfig | None
    apply: bool
    execute_runners: bool
    max_runners: int
    limit: int
    reviewer: str
    note: str
    runner_instruction: str


@dataclass(frozen=True)
class PlannerLLMParams:
    state: dict
    apply: bool
    execute_runners: bool
    max_runners: int
    runner_instruction: str


@dataclass(frozen=True)
class PlannerRecordBuildParams:
    result: object
    state: dict
    gate_summary: dict
    apply: bool
    runner_instruction: str


class _ParentPlannerMixin:

    def run_parent_planner(self, params: RunParentPlannerParams) -> ParentPlannerRecord:
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
            PlannerLLMParams(
                state=state,
                apply=params.apply,
                execute_runners=params.execute_runners,
                max_runners=params.max_runners,
                runner_instruction=params.runner_instruction,
            )
        )
        if result is None:
            return self._make_planner_error_record(
                gate_summary, params.runner_instruction, params.apply
            )
        return self._build_planner_record(
            PlannerRecordBuildParams(
                result=result,
                state=state,
                gate_summary=gate_summary,
                apply=params.apply,
                runner_instruction=params.runner_instruction,
            )
        )

    def _make_heartbeat_ok_record(self, dry_run, gate_summary):
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

    def _execute_planner_llm(self, params: PlannerLLMParams):
        prompt = _build_parent_planner_prompt(
            params.state,
            apply=params.apply,
            execute_runners=params.execute_runners,
            max_runners=params.max_runners,
            runner_instruction=params.runner_instruction,
        )
        self.subagents.write_parent_planner_exchange(prompt)
        try:
            return self.run(prompt, save=False, allowed_tools=PARENT_PLANNER_READ_TOOLS)
        except Exception:
            return None

    def _make_planner_error_record(self, gate_summary, runner_instruction, apply):
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

    def _build_planner_record(self, params: PlannerRecordBuildParams):
        from ..subagents.parsing import parse_parent_planner_output

        prompt_path, response_path = self.subagents.write_parent_planner_exchange(
            params.result.prompt,
            params.result.response,
        )
        parsed = parse_parent_planner_output(params.result.response)
        ok, decision, message, parse_error = _planner_record_status(parsed, params.state)

        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not params.apply,
                triggered=True,
                ok=ok,
                decision=decision,
                message=message,
                gate_summary=params.gate_summary,
                backend=params.result.backend,
                tool_rounds=params.result.tool_rounds,
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
        report = self.subagents.build_parent_planner_report([record], dry_run=not params.apply)
        self.subagents.write_parent_planner_report(report, append_log=params.apply)
        return record


def _planner_record_status(parsed, state: dict) -> tuple[bool, str, str, str]:
    # LLM: parser fallback rules stay separate from parent-planner record persistence.
    ok = parsed.found and parsed.ok
    decision = parsed.decision or "PARSE_ERROR"
    message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
    parse_error = parsed.parse_error
    if not parsed.found:
        return False, "PARSE_ERROR", "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。", "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。"
    if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
        return False, decision, "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。", parse_error or "planner gate blocked HEARTBEAT_OK"
    return ok, decision, message, parse_error
