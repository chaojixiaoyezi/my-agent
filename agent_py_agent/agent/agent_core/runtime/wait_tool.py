from __future__ import annotations

import json
import time

from ...subagents.models import (
    SUBAGENT_FAILED_RESULT_STATUSES,
    SUBAGENT_RESOLVED_TERMINAL_STATUSES,
    TaskStatus,
    task_status_in,
)
from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..agent_tree.status import agent_tree_status_payload
from ..runner.context import current_subagent_run_id

_TOOL_NAME = "wait"
_MIN_SECONDS = 60
_MAX_SECONDS = 7200
_POLL_SECONDS = 5


class WaitTool(BaseTool):
    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.spec = build_wait_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        interval = _seconds(params.get("seconds"), self.agent)
        target = _target(self.agent, params)
        if isinstance(target, ToolExecutionResult):
            return target
        thread_id, task_id = target
        policy = self.agent.conversation_store.set_progress_policy(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": interval,
                "route_channel": str(params.get("route_channel") or "internal"),
                "route_target": str(params.get("route_target") or ""),
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": _TOOL_NAME,
                    "scope": str(params.get("scope") or "own_task_tree"),
                    "reason": str(params.get("reason") or "").strip(),
                    "watch_run_id": str(params.get("run_id") or task_id),
                },
            }
        )
        payload = {
            "ok": True,
            "scheduled": True,
            "mode": _wait_mode(self.agent),
            "policy_id": policy.policy_id,
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": policy.interval_seconds,
            "min_seconds": _MIN_SECONDS,
            "max_seconds": _MAX_SECONDS,
            "next_due_at": policy.next_due_at,
            "reason": str(params.get("reason") or "").strip(),
            "next_action": "continue_without_polling",
            "guidance": (
                "已登记非阻塞的子代理进度查看提醒；主代理可以继续干当前工作或回复用户。"
                "后台调度到期后会唤醒控制面查看进度；这不是硬门，也不会强制收口。"
            ),
        }
        if _should_sleep_current_turn(self.agent):
            slept_seconds, wake_reason = _sleep_current_turn(self.agent, params, policy.interval_seconds)
            payload["slept_seconds"] = slept_seconds
            payload["wake_reason"] = wake_reason
            payload["next_action"] = "inspect_after_wait"
            payload["guidance"] = "等待已返回；现在可以重新查看子代理状态、读取产物或继续收口。"
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


def build_wait_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="read_only",
        description="设置一个子代理进度查看提醒；聊天/网关里非阻塞，CLI 自主运行里会等待后再继续，适合避免反复查看状态。",
        use_cases=[
            "刚派出子代理，希望后台 120 秒后再看一次进度",
            "重复查看代理树进入 cooldown，登记稍后查看提醒而不是继续轮询",
            "子代理正在协调孙代理，希望一段时间后再检查自己的子树",
        ],
        avoid_when=[
            "需要取消、接管、恢复或给子代理补充提示时不要只设提醒，应使用对应控制工具",
            "已有完成产物、错误或新证据时不要等待，直接读取和处理",
        ],
        keywords=["等待", "提醒", "watch", "yield", "wait", "稍后", "冷却", "不要轮询"],
        parameters={
            "seconds": f"多少秒后提醒查看；不填使用配置 subagent_watch_interval_seconds，最低 {_MIN_SECONDS}，最高 {_MAX_SECONDS}",
            "run_id": "可选，想查看的代理 run；默认当前 task/run",
            "task_id": "可选，绑定到哪个任务；默认当前运行任务",
            "thread_id": "可选，绑定到哪个会话线程；默认按 task_id 查找或自动创建内部线程",
            "scope": "可选，查看范围提示，默认 own_task_tree",
            "reason": "可选，为什么等待，便于审计和日志理解",
        },
        parameter_schema={
            "seconds": {"type": "integer", "minimum": 0},
            "run_id": {"type": "string"},
            "task_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "scope": {"type": "string"},
            "reason": {"type": "string"},
        },
        examples=[
            '{"tool":"wait","seconds":120,"reason":"刚启动子代理，稍后看一次进度"}',
            '{"tool":"wait","seconds":240,"run_id":"child-1","reason":"等待子代理和孙代理产出新进展"}',
        ],
    )


def _seconds(value: object, agent: object) -> int:
    if value is None:
        return _clamp_interval(getattr(getattr(agent, "config", None), "subagent_watch_interval_seconds", 120), default=120)
    return _clamp_interval(value, default=_MIN_SECONDS)


