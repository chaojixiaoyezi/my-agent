"""用真实 runtime/reducer 验证入口隔离，保留既有 reducer 的跨线程采纳合同。"""
from dataclasses import replace

import pytest

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, _tui_thread_id


def _usage(thread_id="thread-a", *, generation=0, model="explicit", window=400000):
    return {
        "schema": "model_visible_context_usage.v1", "estimated": True,
        "thread_id": thread_id, "producer": {"scope": "main", "agent_run_id": "main-a"},
        "context_window_tokens": window, "compact_trigger_tokens": window * 4 // 5,
        "current_tokens": 1200, "messages_tokens": 1200,
        "model_name": model, "compact_generation": generation,
    }


def _runtime():
    runtime = TuiRuntime("w10-scope")
    runtime.publish_conversation_reset("thread-a")
    turn = runtime.begin_turn("request-a")
    assert turn.write_context_usage(_usage())
    return runtime, turn


def _rejected(runtime):
    assert any(item.code == "TUI_FOREIGN_THREAD_EVENT_REJECTED"
               for item in runtime.store.snapshot().diagnostics)


@pytest.mark.parametrize("envelope", [False, True])
def test_foreign_usage_never_changes_bound_status(envelope):
    runtime, turn = _runtime()
    before = runtime.store.snapshot().status
    foreign = _usage("thread-foreign", generation=3, model="sol", window=500000)
    payload = {"kind": "context_usage_updated", "context_usage": foreign}
    if envelope:
        payload["thread_id"] = "thread-foreign"
        payload["context_usage"] = {key: value for key, value in foreign.items() if key != "thread_id"}
    assert not turn.on_gateway_event(payload)
    assert runtime.store.snapshot().status == before
    _rejected(runtime)
    assert runtime.update_background_activity(1, {"thread_id": "thread-a", "compact_count": 0})
    assert runtime.store.snapshot().status.thread_id == "thread-a"


def test_direct_usage_callback_checks_snapshot_identity():
    runtime, turn = _runtime()
    before = runtime.store.snapshot().status
    assert not turn.write_context_usage(_usage("thread-child", model="sol", window=500000))
    assert runtime.store.snapshot().status == before
    _rejected(runtime)


def test_foreign_activity_rejection_has_diagnostic_and_keeps_cache():
    runtime, _ = _runtime()
    before, cache = runtime.store.snapshot().status, runtime._background_activity._state
    assert not runtime.update_background_activity(1, {
        "thread_id": "thread-other", "compact_count": 3,
        "context_usage": _usage("thread-other", generation=3, model="sol", window=500000),
    })
    assert runtime.store.snapshot().status == before
    assert runtime._background_activity._state == cache
    _rejected(runtime)


def test_anonymous_background_usage_is_not_a_bound_request_stream():
    runtime, _ = _runtime()
    before = runtime.store.snapshot().status
    assert not runtime.update_background_activity(0, {"compact_count": 3,
                                                     "context_usage": _usage("")})
    assert runtime.store.snapshot().status == before
    _rejected(runtime)


def _background(kind, thread_id, *, seq=1):
    return {
        "schema": "background_transcript_event.v1", "seq": seq,
        "request_id": "bg-main:run", "block_id": f"bg-main:run:block-{seq}",
        "thread_id": thread_id, "kind": kind, "phase": "completed",
        "payload": {"text": "background", "compact_generation": 3, "thread_id": thread_id},
    }


@pytest.mark.parametrize("kind", ["compact_boundary", "system_message"])
@pytest.mark.parametrize("thread_id", ["thread-foreign", ""])
def test_background_transcript_cannot_default_foreign_or_missing_thread(kind, thread_id):
    runtime, _ = _runtime()
    before = runtime.store.snapshot()
    assert runtime.publish_background_transcript_events([_background(kind, thread_id)]) == 1
    after = runtime.store.snapshot()
    assert after.status == before.status
    assert after.stable_blocks == before.stable_blocks
    _rejected(runtime)


def test_compact_frame_identity_is_checked_before_adapter_labels_it():
    runtime, turn = _runtime()
    before = runtime.store.snapshot().status
    assert not turn.on_gateway_event({"kind": "conversation_compacted",
                                      "thread_id": "thread-foreign", "compact_generation": 3})
    assert runtime.store.snapshot().status == before
    _rejected(runtime)
    assert turn.on_gateway_event({"kind": "conversation_compacted",
                                  "thread_id": "thread-a", "compact_generation": 1})
    boundary = runtime.store.snapshot().stable_blocks[-1]
    assert boundary.kind == "compact_boundary"
    assert runtime.store.snapshot().status.compact_count == 1
    events = runtime.store.journal.snapshot()
    assert next(event for event in reversed(events) if event.kind == "compact_boundary").payload["thread_id"] == "thread-a"


def test_status_is_not_an_authority_when_explicit_binding_absent():
    runtime = TuiRuntime("not-status-bound")
    runtime.store.reducer.status = replace(runtime.store.reducer.status, thread_id="event-only-thread")
    assert _tui_thread_id(runtime) == ""
    runtime._background_watch_gateway_binding = (runtime.session_id, "thread-real")
    assert _tui_thread_id(runtime) == "thread-real"


@pytest.mark.parametrize("binding", ["new", "resume", "successor", "submit"])
def test_only_explicit_binding_can_retarget_status(binding):
    runtime, old = _runtime()
    if binding == "new":
        runtime.publish_conversation_reset("thread-b")
    elif binding == "resume":
        runtime.publish_recovered_history((), display_events=({
            "schema": "conversation_history_display.v1", "request_id": "history:thread-b:message",
            "block_id": "history:thread-b:message:user", "kind": "user_message",
            "phase": "completed", "payload": {"text": "resumed"},
        },))
    elif binding == "successor":
        runtime.background_message_thread_id = "thread-b"
        runtime._background_watch_gateway_binding = (runtime.session_id, "thread-b")
    else:
        runtime = TuiRuntime("submission-bound")
        runtime.begin_turn("request-b", task_progress_generation_id="gateway-b")
        runtime._bind_foreground_request_thread("gateway-b", "thread-b")
        assert _tui_thread_id(runtime) == "thread-b"
    current = runtime.begin_turn("request-next")
    assert current.write_context_usage(_usage("thread-b"))
    before = runtime.store.snapshot().status
    assert before.thread_id == "thread-b" and before.compact_count == 0
    old.write_context_usage(_usage("thread-a", generation=3))
    assert runtime.store.snapshot().status == before


def test_legacy_anonymous_frames_only_keep_own_adapter_identity():
    runtime, turn = _runtime()
    usage = _usage()
    usage.pop("thread_id")
    assert turn.on_gateway_event({"kind": "context_usage_updated", "context_usage": usage})
    assert turn.on_gateway_event({"kind": "conversation_compacted", "compact_generation": 1})
    assert runtime.store.snapshot().status.thread_id == "thread-a"
    assert runtime.store.snapshot().status.compact_count == 1
