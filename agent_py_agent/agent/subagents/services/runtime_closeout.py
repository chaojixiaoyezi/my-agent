# LLM: 本模块是 runner 终态收口的唯一权威入口。它解决两个真实缺口：
#   1) 收口写库失败时只 fail-soft 返回 None，既没有记录也没有持久化待重试事实，
#      task/wake 已经终态、runtime 仍 created 的矛盾会永久留档；
#   2) run 收口后、父级唤醒前中断时，同一 exact current attempt 的一致终态重放
#      必须能重入补交付，而不是被判成"与 runtime 终态冲突"。
# 语义边界：本模块只补记账（settle 权威 run）与通知（父级 wake），**绝不重跑业务**；
# 事实来源一律是结构化 runner 结论 + runtime.db 权威行 + 持久化 runner_result，
# 不解析任何自然语言，也不依赖普通文本触发。
# 模块用途: 为子代理 runner 的最终结论提供"先落待重试事实、再收口 run、再通知父级"的可恢复链路。
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...runtime_db.repository import RuntimeRepository
    from ..models import SubAgentTask

# LLM: 待重试事实挂在子代理 canonical task 的 attributes 上；它与 runtime.db 是**不同**
# 存储域，因此 runtime.db 写失败不会连带写不进重试事实。schema 必须版本化，恢复链只认
# 本 schema，避免把历史自由字段当成机器事实。
# 字段用途: 记录一次尚未完成（或已收口未通知）的 runner 终态交付。
RUNTIME_CLOSEOUT_ATTR = "runtime_closeout_pending"
RUNTIME_CLOSEOUT_SCHEMA = "subagent-runtime-closeout.v1"
# 收口事实两处都写不进去时的可达恢复事实（runtime 事件账本，另一个存储域）。
CLOSEOUT_UNPERSISTED_EVENT = "closeout_unpersisted"
# 还原消费标记：事件账本是 append-only，用"同身份已还原"的事实避免每个周期重复还原。
CLOSEOUT_RESTORED_EVENT = "closeout_restored"

_LOGGER = logging.getLogger(__name__)

# 收口结果状态词表（机器判定，不接受自然语言别名）。
CLOSEOUT_SETTLED = "settled"
CLOSEOUT_ALREADY_CONSISTENT = "already_consistent"
CLOSEOUT_NOT_APPLICABLE = "not_applicable"
CLOSEOUT_WRITE_ERROR = "write_error"
CLOSEOUT_UNKNOWN_STATUS = "unknown_status"
CLOSEOUT_MISSING_RUN = "missing_run"
CLOSEOUT_CONFLICT = "conflict"
CLOSEOUT_STALE_ATTEMPT = "stale_attempt"

# 只有这几类是"事实没落成、但以后可能落成"的待重试形态。
RETRYABLE_CLOSEOUT_STATES = frozenset({
    CLOSEOUT_WRITE_ERROR,
    CLOSEOUT_UNKNOWN_STATUS,
    CLOSEOUT_MISSING_RUN,
})
# 收口已提交（含幂等一致）→ 可以继续通知父级。
COMMITTED_CLOSEOUT_STATES = frozenset({CLOSEOUT_SETTLED, CLOSEOUT_ALREADY_CONSISTENT})
# 与权威终态冲突或已换代 → 拒绝交付，只留诊断。
REJECTED_CLOSEOUT_STATES = frozenset({
    CLOSEOUT_CONFLICT,
    CLOSEOUT_STALE_ATTEMPT,
})

_RUN_STATUS_FOR_TASK_STATUS = {
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "DONE": "done",
}
_DELIVERY_PENDING = "pending"
_DELIVERY_DELIVERED = "delivered"


# LLM: 仅构造未发生收口的结果；读写失败有单独的可重试记录，不允许任意字段覆盖默认状态。
# 函数用途: 给无需收口的分支返回一致的无副作用结果，供唯一收口入口使用。
def _noop_outcome(state: str, reason: str) -> dict[str, Any]:
    return {"state": state, "reason": reason, "target_run_status": "", "retryable": False}


