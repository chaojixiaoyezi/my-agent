
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..agent.agent_core.agent_tree.status import agent_tree_status_payload
from ..agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ChannelMessageRuntime,
    FakeChannelHub,
)
from .common import make_agent


def cmd_background_main_agent_message(args) -> int:
    agent = make_agent(args)
    channels = FakeChannelHub()
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=agent.conversation_store,
        channels=channels,
    )
    receiver = ChannelMessageRuntime(runtime=runtime, store=agent.conversation_store)
    thread = receiver.receive({'channel': str(args.channel), 'channel_conversation_id': str(args.conversation_id), 'channel_user_id': str(args.user_id), 'canonical_user_id': str(args.canonical_user_id or args.user_id), 'content': str(args.content), 'now': _optional_float(args.now), 'run_background': bool(args.run_background)})
    payload = {
        "thread_id": thread.thread_id,
        "canonical_user_id": thread.canonical_user_id,
        "message_count": len(agent.conversation_store.recent_messages(thread.thread_id, limit=0)),
        "ran_background": bool(args.run_background),
    }
    _print_payload(payload, json_output=bool(args.json))
    return 0


def cmd_background_main_agent_bind_task(args) -> int:
    agent = make_agent(args)
    now = _optional_float(args.now)
    link = agent.conversation_store.bind_task({'thread_id': str(args.thread_id), 'task_id': str(args.task_id), 'goal': str(args.goal), 'now': now})
    policy_id = ""
    interval = int(args.progress_interval_seconds or 0)
    if interval > 0:
        policy = agent.conversation_store.set_progress_policy({'thread_id': str(args.thread_id), 'task_id': str(args.task_id), 'interval_seconds': interval, 'route_channel': str(args.route_channel or "internal"), 'route_target': str(args.route_target or ""), 'now': now})
        policy_id = policy.policy_id
    payload = {
        "thread_id": link.thread_id,
        "task_id": link.task_id,
        "policy_id": policy_id,
    }
    _print_payload(payload, json_output=bool(args.json))
    return 0


def cmd_background_main_agent_tick(args) -> int:
    agent = make_agent(args)
    reports = _run_tick(agent, now=_optional_float(args.now))
    payload = {
        "reports": len(reports),
        "thread_ids": [report.thread_id for report in reports],
        "task_ids": [report.task_id for report in reports],
    }
    if bool(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"background-main-agent tick reports={len(reports)}")
    return 0


def cmd_background_main_agent_status(args) -> int:
    agent = make_agent(args)
    store = agent.conversation_store
    threads = store.list_threads(limit=0)
    task_links = [link for thread in threads for link in store.task_links(thread.thread_id)]
    pending_wakes = store.pending_wake_signals(limit=0)
    unhandled = store.unhandled_observations_requiring_main(limit=0)
    policies = store.list_progress_policies()
    payload = {
        "ok": True,
        "schema_version": "background_main_agent_status.v1",
        "thread_count": len(threads),
        "bound_task_count": len(task_links),
        "pending_wake_count": len(pending_wakes),
        "unhandled_observation_count": len(unhandled),
        "progress_policy_count": len(policies),
        "threads": [_thread_status_row(store, thread) for thread in threads[-20:]],
        "pending_wakes": [item.to_dict() for item in pending_wakes[-20:]],
        "unhandled_observations": [item.to_dict() for item in unhandled[-20:]],
        "progress_policies": [item.to_dict() for item in policies[-20:]],
        "collaboration": agent.collaboration_store.overview(),
        "agent_tree": agent_tree_status_payload(agent, {}),
    }
    _print_payload(payload, json_output=bool(args.json), text_renderer=_render_status_text)
    return 0


def cmd_background_main_agent_observe(args) -> int:
    agent = make_agent(args)
    store = agent.conversation_store
    task_id = str(getattr(args, "task_id", "") or "").strip()
    thread_id = _thread_id_for_observe(store, args, task_id)
    if not thread_id:
        _print_payload(
            {"ok": False, "error": "thread_required", "message": "thread-id or bound task-id is required"},
            json_output=bool(args.json),
        )
        return 2
    observation = _append_cli_observation(store, args, thread_id, task_id)
    wake_signal_id = _maybe_raise_cli_wake_signal(store, args, thread_id, observation)
    _print_payload(_observe_payload(thread_id, task_id, observation, wake_signal_id), json_output=bool(args.json))
    return 0


def _thread_id_for_observe(store, args, task_id: str) -> str:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if thread_id or not task_id:
        return thread_id
    thread = store.thread_for_task(task_id)
    return thread.thread_id if thread is not None else ""


