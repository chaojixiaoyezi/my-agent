

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..backends import (
    is_provider_quota_exhausted_error,
    is_provider_timeout_error,
    is_provider_transient_error,
)
from ..capability.config import CapabilityConfig
from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..runtime_errors import runtime_error_report
from ..subagents import ParentPlannerRecord
from ..subagents.manager_runner_result_payload import RecordRunnerResultParams
from ..subagents.models import (
    FailureType,
    SubAgentRunnerResult,
    SubAgentTask,
    task_status_reason_code,
)
from ..subagents.parsing import parse_parent_planner_output
from ..subagents.services.dispatch.params import ParentPlannerRecordParams
from .planner_service import (
    PARENT_PLANNER_SYSTEM_PROMPT,
    PlannerPromptParams,
    append_planner_memory_hint,
)
from .planner_service import (
    build_parent_planner_prompt as _build_parent_planner_prompt,
)
from .planner_service import build_parent_planner_state as _build_parent_planner_state
from .runner.prompts import _build_subagent_runner_prompt
from .runtime.owner_roots import runtime_owner_root
from .subagent.finalize_helpers import (
    FinalizedRecoverySnapshotRequest,
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
    write_finalized_recovery_snapshot,
)
from .subagent.lifecycle_service import SubagentLifecycleService
from .subagent.params import (
    RecoverySnapshotParams,
    RunParentPlannerParams,
    SpawnSubagentsParams,
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
    spawn_subagents_params,
    subagent_run_params,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerLLMParams:
    state: dict
    execution_plan: object
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
        return _build_subagent_prompt(self, run_id, max_cards, instruction)

    def _record_subagent_dry_run(self, run_id, active_attempt_id, prompt):
        return _record_subagent_dry_run(self, run_id, active_attempt_id, prompt)

    def _probe_subagent_channel(self, params: SubagentProbeParams):
        return _probe_subagent_channel(self, params)

    def _write_subagent_recovery_snapshot(
        self,
        run_id: str | None = None,
        *,
        params: RecoverySnapshotParams | None = None,
        user_prompt: str = "",
        response_text: str = "",
        backend: str = "",
        status: str = "",
        error_code: str = "",
        tool_calls: list[dict] | None = None,
    ) -> None:
        _write_subagent_recovery_snapshot(
            self,
            _recovery_snapshot_params(
                params,
                run_id=run_id,
                user_prompt=user_prompt,
                response_text=response_text,
                backend=backend,
                status=status,
                error_code=error_code,
                tool_calls=tool_calls,
            ),
        )

    def _handle_subagent_run_failure(self, params: SubagentRunFailureParams):
        return _handle_subagent_run_failure(self, params)

    def _finalize_subagent_run(self, params: SubagentFinalizeParams):
        return _finalize_subagent_run(self, params)

    def run_parent_planner(self, params: RunParentPlannerParams) -> ParentPlannerRecord:
        return _run_parent_planner(self, params)


def _build_subagent_prompt(agent, run_id, max_cards, instruction):
    context = agent.subagents.runner_context.write_execution_context(run_id, max_cards=max_cards)
    prompt = _build_subagent_runner_prompt(context, instruction)
    return context, prompt


def _record_subagent_dry_run(agent, run_id, active_attempt_id, prompt):
    return agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=active_attempt_id,
            dry_run=True,
            ok=True,
            message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
            prompt=prompt,
        )
    )


def _probe_subagent_channel(agent, params: SubagentProbeParams):
    if not params.probe:
        return None
    probe_result = agent.subagents.channel_probe.probe_channel(params.run_id)
    if probe_result.channel_status != "BROKEN":
        return None
    context, prompt = agent._build_subagent_prompt(params.run_id, params.max_cards, params.instruction)
    return agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=False,
            message="通道健康检查为 BROKEN，未启动模型执行。",
            prompt=prompt,
            status="CHANNEL_ERROR",
            turn_end_reason="error",
            verification_status="UNVERIFIED",
            failure_type=FailureType.CHANNEL.value,
        )
    )


def _write_subagent_recovery_snapshot(agent, snapshot: RecoverySnapshotParams) -> None:
    if not bool(getattr(agent.config, "memory_hook_enabled", True)):
        return
    try:
        task = agent.subagents.load(snapshot.run_id)
    except (FileNotFoundError, TypeError):
        task = None
    write_recovery_snapshot(
        _subagent_recovery_snapshot_root(agent, task),
        params=_recovery_snapshot_input(agent, snapshot, _recovery_content_paths(task)),
    )


def _handle_subagent_run_failure(agent, params: SubagentRunFailureParams):
    failure_type = _subagent_run_failure_type(params.exc)
    failed_result = agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=False,
            message=f"runner 执行失败: {params.exc}",
            prompt=params.prompt,
            status="FAILED",
            turn_end_reason="error",
            verification_status="UNVERIFIED",
            failure_type=failure_type,
        )
    )
    agent._write_subagent_recovery_snapshot(
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


def _finalize_subagent_run(agent, params: SubagentFinalizeParams):
    runner_result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, params)
    )
    write_finalized_recovery_snapshot(FinalizedRecoverySnapshotRequest(agent, params, runner_result))
    return runner_result


class SimpleAgentSubagentMixin(_SubagentLifecycleBase):
    pass