# LLM: 目标 run 终态只从 runner 的结构化结论推导；PENDING/BLOCKED/RUNNING 等可恢复形态
# 一律不收口，避免把等待/待续跑误判成结束。返回空串表示"本次不需要 run 级收口"。
# 函数用途: 把 runner 结论映射成 runtime.db 的 run 终态，或表示无需收口。
def closeout_target_run_status(params: Any, result: Any, task: Any) -> str:
    raw = str(
        getattr(result, "status", "")
        or getattr(params, "status", "")
        or getattr(task, "status", "")
        or ""
    ).strip().upper()
    return _RUN_STATUS_FOR_TASK_STATUS.get(raw, "")


# LLM: 该函数只回答"权威 run 现在是什么"，不做任何写操作；读不到就返回空串，
# 由调用方按 fail-closed 处理，绝不把"读不到"当成"已收口"。
# 函数用途: 读取 runtime.db 中某条 run 的当前状态。
def _current_run_status(repo: Any, run_id: str) -> str:
    try:
        row = repo.agent_run_for_run_id(str(run_id or ""))
    except Exception:
        return ""
    if row is None:
        return ""
    try:
        return str(row["status"] or "")
    except Exception:
        return ""


# LLM: 收口失败必须给出可诊断的结构化原因，且错误文本里不得包含密钥；
# 异常类型与消息只用于排障，不改变任何状态判定。
# 函数用途: 把收口异常压成有界错误描述。
def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:300]


# LLM: 本函数只使用显式原 RuntimeDB 进行运行收口，不访问任务存储或通知。返回的 outcome 是后续"是否通知父级 / 是否落待重试事实"
# 的唯一判据：committed 才允许通知，retryable 才落 WAL，rejected 只留诊断。
# 语义不变式：只在 attempt 仍是 exact current 时收口；stale/换代保护沿用 repo 的 CAS。
# 函数用途: 按 runner 结论收口对应 run，读取失败显式记录可重试结果，无操作分支不附加隐式覆盖字段。
def settle_runtime_run_for_result(
    repo: RuntimeRepository | None,
    task: Any,
    params: Any,
    result: Any,
) -> dict[str, Any]:
    if bool(getattr(params, "dry_run", False)):
        return _noop_outcome(CLOSEOUT_NOT_APPLICABLE, "dry_run")
    target = closeout_target_run_status(params, result, task)
    if not target:
        return _noop_outcome(CLOSEOUT_NOT_APPLICABLE, "non_terminal_runner_status")
    if repo is None:
        return _noop_outcome(CLOSEOUT_NOT_APPLICABLE, "no_runtime_db")
    run_id = str(getattr(task, "id", "") or "")
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    try:
        authority = repo.agent_run_for_run_id(run_id)
    except Exception as exc:  # noqa: BLE001 - 读权威行失败同样是"未收口"，必须可重试
        return {
            "state": CLOSEOUT_WRITE_ERROR, "reason": "authority_read_failed", "retryable": True,
            "target_run_status": target, "run_id": run_id, "attempt_id": attempt_id,
            "detail": _error_text(exc),
        }
    if authority is None:
        return _noop_outcome(CLOSEOUT_NOT_APPLICABLE, "no_runtime_authority")
    agent_run_id = str(authority["agent_run_id"] or "")
    if not agent_run_id:
        return _noop_outcome(CLOSEOUT_NOT_APPLICABLE, "no_agent_run_id")
    base = {
        "target_run_status": target,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "agent_run_id": agent_run_id,
    }
    try:
        settled = repo.settle_agent_run(
            agent_run_id=agent_run_id,
            status=target,
            attempt_id=attempt_id,
            payload={
                "status": str(getattr(result, "status", "") or getattr(task, "status", "") or ""),
                "runner_result": True,
                "turn_end_reason": str(getattr(params, "turn_end_reason", "") or ""),
                "runtime_status": "error" if target != "done" else "ok",
                "runtime_reason": str(getattr(result, "message", "") or "")[:200],
                "runtime_source": "subagent_runner",
            },
        )
    except Exception as exc:  # noqa: BLE001 - 写库异常绝不能再被吞成 None
        return {**base, "state": CLOSEOUT_WRITE_ERROR, "reason": "settle_failed",
                "retryable": True, "detail": _error_text(exc)}
    reason = str((settled or {}).get("reason") or "")
    if bool((settled or {}).get("settled")):
        return {**base, "state": CLOSEOUT_SETTLED, "reason": "settled", "retryable": False,
                "event_id": str((settled or {}).get("event_id") or "")}
    if reason == "already_terminal":
        # 已经是终态：只有与目标一致才算幂等收口，否则是权威终态冲突（不得覆盖、不得通知成完成）。
        observed = _current_run_status(repo, run_id)
        if observed and observed == target:
            return {**base, "state": CLOSEOUT_ALREADY_CONSISTENT, "reason": "already_terminal",
                    "retryable": False, "observed_run_status": observed}
        return {**base, "state": CLOSEOUT_CONFLICT, "reason": "already_terminal_mismatch",
                "retryable": False, "observed_run_status": observed}
    if reason == "stale_attempt":
        return {**base, "state": CLOSEOUT_STALE_ATTEMPT, "reason": "stale_attempt", "retryable": False}
    if reason == "unknown_status":
        return {**base, "state": CLOSEOUT_UNKNOWN_STATUS, "reason": "unknown_status", "retryable": True}
    if reason == "no_such_run":
        return {**base, "state": CLOSEOUT_MISSING_RUN, "reason": "no_such_run", "retryable": True}
    if reason == "invalid_status":
        return {**base, "state": CLOSEOUT_CONFLICT, "reason": "invalid_status", "retryable": False}
    # 未知拒绝原因 fail-closed：不通知、不冒充完成，留待重试与诊断。
    return {**base, "state": CLOSEOUT_UNKNOWN_STATUS, "reason": reason or "unknown_rejection",
            "retryable": True}


