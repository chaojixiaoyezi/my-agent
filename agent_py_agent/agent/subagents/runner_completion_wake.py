
from __future__ import annotations

import logging
from typing import Any

from ..runtime_errors import runtime_error_report
from .capability_scope import request_scope_snapshot
from .models import (
    SUBAGENT_WAKE_STATUSES,
    TaskStatus,
    task_status_in,
)
from .runner_completion_payload import completion_evidence_refs, completion_handoff_payload

# LLM: 本模块只负责把子代理终态或能力申请投递给正确父级；完成正文和产物引用由只读投影统一构造。
# 模块用途: 可靠发送父级通知、去重并记录投递错误，保持后台 wake 和前台续轮读取同一交接内容。

_LOGGER = logging.getLogger(__name__)


# LLM: 活动提醒走原 wake/observation 原子对，按 run+attempt+阶段去重；不是终态，不触发强杀。
#   递归父级从 canonical 直属快照和等待调和读取同一诊断，不把孙代理通知越级广播给根。
# 函数用途: 把长时间没有新活动的观测交给直属父级，让它决定查看、插话或继续等。
def notify_parent_on_activity_notice(manager: Any, task: Any, notice: dict[str, Any]) -> bool:
    if _has_persisted_subagent_parent(manager, task):
        return True
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return False
    thread = store.tasks.thread_for(task.id)
    if thread is None:
        return False
    key = str(notice["notice_key"])
    if store.wakes.delivery_receipt(thread.thread_id, key) in {"pending", "handled"}:
        return True
    public = {k: v for k, v in notice.items() if k != "notified"}
    metadata = {"task_id": task.id, "status": task.status, "activity_diagnostic": public}
    shared = {
        "thread_id": thread.thread_id, "urgency": "normal",
        "source_agent_id": task.id, "parent_agent_id": str(task.parent_id),
        "root_task_id": str(task.root_id), "metadata": metadata,
    }
    store.wakes.append_observation(
        {
            **shared, "event_type": "subagent_activity_notice", "requires_main_agent": True,
            "summary": (
                f"子代理 {task.id} 阶段 {notice['phase']} 已 {int(notice['quiet_seconds'])} 秒没有新活动。"
                "这是阶段等待观测，不是已失败或已停止；先核对当前活动，慢首 token、长工具和审批可能正常。"
                "可在原权限内查看、补充消息或继续等待；未知执行效果不得自动重跑。"
            ),
        },
        {**shared, "reason": "subagent_activity_notice", "dedupe_key": key},
    )
    return True


# LLM: Normal runner results notify only statuses that need parent model attention. User-controlled
# cancellation uses the sibling entry below so model-owned cancel_subagents remains an in-turn fact
# and cannot create a duplicate background wake.
# 投递结果是结构化事实（delivered / already_delivered / skipped / failed），供可恢复收口链判定
# "要不要重发"，不解析任何文本。
# 函数用途: 子代理自然结束后按既有唤醒状态集合通知直属父级，并回报投递结果。
def notify_parent_on_runner_result(
    manager: Any,
    task: Any,
    result: Any,
    output_payload: dict[str, object],
    *,
    parent_thread: Any | None = None,
    attempt_id: str = "",
) -> str:
    store = getattr(manager, "conversation_store", None)
    if store is None or bool(getattr(result, "dry_run", False)):
        return "skipped"
    status = str(getattr(result, "status", "") or getattr(task, "status", "") or "").strip()
    if not task_status_in(status, SUBAGENT_WAKE_STATUSES):
        return "skipped"
    return _notify_parent_terminal(
        manager,
        task,
        result,
        output_payload,
        status=status,
        parent_thread=parent_thread,
        attempt_id=attempt_id,
    )


