
from __future__ import annotations

import logging
from typing import Any

from ..runtime_errors import runtime_error_report
from .models import (
    SUBAGENT_WAKE_STATUSES,
    task_status_in,
)

_LOGGER = logging.getLogger(__name__)


# LLM: Root children publish a conversation wake; nested children never skip a
# level and are resumed by the direct-parent runner lifecycle after lease exit.
# 函数用途: 子代理结果落盘后更新会话投影，仅直接属于根会话的孩子发根唤醒。
def notify_parent_on_runner_result(
    manager: Any,
    task: Any,
    result: Any,
    output_payload: dict[str, object],
) -> None:
    store = getattr(manager, "conversation_store", None)
    if store is None or bool(getattr(result, "dry_run", False)):
        return
    status = str(getattr(result, "status", "") or getattr(task, "status", "") or "").strip()
    if not task_status_in(status, SUBAGENT_WAKE_STATUSES):
        return
    task_id = str(getattr(task, "id", "") or getattr(result, "run_id", "") or "").strip()
    if not task_id:
        return
    try:
        thread = store.thread_for_task(task_id)
        if thread is None:
            return
        store.update_task_status({"task_id": task_id, "status": status})
        if _has_persisted_subagent_parent(manager, task):
            return
        root_task_id = str(getattr(task, "root_id", "") or task_id)
        metadata = _metadata(task, result, output_payload)
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
            return
        # Publish through the store's wake-first pair operation. Two separate writes let the
        # scheduler consume the observation in the tiny gap before its wake existed, causing
        # duplicate background turns and duplicate IM progress fragments.
        store.append_observation_with_wake(
            {
                "thread_id": thread.thread_id,
                "event_type": "subagent_runner_finished",
                "summary": _summary(task, result, status),
                "urgency": "normal",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": root_task_id,
                "requires_main_agent": True,
                "metadata": metadata,
            },
            {
                "thread_id": thread.thread_id,
                "urgency": "normal",
                "reason": "subagent_runner_finished",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": root_task_id,
                "dedupe_key": f"subagent-finished:{task_id}:{status}",
                "metadata": metadata,
            },
        )
    except Exception as exc:
        _record_wake_error(manager, task, result, exc)


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
    store.raise_wake_signal(
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
        "artifact_refs": list(output_payload.get("artifacts") or []) if isinstance(output_payload.get("artifacts"), list) else [],
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
def notify_parent_on_capability_request(manager: Any, task: Any, request: Any) -> None:
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
        thread = store.thread_for_task(run_id)
        if thread is None:
            return
        observation = store.append_observation(_capability_open_observation(thread, task, request))
        store.raise_wake_signal(_capability_open_signal(thread, task, request_id, observation))
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


# 函数用途: 构造"能力申请待处理"的 observation payload（requires_main_agent=True）。
def _capability_open_observation(thread: Any, task: Any, request: Any) -> dict[str, object]:
    run_id = str(getattr(task, "id", "") or "")
    request_id = str(getattr(request, "id", "") or "")
    return {
        "thread_id": thread.thread_id,
        "event_type": "subagent_capability_request_open",
        "summary": (
            f"子代理 {run_id} 提交了能力申请 {request_id}"
            f"（{str(getattr(request, 'needed_capability', '') or '')[:80]}），等待父级 grant/deny；"
            "父代理用 resolve_capability_requests 处理，不要放着不管。"
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
        },
    }


# 函数用途: 构造"能力申请待处理"的 wake signal payload（高优先级 + 去重键）。
def _capability_open_signal(
    thread: Any,
    task: Any,
    request_id: str,
    observation: Any,
) -> dict[str, object]:
    run_id = str(getattr(task, "id", "") or "")
    return {
        "thread_id": thread.thread_id,
        "observation": observation,
        "urgency": "high",
        "reason": "subagent_capability_request_open",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or run_id),
        "dedupe_key": f"capability-open:{run_id}:{request_id}",
        "metadata": {"run_id": run_id, "request_id": request_id},
    }


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


__all__ = ["notify_parent_on_capability_request", "notify_parent_on_runner_result"]
