"""真实恢复页缺 Context 的定向定位；不代替 Gateway/TUI 最终验收。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


# LLM: 与公开 preflight schema 相同的数值夹具；不模拟 provider 成功或任务完成。
# 函数用途: 供纯投影、持久化和 TUI reducer 检查使用一份可对照数字。
def usage(tokens=42_100):
    return {
        "schema": "model_visible_context_usage.v1", "estimated": True,
        "context_window_tokens": 128_000, "compact_trigger_tokens": 115_200,
        "current_tokens": tokens, "prompt_tokens": 10_000, "messages_tokens": tokens-20_000,
        "runtime_guidance_tokens": 0, "tool_schema_tokens": 10_000, "protocol": "native",
    }


def test_idle_thread_projection_keeps_context_without_main_activity(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice"})
    store.update_model_context_usage(thread.thread_id, usage(), expected_compact_generation=0)
    reopened = ConversationStore(tmp_path)
    projected = conversation_agent_activity(SimpleNamespace(), reopened, thread.thread_id).to_dict()
    assert projected["context_usage"]["current_tokens"] == 42_100
    assert projected["main_activity"] == {} and projected["active_task_count"] == 0


def test_idle_context_snapshot_reaches_tui_without_working_or_history():
    runtime = TuiRuntime("idle-restore")
    runtime.update_background_activity(0, {"context_usage": usage(), "main_activity": {}})
    snapshot = runtime.store.snapshot()
    assert snapshot.status.context_usage.current_tokens == 42_100
    assert snapshot.status.phase == "idle"
    assert not snapshot.active_blocks and not snapshot.stable_blocks
    assert runtime.update_background_activity(0, {"context_usage": usage(), "main_activity": {}}) is False


def test_authoritative_empty_context_clears_old_snapshot():
    runtime = TuiRuntime("compact-restore")
    runtime.update_background_activity(0, {"context_usage": usage()})
    assert runtime.store.snapshot().status.context_usage.current_tokens == 42_100
    runtime.update_background_activity(0, {"compact_count": 1, "context_usage": {}})
    assert runtime.store.snapshot().status.context_usage is None
    assert runtime.store.snapshot().status.context_tokens == 0


def test_model_context_store_is_generation_fenced_and_not_recency(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice", "now": 5.0})
    updated = store.update_model_context_usage(thread.thread_id, usage(), expected_compact_generation=0)
    assert updated.updated_at == thread.updated_at
    assert updated.provider_context_observation == {}
    denied = store.update_model_context_usage(thread.thread_id, usage(55_000), expected_compact_generation=3)
    assert denied.model_context_usage == updated.model_context_usage
    other = store.get_or_create_thread({"canonical_user_id": "bob", "channel_conversation_id": "other"})
    assert other.model_context_usage == {}
    assert ConversationThread.from_dict(updated.to_dict()).model_context_usage == updated.model_context_usage


@pytest.mark.parametrize("isolated", [False, True])
def test_preflight_context_uses_exact_main_or_child_thread(tmp_path, isolated):
    from agent_py_agent.agent.conversation.context_usage import record_model_context_usage
    store = ConversationStore(tmp_path)
    main = store.get_or_create_thread({"canonical_user_id": "main", "channel_conversation_id": "main"})
    child = store.get_or_create_thread({"canonical_user_id": "child", "channel_conversation_id": "child"})
    agent = SimpleNamespace(conversation_store=store)
    attrs = {"agent_thread_id": child.thread_id, "conversation_thread_id": main.thread_id}
    params = SimpleNamespace(task_attributes=attrs, context_scope="isolated" if isolated else "default")
    assert record_model_context_usage(agent, params, usage()) is (not isolated)
    assert bool(store.load_thread(child.thread_id).model_context_usage) is (not isolated)
    assert store.load_thread(main.thread_id).model_context_usage == {}


def test_compact_and_bad_projection_do_not_restore_old_context(tmp_path):
    from agent_py_agent.agent.conversation.context_usage import context_usage_from_thread
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice"})
    row = store.update_model_context_usage(thread.thread_id, usage(), expected_compact_generation=0)
    assert context_usage_from_thread(replace(row, compact_generation=1)) == {}
    committed = store.update_compact_state(thread.thread_id, expected_generation=0, commit=ConversationCompactCommit(
        summary="保留目标与已完成工作", operation_evidence={}, checkpoint_id="test-checkpoint",
        compacted_through_message_id="", compacted_through_byte_offset=0,
        source_messages=0, source_tool_pairs=0,
    ))
    assert committed.compact_generation == 1 and committed.model_context_usage == {}


def test_context_display_telemetry_never_enters_model_bundle(tmp_path):
    from agent_py_agent.agent.conversation.runtime import _minimal_context_bundle
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice"})
    before = store.context_bundle(thread.thread_id)
    stored = store.update_model_context_usage(thread.thread_id, usage(), expected_compact_generation=0)
    assert store.context_bundle(thread.thread_id) == before
    assert _minimal_context_bundle(stored) == _minimal_context_bundle(thread)


def test_delayed_old_generation_cannot_restore_stale_context():
    runtime = TuiRuntime("ordered-context")
    runtime.update_background_activity(0, {"compact_count": 1, "context_usage": usage(55_000)})
    runtime.update_background_activity(0, {"compact_count": 0, "context_usage": usage()})
    assert runtime.store.snapshot().status.context_usage.current_tokens == 55_000


def test_compact_preflight_cannot_write_through_a_new_generation(tmp_path):
    from agent_py_agent.agent.conversation.context_usage import save_context_usage_snapshot
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit

    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice"})
    store.update_compact_state(thread.thread_id, expected_generation=0, commit=ConversationCompactCommit(
        summary="保留已有任务", operation_evidence={}, checkpoint_id="new-generation",
        compacted_through_message_id="", compacted_through_byte_offset=0,
        source_messages=0, source_tool_pairs=0,
    ))
    assert save_context_usage_snapshot(store, thread, usage()) is False
    assert store.load_thread(thread.thread_id).model_context_usage == {}


def test_model_context_display_whitelist_and_corrupt_generation(tmp_path):
    from agent_py_agent.agent.conversation.context_usage import context_usage_from_thread
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "alice"})
    data = {**usage(), "prompt": "private", "api_key": "private", "current_tokens": True}
    saved = store.update_model_context_usage(thread.thread_id, data, expected_compact_generation=0)
    assert saved.model_context_usage["current_tokens"] == 0
    assert "private" not in str(saved.model_context_usage)
    assert context_usage_from_thread(replace(saved, model_context_usage={**usage(), "compact_generation": False})) == {}


def test_context_projection_load_failure_keeps_last_tui_value():
    from agent_py_agent.cli.chat_parts.tui_threading import _publish_background_activity
    runtime = TuiRuntime("read-failure")
    _publish_background_activity(runtime, {"context_usage": usage(), "active_task_count": 0})
    _publish_background_activity(runtime, {"context_usage": {}, "context_usage_projection_ok": False, "active_task_count": 0})
    assert runtime.store.snapshot().status.context_usage.current_tokens == 42_100


def test_context_measurement_cannot_write_another_owner_store(tmp_path):
    from agent_py_agent.agent.conversation.context_usage import record_model_context_usage
    alice = ConversationStore(tmp_path / "alice")
    bob = ConversationStore(tmp_path / "bob")
    thread = alice.get_or_create_thread({"canonical_user_id": "alice"})
    agent = SimpleNamespace(conversation_store=bob)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    assert record_model_context_usage(agent, params, usage()) is False
    assert alice.load_thread(thread.thread_id).model_context_usage == {}


def test_unknown_child_context_is_not_displayed_as_zero():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        _render_subagent_activity_row,
    )
    lines = _render_subagent_activity_row({"name": "child", "status": "DONE", "context_known": False}, TuiRenderContext(width=120))
    text = "".join(fragment[1] for line in lines for fragment in line)
    assert "ctx —" in text and "ctx 0" not in text