# LLM: An external TUI/Web stop is not a model-owned handled cancellation: after the whole branch
# is closed, it must surface the exact CANCELLED fact to the root conversation. The authorized
# thread is carried across link retirement; nested children still stop at their direct parent.
# 函数用途: 用户从代理详情页停止任务后，复用标准交接信封通知正确父级。
def notify_parent_on_controlled_cancel(
    manager: Any,
    task: Any,
    *,
    parent_thread: Any,
) -> None:
    status = str(getattr(task, "status", "") or "").strip()
    if not task_status_in(status, {TaskStatus.CANCELLED.value}):
        return
    _notify_parent_terminal(
        manager,
        task,
        None,
        {},
        status=status,
        parent_thread=parent_thread,
    )


# LLM: Natural completion and authorized external cancellation converge here after their distinct
# admission rules. One exact task/thread pair updates the projection and publishes at most one
# deduplicated root wake; nested children never skip their persisted direct parent.
# 可恢复语义：去重键按 **exact attempt** 定身份（attempt 缺失时退回既有 task+status 形态，行为不变）。
# 同一 attempt 的唤醒若已发出（pending/handled），本入口回报 already_delivered 而不再重发——
# 「通知不重」；换 attempt 属新事实，绝不因旧 attempt 已交付而被吞掉——「通知不丢」。
# 投递异常返回 failed 并沿用既有错误落账。
# 函数用途: 用同一结构化终态信封更新会话，并在目标直属根会话时发布一次唤醒。
def _notify_parent_terminal(
    manager: Any,
    task: Any,
    result: Any,
    output_payload: dict[str, object],
    *,
    status: str,
    parent_thread: Any | None,
    attempt_id: str = "",
) -> str:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return "skipped"
    task_id = str(getattr(task, "id", "") or getattr(result, "run_id", "") or "").strip()
    if not task_id:
        return "skipped"
    try:
        thread = parent_thread or store.tasks.thread_for(task_id)
        if thread is None:
            return "skipped"
        store.tasks.update_status({"task_id": task_id, "status": status})
        if _has_persisted_subagent_parent(manager, task):
            return "skipped"
        # 去重身份 = task + status + exact attempt（attempt 未知时保持既有 task+status 形态）。
        # 这样同一 attempt 的重复投递会被识别，而换代后的新 attempt 仍能正常通知父级。
        normalized_attempt = str(attempt_id or "").strip()
        dedupe_key = (
            f"subagent-finished:{task_id}:{status}:{normalized_attempt}"
            if normalized_attempt
            else f"subagent-finished:{task_id}:{status}"
        )
        # 已有同一去重键的唤醒（待消费或已消费）→ 视为已投递，绝不重发。
        receipt = getattr(getattr(store, 'wakes', None), 'delivery_receipt', None)
        if callable(receipt):
            try:
                if str(receipt(thread.thread_id, dedupe_key) or "") in {"pending", "handled"}:
                    return "already_delivered"
            except Exception:  # noqa: BLE001 - 回执查询失败不得阻断正常投递
                pass
        root_task_id = str(getattr(task, "root_id", "") or task_id)
        metadata = _metadata(task, result, output_payload)
        evidence_refs = completion_evidence_refs(task, output_payload, metadata)
        if _is_internal_audit_source_lifecycle(status, metadata):
            # A bounded source-worker slice is a continuation of one durable
            # logical worker, not a child completion that needs owner attention.
            # Still publish a machine-only wake *after* the terminal task row is
            # durable.  The background scheduler consumes this without a model
            # turn and reruns the canonical root terminal gate.  Without this
            # edge, the last source worker can reach its durable terminal state after the
            # collector's earlier settle check and leave the root Audit stuck in
            # "waiting for closeout" until an unrelated later request happens.
            _raise_internal_audit_source_wake(
                store,
                thread=thread,
                task=task,
                task_id=task_id,
                root_task_id=root_task_id,
                status=status,
                metadata=metadata,
            )
            return "delivered"
        # Publish through the store's wake-first pair operation. Two separate writes let the
        # scheduler consume the observation in the tiny gap before its wake existed, causing
        # duplicate background turns and duplicate IM progress fragments.
        store.wakes.append_observation(
            {
                "thread_id": thread.thread_id,
                "event_type": "subagent_runner_finished",
                "summary": _summary(task, result, status),
                "urgency": "normal",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": root_task_id,
                "requires_main_agent": True,
                "evidence_refs": evidence_refs,
                "metadata": metadata,
            },
            {
                "thread_id": thread.thread_id,
                "urgency": "normal",
                "reason": "subagent_runner_finished",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": root_task_id,
                "dedupe_key": dedupe_key,
                "evidence_refs": evidence_refs,
                "metadata": metadata,
            },
        )
        return "delivered"
    except Exception as exc:
        _record_wake_error(manager, task, result, exc)
        return "failed"


