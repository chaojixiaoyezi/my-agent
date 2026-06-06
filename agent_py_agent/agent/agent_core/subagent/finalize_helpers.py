
from __future__ import annotations

"""finalized subagent runner persistence helpers kept outside the lifecycle mixin."""

from dataclasses import dataclass

from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from .._subagent_repair_mixin import RecoverySnapshotParams
from .params import SubagentFinalizeParams
from .session_compact_payload import subagent_session_compact_payload_from_result


@dataclass(frozen=True)
class FinalizedRunnerRecordRequest:
    agent: object
    params: SubagentFinalizeParams
    structured: object
    repair_state: dict


@dataclass(frozen=True)
class FinalizedRecoverySnapshotRequest:
    agent: object
    params: SubagentFinalizeParams
    runner_result: object
    repair_state: dict


def record_finalized_runner_result(request: FinalizedRunnerRecordRequest):
    params = request.params
    structured = request.structured
    repair_state = request.repair_state
    return request.agent.subagents.record_runner_result(
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
            status="" if structured.found else "DONE",
            verification_status="" if structured.found else "VERIFIED",
            structured_output=structured,
            actual_tools=params.result.executed_tools or [],
            structured_repair_attempted=repair_state["attempted"],
            structured_repair_ok=repair_state["ok"],
            structured_repair_error=repair_state["error"],
            session_compact=_subagent_session_compact_payload(params.result),
        )
    )


def _subagent_session_compact_payload(result: object) -> dict[str, object]:
    return subagent_session_compact_payload_from_result(result)


def write_finalized_recovery_snapshot(request: FinalizedRecoverySnapshotRequest) -> None:
    request.agent._write_subagent_recovery_snapshot(
        params=RecoverySnapshotParams(
            run_id=request.params.run_id,
            user_prompt=request.params.context.goal,
            response_text=str(request.repair_state["message"]),
            backend=str(request.repair_state["backend_name"]),
            status=request.runner_result.status,
            error_code=str(getattr(request.runner_result, "failure_type", "") or ""),
            tool_calls=[
                {"tool": tool_name, "id": f"{request.params.run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(request.params.result.executed_tools or [], start=1)
            ],
        )
    )