# LLM: 待重试事实是跨进程、跨重启的唯一恢复依据；只从结构化字段重建，不读模型正文。
# 函数用途: 读取某次子代理 run 上尚未完成的收口事实。
def pending_closeout(task: Any) -> dict[str, Any] | None:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return None
    fact = attrs.get(RUNTIME_CLOSEOUT_ATTR)
    if not isinstance(fact, dict):
        return None
    if str(fact.get("schema_version") or "") != RUNTIME_CLOSEOUT_SCHEMA:
        return None
    return dict(fact)


# LLM: WAL 仅通过调用方绑定的保存入口写 canonical task，不持有 manager 或访问运行账。
# 函数用途: 保存或移除一条收口恢复事实；保存失败显式返回 False。
def _store_closeout(save_task: Callable[[SubAgentTask], object], task: Any, fact: dict[str, Any] | None) -> bool:
    attrs = dict(getattr(task, "attributes", {}) or {})
    if fact is None:
        attrs.pop(RUNTIME_CLOSEOUT_ATTR, None)
    else:
        attrs[RUNTIME_CLOSEOUT_ATTR] = fact
    task.attributes = attrs
    try:
        save_task(task)
    except Exception:  # noqa: BLE001 - 事实落盘失败不能反过来伪造完成
        return False
    return True


# LLM: 先写待重试事实再动权威账本，是"收口未提交不得冒充完成"的前置条件；
# 保存回调必须绑定原 canonical task 存储；事实里带足恢复所需的精确身份（run/attempt/agent_run/目标终态），恢复链不需要猜。
# 函数用途: 通过显式保存回调落一条待重试的 runner 终态收口事实。
def record_pending_closeout(
    save_task: Callable[[SubAgentTask], object],
    task: Any,
    params: Any,
    result: Any,
    outcome: dict[str, Any],
    *,
    delivery: str = _DELIVERY_PENDING,
) -> bool:
    existing = pending_closeout(task) or {}
    attempts = int(existing.get("attempts") or 0) + 1
    fact = {
        "schema_version": RUNTIME_CLOSEOUT_SCHEMA,
        "run_id": str(getattr(task, "id", "") or ""),
        "attempt_id": str(getattr(params, "attempt_id", "") or ""),
        "agent_run_id": str(outcome.get("agent_run_id") or existing.get("agent_run_id") or ""),
        "target_status": str(
            getattr(result, "status", "") or getattr(task, "status", "") or ""
        ).strip().upper(),
        "target_run_status": str(outcome.get("target_run_status") or ""),
        "turn_end_reason": str(getattr(params, "turn_end_reason", "") or ""),
        "runner_result_json": str(getattr(task, "runner_result_json", "") or ""),
        "output_json": str(getattr(task, "output_json", "") or ""),
        "closeout_state": str(outcome.get("state") or ""),
        "closeout_reason": str(outcome.get("reason") or ""),
        "closeout_detail": str(outcome.get("detail") or ""),
        "delivery": str(delivery or _DELIVERY_PENDING),
        "attempts": attempts,
        "created_at": float(existing.get("created_at") or time.time()),
        "updated_at": time.time(),
    }
    return _store_closeout(save_task, task, fact)


