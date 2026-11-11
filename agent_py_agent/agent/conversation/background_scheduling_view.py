# 模块用途: 后台工作等待/阻塞的只读调度投影（ROUTE-02/BIZ-001）：把"已受理但没执行"的结构化原因与最近调度证据
#   从原事实源（身份态、claim、canonical任务、任务唤醒定位、唤醒尝试账、模型目录）拼成一份视图。
#   只读：不领取、不确认、不推进唤醒，不实例化执行Agent，也不发模型请求；读不出的来源只降级 coverage，不冒充空闲。
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..runtime_errors import DataCorruptionError
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES

# 身份生命周期优先于工作态：暂停/退休中的身份即使有待办也不会被调度。
_LIFECYCLE_STATES = {
    "suspended": ("paused", "IDENTITY_SUSPENDED"),
    "retiring": ("retiring", "IDENTITY_RETIRING"),
    "retired": ("retired", "IDENTITY_RETIRED"),
}
_READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error, DataCorruptionError)


# 函数用途: 把秒级时间戳转成 UTC ISO 字符串；0 或缺失返回 None。
def _timestamp(value: float) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value and value > 0 else None


# LLM: 只读模型目录与部署配置判定会话所选模型能否真实执行，与后端工厂"缺配置"判据同源；不构造后端、不联网、不迁移空引用。
#   返回 (True, "") 可执行；(False, 原因码) 明确不可执行；(None, "") 目录读不出，未知不冒充可用。
# 函数用途: 判断后台会话当前模型选择是否可执行，供调度投影说明"为什么一直在等"。
def model_readiness(host: object, thread: object) -> tuple[bool | None, str]:
    from ..backends.factory import model_configuration_missing
    from ..settings.model_profiles import selected_model_config
    from ..settings.model_provider_schema import ModelProfileError

    try:
        config = selected_model_config(host, profile_id=str(getattr(thread, "model_profile_id", "") or "") or None)
    except ModelProfileError:
        return False, "MODEL_PROFILE_UNAVAILABLE"
    except _READ_ERRORS:
        return None, ""
    backend = getattr(config, "model_backend", None)
    if not isinstance(backend, str):
        # 解析结果不是正式模型配置（缺后端字段）时不能判定可用，按未知返回。
        return None, ""
    if model_configuration_missing(backend, config):
        return False, "MODEL_NOT_CONFIGURED"
    return True, ""


# LLM: tasks 必须是调用方已读出的同线程非终态 canonical 任务（有界页）；只按准确 task 主键读唤醒定位与尝试账，
#   不 glob 队列或历史。running 只能来自调用方已复核的准确执行，不能由身份 available 推断。支持准入饱和阻塞报告。
# 函数用途: 生成一个后台会话或单个任务的结构化调度视图（状态、原因码、唤醒引用、失败计数、下次重试时间）。
def scheduling_view(store: object, *, thread_id: str, tasks: list, identity_state: str = "", running: bool = False,
                    model_ready: bool | None = None, model_reason: str = "", pending_inputs: bool | None = None,
                    tasks_complete: bool = True, probe_limit: int = 20,
                    admission_limit_reached: bool = False) -> dict:
    view = {"state": "unknown", "reason_code": "", "pending_task_count": len(tasks), "wake_signal_ids": [],
            "failure_count": 0, "uncounted_count": 0, "last_failure_at": None, "next_attempt_at": None,
            "model_ready": model_ready, "coverage": "complete" if tasks_complete else "partial"}
    probed = tasks[:probe_limit]
    if len(tasks) > len(probed):
        view["coverage"] = "partial"
    last_failed, next_attempt, counted_reason, uncounted_reason = 0.0, 0.0, "", ""
    for task in probed:
        try:
            refs, error = store.wakes.task_responsibility_report(thread_id, task.task_id)
        except _READ_ERRORS:
            refs, error = [], {"code": "READ_FAILED"}
        if error is not None:
            view["coverage"] = "partial"
            continue
        for ref in refs:
            wake_id = str(ref.get("wake_signal_id") or "")
            if not wake_id:
                continue
            if len(view["wake_signal_ids"]) < 20:
                view["wake_signal_ids"].append(wake_id)
            try:
                state, state_error = store.wakes.attempts.state_report(wake_id)
            except _READ_ERRORS:
                state, state_error = None, {"code": "READ_FAILED"}
            if state_error is not None or state is None:
                view["coverage"] = "partial"
                continue
            view["failure_count"] += state.total_count
            view["uncounted_count"] += state.uncounted_count
            observed = max(state.last_failed_at, state.uncounted_since)
            if observed >= last_failed and (state.reason_code or state.uncounted_reason_code):
                last_failed = observed
                counted_reason = state.reason_code or counted_reason
                uncounted_reason = state.uncounted_reason_code or uncounted_reason
            if state.next_attempt_at > 0 and (next_attempt == 0 or state.next_attempt_at < next_attempt):
                next_attempt = state.next_attempt_at
    view["last_failure_at"], view["next_attempt_at"] = _timestamp(last_failed), _timestamp(next_attempt)
    lifecycle = _LIFECYCLE_STATES.get(identity_state)
    if lifecycle is not None:
        view["state"], view["reason_code"] = lifecycle
    elif running:
        view["state"] = "running"
    elif not tasks:
        if pending_inputs is None:
            view["coverage"] = "partial" if view["coverage"] == "complete" else view["coverage"]
        else:
            view["state"], view["reason_code"] = ("queued", "PENDING_INPUT") if pending_inputs else ("no_work", "")
    elif model_ready is False:
        view["state"], view["reason_code"] = "model_unavailable", model_reason
    elif admission_limit_reached or counted_reason == "ADMISSION_LIMIT_REACHED" or uncounted_reason == "ADMISSION_LIMIT_REACHED":
        view["state"], view["reason_code"] = "queued", "ADMISSION_LIMIT_REACHED"
    elif view["failure_count"] or view["uncounted_count"]:
        view["state"], view["reason_code"] = "wake_backoff", counted_reason or uncounted_reason
    elif view["wake_signal_ids"]:
        view["state"] = "queued"
    elif view["coverage"] == "complete":
        # 非终态任务却没有任何待处理唤醒：受理没变成可调度工作，必须显式暴露而不是显示成普通等待。
        view["state"], view["reason_code"] = "stalled", "NO_PENDING_WAKE"
    return view


