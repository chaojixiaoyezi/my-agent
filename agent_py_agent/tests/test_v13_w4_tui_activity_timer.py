"""W4：历史面板、回合计时和失败耗时不能互相污染。"""
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def _history():
    return [{"run_id": f"old-{index}", "status": "DONE", "ended_at": 100.0}
            for index in range(64)]


def test_idle_history_does_not_hold_timer_origin(monkeypatch):
    import time

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    runtime = TuiRuntime("history-timer")
    runtime.update_background_activity(0, {"subagents": _history()})
    state = runtime._background_activity._state
    assert state.started_at == 0.0
    assert len(state.subagents) == 64
    runtime.update_background_activity(1, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 1000.0
    now[0] = 1020.0
    runtime.update_background_activity(0, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 0.0
    now[0] = 1100.0
    runtime.update_background_activity(1, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 1100.0


def test_timer_uses_current_live_main_start(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 2000.0)
    runtime = TuiRuntime("main-timer")
    runtime.update_background_activity(1, {"main_activity": {
        "task_id": "reused", "phase": "running", "started_at": 1990.0}})
    assert runtime._background_activity._state.started_at == 1990.0
    runtime.update_background_activity(1, {"main_activity": {
        "task_id": "reused", "phase": "running", "started_at": 1995.0}})
    assert runtime._background_activity._state.started_at == 1995.0
    runtime.update_background_activity(0, {"main_activity": {
        "task_id": "reused", "phase": "completed", "ended_at": 2000.0}})
    assert runtime._background_activity._state.started_at == 0.0


def test_idle_goal_keeps_panel_without_timer(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 2000.0)
    runtime = TuiRuntime("goal-timer")
    runtime.update_background_activity(0, {"goals": [{"goal_id": "paused", "status": "paused"}]})
    assert runtime._background_activity._state.started_at == 0.0
    assert any(block.role == "background" for block in runtime.store.snapshot().active_blocks)


def test_main_activity_uses_turn_start_and_activation_fallback(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import agent_activity

    link = SimpleNamespace(task_id="reused", created_at=100.0,
                           expires_at=2100.0, duration_seconds=120)
    row = {"task_id": "reused", "phase": "running", "started_at": 1990.0}
    monkeypatch.setattr(agent_activity, "background_main_activity", lambda *_: dict(row))
    kwargs = {"workspace_task_id": "reused"}
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 1990.0
    row.clear()
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 1980.0
    link.expires_at = 0.0
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 100.0


def test_gateway_failure_duration_overrides_missing_clock():
    import inspect

    from agent_py_agent.cli.turn_completion import TurnCompletionLedger, render_completion

    assert "gateway_timing" in inspect.signature(TurnCompletionLedger.finish).parameters
    ledger = TurnCompletionLedger()
    completed, first = ledger.finish("early-failure", "failed", gateway_timing={"duration_seconds": 0.584})
    assert first
    assert completed.elapsed_seconds == 0.584
    assert "未知" not in render_completion(completed)
    assert ledger.finish("early-failure", "completed", gateway_timing={"duration_seconds": 99.0}) == (completed, False)


def test_gateway_failure_duration_fallbacks(monkeypatch):
    import inspect

    from agent_py_agent.cli.turn_completion import TurnCompletionLedger

    assert "gateway_timing" in inspect.signature(TurnCompletionLedger.finish).parameters
    monkeypatch.setattr("time.monotonic", lambda: 20.0)
    ledger = TurnCompletionLedger()
    ledger.accept("local")
    monkeypatch.setattr("time.monotonic", lambda: 23.0)
    completed, _ = ledger.finish("local", "failed", gateway_timing={"duration_seconds": float("nan")})
    assert completed.elapsed_seconds == 3.0
    completed, _ = ledger.finish("range", "failed", gateway_timing={"duration_seconds": -1.0, "started_at": 10.0, "ended_at": 10.584})
    assert abs(completed.elapsed_seconds - 0.584) < 1e-9
    completed, _ = ledger.finish("unknown", "failed", gateway_timing={"duration_seconds": float("inf"), "started_at": 20.0, "ended_at": 10.0})
    assert completed.elapsed_seconds is None


def test_failed_tui_summary_uses_gateway_duration():
    from dataclasses import fields

    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary

    assert "gateway_timing" in {field.name for field in fields(TuiTurnSummary)}
    runtime = TuiRuntime("early-failure-runtime")
    runtime.begin_turn("failure")
    runtime.complete_turn("failure", TuiTurnSummary(ok=False, error="failed", gateway_timing={"duration_seconds": 0.584}))
    completion = runtime.completions.clocks["failure"].completion
    assert completion.elapsed_seconds == 0.584
    blocks = runtime.store.snapshot().stable_blocks
    assert any(block.metadata.get("source_kind") == "turn_timing" and "未知" not in block.text for block in blocks)
