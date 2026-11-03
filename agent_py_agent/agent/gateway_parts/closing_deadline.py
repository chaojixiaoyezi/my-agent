"""closing期限只催收原执行，不制造退出证明或释放仍在运行的会话claim。"""
from __future__ import annotations

import logging
import math
import time
from contextlib import ExitStack

from ..common.cancellation import ToolCancelled
from ..common.log_redaction import redact_sensitive_text
from ..concurrency.interrupt import interrupt_by_name
from ..conversation.agent_tool_approval import (
    _BoundedJsonDirectoryScan,
    retire_pending_agent_tool_approvals,
)
from ..conversation.control_commands import conversation_request_interrupt_name
from ..conversation.run_claim import conversation_claim_interrupt_name
from .io import read_json_file_report, try_gateway_turn_transition, update_json_file_atomic
from .request_binding import gateway_bound_request_task_id


class _DeadlineScan:
    _ENTRY_LIMIT = 64
    _MAX_MS = 50.0
    _REINTERRUPT_SECONDS = 30.0
    scanner = _BoundedJsonDirectoryScan()


# LLM: 只读XC-3字段；原生扫描租约有界并在完成/失败/闲置时关闭，同一deadline覆盖审批；不取执行权或写claim。
# 函数用途: 超过硬期限时撤销原审批、重发协作中断，并在原请求上保留一次结构化诊断。
def evaluate_closing_deadlines(paths, agent, *, now: float,
                               closing_hard_timeout_seconds: float = 120.0) -> dict:
    if not math.isfinite(now) or not math.isfinite(closing_hard_timeout_seconds) or closing_hard_timeout_seconds < 0:
        raise ValueError("closing deadline must be finite and nonnegative")
    summary = {"checked": 0, "timed_out": 0, "retired": 0, "errors": 0, "complete": True}
    deadline = time.monotonic() + _DeadlineScan._MAX_MS / 1000.0

    def visit(path):
        summary["checked"] += 1
        report = read_json_file_report(path, blocking=False)
        if report.load_error is not None:
            summary["errors"] += 1
            return
        if _expired(report.payload, now, closing_hard_timeout_seconds):
            try:
                result = _evaluate_one(paths, path, agent, now, closing_hard_timeout_seconds, deadline=deadline)
                for key in ("timed_out", "retired", "errors"):
                    summary[key] += result.get(key, 0)
            except ToolCancelled:
                raise
            except (OSError, RuntimeError, ValueError):
                summary["errors"] += 1
    try:
        summary["complete"] = _DeadlineScan.scanner.scan(paths.processing, visit,
            deadline=deadline, max_entries=_DeadlineScan._ENTRY_LIMIT)
    except FileNotFoundError:
        return summary
    return summary


# LLM: 不解释错误文案，时间必须为真实有限数；损坏或缺失时间不授予超时处理。
# 函数用途: 仅选择已请求取消、仍处于stopping/closing且超过硬期限的原请求。
def _expired(payload, now, timeout):
    cancelled = payload.get("cancel_requested_at")
    return (payload.get("status") == "processing" and payload.get("control_status") == "stopping"
            and payload.get("turn_phase") == "closing" and payload.get("cancel_requested") is True
            and type(cancelled) in (int, float) and math.isfinite(cancelled)
            and cancelled >= 0 and now - cancelled >= timeout)


