
from __future__ import annotations

import json
import time
from typing import Any

from .models import ConversationThread, ObservationEvent, WakeSignal
from .store_context import ConversationStore


def background_prompt(reason: str) -> str:
    return (
        "后台主代理被唤醒。请基于持久会话、任务绑定和代理树状态判断下一步："
        "如果只是定时汇报，就给出清楚的阶段进展；如果发现子代理阻塞或需要推进，可以调用调度工具。"
        f"\n唤醒原因：{reason}"
    )


def default_route_target(thread: ConversationThread, route_channel: str) -> str:
    for binding in thread.channel_bindings:
        if binding.channel == route_channel:
            return binding.channel_conversation_id
    return thread.channel_bindings[-1].channel_conversation_id if thread.channel_bindings else thread.thread_id


def json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def pending_wake_payload(store: ConversationStore, thread_id: str, *, limit: int) -> list[dict[str, Any]]:
    return [item.to_dict() for item in store.pending_wake_signals(limit=limit) if item.thread_id == thread_id]


def wake_signal_payload(signal: WakeSignal | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(signal, WakeSignal):
        return signal.to_dict()
    return dict(signal) if isinstance(signal, dict) else None


def observations_by_thread(observations: list[ObservationEvent]) -> dict[str, list[ObservationEvent]]:
    grouped: dict[str, list[ObservationEvent]] = {}
    for observation in observations:
        grouped.setdefault(observation.thread_id, []).append(observation)
    return grouped


def first_root_task_id(observations: list[ObservationEvent]) -> str:
    return next((item.root_task_id for item in observations if item.root_task_id), "")


def claim_heartbeat_interval_seconds(*, ttl_seconds: int, configured_interval_seconds: float | None) -> float:
    ttl = max(1.0, float(ttl_seconds or 1))
    if configured_interval_seconds is not None:
        interval = max(0.05, float(configured_interval_seconds))
    elif ttl >= 90.0:
        interval = max(30.0, ttl / 3.0)
    else:
        interval = max(0.05, ttl / 3.0)
    return min(interval, max(0.05, ttl * 0.8))


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)
