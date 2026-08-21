from __future__ import annotations

"""模块用途: 把子代理的一轮宿主运行结果持久化并写入恢复快照。

LLM: 本模块只消费 AgentRunResult 的运行字段与工具账本；生命周期由
turn_end_reason 映射，禁止解析模型正文、测试、产物或验收声明来改写状态。
"""

from dataclasses import dataclass

from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.tool_failure_ledger import tool_failures_from_archive
from ...turn_end import infer_turn_end_reason, subagent_outcome_for_turn_end
from .params import RecoverySnapshotParams, SubagentFinalizeParams


# LLM: 该请求只封装 runner 本轮的宿主结果，不再携带模型生成的验收结构。
# 类用途: 把 agent 与待落盘的子代理运行参数捆在一起，避免长参数列表。
@dataclass(frozen=True)
class FinalizedRunnerRecordRequest:
    agent: object
    params: SubagentFinalizeParams


# LLM: 恢复快照必须复用刚持久化的 runner 结果与同一轮参数。
# 类用途: 打包子代理恢复快照所需的三个对象。
@dataclass(frozen=True)
class FinalizedRecoverySnapshotRequest:
    agent: object
    params: SubagentFinalizeParams
    runner_result: object


# LLM: 生命周期只由 infer_turn_end_reason + subagent_outcome_for_turn_end 决定；
# 模型正文只作为 response 原样归档，绝不参与 status/ok/failure_type 计算。
# 函数用途: 记录子代理自然收口、阻塞、取消、截断、中断或错误的真实结果。
def record_finalized_runner_result(request: FinalizedRunnerRecordRequest):
    params = request.params
    archive_calls = getattr(params.result, "archive_tool_calls", None)
    turn_end_reason = infer_turn_end_reason(
        explicit=getattr(params.result, "turn_end_reason", ""),
        runtime_status=getattr(params.result, "runtime_status", ""),
        runtime_reason=getattr(params.result, "runtime_reason", ""),
    )
    status, failure_type, ok = subagent_outcome_for_turn_end(turn_end_reason)
    return request.agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=ok,
            message=_turn_end_message(params.result, turn_end_reason),
            prompt=str(
                getattr(params.result, "prompt", "")
                or getattr(params, "prompt", "")
                or ""
            ),
            response=str(getattr(params.result, "response", "") or ""),
            backend=str(getattr(params.result, "backend", "") or ""),
            tool_rounds=int(getattr(params.result, "tool_rounds", 0) or 0),
            status=status,
            turn_end_reason=turn_end_reason,
            failure_type=failure_type,
            structured_output=None,
            actual_tools=list(getattr(params.result, "executed_tools", None) or []),
            tool_failures=(
                None if archive_calls is None else tool_failures_from_archive(archive_calls)
            ),
        )
    )


# LLM: 只读取运行字段，不能摘要或判断 response 正文。
# 函数用途: 为父代理通知生成一句短的 runner 结束说明。
def _turn_end_message(result: object, turn_end_reason: str) -> str:
    detail = str(getattr(result, "runtime_reason", "") or "").strip()
    suffix = f" ({detail})" if detail else ""
    return f"runner 本轮结束: {turn_end_reason}{suffix}"


# LLM: 快照是恢复证据，不是第二次验收；错误码沿用 runner 结构化失败字段。
# 函数用途: 把本轮原始回复、后端与工具清单写入既有恢复快照。
def write_finalized_recovery_snapshot(request: FinalizedRecoverySnapshotRequest) -> None:
    request.agent._write_subagent_recovery_snapshot(
        params=RecoverySnapshotParams(
            run_id=request.params.run_id,
            user_prompt=request.params.context.goal,
            response_text=str(getattr(request.params.result, "response", "") or ""),
            backend=str(getattr(request.params.result, "backend", "") or ""),
            status=request.runner_result.status,
            error_code=str(getattr(request.runner_result, "failure_type", "") or ""),
            tool_calls=[
                {"tool": tool_name, "id": f"{request.params.run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(
                    getattr(request.params.result, "executed_tools", None) or [], start=1
                )
            ],
        )
    )
