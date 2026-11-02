# LLM: 正MP沿原请求短事务限制既有写者，不改变main canonical/RuntimeDB/F4和Count0逻辑，检查所有原调用方锁序。
# 模块用途: 维护原子代理写入与收口，原执行撤权后拒绝迟到副作用。
# LLM: 已持久化 runner 结果的初次收口按 WAL→RuntimeDB→父级 wake→已投递标记→清账推进；
#   显式接收原 RuntimeDB、保存及绑定交付回调，不能从大上下文取得额外权限；复用 runtime_closeout 的权威原语和恢复扫描，不另建状态或改变 exact attempt 身份。
# 模块用途: 在子代理结果文件和任务投影保存后，可靠结算运行账并通知直属父级。
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...common.request_execution_ports import gateway_short_transaction

if TYPE_CHECKING:
    from ...runtime_db.repository import RuntimeRepository

from ..manager_runner_result_payload import RecordRunnerResultParams
from ..models import SubAgentRunnerResult, SubAgentTask
from .runtime_closeout import (
    REJECTED_CLOSEOUT_STATES,
    RETRYABLE_CLOSEOUT_STATES,
    clear_closeout,
    closeout_target_run_status,
    ensure_closeout_fact,
    mark_closeout_delivered,
    record_closeout_event,
    record_unpersisted_closeout,
    settle_runtime_run_for_result,
)

# 提交准备的三种结果：收口事实已与 task 同次落盘、本次无需收口、两次保存都失败（已另记原兜底事件）。
_PREPARED_STATES = frozenset({"persisted", "not_required", "unpersisted"})


# LLM: 调用方已把最终 canonical 字段组装到 task，且原结果/输出文件已落盘；需要收口时把原 exact pending 收口事实
#   与首个终态 task（含清活动 attempt）经同一次 save 落盘，两次失败记原 closeout_unpersisted 兜底事件。
#   无需收口时不写任何东西，由调用方决定是否做普通 canonical 保存；本函数不结算 RuntimeDB、不通知父级。
# 函数用途: 准备一次子代理结果提交，返回 persisted、not_required 或 unpersisted。
def prepare_runner_result_commit(
    runtime_db: RuntimeRepository | None,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
    result: SubAgentRunnerResult,
    *,
    save_task: Callable[[SubAgentTask], object],
) -> str:
    with gateway_short_transaction('child_result_commit'):
        target_run_status = closeout_target_run_status(params, result, task)
        if not target_run_status and params.failure_type != "executor_effects_unknown":
            return "not_required"
        wal_state = ensure_closeout_fact(
            save_task, task, params, result,
            {"state": "pending", "target_run_status": target_run_status},
        )
        if wal_state == "unpersisted":
            record_unpersisted_closeout(runtime_db, task, params, result)
        return wal_state


# LLM: 调用方绑定本次保存及 trace→通知的交付回调；本模块不接收 manager。调用方已保存业务结果和 canonical task；本入口先持久化可恢复事实，再结算 exact run／attempt。
#   未落 WAL、收口待重试或权威终态冲突时不能进入父级投递，恢复只补账和通知、不重跑业务。
#   prepared 为调用方已经做过的 prepare_runner_result_commit 结果，避免重复写同一收口事实；缺省时本入口自己准备。
# 函数用途: 按原提交顺序推进一次子代理结果的运行收口，并在可交付时通知父级。
# LLM: 原T/P3/P5短门先于既有业务锁；Count0零新增IO，保main原CAS/owner/attempt与用户确认，未知不自动写失败或释放。
# 函数用途: 在原请求仍有效时完成这次已有写入，不持门等待模型或新建执行身份。
def commit_runner_result(
    runtime_db: RuntimeRepository | None,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
    result: SubAgentRunnerResult,
    *,
    save_task: Callable[[SubAgentTask], object],
    deliver_result: Callable[[], str],
    prepared: str = "",
) -> None:
    if prepared and prepared not in _PREPARED_STATES:
        raise ValueError(f"unknown runner result commit preparation: {prepared!r}")
    with gateway_short_transaction('child_result_commit'):
        wal_state = prepared or prepare_runner_result_commit(
            runtime_db, task, params, result, save_task=save_task,
        )
        if wal_state == "unpersisted":
            return
        outcome = settle_runtime_run_for_result(runtime_db, task, params, result)
        state = str(outcome.get("state") or "")
        if state in RETRYABLE_CLOSEOUT_STATES:
            if wal_state != "not_required":
                ensure_closeout_fact(save_task, task, params, result, outcome)
            record_closeout_event(
                runtime_db, task, event_type="closeout_pending", params=params, outcome=outcome
            )
            return
        if state in REJECTED_CLOSEOUT_STATES:
            record_closeout_event(
                runtime_db, task, event_type="closeout_blocked", params=params, outcome=outcome
            )
            if wal_state == "persisted":
                clear_closeout(save_task, task)
            return
        delivery = deliver_result()
        if wal_state == "persisted":
            if delivery == "failed":
                ensure_closeout_fact(save_task, task, params, result, outcome)
            else:
                if not mark_closeout_delivered(save_task, task, params, result, outcome):
                    return
                clear_closeout(save_task, task)
