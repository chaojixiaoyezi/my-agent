from __future__ import annotations

import json

from ...tooling.models import ToolHandlerOutcome
from ..runner.context import current_subagent_run_id
from .task_identity import durable_task_id

_TOOL_NAME = "wait"
_MIN_SECONDS = 60
_MAX_SECONDS = 7200


def register_dispatch_supervision_policy(
    agent: object,
    *,
    run_ids: list[str] | None = None,
) -> dict[str, object] | None:
    try:
        return _register_dispatch_supervision(agent, run_ids=run_ids or [])
    except Exception:  # noqa: BLE001 - 监督提醒是增强,失败绝不影响派工
        import logging

        logging.getLogger(__name__).warning("dispatch supervision policy register failed", exc_info=True)
        return None


def _register_dispatch_supervision(
    agent: object,
    *,
    run_ids: list[str],
) -> dict[str, object] | None:
    interval = int(getattr(getattr(agent, "config", None), "dispatch_supervision_reminder_seconds", 0) or 0)
    if interval <= 0:
        return None
    target = _target(agent, {})
    if isinstance(target, ToolHandlerOutcome):
        return None
    thread_id, task_id = target
    store = agent.conversation_store
    if existing := _enabled_policy_for(store, thread_id, task_id):
        return _supervision_payload(existing, existing=True)
    from ...conversation.progress_fingerprint import subagent_material_signature

    watched_run_ids = _periodic_supervision_run_ids(agent, run_ids)
    if run_ids and not watched_run_ids:
        return None
    signature = subagent_material_signature(
        agent,
        task_id=task_id,
        watched_run_ids=watched_run_ids,
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": _clamp_interval(interval, default=180),
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "scope": "own_task_tree",
                "reason": "机制层派工监督:巡查子代理进展/卡点,处理能力申请,有中途结论及时上报",
                "watch_run_id": task_id,
                "watched_run_ids": watched_run_ids,
                "material_signature": signature,
            },
        }
    )
    return _supervision_payload(policy, existing=False)


def _periodic_supervision_run_ids(agent: object, run_ids: list[str]) -> list[str]:
    """Exclude durable Audit source workers from periodic parent LLM polling.

    Ordinary delegated work still needs low-frequency supervision between
    lifecycle events.  A source worker is a long-lived lease consumer with its
    own stall recovery and finding outbox; every settled batch is material
    progress, so a generic progress policy would wake the main Agent forever.
    """

    from ...common.audit_activation import AUDIT_SOURCE_WORKER_ATTR

    selected: list[str] = []
    manager = getattr(agent, "subagents", None)
    loader = getattr(manager, "load", None)
    for value in sorted({str(item) for item in run_ids if str(item).strip()}):
        if not callable(loader):
            selected.append(value)
            continue
        try:
            task = loader(value)
        except Exception:
            selected.append(value)
            continue
        attrs = getattr(task, "attributes", None)
        if isinstance(attrs, dict) and attrs.get(AUDIT_SOURCE_WORKER_ATTR) is True:
            continue
        selected.append(value)
    return selected


# 函数用途: 监督登记回执只带原生类型(policy 对象可能是测试替身,原样塞进工具
#   payload 会让 create_subagents 的 json.dumps 在注册 try/except 之外炸)。
def _supervision_payload(policy: object, *, existing: bool) -> dict[str, object]:
    try:
        interval = int(getattr(policy, "interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        interval = 0
    return {
        "policy_id": str(getattr(policy, "policy_id", "") or ""),
        "existing": existing,
        "interval_seconds": interval,
    }


def _enabled_policy_for(store, thread_id: str, task_id: str):
    for policy in store.list_progress_policies(enabled_only=True):
        if policy.thread_id == thread_id and policy.task_id == task_id:
            return policy
    return None


# 函数用途: 监督间隔夹在 60s~7200s，缺省用配置默认值。
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


def _target(agent: object, params: dict[str, object]) -> tuple[str, str] | ToolHandlerOutcome:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "set_progress_policy", None)):
        return _error(
            "conversation_store_unavailable",
            "当前运行时没有会话调度存储，无法登记非阻塞提醒。",
            error_code="TOOL_UNAVAILABLE",
        )
    task_id = _task_id(agent, params)
    if not task_id:
        return _error(
            "task_id_required",
            "缺少 task_id/run_id；无法知道要稍后查看哪棵代理树。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
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
    value = durable_task_id(current)
    if value:
        return value
    # A background-main run id identifies one execution attempt, not a durable
    # task.  Taskless internal events must stay taskless: using the transient
    # ``bg-main-*`` id here creates a synthetic task whose own wait policy wakes
    # it again forever.  Real background continuations already carry their
    # exact durable task id through ``RunParams``/task attributes above.
    if str(getattr(current, "source", "") or "").strip() == "background_main_agent":
        return ""
    return str(current_subagent_run_id(agent) or getattr(agent, "_main_agent_run_id", "") or "").strip()


# LLM: This guard reads only typed Audit activation plus exact durable receipt
# counters. It does not choose a judgment route, score threshold, worker count,
# or business meaning; it only prevents a delay primitive from replacing work
# that is already durably available.
# 函数用途: Audit 已有待签收数据时拒绝继续空等，并把精确积压事实返回给模型自行决策。
def _error(
    code: str,
    message: str,
    *,
    error_code: str | None = None,
    facts: dict[str, object] | None = None,
) -> ToolHandlerOutcome:
    # code 是给人/日志看的语义标签(写进 payload.error)；error_code 必须是 taxonomy 已注册码，
    # 否则 ToolHandlerOutcome 会把未注册的小写 code 兜底成 UNKNOWN_ERROR(retryable=False)，
    # 误导模型"放弃报阻塞"，而 store 缺失/缺 task_id 其实是可换工具/补参数修复的。
    payload = {"ok": False, "error": code, "message": message}
    if facts:
        payload["facts"] = facts
    return ToolHandlerOutcome(
        _TOOL_NAME,
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code or code,
    )


__all__ = ["register_dispatch_supervision_policy"]
