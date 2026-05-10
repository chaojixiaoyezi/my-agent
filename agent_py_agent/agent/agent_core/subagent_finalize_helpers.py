# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""finalized subagent runner persistence helpers kept outside the lifecycle mixin."""

from dataclasses import dataclass

from ..subagent import RecordRunnerResultParams, SubAgentParsedOutput
from ._subagent_repair_mixin import RecoverySnapshotParams
from .subagent_params import SubagentFinalizeParams


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
    structured = _coordinator_child_completion_override(request, request.structured)
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
            status="" if structured.found else "AWAITING_ACCEPTANCE",
            verification_status="" if structured.found else "NEEDS_ACCEPTANCE",
            structured_output=structured,
            actual_tools=params.result.executed_tools or [],
            structured_repair_attempted=repair_state["attempted"],
            structured_repair_ok=repair_state["ok"],
            structured_repair_error=repair_state["error"],
        )
    )


# LLM: _coordinator_child_completion_override prevents a coordinator from being marked failed after its children finished.
# 函数用途: 当 coordinator 已经把直接 child 都推进到 DONE/VERIFIED，但收尾因工具轮数上限误报 BLOCKED 时，改成等待父级验收。
def _coordinator_child_completion_override(
    request: FinalizedRunnerRecordRequest,
    structured: object,
) -> object:
    if not _is_coordinator_context(request.params.context):
        return structured
    if not _looks_like_tool_limit_cleanup(structured):
        return structured
    child_refs = _verified_direct_child_refs(request.agent, request.params.run_id)
    if not child_refs:
        return structured
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary=(
            "coordinator direct children already reached DONE/VERIFIED; "
            f"waiting for parent acceptance. children={', '.join(child_refs)}"
        ),
        evidence=[
            {
                "kind": "child_acceptance",
                "summary": "all direct children are DONE/VERIFIED",
                "path": "",
                "url": "",
                "ok": True,
            }
        ],
        capability_requests=[],
        artifacts=[],
        tests=[{
            "name": "direct children acceptance",
            "validation_method": "child_acceptance",
            "ok": True,
            "summary": "all direct children are DONE/VERIFIED",
        }],
        patches=[],
        lessons=[],
        next_actions=["parent_acceptance"],
    )


# LLM: _is_coordinator_context keeps the override limited to subagent leaders that can create children.
# 函数用途: 判断当前 runner 是否是带派工能力的协调类节点，避免普通 worker 被误套用 child 完成规则。
def _is_coordinator_context(context: object) -> bool:
    role_text = f"{getattr(context, 'role', '')} {getattr(context, 'agent_name', '')}".lower()
    tools = set(getattr(context, "allowed_tools", []) or [])
    return ("coordinator" in role_text or "lead" in role_text) and "schedule_child_subagents" in tools


# LLM: _looks_like_tool_limit_cleanup avoids overriding real capability or business blockers.
# 函数用途: 只对“缺结果块/工具轮数上限/收尾多查一次”这类清理失败放行，保留真实 BLOCKED。
def _looks_like_tool_limit_cleanup(structured: object) -> bool:
    if not bool(getattr(structured, "found", False)):
        return True
    if not bool(getattr(structured, "ok", False)):
        return False
    status = str(getattr(structured, "status", "") or "").strip().upper()
    has_capability_requests = bool(getattr(structured, "capability_requests", []) or [])
    if status not in {"BLOCKED", "FAILED", "TIMEOUT"} and not has_capability_requests:
        return False
    probe_text = " ".join(
        [
            *(str(getattr(structured, attr, "") or "") for attr in ("blocked_reason", "failure_type", "summary", "parse_error")),
            *[str(item) for item in (getattr(structured, "capability_requests", []) or [])],
        ]
    ).lower()
    return any(token in probe_text for token in ("max_tool", "tool_round", "工具", "轮数", "上限"))


# LLM: _verified_direct_child_refs checks refs-only child status before synthesizing coordinator completion.
# 函数用途: 读取当前 coordinator 的直接 child 列表，只有全部 DONE/VERIFIED 时才返回 child ids。
def _verified_direct_child_refs(agent: object, run_id: str) -> list[str]:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return []
    child_ids = [str(item) for item in (getattr(task, "child_ids", []) or []) if str(item).strip()]
    if not child_ids:
        return []
    verified: list[str] = []
    for child_id in child_ids:
        try:
            child = agent.subagents.load(child_id)
        except Exception:
            return []
        if str(getattr(child, "status", "")).upper() != "DONE":
            return []
        if str(getattr(child, "verification_status", "")).upper() != "VERIFIED":
            return []
        verified.append(child_id)
    return verified


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