def _clamp_interval(value: object, *, default: int) -> int:
    parsed = _interval_int(value)
    if parsed is None:
        return default
    if parsed < _MIN_SECONDS:
        return _MIN_SECONDS
    if parsed > _MAX_SECONDS:
        return _MAX_SECONDS
    return parsed


def _interval_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _wait_mode(agent: object) -> str:
    if _should_sleep_current_turn(agent):
        return "blocking_sleep"
    return "nonblocking_schedule"


def _should_sleep_current_turn(agent: object) -> bool:
    source = _current_source(agent)
    if source in {"chat", "gateway", "background_main_agent"}:
        return False
    return source in {"cli_run", "run", "subagent_runner", "subagent_worker"}


def _current_source(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    return str(getattr(current, "source", "") or "").strip()


def _sleep_current_turn(agent: object, params: dict[str, object], interval_seconds: int) -> tuple[float, str]:
    if _watch_tree_ready(agent, params):
        return 0.0, "watch_tree_ready"
    if not _watch_tree_available(agent, params):
        time.sleep(interval_seconds)
        return float(interval_seconds), "timer_elapsed"
    slept = 0.0
    while slept < interval_seconds:
        step = min(float(_POLL_SECONDS), float(interval_seconds) - slept)
        time.sleep(step)
        slept += step
        if _watch_tree_ready(agent, params):
            return round(slept, 3), "watch_tree_ready"
    return round(slept, 3), "timer_elapsed"


def _watch_tree_ready(agent: object, params: dict[str, object]) -> bool:
    payload = _watch_tree_payload(agent, params)
    nodes = _payload_nodes(payload)
    if not nodes:
        return False
    target_run_id = str(params.get("run_id") or params.get("root_id") or "").strip()
    if target_run_id:
        node = _target_node(nodes, target_run_id)
        return _node_terminal(node) if node is not None else False
    return all(_node_terminal(node) for node in nodes)


def _target_node(nodes: list[dict[str, object]], run_id: str) -> dict[str, object] | None:
    return next((node for node in nodes if str(node.get("run_id") or "") == run_id), None)


def _watch_tree_available(agent: object, params: dict[str, object]) -> bool:
    return bool(_payload_nodes(_watch_tree_payload(agent, params)))


def _watch_tree_payload(agent: object, params: dict[str, object]) -> dict[str, object] | None:
    try:
        return agent_tree_status_payload(agent, _watch_tree_params(params))
    except Exception:
        return None


def _watch_tree_params(params: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in ("run_id", "root_id", "scope"):
        value = params.get(key)
        if value:
            result[key] = value
    return result


def _payload_nodes(payload: dict[str, object] | None) -> list[dict[str, object]]:
    nodes = payload.get("nodes") if isinstance(payload, dict) else []
    return [node for node in nodes if isinstance(node, dict)]


def _node_terminal(node: dict[str, object]) -> bool:
    terminal_statuses = frozenset(
        {
            TaskStatus.DONE.value,
            *SUBAGENT_RESOLVED_TERMINAL_STATUSES,
            *SUBAGENT_FAILED_RESULT_STATUSES,
        }
    )
    return task_status_in(node.get("status"), terminal_statuses)


def _target(agent: object, params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "set_progress_policy", None)):
        return _error("conversation_store_unavailable", "当前运行时没有会话调度存储，无法登记非阻塞提醒。")
    task_id = _task_id(agent, params)
    if not task_id:
        return _error("task_id_required", "缺少 task_id/run_id；无法知道要稍后查看哪棵代理树。")
    thread_id = str(params.get("thread_id") or "").strip()
    thread = None
    if not thread_id:
        try:
            thread = store.thread_for_task(task_id)
        except Exception:
            thread = None
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": "local-main-agent",
                "title": f"subagent-watch:{task_id}",
            }
        )
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
        store.bind_task({"thread_id": thread_id, "task_id": task_id, "goal": f"子代理进度查看: {task_id}"})
    return thread_id, task_id


def _task_id(agent: object, params: dict[str, object]) -> str:
    for key in ("task_id", "run_id", "root_id"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    current = getattr(agent, "_current_run_params", None)
    value = str(getattr(current, "task_id", "") or "").strip()
    if value:
        return value
    return str(current_subagent_run_id(agent) or getattr(agent, "_main_agent_run_id", "") or "").strip()


def _error(code: str, message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code=code)


__all__ = ["WaitTool", "build_wait_spec"]
