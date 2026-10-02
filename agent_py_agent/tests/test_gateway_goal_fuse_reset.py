"""Gateway 前台新消息重置 Goal 空片计数（2026-10-01，ae 复测 D4）。

真机：TUI/飞书经 Gateway 发来的新消息（gwreq-1790863471）之后 idle_slices 仍是 3；设计是新用户消息清零计数。
reset_goal_progress_fuse 只接在 ChannelMessageRuntime.receive 和 CLI 入口，Gateway 前台
（request_execution._execute_gateway_conversation_turn）写入用户消息后没有重置。钉住两个方向（真实 echo Gateway + 真实后台调度器）：
1. Gateway 前台用户消息写入成功后清零计数；熔断暂停的 Goal 只清计数、保留暂停原因（与另两个入口同口径）；写入失败不清；
2. Goal 自动续跑片走后台 wake，不经过这条前台路径：清零之后仍要连续 3 个空片才熔断。
3. C5/O4（2026-10-02）：第 3 个空片还在跑时用户发来消息，用户消息先处理——这一片不计入，消息写入后清零，目标不暂停。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    FUSE_METADATA_KEY,
    NO_PROGRESS_REASON_CODE,
)
from agent_py_agent.agent.conversation.goal_runtime import raise_goal_continuation_wake
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    request_execution,
    request_history,
    submit_gateway_ask,
)
from agent_py_agent.agent.settings.config import AgentConfig

_SESSION = "goal-fuse-session"


@pytest.fixture
def world(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False), tmp_path / "ws")
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    store = agent.conversation_store
    _ask(agent, paths, "开个头")
    thread_id = store.threads.resolve(channel="chat", channel_conversation_id=_SESSION,
                                      channel_user_id="local-agent").thread_id
    goal = store.goals.create({"thread_id": thread_id, "objective": "持续推进同一件事"})
    store.tasks.bind({"thread_id": thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    world = type("World", (), {})()
    world.agent, world.paths, world.store, world.thread_id, world.goal, world.scheduler = (
        agent, paths, store, thread_id, goal, scheduler)
    return world


def _ask(agent, paths, prompt: str) -> dict:
    _request_id, _request_path, response_path = submit_gateway_ask(
        paths, params=GatewayAskParams(prompt=prompt, save=False, chat_session_id=_SESSION, agent=agent))
    _process_gateway_requests(agent, paths)
    return json.loads(response_path.read_text(encoding="utf-8"))


def _fuse(world) -> tuple[str, int, str]:
    goal = world.store.goals.load(world.thread_id, goal_id=world.goal.goal_id)
    state = goal.metadata.get(FUSE_METADATA_KEY) or {}
    return goal.status, int(state.get("idle_slices", 0) or 0), str(state.get("reason_code") or "")


def _background_slice(world, now: float) -> None:
    batch = world.scheduler.tick(now=now)
    assert len(batch) == 1 and batch[0].tool_call_count == 0


def test_gateway_user_message_resets_idle_count_but_background_slices_still_trip(world, monkeypatch):
    raise_goal_continuation_wake(world.store, world.goal, now=100.0)
    _background_slice(world, 101.0)
    _background_slice(world, 102.0)
    assert _fuse(world) == ("active", 2, "")

    response = _ask(world.agent, world.paths, "我补充一句新的要求")

    # 改前：Gateway 前台新消息之后仍是 ("active", 2, "")。
    assert response["ok"] is True
    assert _fuse(world) == ("active", 0, "")
    foreground_turns = []
    original = request_execution._execute_gateway_conversation_turn
    monkeypatch.setattr(request_execution, "_execute_gateway_conversation_turn",
                        lambda *args, **kwargs: foreground_turns.append(1) or original(*args, **kwargs))
    _background_slice(world, 103.0)
    _background_slice(world, 104.0)
    assert _fuse(world) == ("active", 2, "")
    _background_slice(world, 105.0)
    assert _fuse(world) == ("paused", 3, NO_PROGRESS_REASON_CODE)
    # 续跑片全程走后台 wake，没有一片经过 Gateway 前台回合，所以不会被前台的重置清零。
    assert foreground_turns == []
    assert world.store.wakes.pending() == []


def test_gateway_message_after_fuse_pause_clears_count_and_keeps_the_pause_reason(world):
    for index in range(3):
        world.store.goals.record_continuation_fuse({
            "thread_id": world.thread_id, "goal_id": world.goal.goal_id, "task_id": world.goal.task_id,
            "wake_signal_id": f"idle-{index}", "progressed": False, "idle_limit": 3,
        })
    assert _fuse(world) == ("paused", 3, NO_PROGRESS_REASON_CODE)

    assert _ask(world.agent, world.paths, "看看现在进展如何")["ok"] is True

    # 与 ChannelMessageRuntime.receive / CLI 同口径：只清计数，暂停与原因留给 /goal resume 处理。
    assert _fuse(world) == ("paused", 0, NO_PROGRESS_REASON_CODE)


def test_unpersisted_gateway_message_does_not_reset_the_count(world, monkeypatch):
    raise_goal_continuation_wake(world.store, world.goal, now=100.0)
    _background_slice(world, 101.0)
    _background_slice(world, 102.0)
    monkeypatch.setattr(request_history, "append_gateway_conversation_message", lambda *args, **kwargs: False)

    response = _ask(world.agent, world.paths, "这条写不进会话")

    assert response["ok"] is False
    assert _fuse(world) == ("active", 2, "")


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.01)


def test_user_message_queued_during_the_third_idle_slice_is_handled_first(world, monkeypatch):
    from agent_py_agent.agent.conversation import runtime as conversation_runtime
    from agent_py_agent.agent.conversation.run_claim import user_input_turn_on_lane

    raise_goal_continuation_wake(world.store, world.goal, now=100.0)
    _background_slice(world, 101.0)
    _background_slice(world, 102.0)
    assert _fuse(world) == ("active", 2, "")
    slice_accounted, worker = threading.Event(), []
    original_append = request_history.append_gateway_conversation_message
    original_invoke = conversation_runtime._invoke_background_main_agent

    def append_after_accounting(*args, **kwargs):
        # 用户回合已排到车道，停在写入会话之前，直到第 3 片记完账（复现 O4 的先后：记账在用户消息写入之前）。
        slice_accounted.wait(10)
        return original_append(*args, **kwargs)

    def third_slice(*args, **kwargs):
        if not worker:
            # 第 3 片持有本会话车道时用户发来消息：网关回合排队等车道。
            assert world.store.claims.load(world.thread_id).get("status") == "running"
            submit_gateway_ask(world.paths, params=GatewayAskParams(
                prompt="我补充一句：先别停", save=False, chat_session_id=_SESSION, agent=world.agent))
            worker.append(threading.Thread(target=_process_gateway_requests, args=(world.agent, world.paths),
                                           daemon=True))
            worker[0].start()
            _wait_until(lambda: user_input_turn_on_lane(world.store, world.thread_id))
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(request_history, "append_gateway_conversation_message", append_after_accounting)
    monkeypatch.setattr(conversation_runtime, "_invoke_background_main_agent", third_slice)
    _background_slice(world, 103.0)

    # 改前这里已是 ("paused", 3, NO_PROGRESS_REASON_CODE)：第 3 片记账时熔断，用户消息之后只能清计数。
    assert _fuse(world) == ("active", 2, "")
    slice_accounted.set()
    worker[0].join(10)
    assert not worker[0].is_alive()
    # 用户消息写入会话后按 D4 清零，目标没有暂停；用户回合离开车道后不再算在场。
    assert _fuse(world) == ("active", 0, "")
    assert not user_input_turn_on_lane(world.store, world.thread_id)
    # 之后仍要连续 3 个空片才熔断。
    _background_slice(world, 104.0)
    _background_slice(world, 105.0)
    assert _fuse(world) == ("active", 2, "")
    _background_slice(world, 106.0)
    assert _fuse(world) == ("paused", 3, NO_PROGRESS_REASON_CODE)
