"""逐块显示持久化的定位测试；不调用模型，不代替真实 TUI/Gateway 重启验收。"""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_transcript import append_agent_transcript_event
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_metadata
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import _publish_background_notice_row


# LLM: 真实owner ConversationStore和主/child sink；身份是结构化夹具，不启动模型或工具。
# 函数用途: 在临时目录创建公开流与原用户消息，支持进程丢失前后的显示/输入对照。
def _session(tmp_path, surface="main"):
    store = ConversationStore(tmp_path / "conversations")
    child = surface == "child"
    thread = (store.ensure_agent_thread({"thread_id": "thread-run-a", "agent_run_id": "run-a", "canonical_user_id": "owner-a"})
              if child else store.get_or_create_thread({"canonical_user_id": "owner-a"}))
    request_id = "bg-agent:run-a:attempt-1" if child else f"bg-main:{thread.thread_id}:piece-1"
    metadata = ({"conversation_request_id": "attempt-1", "agent_attempt_id": "attempt-1", "agent_run_id": "run-a"}
                if child else {"gateway_request_id": "gateway-a", "conversation_request_id": "request-a"})
    if surface == "background":
        metadata = {"background_transcript_request_id": request_id, "background_delivery_reason": "test", "task_id": "run-a"}
    user = store.append_message({"thread_id": thread.thread_id, "role": "user", "content": "检查现有项目", "metadata": metadata, "now": 10})
    agent = SimpleNamespace(conversation_store=store)
    sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="run-a", request_id=request_id,
                                    gateway_request_id="gateway-a" if surface == "main" else "",
                                    event_writer=append_agent_transcript_event if child else None)
    return store, thread, user, agent, sink


# LLM: 所有公开数据经过真实sink；不写业务文件，只有工具显示状态夹具。
# 函数用途: 生成一块思考、过程回复和有终态工具，最后留下没有终态的工具用于崩溃恢复。
def _work(sink):
    sink.write_thinking_delta("先查看")
    sink.write_thinking("先查看项目", duration_seconds=3)
    sink.write_model("我来读文件。")
    sink.write_progress({"tool": "read_file", "round": 1, "call_index": 0, "phase": "started", "detail": "main.py"})
    sink.write_progress({"tool": "read_file", "round": 1, "call_index": 0, "phase": "completed", "ok": True, "output": "公开工具结果"})
    sink.write_progress({"tool": "run_command", "round": 2, "call_index": 0, "phase": "started", "detail": "验证命令"})


@pytest.mark.parametrize("surface", ["main", "background", "child"])
def test_completed_blocks_survive_before_final_without_model_input_or_activity_change(tmp_path, surface):
    store, thread, user, _, sink = _session(tmp_path, surface)
    before = store.context_bundle(thread.thread_id)
    _work(sink)
    reopened = ConversationStore(store.root, initialize=False)
    assert reopened.context_bundle(thread.thread_id) == before
    assert reopened.recent_messages(thread.thread_id, limit=1) == [user]
    page = reopened.history_page_report(thread.thread_id, limit=1)
    assert not page.errors and page.before == 0
    events = conversation_history_display_events(page.rows)
    runtime = TuiRuntime("reopened")
    runtime.publish_recovered_history([], display_events=events)
    snapshot = runtime.store.snapshot()
    assert [(b.role, b.text) for b in snapshot.stable_blocks] == [
        ("user", "检查现有项目"), ("thinking", "先查看项目"), ("assistant", "我来读文件。"),
        ("tool", ""), ("system", "run_command：本工作片没有保存完整终态。"),
    ]
    assert "公开工具结果" in snapshot.stable_blocks[3].detail
    assert not snapshot.has_active_work and not snapshot.queued_inputs
    assert provider_history_messages_from_rows(page.rows) == provider_history_messages_from_rows([user])
    runtime.publish_recovered_history([], display_events=events)
    assert runtime.store.snapshot().stable_blocks == snapshot.stable_blocks


