from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import (
    _publish_background_activity,
    _refresh_loop,
)


def test_child_context_uses_root_client_refresh_health_without_copying_state():
    from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
    from agent_py_agent.cli.chat_parts.tui_ui_setup import _make_render_context_factory

    root = TuiRuntime("root")
    child = TuiRuntime("child")
    params = SimpleNamespace(
        agent=SimpleNamespace(config=SimpleNamespace(), root=""),
        tui_runtime=root,
        agent_navigation=SimpleNamespace(active_runtime=lambda: child),
    )
    factory = _make_render_context_factory(
        params, TuiInteractionState(), TuiTranscriptModeState()
    )
    assert factory(100).background_sync_failed is False
    root.publish_background_sync_status(ok=False)
    assert factory(100).background_sync_failed is True
    assert child.store.snapshot().background_sync_failed is False
    root.publish_background_sync_status(ok=True)
    assert factory(100).background_sync_failed is False


class _FiniteStop:
    def __init__(self, ticks: int) -> None:
        self._remaining = ticks

    def wait(self, _timeout: float) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


class _RefreshDecisions:
    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = iter(decisions)

    def needs_periodic_refresh(self) -> bool:
        return next(self._decisions)


def test_runtime_periodic_refresh_stops_when_visible_animation_finishes(monkeypatch) -> None:
    clock = iter([10.0, 10.2, 11.0, 11.2, 12.0, 12.1])
    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.tui_runtime.time.monotonic",
        lambda: next(clock),
    )
    runtime = TuiRuntime("refresh-runtime")
    assert runtime.needs_periodic_refresh() is False

    runtime.publish_connection_check()
    assert runtime.needs_periodic_refresh() is True
    runtime.resolve_connection_check(ok=True)
    runtime.set_notice("Saved", duration_seconds=0.5)
    assert runtime.needs_periodic_refresh() is True
    # 到期的第一帧仍需 invalidate，才能从真实终端上擦掉上一帧 footer。
    assert runtime.needs_periodic_refresh() is True
    assert runtime.needs_periodic_refresh() is False


def test_refresh_loop_invalidates_only_active_ticks() -> None:
    app = SimpleNamespace(invalidate_calls=0)

    def invalidate() -> None:
        app.invalidate_calls += 1

    app.invalidate = invalidate
    _refresh_loop(
        _FiniteStop(3),
        [app],
        _RefreshDecisions([False, True, False]),
    )
    assert app.invalidate_calls == 1


def test_background_terminal_transition_requests_exactly_one_followup_frame() -> None:
    runtime = TuiRuntime("background-terminal-frame")

    assert runtime.update_background_activity(1) is True
    assert runtime.needs_periodic_refresh() is True
    assert runtime.update_background_activity(0) is True
    assert runtime.needs_periodic_refresh() is True
    assert runtime.needs_periodic_refresh() is False


def test_background_final_response_requests_exactly_one_followup_frame() -> None:
    runtime = TuiRuntime("background-final-frame")

    runtime.publish_background_response("最终回复", thread_id="thread-final", message_id="msg-final")

    assert runtime.needs_periodic_refresh() is True
    assert runtime.needs_periodic_refresh() is False


def test_background_activity_updates_goal_and_child_navigation_together() -> None:
    runtime = TuiRuntime("threading-goal-projection")
    calls: list[tuple[str, object]] = []
    navigation = SimpleNamespace(
        update_goal_rows=lambda value: calls.append(("goals", value)) or True,
        update_rows=lambda parent, value: calls.append((parent, value)) or True,
    )
    goal = {
        "goal_id": "goal-one",
        "name": "持续验证",
        "objective": "验证底座",
        "status": "active",
    }
    child = {
        "run_id": "child-one",
        "parent_run_id": "",
        "status": "RUNNING",
    }

    assert _publish_background_activity(
        runtime,
        {
            "active_task_count": 1,
            "active_task_projection_ok": True,
            "goal_projection_ok": True,
            "subagent_projection_ok": True,
            "goals": [goal],
            "subagents": [child],
        },
        agent_navigation=navigation,
    ) is True

    assert calls == [("goals", [goal]), ("", [child])]