def _run_parent_planner(agent, params: RunParentPlannerParams) -> ParentPlannerRecord:
    cfg = params.capability_config or CapabilityConfig()
    state = _build_parent_planner_state(
        agent,
        cfg,
        max_runners=params.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
    )
    gate_summary = {key: int(value) for key, value in state["gate"].items() if isinstance(value, int)}

    if not state["gate"].get("needs_planner", 0):
        return _make_heartbeat_ok_record(
            agent,
            not params.execution_plan.mutate_state,
            gate_summary,
        )

    result = _execute_planner_llm(
        agent,
        PlannerLLMParams(
            state=state,
            execution_plan=params.execution_plan,
            runner_instruction=params.runner_instruction,
        ),
    )
    if result.response is None:
        return _make_planner_error_record(
            agent,
            PlannerErrorRecordParams(
                gate_summary=gate_summary,
                runner_instruction=params.runner_instruction,
                mutate_state=params.execution_plan.mutate_state,
                runtime_error=result.runtime_error,
            ),
        )
    return _build_planner_record(
        agent,
        PlannerRecordBuildParams(
            result=result.response,
            state=state,
            gate_summary=gate_summary,
            mutate_state=params.execution_plan.mutate_state,
            runner_instruction=params.runner_instruction,
        ),
    )


def _make_heartbeat_ok_record(agent, dry_run, gate_summary):
    record = agent.subagents.parent_planner.make_parent_planner_record(
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
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=dry_run,
    )
    agent.subagents.parent_planner.write_parent_planner_report(report, append_log=False)
    return record


def _execute_planner_llm(agent, params: PlannerLLMParams):
    prompt = _build_parent_planner_prompt(
        params.state,
        params=PlannerPromptParams(
            params.execution_plan,
            params.runner_instruction,
        ),
    )
    # 记忆推模式(B1):planner 决策点自动注入相关教训(软注入,失败不阻断)。
    prompt = append_planner_memory_hint(agent, params.state, prompt)
    agent.subagents.parent_planner.write_parent_planner_exchange(prompt)
    try:
        response = agent.run(
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


def _make_planner_error_record(agent, params: PlannerErrorRecordParams):
    record = agent.subagents.parent_planner.make_parent_planner_record(
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
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=not params.mutate_state,
    )
    agent.subagents.parent_planner.write_parent_planner_report(
        report,
        append_log=params.mutate_state,
    )
    return record


def _build_planner_record(agent, params: PlannerRecordBuildParams):
    prompt_path, response_path = agent.subagents.parent_planner.write_parent_planner_exchange(
        params.result.prompt,
        params.result.response,
    )
    parsed = parse_parent_planner_output(params.result.response)
    ok, decision, message, parse_error = _planner_record_status(parsed, params.state)

    record = agent.subagents.parent_planner.make_parent_planner_record(
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
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=not params.mutate_state,
    )
    agent.subagents.parent_planner.write_parent_planner_report(
        report,
        append_log=params.mutate_state,
    )
    return record


def _recovery_snapshot_params(
    params: RecoverySnapshotParams | None,
    *,
    run_id: str | None,
    user_prompt: str,
    response_text: str,
    backend: str,
    status: str,
    error_code: str,
    tool_calls: list[dict] | None,
) -> RecoverySnapshotParams:
    if params is not None:
        if not isinstance(params, RecoverySnapshotParams):
            raise TypeError("subagent recovery snapshot requires params: RecoverySnapshotParams")
        return params
    return RecoverySnapshotParams(
        run_id=str(run_id or ""),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        status=str(status),
        error_code=str(error_code),
        tool_calls=list(tool_calls or []),
    )


def _recovery_content_paths(task) -> list[str]:
    if task is None:
        return []
    return [
        task.status_file,
        task.work_log_file,
        task.runner_result_file,
        task.runner_result_json,
        task.output_json,
        task.handoff_file,
    ]


def _subagent_recovery_snapshot_root(agent, task: SubAgentTask | None) -> Path:
    raw_workspace = getattr(task, "agent_run_workspace_dir", "") if task is not None else ""
    workspace = str(raw_workspace).strip() if isinstance(raw_workspace, str | Path) else ""
    if workspace:
        return Path(workspace).expanduser().resolve(strict=False)
    return runtime_owner_root(agent)


def _recovery_snapshot_input(
    agent,
    snapshot: RecoverySnapshotParams,
    content_paths: list[str],
):
    next_actions = [
        "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
    ]
    return RecoverySnapshotInput(
        session_id=getattr(agent, "session_id", agent.config.agent_name),
        user_prompt=snapshot.user_prompt,
        response_text=snapshot.response_text,
        backend=snapshot.backend,
        source="subagent_run",
        request_id=f"subagent-run:{snapshot.run_id}",
        run_id=snapshot.run_id,
        task_id=snapshot.run_id,
        status=task_status_reason_code(snapshot.status) or "unknown",
        error_code=snapshot.error_code,
        tool_calls=snapshot.tool_calls,
        task_refs=[snapshot.run_id],
        content_paths=content_paths,
        next_actions=next_actions,
        archive_level=int(getattr(agent.config, "memory_hook_archive_level", 3)),
    )


def _planner_record_status(parsed, state: dict) -> tuple[bool, str, str, str]:
    ok = parsed.found and parsed.ok
    decision = parsed.decision or "PARSE_ERROR"
    message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
    parse_error = parsed.parse_error
    if not parsed.found:
        return (
            False,
            "PARSE_ERROR",
            "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。",
            "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。",
        )
    if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
        return (
            False,
            decision,
            "状态快照仍有待处理事项，planner 只返回了 HEARTBEAT_OK；请给出推进建议。",
            parse_error or "planner_empty_heartbeat_with_pending_work",
        )
    return ok, decision, message, parse_error


def _subagent_run_failure_type(exc: BaseException) -> str:
    if is_provider_timeout_error(exc):
        return FailureType.PROVIDER_TIMEOUT.value
    if is_provider_quota_exhausted_error(exc):
        return FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    if is_provider_transient_error(exc):
        return FailureType.TRANSIENT_ERROR.value
    return FailureType.RUNNER_ERROR.value