def test_many_display_rows_do_not_crowd_recent_compact_or_memory_batches(tmp_path):
    store, thread, user, _, sink = _session(tmp_path)
    for index in range(64):
        sink.write_thinking(f"公开过程 {index}")
    answer = store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "已检查", "metadata": {"assistant_part_id": "final"}})
    for index in range(64):
        sink.write_thinking(f"后续过程 {index}")
    assert store.recent_messages(thread.thread_id, limit=2) == [user, answer]
    assert store.messages_after_report(thread.thread_id, after_message_id=user.message_id, limit=1) == ([answer], [])
    offset = store.message_byte_offset_after(thread.thread_id, user.message_id)
    assert store.messages_after_compact_report(replace(thread, compacted_through_byte_offset=offset)) == ([answer], [])
    assert store.messages_after_compact_report(thread) == ([user, answer], [])
    all_rows = store.history_page_report(thread.thread_id, limit=800).rows
    forged = copy.deepcopy(next(row for row in all_rows if row.role == "display"))
    forged.metadata["canonical_native_messages"] = canonical_native_messages_envelope([{"role": "assistant", "content": "不能进入模型"}])
    assert provider_history_messages_from_rows([user, forged, answer]) == provider_history_messages_from_rows([user, answer])


def test_final_snapshot_replaces_same_piece_but_not_earlier_uncommitted_piece(tmp_path):
    store, thread, user, agent, old = _session(tmp_path)
    old.write_thinking("崩溃前已完成的一块")
    current = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="run-a", gateway_request_id="gateway-a")
    current.write_thinking("恢复后的一块")
    current.finish()
    snapshot = current.display_history_snapshot()
    snapshot["gateway_request_id"] = "gateway-a"
    meta = {**user.metadata, "assistant_part_id": "final", "background_display_turn": snapshot,
            "background_transcript_request_id": current.request_id}
    final = store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "最终回复", "metadata": meta})
    page = store.history_page_report(thread.thread_id, limit=1)
    assert page.before == 0
    events = conversation_history_display_events(page.rows)
    assert [event["payload"].get("text") for event in events] == ["检查现有项目", "崩溃前已完成的一块", "恢复后的一块", "最终回复"]
    assert events[-1]["covered_background_request_id"] == current.request_id
    assert events[-1]["block_id"] == f"history:{thread.thread_id}:{final.message_id}:assistant"


def test_child_final_native_does_not_duplicate_public_checkpoint_or_commentary(tmp_path):
    store, thread, user, _, sink = _session(tmp_path, "child")
    _work(sink)
    store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "我来读文件。",
                          "metadata": {**user.metadata, "assistant_part_id": "commentary:1"}})
    store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "最终回复", "metadata": {
        **user.metadata, "assistant_part_id": "final", "canonical_native_messages": canonical_native_messages_envelope([
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "先查看项目"}, {"type": "text", "text": "我来读文件。"}]},
        ]),
    }})
    events = conversation_history_display_events(store.history_page_report(thread.thread_id).rows)
    texts = [event["payload"].get("text") for event in events]
    assert texts.count("先查看项目") == texts.count("我来读文件。") == texts.count("最终回复") == 1


@pytest.mark.parametrize("kind,phase", [("thinking_delta", "delta"), ("thinking_started", "started"), ("permission_requested", "started"), ("agent_cancelled", "completed"), ("assistant_completed", "completed")])
def test_deltas_permissions_and_uncommitted_final_are_not_saved(tmp_path, kind, phase):
    store, thread, user, _, sink = _session(tmp_path)
    sink._event(kind, phase, f"{sink.request_id}:private", {"text": "不要保存", "process": False})
    assert store.history_page_report(thread.thread_id).rows == (user,)


@pytest.mark.parametrize("field,value", [("thread_id", "another"), ("request_id", "bg-main:other:one"), ("block_id", "other:block"), ("kind", "permission_requested")])
def test_invalid_checkpoint_is_not_authority(tmp_path, field, value):
    store, thread, user, _, sink = _session(tmp_path)
    values = dict(thread_id=thread.thread_id, task_id="run-a", request_id=sink.request_id,
                  gateway_request_id="gateway-a", kind="thinking_completed", phase="completed",
                  block_id=f"{sink.request_id}:thinking:1", payload={"text": "公开思考"})
    metadata = display_checkpoint_metadata(**values)
    metadata["display_checkpoint"][field] = value
    with pytest.raises(ValueError):
        store.append_display_checkpoint(thread.thread_id, metadata)
    assert store.history_page_report(thread.thread_id).rows == (user,)


def test_storage_failure_warns_without_stopping_live_output(tmp_path, monkeypatch, caplog):
    store, thread, user, agent, _ = _session(tmp_path)

    def unavailable(*_args):
        raise OSError("PRIVATE_PATH_AND_KEY")

    monkeypatch.setattr(store, "append_display_checkpoint", unavailable)
    sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="run-a")
    sink.write_thinking("仍可显示")
    sink.write_thinking("继续显示")
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    assert sum(item["kind"] == "thinking_completed" for item in events) == 2
    assert sum(item["block_id"].endswith("history-write-failed") for item in events) == 1
    assert "PRIVATE_PATH_AND_KEY" not in json.dumps(events) + caplog.text
    assert store.history_page_report(thread.thread_id).rows == (user,)