# LLM: 仅调用显式保存回调；「先成功持久化再收口」必须是可判定的三态，而不是一个可能悄悄为 False 的布尔：
#   persisted   = canonical task WAL 已落盘（唯一允许继续收口+通知的形态）；
#   unpersisted = 两种介质都写不进去（调用方必须停止后续不可恢复动作并留响亮诊断）；
#   not_required = 本次结论不需要 run 级收口（非终态/无 runtime 权威）。
# 直接写 WAL 失败时先重试一次（吸收瞬时抖动），仍失败则退到 runtime 事件账本留同身份事实。
# 函数用途: 落待重试收口事实并回报它到底有没有被持久化。
def ensure_closeout_fact(
    save_task: Callable[[SubAgentTask], object],
    task: Any,
    params: Any,
    result: Any,
    outcome: dict[str, Any],
    *,
    delivery: str = _DELIVERY_PENDING,
) -> str:
    if record_pending_closeout(save_task, task, params, result, outcome, delivery=delivery):
        return "persisted"
    if record_pending_closeout(save_task, task, params, result, outcome, delivery=delivery):
        return "persisted"
    return "unpersisted"


# LLM: 本入口仅持有显式 RuntimeDB；canonical task 写不进去时，恢复依据必须以**另一个存储域**（runtime 事件账本）留下，
# 否则恢复链没有任何可达依据。事件带足重建 WAL 所需的精确身份与引用，恢复扫描据此重试。
# 函数用途: 在 runtime 事件账本里落一条"收口事实未能落盘"的可达恢复事实。
def record_unpersisted_closeout(repo: RuntimeRepository | None, task: Any, params: Any, result: Any) -> bool:
    append = getattr(repo, "append_event", None)
    payload = {
        "schema_version": RUNTIME_CLOSEOUT_SCHEMA,
        "reason": "closeout_fact_unpersisted",
        "run_id": str(getattr(task, "id", "") or ""),
        "attempt_id": str(getattr(params, "attempt_id", "") or ""),
        "target_status": str(
            getattr(result, "status", "") or getattr(task, "status", "") or ""
        ).strip().upper(),
        "turn_end_reason": str(getattr(params, "turn_end_reason", "") or ""),
        "runner_result_json": str(getattr(task, "runner_result_json", "") or ""),
        "output_json": str(getattr(task, "output_json", "") or ""),
        "task_status": str(getattr(task, "status", "") or ""),
    }
    agent_run_id = ""
    lookup = getattr(repo, "agent_run_for_run_id", None)
    if callable(lookup):
        try:
            row = lookup(payload["run_id"])
            agent_run_id = str(row["agent_run_id"] or "") if row is not None else ""
        except Exception:  # noqa: BLE001 - 查不到身份不阻断记录本身
            agent_run_id = ""
    payload["agent_run_id"] = agent_run_id
    if callable(append):
        try:
            # append_event 要求 agent_run_id；缺失或为空都会让这条可达事实写不进去，
            # 所以身份必须先解析出来再写。
            append(
                event_type=CLOSEOUT_UNPERSISTED_EVENT,
                attempt_id=payload["attempt_id"],
                agent_run_id=agent_run_id,
                payload=payload,
            )
            return True
        except Exception:  # noqa: BLE001 - 记不下来就只能响亮上报
            pass
    _LOGGER.error(
        "runner 收口事实无法持久化（canonical task 与 runtime 账本都失败），已停止本片收口与通知: %s",
        json.dumps(payload, ensure_ascii=False),
    )
    return False