def _raise_internal_audit_source_wake(
    store: Any,
    *,
    thread: Any,
    task: Any,
    task_id: str,
    root_task_id: str,
    status: str,
    metadata: dict[str, object],
) -> None:
    """Wake the deterministic Audit supervisor without creating owner chatter."""

    runner_attempts = max(0, int(getattr(task, "runner_attempts", 0) or 0))
    ended_at = max(0, int(float(getattr(task, "ended_at", 0.0) or 0.0) * 1_000_000))
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "urgency": "normal",
            "reason": "subagent_runner_finished",
            "source_agent_id": task_id,
            "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
            "root_task_id": root_task_id,
            "dedupe_key": (
                f"audit-source-lifecycle:{task_id}:{status}:"
                f"{runner_attempts}:{ended_at}"
            ),
            "metadata": metadata,
        }
    )


def _summary(task: Any, result: Any, status: str) -> str:
    name = str(getattr(task, "agent_name", "") or getattr(task, "role", "") or "子代理")
    run_id = str(getattr(task, "id", "") or getattr(result, "run_id", "") or "")
    reason = str(
        getattr(result, "turn_end_reason", "")
        or getattr(task, "turn_end_reason", "")
        or "interrupted"
    )
    base = f"{name} {run_id} 本轮已结束：status={status}, reason={reason}。请父代理查看结果。"
    remaining = _service_window_remaining(task)
    if remaining <= 0:
        return base
    return base + (
        f"结构化事实：该 run 声明的 service window 还剩 {int(remaining)}s；"
        "此事实本身不指定接管、重派、复核或收口路线。"
    )


# LLM: Completion metadata combines host-owned lifecycle facts, the exact originating
# conversation turn id, and bounded read-only delivery content; prose never owns status.
# 函数用途: 组装子代理完成通知的结构化状态、本轮编号、最终回复预览和精确交付位置。
def _metadata(task: Any, result: Any, output_payload: dict[str, object]) -> dict[str, object]:
    payload = {
        "task_id": str(getattr(task, "id", "") or getattr(result, "run_id", "") or ""),
        "status": str(getattr(result, "status", "") or getattr(task, "status", "") or ""),
        "turn_end_reason": str(
            getattr(result, "turn_end_reason", "")
            or getattr(task, "turn_end_reason", "")
            or ""
        ),
        "failure_type": str(getattr(task, "failure_type", "") or ""),
        "runner_result_json": str(getattr(result, "result_json", "") or getattr(task, "runner_result_json", "") or ""),
        "output_json": str(getattr(task, "output_json", "") or ""),
        **completion_handoff_payload(task, result, output_payload),
    }
    remaining = _service_window_remaining(task)
    if remaining > 0:
        payload["service_window_incomplete"] = True
        payload["service_window_remaining_seconds"] = int(remaining)
    attrs = getattr(task, "attributes", {}) or {}
    from ..common.audit_activation import (
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_KEY_ATTR,
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
        structured_audit_supervised_worker_attributes,
    )
    from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR

    conversation_request_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    if conversation_request_id:
        payload[CONVERSATION_REQUEST_ID_ATTR] = conversation_request_id

    if structured_audit_supervised_worker_attributes(attrs):
        bound = structured_audit_source_worker_attributes(attrs)
        binding_pending = structured_audit_source_binding_attributes(attrs)
        payload.update(
            {
                # This lifecycle event is consumed by the deterministic Audit
                # supervisor.  It is not a user report and must not wake the
                # owner-facing model merely to narrate a slice rotation.
                "audit_source_worker": True,
                "audit_source_worker_phase": (
                    "bound" if bound else "binding_pending" if binding_pending else ""
                ),
                "audit_id": str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or ""),
                "source_id": str(attrs.get(AUDIT_SOURCE_ID_ATTR) or ""),
                "watch_id": str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or ""),
                "worker_key": str(attrs.get(AUDIT_SOURCE_WORKER_KEY_ATTR) or ""),
            }
        )
    return payload