# LLM: 同原回合T锁内复读并冻结运输代次；只租用已驻留owner，退出时释放pin，冷owner留原recovery处理。
# 函数用途: 在不释放会话claim的前提下催收这一条超时回合。
def _evaluate_one(paths, path, agent, now, timeout, *, deadline=None):
    from .request_worker import _resolve_request_owner_identity

    result = {"timed_out": 0, "retired": 0, "errors": 0}
    with try_gateway_turn_transition(paths, path.stem) as acquired:
        if not acquired:
            return {**result, "errors": 1}
        report = read_json_file_report(path, blocking=False)
        row = report.payload
        if report.load_error is not None or not _expired(row, now, timeout) or row.get("id") != path.stem:
            return result
        prior = row.get("closing_deadline")
        prior = prior if isinstance(prior, dict) else {}
        interrupt_due = not (prior.get("execution_attempt_id") == row.get("execution_attempt_id")
                and prior.get("cancel_requested_at") == row.get("cancel_requested_at")
                and now - float(prior.get("last_interrupt_at", 0)) < _DeadlineScan._REINTERRUPT_SECONDS)
        if not interrupt_due and prior.get("approval_retirement", {}).get("complete", True):
            return result
        binding = row.get("conversation_claim")
        binding = binding if isinstance(binding, dict) else {}
        runtime = row.get("conversation_runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        thread_id = str(binding.get("thread_id") or runtime.get("thread_id") or "")
        root_task_id = gateway_bound_request_task_id(row, request_id=path.stem, thread_id=thread_id)
        with ExitStack() as stack:
            approval = {"retired": 0, "errors": 0, "complete": True}
            if thread_id and root_task_id:
                try:
                    owner = _resolve_request_owner_identity(agent, row)
                    scoped = _pin_same_resident(agent, owner, None, stack)
                    if scoped is not None:
                        approval = retire_pending_agent_tool_approvals(scoped, thread_id=thread_id,
                            root_task_id=root_task_id, deadline=deadline)
                    else:
                        approval.update(errors=1, complete=False, reason="resident_unavailable")
                except ToolCancelled:
                    raise
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    # 审批失败不阻断独立的原请求/claim催收；错误随本回合诊断保留，不每拍刷栈。
                    approval.update(errors=1, complete=False, error_type=type(exc).__name__,
                        error=redact_sensitive_text(exc)[:1000])
            claim_id = str(binding.get("claim_id") or "")
            # 只发原请求/原claim的协作信号，绝不调用claims.finish或删除锁。
            interrupted = bool(prior.get("interrupt_delivered"))
            if interrupt_due:
                interrupted = interrupt_by_name(conversation_request_interrupt_name(path.stem))
            if claim_id and interrupt_due:
                interrupted = interrupt_by_name(conversation_claim_interrupt_name(claim_id)) or interrupted
            diagnostic = {"code": "CLOSING_HARD_TIMEOUT", "detected_at": prior.get("detected_at", now),
                          "last_interrupt_at": now if interrupt_due else prior["last_interrupt_at"], "execution_attempt_id": row.get("execution_attempt_id"),
                          "cancel_requested_at": row.get("cancel_requested_at"), "interrupt_delivered": interrupted,
                          "approval_retirement": approval}

            def stamp(current):
                if (current.get("execution_attempt_id") != row.get("execution_attempt_id")
                        or current.get("cancel_requested_at") != row.get("cancel_requested_at")
                        or not _expired(current, now, timeout)):
                    return current
                return {**current, "closing_deadline": diagnostic}

            update_json_file_atomic(path, stamp, require_existing=True, blocking=False)
        if not prior:
            logging.getLogger(__name__).warning("CLOSING_HARD_TIMEOUT request_id=%s thread_id=%s", path.stem, thread_id)
        return {"timed_out": 1, "retired": approval["retired"], "errors": approval["errors"]}


# LLM: 复用原缓存命中pin及unpin；同一非阻塞短锁内查找与pin，resident非空时额外核对原对象，不走peek/get/build/reopen。
# 函数用途: 只保住当前驻留实例；池忙或原实例已逐出时直接留待下一拍，完整owner安全门仍现场复核。
def _pin_same_resident(agent, owner, resident, stack):
    from ..user_space.owner_resolver import owner_identity_from_config

    if owner == owner_identity_from_config(getattr(agent, "config", None)):
        return agent if resident is None or resident is agent else None
    pool = getattr(agent, "_owner_pool", None)
    if pool is None or not pool._lock.acquire(blocking=False):
        return None
    try:
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        current = pool._hard_agents.get(key) or pool._soft_agents.get(key)
        if current is None or (resident is not None and current is not resident):
            return None
        resident = current
        pool._pin_result_locked(resident, pin=True)
        stack.callback(pool._unpin, resident, touch=False)
    finally:
        pool._lock.release()
    pool._require_worker_owner(owner, resident)
    return resident