def _append_cli_observation(store, args, thread_id: str, task_id: str):
    return store.append_observation({'thread_id': thread_id, 'event_type': str(getattr(args, "event_type", "") or "observation"), 'summary': str(args.summary), 'urgency': str(getattr(args, "urgency", "") or "normal"), 'severity': str(getattr(args, "severity", "") or ""), 'source_agent_id': str(getattr(args, "source_agent_id", "") or ""), 'parent_agent_id': str(getattr(args, "parent_agent_id", "") or ""), 'root_task_id': str(getattr(args, "root_task_id", "") or task_id), 'evidence_refs': list(getattr(args, "evidence_ref", []) or []), 'requires_main_agent': bool(getattr(args, "requires_main_agent", False)), 'requires_llm_report': bool(getattr(args, "requires_llm_report", False)), 'now': _optional_float(getattr(args, "now", None))})


def _maybe_raise_cli_wake_signal(store, args, thread_id: str, observation) -> str:
    urgency = str(getattr(args, "urgency", "") or "").lower()
    if not bool(getattr(args, "wake", False)) and urgency != "urgent":
        return ""
    signal = store.raise_wake_signal({'thread_id': thread_id, 'observation': observation, 'urgency': str(getattr(args, "urgency", "") or "urgent"), 'reason': str(getattr(args, "event_type", "") or "observation"), 'dedupe_key': str(getattr(args, "dedupe_key", "") or ""), 'now': _optional_float(getattr(args, "now", None))})
    return signal.wake_signal_id


def _observe_payload(thread_id: str, task_id: str, observation, wake_signal_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "thread_id": thread_id,
        "task_id": task_id,
        "observation_id": observation.observation_id,
        "wake_signal_id": wake_signal_id,
    }


def cmd_background_main_agent_service(args) -> int:
    agent = make_agent(args)
    interval = max(0.0, float(args.interval or 0.0))
    max_cycles = max(0, int(args.max_cycles or 0))
    stop_file = Path(args.stop_file).expanduser() if str(args.stop_file or "").strip() else None
    cycle = 0
    total_reports = 0
    while max_cycles == 0 or cycle < max_cycles:
        if stop_file is not None and stop_file.exists():
            break
        cycle += 1
        reports = _run_tick(agent)
        total_reports += len(reports)
        if max_cycles and cycle >= max_cycles:
            break
        if interval > 0:
            _wait_for_service_interval(agent, interval=interval, stop_file=stop_file)
    payload = {"cycles": cycle, "reports": total_reports}
    if bool(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"background-main-agent service cycles={cycle} reports={total_reports}")
    return 0


def _thread_status_row(store, thread) -> dict[str, Any]:
    return {
        "thread_id": thread.thread_id,
        "canonical_user_id": thread.canonical_user_id,
        "title": thread.title,
        "active_task_count": len(thread.active_task_ids),
        "message_count": len(store.recent_messages(thread.thread_id, limit=0)),
        "observation_count": len(store.recent_observations(thread.thread_id, limit=0)),
        "bindings": [item.to_dict() for item in thread.channel_bindings],
        "tasks": [item.to_dict() for item in store.task_links(thread.thread_id)],
    }


def _render_status_text(payload: dict[str, Any]) -> str:
    return (
        "background-main-agent status "
        f"threads={payload.get('thread_count', 0)} "
        f"tasks={payload.get('bound_task_count', 0)} "
        f"pending_wakes={payload.get('pending_wake_count', 0)} "
        f"observations={payload.get('unhandled_observation_count', 0)} "
        f"policies={payload.get('progress_policy_count', 0)} "
        f"collaboration_cases={_dict(payload.get('collaboration')).get('case_count', 0)}"
    )


def _run_tick(agent: object, *, now: float | None = None):
    channels = FakeChannelHub()
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=agent.conversation_store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': agent.conversation_store})
    return scheduler.tick(now=now)


def _wait_for_service_interval(agent: object, *, interval: float, stop_file: Path | None = None) -> str:
    deadline = time.monotonic() + max(0.0, interval)
    while time.monotonic() < deadline:
        if stop_file is not None and stop_file.exists():
            return "stop_file"
        if agent.conversation_store.pending_wake_signals(limit=1):
            return "wake_signal"
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return "interval_elapsed"


def _print_payload(payload: dict[str, Any], *, json_output: bool, text_renderer=None) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if text_renderer is not None:
        print(text_renderer(payload))
        return
    print(" ".join(f"{key}={value}" for key, value in payload.items()))


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


__all__ = [
    "cmd_background_main_agent_bind_task",
    "cmd_background_main_agent_message",
    "cmd_background_main_agent_observe",
    "cmd_background_main_agent_service",
    "cmd_background_main_agent_status",
    "cmd_background_main_agent_tick",
]