def _is_internal_audit_source_lifecycle(
    status: str,
    metadata: dict[str, object],
) -> bool:
    """Keep mechanically recoverable Audit lifecycle facts out of owner chat."""
    if metadata.get("audit_source_worker") is not True:
        return False
    del status
    failure_type = str(metadata.get("failure_type") or "").strip()
    from .models import FailureType

    # Every source-worker terminal row is supervisor input, not owner content.
    # Findings and aggregate capacity alerts have their own typed delivery
    # events.  Account quota is the one exception because it requires an
    # operator decision and has a dedicated owner-facing prompt/fallback.
    return failure_type != FailureType.PROVIDER_QUOTA_EXHAUSTED.value


# LLM: Service-window projection is advisory and fail-silent; it never changes
# the recursive parent-child wake route.
# 函数用途: 安全读取长期任务剩余值守时间，读取失败按零处理。
def _service_window_remaining(task: Any) -> float:
    from .service_window import service_window_remaining_seconds

    try:
        return service_window_remaining_seconds(task)
    except Exception:
        return 0.0


# LLM: Capability requests are routed one level upward. Nested requests wait
# for the child's BLOCKED result to resume that exact parent; notification
# failure is recorded and must not erase the durable request.
# 函数用途: 直接根孩子申请权限时唤醒根会话；孙代理申请交给直属父级处理。
def notify_parent_on_capability_request(
    manager: Any,
    task: Any,
    request: Any,
    *,
    parent_tool_authority: dict[str, object] | None = None,
) -> None:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return
    if _has_persisted_subagent_parent(manager, task):
        return
    run_id = str(getattr(task, "id", "") or "").strip()
    request_id = str(getattr(request, "id", "") or "").strip()
    if not run_id or not request_id:
        return
    try:
        thread = store.tasks.thread_for(run_id)
        if thread is None:
            return
        observation = store.observations.append(
            _capability_open_observation(
                thread,
                task,
                request,
                parent_tool_authority=parent_tool_authority,
            )
        )
        store.wakes.raise_signal(
            _capability_open_signal(
                thread,
                task,
                request,
                observation,
                parent_tool_authority=parent_tool_authority,
            )
        )
    except Exception as exc:
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["capability_request_notify_error"] = runtime_error_report(
            exc, context="subagent_capability_request.notify_parent"
        )
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception:
            _LOGGER.warning("capability request notify error could not be saved for %s", run_id)


# LLM: A parent id is considered nested only when it resolves to a canonical
# subagent task; gateway/root request ids deliberately do not resolve here.
# 函数用途: 判断当前孩子的直属父级是不是另一个真实子代理。
def _has_persisted_subagent_parent(manager: Any, task: Any) -> bool:
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if not parent_id or not callable(getattr(manager, "load", None)):
        return False
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, TypeError, ValueError):
        return False
    return str(getattr(parent, "id", "") or "").strip() == parent_id