# LLM: tasks.get 用：终态任务只报终态与宿主原因码（如 SESSION_TASK_WAKE_QUARANTINED），非终态按同一投影读准确唤醒；
#   本入口没有宿主模型目录，model_ready 留 None，不猜。支持准入饱和阻塞报告。
# 函数用途: 为单个 canonical 会话任务生成调度视图。
def task_scheduling_view(store: object, task: object, *, admission_limit_reached: bool = False) -> dict:
    if task.status in SESSION_TASK_TERMINAL_STATUSES:
        view = scheduling_view(store, thread_id=task.target_thread_id, tasks=[], pending_inputs=False)
        view.update(state="terminal", reason_code=str(task.failure_code or "")[:128], pending_task_count=0)
        return view
    return scheduling_view(store, thread_id=task.target_thread_id, tasks=[task],
                           admission_limit_reached=admission_limit_reached)


# 已结束任务摘要在业务投影里的最大字符数（与 TaskResultView.summary 的 maxLength 一致）。
_MAX_RESULT_SUMMARY_CHARS = 4000


# 结果回报的 guidance 回执状态按账本原名投影；reserved/submitted/consumed 是来源会话回合的认领、提交与消费。
_GUIDANCE_REPORT_STATES = frozenset({"pending", "reserved", "submitted", "consumed", "rejected"})


# LLM: 纯读结果回报的准确幂等回执（含归档回执），不取回执转换锁、不迁移旧版、不修投影；坏账或旧版回执记 unknown。
# 函数用途: 返回结果回报这条 next_turn 消息在来源会话里的发布/消费事实，缺失与读不出分开。
def _guidance_report_state(store: object, key: str) -> str:
    from ..gateway_parts.io import read_json_file_report

    path = store.guidance.storage.guidance_dedupe_path(key)
    report = read_json_file_report(path, context="business.task_result.guidance")
    if report.load_error is not None or report.lock_busy:
        return "unknown"
    payload = report.payload
    if not payload:
        return "unknown" if path.exists() else "absent"
    if payload.get("schema_version") != "conversation_guidance_once.v4" or payload.get("dedupe_key") != key:
        return "unknown"
    status = str(payload.get("status") or "")
    return status if status in _GUIDANCE_REPORT_STATES else "unknown"


# LLM: 只读原 SessionTask 的耐久摘要和结果回报的两条独立事实：guidance 是回报消息本身（普通发送会话只收这条），
#   wake 是主动唤醒（只有绑定/控制/后台身份发送方才有）；两者都不表示对外渠道已送达。不读队列、不改任务，读失败记 unknown。
# 函数用途: 区分“任务已结束”“回报已发布/已被来源会话消费”和“唤醒已发布/已处理”，未结束的任务不给结果视图。
def task_result_view(store: object, task: object) -> dict | None:
    if task.status not in SESSION_TASK_TERMINAL_STATUSES:
        return None
    from ..common.log_redaction import redact_sensitive_value
    from .session_task_report import result_report_key

    summary = str(redact_sensitive_value(str(getattr(task, "summary", "") or "")))
    report = {"guidance": "not_applicable", "wake": "not_applicable"}
    if str(getattr(task, "sender_thread_id", "") or "").strip():
        key = result_report_key(task)
        try:
            report["guidance"] = _guidance_report_state(store, key)
        except (OSError, ValueError, RuntimeError, AttributeError):
            report["guidance"] = "unknown"
        try:
            receipt = store.wakes.delivery_receipt(task.sender_thread_id, key)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            receipt = None
        report["wake"] = {"pending": "pending", "handled": "handled", "": "absent"}.get(receipt, "unknown")
    return {"summary": summary[:_MAX_RESULT_SUMMARY_CHARS],
            "summary_truncated": len(summary) > _MAX_RESULT_SUMMARY_CHARS, "report": report}


__all__ = ["model_readiness", "scheduling_view", "task_result_view", "task_scheduling_view"]
