

from __future__ import annotations

from dataclasses import dataclass

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ..runtime_errors import runtime_error_report
from ..subagents import ParentPlannerRecord
from ..subagents.services.dispatch.params import ParentPlannerRecordParams
from .orchestration.dispatch.params import DispatchExecutionPlan
from .planner_service import PlannerPromptParams
from .planner_service import (
    build_parent_planner_prompt as _build_parent_planner_prompt,
)
from .planner_service import (
    build_parent_planner_state as _build_parent_planner_state,
)
from .planner_templates import PARENT_PLANNER_SYSTEM_PROMPT


@dataclass(frozen=True)
class RunParentPlannerParams:

    router: CapabilityRouter
    capability_config: CapabilityConfig | None
    execution_plan: DispatchExecutionPlan
    max_runners: int
    limit: int
    reviewer: str
    note: str
    runner_instruction: str


@dataclass(frozen=True)
class PlannerLLMParams:
    state: dict
    execution_plan: DispatchExecutionPlan
    runner_instruction: str


@dataclass(frozen=True)
class PlannerRecordBuildParams:
    result: object
    state: dict
    gate_summary: dict
    mutate_state: bool
    runner_instruction: str


@dataclass(frozen=True)
class PlannerErrorRecordParams:
    gate_summary: dict
    mutate_state: bool
    runner_instruction: str
    runtime_error: dict[str, object] | None = None


@dataclass(frozen=True)
class PlannerLLMResult:
    response: object | None
    runtime_error: dict[str, object] | None = None


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
            return self._make_heartbeat_ok_record(not params.execution_plan.mutate_state, gate_summary)

        result = self._execute_planner_llm(
            PlannerLLMParams(
                state=state,
                execution_plan=params.execution_plan,
                runner_instruction=params.runner_instruction,
            )
        )
        if result.response is None:
            return self._make_planner_error_record(
                PlannerErrorRecordParams(
                    gate_summary=gate_summary,
                    runner_instruction=params.runner_instruction,
                    mutate_state=params.execution_plan.mutate_state,
                    runtime_error=result.runtime_error,
                )
            )
        return self._build_planner_record(
            PlannerRecordBuildParams(
                result=result.response,
                state=state,
                gate_summary=gate_summary,
                mutate_state=params.execution_plan.mutate_state,
                runner_instruction=params.runner_instruction,
            )
        )

    def _make_heartbeat_ok_record(self, dry_run, gate_summary):
        record = self.subagents.parent_planner.make_parent_planner_record(
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
        report = self.subagents.parent_planner.build_parent_planner_report([record], dry_run=dry_run)
        self.subagents.parent_planner.write_parent_planner_report(report, append_log=False)
        return record

    def _execute_planner_llm(self, params: PlannerLLMParams):
        prompt = _build_parent_planner_prompt(
            params.state,
            params=PlannerPromptParams(
                params.execution_plan,
                params.runner_instruction,
            ),
        )
        self.subagents.parent_planner.write_parent_planner_exchange(prompt)
        try:
            response = self.run(
                prompt,
                save=False,
                allowed_tools=[],
                resume_context=False,
                system_prompt_override=PARENT_PLANNER_SYSTEM_PROMPT,
                context_scope="control_plane",
            )
        except Exception as exc:
            return PlannerLLMResult(
                response=None,
                runtime_error=runtime_error_report(exc, context="parent_planner.run"),
            )
        return PlannerLLMResult(response=response)

    def _make_planner_error_record(self, params: PlannerErrorRecordParams):
        record = self.subagents.parent_planner.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not params.mutate_state,
                triggered=True,
                ok=False,
                decision="PLANNER_ERROR",
                message="父代理 planner 调用失败",
                gate_summary=params.gate_summary,
                runner_instruction=params.runner_instruction,
                runtime_error=params.runtime_error,
                evidence_paths=[],
            ),
        )
        report = self.subagents.parent_planner.build_parent_planner_report([record], dry_run=not params.mutate_state)
        self.subagents.parent_planner.write_parent_planner_report(report, append_log=params.mutate_state)
        return record

    def _build_planner_record(self, params: PlannerRecordBuildParams):
        from ..subagents.parsing import parse_parent_planner_output

        prompt_path, response_path = self.subagents.parent_planner.write_parent_planner_exchange(
            params.result.prompt,
            params.result.response,
        )
        parsed = parse_parent_planner_output(params.result.response)
        ok, decision, message, parse_error = _planner_record_status(parsed, params.state)

        record = self.subagents.parent_planner.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not params.mutate_state,
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
        report = self.subagents.parent_planner.build_parent_planner_report([record], dry_run=not params.mutate_state)
        self.subagents.parent_planner.write_parent_planner_report(report, append_log=params.mutate_state)
        return record


def _planner_record_status(parsed, state: dict) -> tuple[bool, str, str, str]:
    ok = parsed.found and parsed.ok
    decision = parsed.decision or "PARSE_ERROR"
    message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
    parse_error = parsed.parse_error
    if not parsed.found:
        return False, "PARSE_ERROR", "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。", "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。"
    if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
        return False, decision, "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。", parse_error or "planner gate blocked HEARTBEAT_OK"
    return ok, decision, message, parse_error
