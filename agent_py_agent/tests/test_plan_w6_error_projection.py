"""W6：诊断出口必须保留机器事实，展示文案不能改变错误类别。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotEntry, SkillSnapshotError
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.gateway_parts import request_errors, wake_ops_control
from agent_py_agent.agent.gateway_parts.loop_health import GatewayLoopHealth
from agent_py_agent.cli.chat_parts import tui, tui_preflight
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def _tui_context(monkeypatch, run, **facts):
    lines, touched = [], []
    monkeypatch.setattr(tui, "patch_stdout", nullcontext)
    monkeypatch.setattr(tui, "_cprint", lines.append)
    app = SimpleNamespace(run=run, **facts)
    ctx = tui.TuiLoopContext(
        app=app, refresh_stop=threading.Event(), stop_event=threading.Event(),
        session_manager=SimpleNamespace(touch_session=lambda *a, **k: touched.append(a)),
        current_session_id="session-w6",
    )
    return ctx, lines, touched


def test_tui_nonzero_exit_does_not_claim_normal_recovery(monkeypatch):
    facts = {"error_code": "GATEWAY_START_FAILED", "request_id": "request-w6", "diagnostic_ref": "diag-w6"}
    ctx, lines, touched = _tui_context(
        monkeypatch, lambda **kw: 7, _my_agent_exit_diagnostic=facts,
    )
    assert tui._run_tui_loop(ctx) == 7
    text = "\n".join(lines)
    assert "已退出界面；会话与 Gateway" not in text and "恢复：" not in text
    assert all(str(value) in text for value in facts.values()) and '"exit_code": 7' in text
    assert not touched
    assert ctx.stop_event.is_set() and ctx.refresh_stop.is_set()


def test_tui_exception_keeps_diagnostic_and_original_exception(monkeypatch):
    error = RuntimeError("private body not for display")
    error.error_code, error.request_id, error.diagnostic_ref = "TUI_RUNTIME_FAILED", "r-exc", "d-exc"

    def fail(**kwargs):
        raise error

    ctx, lines, touched = _tui_context(monkeypatch, fail)
    with pytest.raises(RuntimeError) as caught:
        tui._run_tui_loop(ctx)
    assert caught.value is error
    text = "\n".join(lines)
    assert all(value in text for value in ("TUI_RUNTIME_FAILED", "r-exc", "d-exc"))
    assert "private body" not in text and "恢复：" not in text and not touched
    assert ctx.stop_event.is_set() and ctx.refresh_stop.is_set()


def test_tui_normal_exit_retains_original_resume_notice(monkeypatch):
    ctx, lines, touched = _tui_context(monkeypatch, lambda **kw: 0)
    assert tui._run_tui_loop(ctx) == 0
    assert touched and "恢复：my-agent resume session-w6" in "\n".join(lines)


def test_tui_exception_diagnostic_is_printed_after_output_sink_is_released(monkeypatch):
    from agent_py_agent.cli.chat_parts import rendering

    def fail(**kwargs):
        raise RuntimeError("application failed")

    ctx, lines, _ = _tui_context(monkeypatch, fail)
    sink = [object()]
    monkeypatch.setattr(rendering, "set_tui_output_sink", lambda value: sink.__setitem__(0, value))
    monkeypatch.setattr(tui, "_cprint", lambda text: lines.append((sink[0] is None, text)))
    with pytest.raises(RuntimeError):
        tui._run_tui_loop(ctx)
    assert lines and all(released for released, text in lines)


def test_preflight_failure_reaches_outer_tui_diagnostic(monkeypatch):
    runtime = TuiRuntime("w6-preflight")
    future = SimpleNamespace(done=lambda: False)
    app = SimpleNamespace(future=future, invalidate=lambda: None)
    app.exit = lambda *, result: setattr(app, "result", result)
    monkeypatch.setattr(tui_preflight, "wait_for_gateway_readiness", lambda *a, **k: SimpleNamespace(
        ready=False, reason="GATEWAY_START_FAILED", request_id="r-preflight", diagnostic_ref="d-preflight"))
    config = tui_preflight.TuiGatewayPreflight(
        application=app, runtime=runtime, paths=object(), timeout_seconds=1,
        stop_event=threading.Event(), on_ready=lambda: pytest.fail("worker must not start"),
    )
    tui_preflight._run_gateway_preflight(config)
    assert app.result == 2
    assert getattr(app, "_my_agent_exit_diagnostic", {}) == {
        "error_code": "GATEWAY_START_FAILED", "request_id": "r-preflight", "diagnostic_ref": "d-preflight",
    }


def test_wake_list_and_preview_retain_structured_reasons_only():
    fields = {"error_code": "SKILL_SNAPSHOT_STALE", "diagnostic_ref": "wake-diag"}
    row = dict(wake_signal_id="wake-1", thread_id="thread-1", reason="session_task",
               reason_code="error:SKILL_SNAPSHOT_STALE", quarantined_at=1)
    record = {"quarantine": {"decision": row, "last_error": fields | {"message": "PRIVATE"}}}
    attempts = SimpleNamespace(quarantined=lambda: ([row], []), replay_source=lambda key: ("", record))
    text = wake_ops_control._render_list(SimpleNamespace(wakes=SimpleNamespace(attempts=attempts)))
    signal = SimpleNamespace(wake_signal_id="wake-1", thread_id="thread-1", reason="session_task")
    preview = wake_ops_control._render_plan(signal, record)
    for value in (text, preview):
        assert all(item in value for item in (*fields.values(), row["reason_code"]))
        assert "PRIVATE" not in value


@pytest.mark.parametrize("code", ["SKILL_SNAPSHOT_STALE", "SKILL_SNAPSHOT_MISSING"])
def test_skill_snapshot_keeps_original_typed_error(tmp_path, code):
    entry = SkillSnapshotEntry(
        stable_id="owner:w6", name="w6", description="test", path=str(tmp_path / "w6" / "SKILL.md"),
        source="owner", category="general", content_sha256=sha256(b"body").hexdigest(),
    )
    error = SkillSnapshotError(code, "展示明细", reason="snapshot_reason")

    def fail(_stable_id):
        raise error

    snapshot = SimpleNamespace(enabled_entries=lambda: (entry,), read_body=fail)
    # 元数据不预读正文；显式正文读取仍须原样传播同一个类型化异常。
    metadata = get_owner_skill_descriptor(tmp_path, "owner:w6", skill_snapshot=snapshot)
    assert metadata["metadata"]["content_hash"] == entry.content_sha256 and "body_markdown" not in metadata
    with pytest.raises(SkillSnapshotError) as caught:
        get_owner_skill_descriptor(tmp_path, "owner:w6", skill_snapshot=snapshot, include_body=True)
    assert caught.value is error and caught.value.error_code == code


def _sqlite_wrapper():
    try:
        with sqlite3.connect(":memory:") as db:
            db.execute("SELECT * FROM w6_missing_table")
    except sqlite3.Error as cause:
        try:
            raise RuntimeError("outer error, not a classification source") from cause
        except RuntimeError as outer:
            return outer, cause
    raise AssertionError("SQLite probe must fail")


def test_sqlite_explicit_cause_is_preserved_in_health_and_gateway():
    error, cause = _sqlite_wrapper()
    health = GatewayLoopHealth()
    health.note_loop_error("dispatcher", "w6", error)
    loop_record = health.snapshot()["last_dispatch_tick_error"]
    projection = request_errors.gateway_provider_error_projection(error)
    response = request_errors.gateway_unhandled_worker_error_response(
        Path("request-w6.json"), {}, error, request_id="request-w6")
    for value in (loop_record, projection, response["worker_error"]):
        assert value.get("cause_type") == type(cause).__name__
        assert value.get("cause_sqlite_errorcode") == cause.sqlite_errorcode
        assert value.get("cause_sqlite_errorname") == cause.sqlite_errorname


def test_sqlite_words_and_implicit_context_do_not_create_native_cause():
    try:
        raise sqlite3.OperationalError("database is locked")
    except sqlite3.Error:
        error = RuntimeError("SQLITE_BUSY sqlite_errorcode=5 database is locked")
    health = GatewayLoopHealth()
    health.note_loop_error("dispatcher", "w6", error)
    for value in (health.snapshot()["last_dispatch_tick_error"], request_errors.gateway_provider_error_projection(error)):
        assert not any(key.startswith("cause_") for key in value)


def test_gateway_keeps_explicit_contract_fields_even_when_detail_disagrees():
    error = RuntimeError("permission denied SQL locked retry now")
    facts = dict(error_code="RUNTIME_CONFLICT", category="custom-state", retryable=False,
                 retry_after_ms=0, request_id="request-typed", diagnostic_ref="diagnostic-typed")
    for key, value in facts.items():
        setattr(error, key, value)
    error.detail = "missing timeout all done"
    projection = request_errors.gateway_provider_error_projection(error)
    assert {key: projection.get(key) for key in facts} == facts
    assert "missing timeout" not in json.dumps(projection)


@pytest.mark.parametrize("code", ["RUNTIME_CONFLICT", "PROVIDER_TIMEOUT", "TRANSIENT_ERROR"])
def test_gateway_runtime_codes_have_registered_actionable_display(code):
    message = request_errors.gateway_client_error_message(code)
    assert message != request_errors.gateway_client_error_message("W6_UNKNOWN_CODE")
    assert message == error_contract(code).recovery_hint


def test_empty_provider_error_code_cannot_erase_request_or_worker_fallback(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderResponseError
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), tmp_path)
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    request = {"id": "request-empty-code", "kind": "ask", "status": "processing", "turn_phase": "open",
               "prompt": "test", "execution_attempt_id": "claimed-empty-code-attempt",
               "conversation": {"channel": "chat", "channel_conversation_id": "w6-empty-code",
                                "channel_user_id": "local-agent", "canonical_user_id": "local-agent"}}
    request_path = paths.processing / "request-empty-code.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    error = ProviderResponseError("Responses 流终态格式无效。")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(agent, "run", fail)
    response = _handle_gateway_request(agent, request_path)
    assert response["error_code"] == "UNKNOWN_ERROR"
    worker = request_errors.gateway_unhandled_worker_error_response(
        request_path, request, error, request_id="request-empty-code")
    assert worker["error_code"] == "GATEWAY_WORKER_UNHANDLED_ERROR"


def _context_usage(tokens, **fields):
    return {"schema": "model_visible_context_usage.v1", "current_tokens": tokens,
            "context_window_tokens": 500_000, "compact_trigger_tokens": 450_000,
            "estimated": True, "protocol": "native", **fields}


@pytest.mark.parametrize("include_poll_usage", [False, True])
def test_new_conversation_does_not_restore_old_compact_context(include_poll_usage):
    runtime = TuiRuntime("w6-context-reset")
    runtime.update_background_activity(1, {"compact_count": 2, "context_usage": _context_usage(40_000)})
    controller = runtime._background_activity
    runtime.publish_conversation_reset("new-thread-id")
    runtime.enqueue_prompt("new-request", "新线程", queued=False)
    turn = runtime.begin_turn("new-request")
    assert turn.write_context_usage(_context_usage(270_000))
    poll = {"compact_count": 0}
    if include_poll_usage:
        poll["context_usage"] = _context_usage(270_000)
    runtime.update_background_activity(1, poll)
    status = runtime.store.snapshot().status
    assert runtime._background_activity is controller
    assert status.compact_count == 0 and status.context_usage.current_tokens == 270_000
    assert controller._state.compact_count == 0
    assert controller._state.context_usage["current_tokens"] == 270_000


def test_live_context_usage_survives_same_generation_heartbeat_without_usage():
    runtime = TuiRuntime("w6-live-context")
    runtime.update_background_activity(1, {"compact_count": 0, "context_usage": _context_usage(40_000)})
    turn = runtime.begin_turn("live-request")
    assert turn.write_context_usage(_context_usage(270_000))
    runtime.update_background_activity(2, {"compact_count": 0})
    status = runtime.store.snapshot().status
    assert status.compact_count == 0 and status.context_usage.current_tokens == 270_000


def test_late_previous_conversation_context_cannot_change_new_thread_cache():
    runtime = TuiRuntime("w6-context-old-turn")
    old = runtime.begin_turn("old-request")
    runtime.publish_conversation_reset("new-thread")
    fresh = runtime.begin_turn("new-request")
    assert fresh.write_context_usage(_context_usage(270_000))
    before = runtime.store.snapshot()
    assert old.write_context_usage(_context_usage(40_000)) is False
    assert runtime.store.snapshot() == before
    assert runtime._background_activity._state.context_usage["current_tokens"] == 270_000


def test_explicit_old_compact_context_cannot_poison_live_cache():
    runtime = TuiRuntime("w6-context-old-compact")
    runtime.update_background_activity(1, {"compact_count": 2, "context_usage": _context_usage(40_000)})
    turn = runtime.begin_turn("request")
    before = runtime.store.snapshot()
    assert turn.write_context_usage(_context_usage(270_000, compact_generation=1)) is False
    assert runtime.store.snapshot() == before


def test_compact_boundary_does_not_republish_precompact_live_usage():
    runtime = TuiRuntime("w6-context-boundary")
    turn = runtime.begin_turn("request")
    assert turn.write_context_usage(_context_usage(270_000))
    runtime.publish_compact_boundary(1)
    runtime.update_background_activity(1, {"compact_count": 1})
    status = runtime.store.snapshot().status
    assert status.compact_count == 1 and status.context_usage is None


@pytest.mark.parametrize("next_count", [1, 2])
@pytest.mark.parametrize("boundary_source", ["stream", "poll"])
def test_compact_boundary_retires_nested_background_context(next_count, boundary_source):
    runtime = TuiRuntime("w6-context-nested-boundary")
    usage = _context_usage(270_000)
    runtime.update_background_activity(1, {
        "compact_count": 0, "context_usage": usage,
        "main_activity": {"phase": "thinking", "activity": "正在分析", "context_usage": usage},
    })
    if boundary_source == "stream":
        runtime.publish_compact_boundary(1)
    runtime.update_background_activity(next_count, {"compact_count": 1})
    status = runtime.store.snapshot().status
    cached = runtime._background_activity._state
    assert status.compact_count == 1 and status.context_usage is None
    assert cached.compact_count == 1 and cached.context_usage is None
    assert "context_usage" not in cached.main_activity
    assert cached.main_activity["activity"] == "正在分析"


@pytest.mark.parametrize("poll_generation", [0, 1])
def test_nested_poll_context_must_match_current_compact_generation(poll_generation):
    runtime = TuiRuntime("w6-context-nested-poll")
    runtime.update_background_activity(1, {"compact_count": 0, "context_usage": _context_usage(40_000)})
    runtime.publish_compact_boundary(1)
    runtime.update_background_activity(2, {
        "compact_count": poll_generation,
        "main_activity": {"phase": "thinking", "context_usage": _context_usage(270_000)},
    })
    status = runtime.store.snapshot().status
    assert status.compact_count == 1
    if poll_generation == 1:
        assert status.context_usage.current_tokens == 270_000
    else:
        assert status.context_usage is None
        assert "context_usage" not in runtime._background_activity._state.main_activity


def test_old_adapter_compact_event_cannot_clear_new_conversation_usage():
    runtime = TuiRuntime("w6-context-old-compact-event")
    old = runtime.begin_turn("old-request")
    runtime.publish_conversation_reset("new-thread")
    fresh = runtime.begin_turn("new-request")
    assert fresh.write_context_usage(_context_usage(270_000))
    before = runtime.store.snapshot()
    assert old.on_gateway_event({"kind": "conversation_compacted", "compact_generation": 2}) is False
    assert runtime.store.snapshot() == before
    runtime.update_background_activity(1, {"compact_count": 0})
    status = runtime.store.snapshot().status
    assert status.compact_count == 0 and status.context_usage.current_tokens == 270_000