def test_live_checkpoint_notices_require_capability_and_do_not_publish_start_placeholders(tmp_path):
    store, thread, _, _, sink = _session(tmp_path)
    _work(sink)
    old, cursor, ok = read_background_response_page(store, thread.thread_id, include_foreground=True)
    assert ok and len(old) == 1 and old[0]["display_kind"] == "user_message"
    new, new_cursor, ok = read_background_response_page(store, thread.thread_id, include_foreground=True, include_display_checkpoints=True)
    assert ok and new_cursor == cursor
    process = [item for item in new if item["display_kind"] == "process_event"]
    assert len(process) == 3 and all(item["content"] == "" for item in process)
    runtime = TuiRuntime("observer")
    for notice in new:
        assert _publish_background_notice_row(runtime, notice, set())
    stable = runtime.store.snapshot()
    assert [block.role for block in stable.stable_blocks] == ["user", "thinking", "assistant", "tool"]
    assert not stable.active_blocks
    assert not runtime._recovered_background_turns
    for notice in new:
        assert _publish_background_notice_row(runtime, notice, set())
    assert runtime.store.snapshot().stable_blocks == stable.stable_blocks


def test_completed_checkpoint_and_volatile_deltas_converge_but_new_blocks_stay_live(tmp_path):
    store, thread, _, agent, sink = _session(tmp_path)
    _work(sink)
    notices, _, _ = read_background_response_page(store, thread.thread_id, include_foreground=True, include_display_checkpoints=True)
    runtime = TuiRuntime("observer")
    for notice in notices:
        _publish_background_notice_row(runtime, notice, set())
    stable = runtime.store.snapshot().stable_blocks
    sink.write_thinking_delta("本片后续仍在思考")
    stream = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    runtime.publish_background_transcript_events(stream)
    after = runtime.store.snapshot()
    assert after.stable_blocks == stable
    assert {block.role for block in after.active_blocks} == {"thinking", "tool"}
    assert any(block.text == "本片后续仍在思考" for block in after.active_blocks)


def test_recovered_unknown_placeholder_upgrades_only_when_exact_tool_terminal_arrives(tmp_path):
    store, thread, _, _, sink = _session(tmp_path)
    _work(sink)
    page = store.history_page_report(thread.thread_id)
    runtime = TuiRuntime("observer")
    runtime.publish_recovered_history([], display_events=conversation_history_display_events(page.rows))
    old = runtime.store.snapshot().stable_blocks
    sink.write_progress({"tool": "run_command", "round": 2, "call_index": 0, "phase": "completed", "ok": True, "output": "迟到的完整结果"})
    notices, _, _ = read_background_response_page(store, thread.thread_id, after=page.after, include_foreground=True, include_display_checkpoints=True)
    for notice in notices:
        _publish_background_notice_row(runtime, notice, set())
    after = runtime.store.snapshot()
    assert len(after.stable_blocks) == len(old)
    assert after.stable_blocks[-1].role == "tool"
    assert after.stable_blocks[-1].block_id == old[-1].block_id
    assert after.stable_blocks[-1].created_seq == old[-1].created_seq
    assert "迟到的完整结果" in after.stable_blocks[-1].detail
    runtime.publish_recovered_history([], display_events=conversation_history_display_events(page.rows))
    assert runtime.store.snapshot().stable_blocks == after.stable_blocks


@pytest.mark.parametrize("surface,field", [("main", "gateway_request_id"), ("main", "background_transcript_request_id"), ("child", "agent_run_id"), ("child", "agent_attempt_id")])
def test_mismatched_outer_checkpoint_binding_cannot_enter_another_history_group(tmp_path, surface, field):
    store, thread, _, _, sink = _session(tmp_path, surface)
    sink.write_thinking("公开记录")
    record = copy.deepcopy(store.history_page_report(thread.thread_id).rows[-1])
    record.metadata[field] = "unrelated"
    assert conversation_history_display_events([record]) == ()


def test_display_capabilities_require_explicit_boolean_true():
    from agent_py_agent.agent.conversation.message_stream import NoticeDisplayCapabilities

    assert NoticeDisplayCapabilities.from_payload({"foreground_messages": "true", "display_checkpoints": 1}) == NoticeDisplayCapabilities()
    assert NoticeDisplayCapabilities.from_payload({"display_checkpoints": True}) == NoticeDisplayCapabilities(display_checkpoints=True)