# LLM: 只调用显式保存回调；「不重」要求"已交付"本身先落盘再清账：清账失败时恢复链必须看到 delivered 并跳过重发，
# 否则一次成功的父级唤醒会因为清账失败被再发一次。
# 函数用途: 把收口事实标记为已交付并持久化。
def mark_closeout_delivered(
    save_task: Callable[[SubAgentTask], object],
    task: Any,
    params: Any,
    result: Any,
    outcome: dict[str, Any],
) -> bool:
    return record_pending_closeout(
        save_task, task, params, result, outcome, delivery=_DELIVERY_DELIVERED
    )


# LLM: 只调用显式保存回调；收口与交付都完成后必须清掉事实，否则恢复链会反复补交付；清理失败要显式报告，
# 不能静默当作已完成。
# 函数用途: 清除已完成的收口事实。
def clear_closeout(save_task: Callable[[SubAgentTask], object], task: Any) -> bool:
    return _store_closeout(save_task, task, None)


# LLM: 显式原 RuntimeDB 中的诊断事件只用于排障与监督，不改变任何状态判定；runtime.db 不可用时静默跳过，
# 因为此时权威事实本就写不进去，事件也写不进去，不能因此改变主链语义。
# 函数用途: 追加一条 runner 收口相关的结构化运行时事件。
def record_closeout_event(
    repo: RuntimeRepository | None,
    task: Any,
    *,
    event_type: str,
    params: Any,
    outcome: dict[str, Any],
) -> None:
    append = getattr(repo, "append_event", None)
    if not callable(append):
        return
    try:
        append(
            event_type=event_type,
            attempt_id=str(getattr(params, "attempt_id", "") or ""),
            agent_run_id=str(outcome.get("agent_run_id") or ""),
            payload={
                "schema_version": RUNTIME_CLOSEOUT_SCHEMA,
                "run_id": str(getattr(task, "id", "") or ""),
                "state": str(outcome.get("state") or ""),
                "reason": str(outcome.get("reason") or ""),
                "detail": str(outcome.get("detail") or "")[:300],
                "target_run_status": str(outcome.get("target_run_status") or ""),
                "observed_run_status": str(outcome.get("observed_run_status") or ""),
                "task_status": str(getattr(task, "status", "") or ""),
            },
        )
    except Exception:  # noqa: BLE001 - 诊断失败不得改变主链判定
        return


# LLM: 调用方绑定原身份与通知参数，本入口仅调用一次绑定回调。父级交付必须幂等：调用方按结构化投递结果推进 WAL，绝不按文本判断是否已通知。
# 去重身份带 exact attempt，因此同一 attempt 重发被识别为已交付，换代后的新 attempt 不受影响。
# "skipped"（没有可通知的父会话/状态）不是失败，不重试；"already_delivered" 由唤醒回执保证。
# 函数用途: 把 runner 终态结果通知直属父级，并回报结构化投递结果。
def deliver_parent_wake(notify_parent: Callable[[], str]) -> str:
    try:
        outcome = notify_parent()
    except Exception:  # noqa: BLE001 - 通知异常要落回待重试事实，不能丢
        return "failed"
    return str(outcome or "delivered")


