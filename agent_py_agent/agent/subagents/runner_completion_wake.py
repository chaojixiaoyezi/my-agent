
from __future__ import annotations

import logging
from typing import Any

from ..runtime_errors import runtime_error_report
from .models import SUBAGENT_WAKE_STATUSES, task_status_in

_LOGGER = logging.getLogger(__name__)


def notify_parent_on_runner_result(manager: Any, task: Any, result: Any, output_payload: dict[str, object]) -> None:
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
        observation = store.append_observation(
            {
                "thread_id": thread.thread_id,
                "event_type": "subagent_runner_finished",
                "summary": _summary(task, result, status),
                "urgency": "normal",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": str(getattr(task, "root_id", "") or task_id),
                "requires_main_agent": True,
                "metadata": _metadata(task, result, output_payload),
            }
        )
        store.raise_wake_signal(
            {
                "thread_id": thread.thread_id,
                "observation": observation,
                "urgency": "normal",
                "reason": "subagent_runner_finished",
                "source_agent_id": task_id,
                "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
                "root_task_id": str(getattr(task, "root_id", "") or task_id),
                "dedupe_key": f"subagent-finished:{task_id}:{status}",
                "metadata": {"task_id": task_id, "status": status},
            }
        )
    except Exception as exc:
        _record_wake_error(manager, task, result, exc)


def _summary(task: Any, result: Any, status: str) -> str:
    name = str(getattr(task, "agent_name", "") or getattr(task, "role", "") or "子代理")
    run_id = str(getattr(task, "id", "") or getattr(result, "run_id", "") or "")
    verification = str(getattr(result, "verification_status", "") or getattr(task, "verification_status", "") or "")
    return f"{name} {run_id} 已结束：status={status}, verification={verification}。请父代理查看结果并决定下一步。"


def _metadata(task: Any, result: Any, output_payload: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": str(getattr(task, "id", "") or getattr(result, "run_id", "") or ""),
        "status": str(getattr(result, "status", "") or getattr(task, "status", "") or ""),
        "verification_status": str(getattr(result, "verification_status", "") or getattr(task, "verification_status", "") or ""),
        "runner_result_json": str(getattr(result, "result_json", "") or getattr(task, "runner_result_json", "") or ""),
        "output_json": str(getattr(task, "output_json", "") or ""),
        "artifact_refs": list(output_payload.get("artifacts") or []) if isinstance(output_payload.get("artifacts"), list) else [],
    }


# LLM: R4 子项②的提交端推送：子代理记录 capability_request 后立刻向父级线程发
#   observation + wake signal（requires_main_agent=True），主代理在 run 循环里会被
#   推到这个决策点，再用 resolve_capability_requests 显式 grant/deny。失败只记账不抛，
#   不能因为通知失败把子代理的申请也丢掉。
# 函数用途: 子代理提交能力申请时通知主代理，修"主代理全程不知道有请求"的静默断链。
def notify_parent_on_capability_request(manager: Any, task: Any, request: Any) -> None:
    store = getattr(manager, "conversation_store", None)
    if store is None:
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
