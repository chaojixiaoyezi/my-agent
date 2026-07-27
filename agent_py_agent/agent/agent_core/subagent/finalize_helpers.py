
from __future__ import annotations

"""finalized subagent runner persistence helpers kept outside the lifecycle mixin."""

from dataclasses import dataclass

from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import FailureType, TaskStatus, VerificationStatus
from ...subagents.tool_failure_ledger import tool_failures_from_archive
from .params import RecoverySnapshotParams, SubagentFinalizeParams


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
    # 系统级工具失败账本(A1)的来源裁决:archive 为 None(result 缺字段/异常路径
    # 未填)= 拿不到系统数据,必须传 None 不覆盖旧账本;archive 是 list(正常轮,
    # 含空 list)才提取失败摘要,[] = 系统确认零失败。两个语义不可混淆。
    archive_calls = getattr(params.result, "archive_tool_calls", None)
    structured_missing = not bool(getattr(structured, "found", False))
    final_message = (
        _missing_structured_output_message(repair_state)
        if structured_missing
        else str(repair_state["message"])
    )
    return request.agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            # Missing machine output after the repair round is not success.
            # The old fallback wrote DONE/VERIFIED even for an empty response,
            # turning provider/model failures into false-green subagent facts.
            ok=bool(structured.ok) if structured.found else False,
            message=final_message,
            prompt=repair_state["prompt_for_log"],
            response=repair_state["response_for_log"],
            backend=repair_state["backend_name"],
            tool_rounds=params.result.tool_rounds,
            live_context_compaction=dict(
                getattr(params.result, "live_context_compaction", None) or {}
            ),
            status="" if structured.found else TaskStatus.BLOCKED.value,
            verification_status="" if structured.found else VerificationStatus.UNVERIFIED.value,
            failure_type=(
                "" if structured.found else FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value
            ),
            structured_output=structured,
            actual_tools=params.result.executed_tools or [],
            tool_failures=(
                None if archive_calls is None else tool_failures_from_archive(archive_calls)
            ),
            structured_repair_attempted=repair_state["attempted"],
            structured_repair_ok=repair_state["ok"],
            structured_repair_error=repair_state["error"],
        )
    )


def _missing_structured_output_message(repair_state: dict[str, object]) -> str:
    detail = str(repair_state.get("error") or "structured output missing after repair").strip()
    return f"runner structured output unavailable: {detail}"

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
