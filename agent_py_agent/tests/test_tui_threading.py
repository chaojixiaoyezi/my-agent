from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import (
    _publish_background_activity,
    _refresh_loop,
)


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