# LLM: 本入口是恢复装配边界，向收口与 WAL 原语分别传原 RuntimeDB、save 和绑定通知；
# 恢复链只做两件事：补收口（settle 权威 run）与补通知（父级 wake）。
# 它必须能安全重复执行：settle 是 CAS 幂等、wake 有去重键 + 回执，因此"重复恢复"不会产生
# 重复终态事件或重复父级唤醒；已经交付过的事实直接清账。
# 函数用途: 推进一条待重试的收口事实；返回本次是否已彻底完成。
def advance_pending_closeout(manager: Any, task: Any, fact: dict[str, Any]) -> dict[str, Any]:
    from ..runner_completion_wake import notify_parent_on_runner_result

    params = _CloseoutReplayParams(fact)
    result = _load_persisted_runner_result(fact)
    if result is None:
        # runner_result 落盘缺失说明事实与持久化结果不一致：保持事实并留诊断，绝不伪造结论。
        return {"advanced": False, "state": "runner_result_missing"}
    repo = getattr(manager, "runtime_db", None)
    outcome = settle_runtime_run_for_result(repo, params=params, task=task, result=result)
    if outcome.get("state") in REJECTED_CLOSEOUT_STATES:
        # LLM: 被拒（与权威终态冲突 / 已换代）不是"以后可能成"——重试永远不会成功。
        # 所以诊断只写一次，然后清账：否则每个 reconcile 周期都会重复写事件，
        # 且 owner 会因为这条永久事实一直被当成硬事实反复扫描（无界重试 + 事件洪泛）。
        # 权威 run/task 事实已经成立，换代后的新 attempt 会走它自己的正常收口与通知。
        record_closeout_event(repo, task, event_type="closeout_blocked", params=params, outcome=outcome)
        if not clear_closeout(manager.save, task):
            # 清账失败：事实仍在，下一轮会再次走到这里（仍然只诊断、不交付）；不报 advanced 成功。
            return {"advanced": False, "state": "cleanup_failed", "rejected": True}
        return {"advanced": False, "state": str(outcome.get("state") or ""), "rejected": True}
    if outcome.get("state") not in COMMITTED_CLOSEOUT_STATES | {CLOSEOUT_NOT_APPLICABLE}:
        # 仍未收口（写库失败 / 状态未知等可重试形态）：刷新事实里的诊断字段，等待下一次恢复。
        refreshed = record_pending_closeout(
            manager.save, task, params, result, outcome,
            delivery=str(fact.get("delivery") or _DELIVERY_PENDING),
        )
        # LLM: 可重试形态会一直挂着（例如 run 状态一直没被修复），所以诊断事件必须有界：
        # 只在首个周期以及每 10 次重试时落一条，避免每个 reconcile 周期都写一条造成事件洪泛。
        attempts = int((pending_closeout(task) or {}).get("attempts") or 0)
        if attempts <= 1 or attempts % 10 == 0:
            record_closeout_event(
                repo, task, event_type="closeout_pending", params=params, outcome=outcome
            )
        return {
            "advanced": False,
            "state": str(outcome.get("state") or ""),
            "fact_persisted": refreshed,
        }
    if str(fact.get("delivery") or "") == _DELIVERY_DELIVERED:
        # 已交付：只需清账。清账失败**不能**报成功——事实还在，下一轮还会看到它。
        if not clear_closeout(manager.save, task):
            record_closeout_event(
                repo, task, event_type="closeout_pending", params=params, outcome=outcome
            )
            return {"advanced": False, "state": "cleanup_failed"}
        return {"advanced": True, "state": "already_delivered"}
    delivery = deliver_parent_wake(partial(
        notify_parent_on_runner_result, manager, task, result, _load_persisted_output(fact),
        attempt_id=str(fact.get("attempt_id") or ""),
    ))
    if delivery == "failed":
        record_pending_closeout(manager.save, task, params, result, outcome, delivery=_DELIVERY_PENDING)
        return {"advanced": False, "state": "delivery_failed"}
    # 先持久化"已交付"，再清账：清账失败时下一轮会看到 delivered 而跳过重发，避免重复通知。
    if not mark_closeout_delivered(manager.save, task, params, result, outcome):
        _LOGGER.error(
            "runner 收口已交付但无法持久化交付标记（不清账、不报成功）: run_id=%s attempt_id=%s",
            str(getattr(task, "id", "") or ""), str(fact.get("attempt_id") or ""),
        )
        return {"advanced": False, "state": "delivery_mark_unpersisted"}
    if not clear_closeout(manager.save, task):
        return {"advanced": False, "state": "cleanup_failed"}
    return {"advanced": True, "state": delivery}