# LLM: 只替换网络出口并调用真实客户端方法；超时和请求载荷须一起保持，不能靠静态源码匹配。
# 函数用途: 锁住两个轻量轮询请求的五秒预算，正常响应原样交回。
@pytest.mark.parametrize('endpoint', ['notices', 'agent-view'])
def test_lightweight_poll_requests_use_five_second_timeout(endpoint):
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    calls, response = [], {'ok': True}
    # LLM: 记录真实方法交给公共HTTP出口的参数，不访问网络或绕过身份裁决。
    # 函数用途: 返回可核对的轮询响应并捕获超时。
    def post(path, payload, *, timeout):
        calls.append((path, payload, timeout))
        return 200, response
    client = SimpleNamespace(post_gateway_json=post)
    if endpoint == 'notices':
        result = GatewayChatClientAgent.request_background_notices(
            client, 'session-poll', after=12, event_after=7, event_stream_id='stream-poll')
        expected = {'after': 12, 'event_after': 7, 'event_stream_id': 'stream-poll',
            'client_capabilities': {'tool_approval': True, 'foreground_messages': True,
                                    'foreground_transcript': True, 'display_checkpoints': True},
            'user_id': 'local-agent', 'channel': 'chat', 'conversation_id': 'session-poll'}
    else:
        result = GatewayChatClientAgent.request_agent_view(client, 'session-poll', run_id='child-poll', event_after=7)
        expected = {'user_id': 'local-agent', 'channel': 'chat', 'conversation_id': 'session-poll',
                    'run_id': 'child-poll', 'event_after': 7}
    assert result is response
    assert calls == [(f'/client/{endpoint}', expected, 5.0)]


# LLM: 替换传输和单调时钟，运行真实循环及TuiRuntime；健康休眠不算故障，故障退避仍累计并在恢复时清除。
# 函数用途: 验证空闲/活动轮询两次短失败不闪警告、持续失败会警告，恢复后重新计时。
@pytest.mark.parametrize('foreground_running', [False, True])
@pytest.mark.parametrize('raise_failure', [False, True])
def test_notice_loop_excludes_healthy_sleep_from_failure_duration(monkeypatch, foreground_running, raise_failure):
    from agent_py_agent.cli.chat_parts import tui_threading

    clock, waits, failed_states = [100.0], [], []
    outcomes = iter([True, False, False, False, False, True, False, False])
    runtime = TuiRuntime('healthy-idle-debounce')
    # LLM: 单次传输仅耗时0.1秒；异常路径也必须沿真实循环去抖，而非直接控制界面状态。
    # 函数用途: 提供健康、持续失败、恢复和再次短失败的确定序列。
    def consume(*_args, **_kwargs):
        ok = next(outcomes)
        clock[0] += 0.1
        if not ok and raise_failure:
            raise TimeoutError('poll failed')
        return ok
    # LLM: 等待前读取真实展示快照，再推进假时钟；按轮数停止，不按告警次数停止。
    # 函数用途: 记录每轮失败标志及退避延迟，避免真实睡眠。
    def wait(delay):
        failed_states.append(runtime.store.snapshot().background_sync_failed)
        waits.append(delay)
        clock[0] += delay
        return len(waits) == 8
    monkeypatch.setattr(tui_threading.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(tui_threading, '_consume_background_notices', consume)
    stop = SimpleNamespace(is_set=lambda: False, wait=wait)
    tui_threading._background_notice_loop(stop, object(), 'session-poll', runtime, [None],
                                        foreground_running_ref=[foreground_running])
    healthy_delay = 1.0 if foreground_running else 5.0
    assert waits == [healthy_delay, .5, 1.0, 2.0, 4.0, healthy_delay, .5, 1.0]
    assert failed_states == [False, False, False, False, True, False, False, False]
    assert tui_threading.TUI_BACKGROUND_NOTICE_WARN_AFTER_FAILURE_COUNT == 2
    assert tui_threading.TUI_BACKGROUND_NOTICE_WARN_AFTER_SECONDS == 3.0
