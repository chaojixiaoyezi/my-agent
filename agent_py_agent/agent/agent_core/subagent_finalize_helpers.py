# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""finalized subagent runner persistence helpers kept outside the lifecycle mixin."""

from dataclasses import dataclass

from ..subagent import RecordRunnerResultParams
from ._subagent_repair_mixin import RecoverySnapshotParams
from .subagent_params import SubagentFinalizeParams
from .subagent_session_compact_payload import subagent_session_compact_payload_from_result


# LLM: FinalizedRunnerRecordRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存finalized执行器记录请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class FinalizedRunnerRecordRequest:
    agent: object
    params: SubagentFinalizeParams
    structured: object
    repair_state: dict


# LLM: FinalizedRecoverySnapshotRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存finalized恢复snapshot请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class FinalizedRecoverySnapshotRequest:
    agent: object
    params: SubagentFinalizeParams
    runner_result: object
    repair_state: dict


# LLM: record_finalized_runner_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入finalized执行器结果的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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


# LLM: _subagent_session_compact_payload converts save=False compact signals into task-local package facts.
# 函数用途: 从 AgentRunResult 提取自动 compact 建议；子代理不会写主 memory，只把这些字段交给 task-local writer。
def _subagent_session_compact_payload(result: object) -> dict[str, object]:
    return subagent_session_compact_payload_from_result(result)


# LLM: write_finalized_recovery_snapshot 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入finalized恢复snapshot的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def write_finalized_recovery_snapshot(request: FinalizedRecoverySnapshotRequest) -> None:
    request.agent._write_subagent_recovery_snapshot(
        params=RecoverySnapshotParams(
            run_id=request.params.run_id,
            user_prompt=request.params.context.goal,
            response_text=str(request.repair_state["message"]),
            backend=str(request.repair_state["backend_name"]),
            status=request.runner_result.status,
            error_code=request.runner_result.runner_last_error,
            tool_calls=[
                {"tool": tool_name, "id": f"{request.params.run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(request.params.result.executed_tools or [], start=1)
            ],
        )
    )
