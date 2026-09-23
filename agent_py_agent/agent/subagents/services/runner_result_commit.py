# LLM: 已持久化 runner 结果的初次收口按 WAL→RuntimeDB→父级 wake→已投递标记→清账推进；
#   复用 runtime_closeout 的权威原语和恢复扫描，不另建状态或改变 exact attempt 身份。
# 模块用途: 在子代理结果文件和任务投影保存后，可靠结算运行账并通知直属父级。
from __future__ import annotations

from typing import Any

from ..manager_runner_result_payload import RecordRunnerResultParams
from ..models import SubAgentRunnerResult, SubAgentTask
from .runtime_closeout import (
    REJECTED_CLOSEOUT_STATES,
    RETRYABLE_CLOSEOUT_STATES,
    clear_closeout,
    closeout_target_run_status,
    deliver_parent_wake,
    ensure_closeout_fact,
    mark_closeout_delivered,
    record_closeout_event,
    record_unpersisted_closeout,
    settle_runtime_run_for_result,
)


# LLM: 调用方已保存业务结果和 canonical task；本入口先持久化可恢复事实，再结算 exact run／attempt。
#   未落 WAL、收口待重试或权威终态冲突时不能进入父级投递，恢复只补账和通知、不重跑业务。
# 函数用途: 按原提交顺序推进一次子代理结果的运行收口，并在可交付时通知父级。
def commit_runner_result(
    manager: Any,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
    result: SubAgentRunnerResult,
    output_payload: dict[str, object],
) -> None:
    target_run_status = closeout_target_run_status(params, result, task)
    wal_state = "not_required"
    if target_run_status or params.failure_type == "executor_effects_unknown":
        wal_state = ensure_closeout_fact(
            manager, task, params, result,
            {"state": "pending", "target_run_status": target_run_status},
        )
        if wal_state == "unpersisted":
            record_unpersisted_closeout(manager, task, params, result)
            return
    outcome = settle_runtime_run_for_result(manager, task, params, result)
    state = str(outcome.get("state") or "")
    if state in RETRYABLE_CLOSEOUT_STATES:
        if wal_state != "not_required":
            ensure_closeout_fact(manager, task, params, result, outcome)
        record_closeout_event(
            manager, task, event_type="closeout_pending", params=params, outcome=outcome
        )
        return
    if state in REJECTED_CLOSEOUT_STATES:
        record_closeout_event(
            manager, task, event_type="closeout_blocked", params=params, outcome=outcome
        )
        if wal_state == "persisted":
            clear_closeout(manager, task)
        return
    _deliver_committed_result(
        manager, task, params, result, output_payload,
        wal_state=wal_state, outcome=outcome,
    )


# LLM: 只有已结算或原规则允许的无操作结果可到这里；唤醒按 exact attempt 去重，失败保留 WAL。
#   已交付标记必须成功持久化才可清账；失败时保留原 WAL，恢复链据原回执判断是否重发。
# 函数用途: 在收口已可交付后记录原调试踪迹、通知父级并安全推进交付账。
def _deliver_committed_result(
    manager: Any,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
    result: SubAgentRunnerResult,
    output_payload: dict[str, object],
    *,
    wal_state: str,
    outcome: dict[str, Any],
) -> None:
    from ..debug_trace import SubAgentRunnerTraceRequest, trace_runner_result

    trace_runner_result(SubAgentRunnerTraceRequest(manager, task, result, params))
    delivery = deliver_parent_wake(
        manager, task, result, output_payload,
        attempt_id=str(getattr(params, "attempt_id", "") or ""),
    )
    if wal_state == "persisted":
        if delivery == "failed":
            ensure_closeout_fact(manager, task, params, result, outcome)
        else:
            if not mark_closeout_delivered(manager, task, params, result, outcome):
                return
            clear_closeout(manager, task)