# LLM: This observation carries exact request and parent-authority facts to the root wake. Keep
# capability resolution distinct from later per-ToolCall user approval and never infer either.
# 函数用途: 构造“能力申请待处理”的 observation，带上申请和直属父级权限事实供 main 裁决。
def _capability_open_observation(
    thread: Any,
    task: Any,
    request: Any,
    *,
    parent_tool_authority: dict[str, object] | None = None,
) -> dict[str, object]:
    run_id = str(getattr(task, "id", "") or "")
    request_id = str(getattr(request, "id", "") or "")
    authority = dict(parent_tool_authority or {})
    authority_summary = _capability_authority_summary(authority)
    return {
        "thread_id": thread.thread_id,
        "event_type": "subagent_capability_request_open",
        "summary": (
            f"子代理 {run_id} 提交了能力申请 {request_id}"
            f"（{str(getattr(request, 'needed_capability', '') or '')[:80]}），等待父级 grant/deny；"
            "父代理用 resolve_capability_requests 处理，不要放着不管。"
            f"{authority_summary}能力授权不是用户对具体危险 ToolCall 的批准。"
        ),
        "urgency": "high",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or run_id),
        "requires_main_agent": True,
        "metadata": {
            "run_id": run_id,
            "request_id": request_id,
            "capability_type": str(getattr(request, "capability_type", "") or ""),
            "path_scope": list(getattr(request, "path_scope", []) or []),
            "capability_request": request_scope_snapshot(request),
            "parent_tool_authority": authority,
        },
    }


# LLM: The wake signal mirrors the durable observation and dedupes by exact run/request id. Its
# metadata is advisory model context; canonical request/grant state remains the machine authority.
# 函数用途: 构造高优先级且可去重的能力申请 wake，唤醒直属 main 处理同一条申请。
def _capability_open_signal(
    thread: Any,
    task: Any,
    request: Any,
    observation: Any,
    *,
    parent_tool_authority: dict[str, object] | None = None,
) -> dict[str, object]:
    run_id = str(getattr(task, "id", "") or "")
    request_id = str(getattr(request, "id", "") or "")
    return {
        "thread_id": thread.thread_id,
        "observation": observation,
        "urgency": "high",
        "reason": "subagent_capability_request_open",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or run_id),
        "dedupe_key": f"capability-open:{run_id}:{request_id}",
        "metadata": {
            "run_id": run_id,
            "request_id": request_id,
            "capability_request": request_scope_snapshot(request),
            "parent_tool_authority": dict(parent_tool_authority or {}),
        },
    }


# LLM: This sentence projects only typed host authority. It must not choose grant/deny or infer
# user approval from availability; the parent model still owns the scoped capability decision.
# 函数用途: 把父级工具可授予快照压成一条短提示，减少模型把 capability 当用户审批的概率。
def _capability_authority_summary(authority: dict[str, object]) -> str:
    if authority.get("has_tool_request") is not True:
        return ""
    grantable = [str(item) for item in authority.get("grantable_tools", []) if str(item)]
    unavailable = [str(item) for item in authority.get("unavailable_tools", []) if str(item)]
    if unavailable:
        return "宿主父级权限快照显示不可授予工具=" + ",".join(unavailable) + "；"
    if grantable:
        return "宿主父级权限快照显示申请工具均可授予；"
    return "宿主父级权限快照不可用，grant 必须失败关闭；"


def _record_wake_error(manager: Any, task: Any, result: Any, exc: BaseException) -> None:
    status = str(getattr(result, "status", "") or getattr(task, "status", "") or "").strip()
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["runner_completion_wake_error"] = {
        "status": status,
        "run_id": str(getattr(task, "id", "") or getattr(result, "run_id", "") or ""),
        "error": runtime_error_report(exc, context="subagent_runner_completion_wake.notify_parent"),
    }
    task.attributes = attrs
    try:
        manager.save(task)
    except Exception as save_exc:
        report = runtime_error_report(save_exc, context="subagent_runner_completion_wake.record_error")
        report["run_id"] = str(getattr(task, "id", "") or getattr(result, "run_id", "") or "")
        _LOGGER.warning("subagent runner completion wake error could not be saved: %s", report)


__all__ = [
    "notify_parent_on_capability_request",
    "notify_parent_on_controlled_cancel",
    "notify_parent_on_runner_result",
]