# LLM: 恢复只补 WAL，不执行业务。持久扫描游标与消费回执分离，页尾绕回保证坏记录可重试且不挡后项。
# exact attempt 的消费回执无窗口限制；结果文件须与事件身份一致，不能把换代后的结果绑定到旧 attempt。
# 函数用途: 分页还原未落盘收口事实；单条损坏不拖垮本页，成功持久化后才写消费回执。
def restore_unpersisted_closeouts(manager: Any) -> int:
    repo = getattr(manager, "runtime_db", None)
    lister = getattr(repo, "pending_events_page", None)
    if not callable(lister):
        return 0
    try:
        events = lister(
            CLOSEOUT_UNPERSISTED_EVENT, consumed_event_type=CLOSEOUT_RESTORED_EVENT,
            consumer=RUNTIME_CLOSEOUT_SCHEMA, limit=50,
        )
    except Exception:  # noqa: BLE001 - 读不到就不还原，不影响其它恢复
        return 0
    restored = 0
    for event in events:
        try:
            restored += _restore_closeout_event(manager, event)
        except Exception:  # noqa: BLE001 - 读写失败保持未消费，下次轮转还会重试
            _LOGGER.debug("runtime closeout event restore failed", exc_info=True)
    return restored


# LLM: 恢复装配边界绑定原 manager.save 给 WAL 原语；只恢复 exact current 的结果引用；同身份 WAL 已存在时补消费回执，旧身份只记已淘汰，不覆盖新事实。
# 函数用途: 还原一条事件到正式收口事实，并在成功保存后确认消费；失败会留在恢复分页中。
def _restore_closeout_event(manager: Any, event: dict[str, Any]) -> int:
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("schema_version") != RUNTIME_CLOSEOUT_SCHEMA:
        return 0
    run_id = str(payload.get("run_id") or "").strip()
    attempt_id = str(payload.get("attempt_id") or "").strip()
    if not run_id or not attempt_id or attempt_id != event.get("attempt_id"):
        return 0
    repo = manager.runtime_db
    authority = repo.agent_run_for_run_id(run_id)
    if authority is None or str(authority["agent_run_id"]) != str(event.get("agent_run_id") or ""):
        return 0
    task = manager.load(run_id)
    existing = pending_closeout(task)
    stale = str(authority["current_attempt_id"] or "") != attempt_id
    restored = 0
    if not stale and existing is None:
        target_status = str(payload.get("target_status") or "").strip().upper()
        fact = {
            "schema_version": RUNTIME_CLOSEOUT_SCHEMA,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "agent_run_id": str(event.get("agent_run_id") or ""),
            "target_status": target_status,
            "target_run_status": _RUN_STATUS_FOR_TASK_STATUS.get(target_status, ""),
            "turn_end_reason": str(payload.get("turn_end_reason") or ""),
            "runner_result_json": str(payload.get("runner_result_json") or ""),
            "output_json": str(payload.get("output_json") or ""),
            "closeout_state": CLOSEOUT_UNKNOWN_STATUS,
            "closeout_reason": "restored_from_event",
            "closeout_detail": "",
            "delivery": _DELIVERY_PENDING,
            "attempts": int((pending_closeout(task) or {}).get("attempts") or 0),
            "created_at": float(event.get("created_at") or time.time()),
            "updated_at": time.time(),
        }
        result = _load_persisted_runner_result(fact)
        if result is None or str(result.run_id) != run_id:
            return 0
        if str(result.status) != target_status:
            return 0
        if not _store_closeout(manager.save, task, fact):
            return 0
        restored = 1
    elif not stale and str(existing.get("attempt_id") or "") != attempt_id:
        return 0
    repo.append_event(
        event_type=CLOSEOUT_RESTORED_EVENT, attempt_id=attempt_id,
        agent_run_id=str(event.get("agent_run_id") or ""),
        payload={"schema_version": RUNTIME_CLOSEOUT_SCHEMA,
                 "run_id": run_id, "attempt_id": attempt_id,
                 "reason": "superseded" if stale else "wal_persisted"},
    )
    return restored


