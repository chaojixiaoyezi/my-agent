

from __future__ import annotations

import logging

from ..backends import is_provider_timeout_error, is_provider_transient_error
from ..subagents.manager_runner_result_payload import RecordRunnerResultParams
from ..subagents.models import SubAgentRunnerResult, SubAgentTask
from ..subagents.parsing import parse_subagent_runner_output
from ._subagent_planner_mixin import _ParentPlannerMixin
from ._subagent_repair_mixin import (
    RecoverySnapshotParams,
    SubagentRepairParams,
    _SubagentRepairMixin,
)
from .planner_service import build_parent_planner_state as _build_parent_planner_state
from .runner.prompts import (
    _build_subagent_runner_prompt,
)
from .subagent.finalize_helpers import (
    FinalizedRecoverySnapshotRequest,
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
    write_finalized_recovery_snapshot,
)
from .subagent.lifecycle_service import SubagentLifecycleService
from .subagent.params import (
    SpawnSubagentsParams,
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
    spawn_subagents_params,
    subagent_run_params,
)

logger = logging.getLogger(__name__)


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

    _subagent_lifecycle_service: SubagentLifecycleService | None = None

    def _get_subagent_lifecycle_service(self) -> SubagentLifecycleService:
        if self._subagent_lifecycle_service is None:
            self._subagent_lifecycle_service = SubagentLifecycleService(self)
        return self._subagent_lifecycle_service

    def spawn_subagents(
        self,
        goal: str | None = None,
        count: int | None = None,
        *,
        params: SpawnSubagentsParams | None = None,
    ) -> list[SubAgentTask]:

        options = spawn_subagents_params(
            params,
            goal=goal,
            count=count,
        )
        return self._get_subagent_lifecycle_service().spawn_subagents(options)

    def run_subagent(
        self,
        run_id: str | None = None,
        *,
        params: SubagentRunParams | None = None,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
        retry_reason: str = "",
        attempt_id: str = "",
    ) -> SubAgentRunnerResult:
        options = subagent_run_params(
            params,
            run_id=run_id,
            instruction=instruction,
            dry_run=dry_run,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
            attempt_id=attempt_id,
        )
        return self._get_subagent_lifecycle_service().run_subagent(options)

    def _build_subagent_prompt(self, run_id, max_cards, instruction):
        context = self.subagents.runner_context.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        return context, prompt

    def _record_subagent_dry_run(self, run_id, active_attempt_id, prompt):
        return self.subagents.runner_result.record_runner_result(
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
        probe_result = self.subagents.channel_probe.probe_channel(params.run_id)
        if probe_result.channel_status != "BROKEN":
            return None
        context, prompt = self._build_subagent_prompt(
            params.run_id, params.max_cards, params.instruction
        )
        return self.subagents.runner_result.record_runner_result(
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
        failure_type = _subagent_run_failure_type(params.exc)
        failed_result = self.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {params.exc}",
                prompt=params.prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type=failure_type,
            )
        )
        self._write_subagent_recovery_snapshot(
            params=RecoverySnapshotParams(
                run_id=params.run_id,
                user_prompt=params.context.goal,
                response_text=failed_result.message,
                backend="",
                status=failed_result.status,
                error_code=failure_type,
                tool_calls=[],
            )
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

        runner_result = record_finalized_runner_result(
            FinalizedRunnerRecordRequest(self, params, structured, repair_state)
        )
        write_finalized_recovery_snapshot(FinalizedRecoverySnapshotRequest(self, params, runner_result, repair_state))
        return runner_result

class SimpleAgentSubagentMixin(
    _SubagentLifecycleBase,
    _SubagentRepairMixin,
    _ParentPlannerMixin,
):
    pass


def _subagent_run_failure_type(exc: BaseException) -> str:
    if is_provider_timeout_error(exc):
        return "provider_timeout"
    if is_provider_transient_error(exc):
        return "transient_error"
    return "runner_error"
