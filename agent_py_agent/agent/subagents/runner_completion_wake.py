# LLM: Runner completion wake bridges subagent tree facts back to the parent conversation.
# 模块用途: 子代理执行器完成后写 observation 和 wake signal，让父代理后续自动继续编排。

from __future__ import annotations

from typing import Any

_WAKE_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}


# LLM: notify_parent_on_runner_result is best-effort; child completion must not fail because wake IO failed.
# 函数用途: 当子代理完成或失败时，给绑定的长期会话写一条可去重唤醒信号；没有绑定会话时保持静默。
def notify_parent_on_runner_result(manager: Any, task: Any, result: Any, output_payload: dict[str, object]) -> None:
    store = getattr(manager, "conversation_store", None)
    if store is None or bool(getattr(result, "dry_run", False)):
        return
    status = str(getattr(result, "status", "") or getattr(task, "status", "") or "").upper()
    if status not in _WAKE_STATUSES:
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
    except Exception:
        return


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


__all__ = ["notify_parent_on_runner_result"]