# LLM: 恢复扫描是确定性的、零 LLM 成本的宿主巡查入口，由既有关键sweep调用；
# 它只推进已有事实，不创建新业务，也不依赖任何文本触发。
# 函数用途: 扫描并推进所有待重试的 runner 收口事实，返回结构化汇总。
def recover_pending_closeouts(manager: Any) -> dict[str, int]:
    summary = {"runtime_closeouts_recovered": 0, "runtime_closeouts_pending": 0,
               "runtime_closeouts_rejected": 0, "runtime_closeouts_restored": 0}
    # 先把"事实没能落盘"的事件还原成正式 WAL（task 存储恢复可写时），再走正常推进；
    # 这样 canonical 写失败不再等于"没有恢复依据"。
    try:
        summary["runtime_closeouts_restored"] = restore_unpersisted_closeouts(manager)
    except Exception:  # noqa: BLE001 - 还原失败不影响既有事实的推进
        _LOGGER.debug("runtime closeout restore failed", exc_info=True)
    list_runs = getattr(manager, "list_runs", None)
    if not callable(list_runs):
        return summary
    try:
        tasks = list(list_runs() or [])
    except Exception:  # noqa: BLE001 - 扫描失败不得影响其它监督动作
        return summary
    for task in tasks:
        fact = pending_closeout(task)
        if fact is None:
            continue
        try:
            outcome = advance_pending_closeout(manager, task, fact)
        except Exception:  # noqa: BLE001 - 单条推进失败不拖垮整轮
            summary["runtime_closeouts_pending"] += 1
            continue
        if outcome.get("advanced"):
            summary["runtime_closeouts_recovered"] += 1
        elif str(outcome.get("state") or "") in REJECTED_CLOSEOUT_STATES:
            summary["runtime_closeouts_rejected"] += 1
        else:
            summary["runtime_closeouts_pending"] += 1
    return summary


class _CloseoutReplayParams:
    """把持久化事实还原成收口所需的参数形状（只暴露结构化字段）。"""

    def __init__(self, fact: dict[str, Any]) -> None:
        self.run_id = str(fact.get("run_id") or "")
        self.attempt_id = str(fact.get("attempt_id") or "")
        self.turn_end_reason = str(fact.get("turn_end_reason") or "")
        self.dry_run = False


# LLM: 恢复只读已持久化的 runner 结果文件；读不到就保持事实并留诊断，绝不重跑 runner。
# 函数用途: 从 runner_result.json 还原结构化 runner 结论。
def _load_persisted_runner_result(fact: dict[str, Any]) -> Any | None:
    path = str(fact.get("runner_result_json") or "")
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缺失/损坏都不伪造结论
        return None
    if not isinstance(payload, dict):
        return None
    try:
        from ..model_runtime import SubAgentRunnerResult

        allowed = {item.name for item in SubAgentRunnerResult.__dataclass_fields__.values()}
        return SubAgentRunnerResult(**{key: value for key, value in payload.items() if key in allowed})
    except Exception:  # noqa: BLE001
        return None


# LLM: 交付需要 output_payload 提供产物与正文引用；读不到就给空 payload，
# 由既有交付逻辑按缺失处理，不在这里猜测内容。
# 函数用途: 读取本次收口对应的 output.json 交付负载。
def _load_persisted_output(fact: dict[str, Any]) -> dict[str, Any]:
    path = str(fact.get("output_json") or "")
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "CLOSEOUT_ALREADY_CONSISTENT",
    "CLOSEOUT_UNPERSISTED_EVENT",
    "CLOSEOUT_CONFLICT",
    "CLOSEOUT_NOT_APPLICABLE",
    "CLOSEOUT_SETTLED",
    "CLOSEOUT_STALE_ATTEMPT",
    "CLOSEOUT_UNKNOWN_STATUS",
    "CLOSEOUT_WRITE_ERROR",
    "COMMITTED_CLOSEOUT_STATES",
    "REJECTED_CLOSEOUT_STATES",
    "RETRYABLE_CLOSEOUT_STATES",
    "RUNTIME_CLOSEOUT_ATTR",
    "RUNTIME_CLOSEOUT_SCHEMA",
    "advance_pending_closeout",
    "clear_closeout",
    "record_unpersisted_closeout",
    "restore_unpersisted_closeouts",
    "mark_closeout_delivered",
    "ensure_closeout_fact",
    "closeout_target_run_status",
    "deliver_parent_wake",
    "pending_closeout",
    "record_closeout_event",
    "record_pending_closeout",
    "recover_pending_closeouts",
    "settle_runtime_run_for_result",
]
