from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_history import background_display_turn_from_row
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import _publish_background_notice_row


# LLM: 使用临时 canonical store 和真实展示 sink，不调用模型、不执行业务任务。
# 函数用途: 建立一轮可回放的后台思考、commentary、工具和 final，验证两个显示入口共享身份。
def _completed_turn(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({"canonical_user_id": "owner-a"})
    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-a")
    sink.write_thinking("先核对文件", duration_seconds=2)
    sink.write_model("开始读取")
    sink._flush_model_commentary()
    sink.write_progress({"round": 1, "call_index": 1, "tool": "read_file", "phase": "completed", "ok": True, "output": "文件正文"})
    sink.finish()
    metadata = {"background_delivery_reason": "root_subagents_terminal", "task_id": "task-a",
                "background_transcript_request_id": sink.request_id}
    store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "开始读取",
                          "metadata": {**metadata, "assistant_part_id": "commentary:1"}})
    final = store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "这是最终检查结果",
                                  "metadata": {**metadata, "assistant_part_id": "final", "background_display_turn": sink.display_history_snapshot()}})
    return agent, sink, store, thread, final


@pytest.mark.parametrize("live_prefix", ["none", "all", "tail"])
def test_canonical_final_repairs_missed_process_without_duplicates(tmp_path, live_prefix):
    agent, sink, store, thread, final = _completed_turn(tmp_path)
    runtime = TuiRuntime("client")
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    if live_prefix != "none":
        runtime.publish_background_transcript_events(events if live_prefix == "all" else events[-1:])
    notices, _, ok = read_background_response_page(store, thread.thread_id)
    assert ok
    assert _publish_background_notice_row(runtime, notices[0], set())
    stable = runtime.store.snapshot().stable_blocks
    assert [(block.role, block.text) for block in stable] == [
        ("thinking", "先核对文件"), ("assistant", "开始读取"), ("tool", ""), ("assistant", final.content),
    ]
    runtime.publish_background_transcript_events(events)
    assert runtime.store.snapshot().stable_blocks == stable
    assert _publish_background_notice_row(runtime, notices[0], set())
    assert runtime.store.snapshot().stable_blocks == stable
    assert sink.request_id in runtime._recovered_background_turns


def test_snapshot_outlives_transport_ring_and_process_identity(tmp_path):
    from agent_py_agent.agent.conversation.background_transcript import (
        BACKGROUND_TRANSCRIPT_MAX_EVENTS,
    )

    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(agent, thread_id="thread-a", task_id="task-a")
    for index in range(BACKGROUND_TRANSCRIPT_MAX_EVENTS + 5):
        sink.write_thinking(f"第 {index} 次检查")
    sink.finish()
    assert read_background_transcript_events(agent, thread_id="thread-a", after=0)["truncated"]
    assert len(sink.display_history_snapshot()["events"]) == BACKGROUND_TRANSCRIPT_MAX_EVENTS + 5
    restarted = BackgroundTranscriptSink(SimpleNamespace(), thread_id="thread-a", task_id="task-a")
    assert restarted.request_id != sink.request_id


@pytest.mark.parametrize("damage", ["incomplete", "thread", "task", "request", "block", "control", "duplicate"])
def test_invalid_snapshot_does_not_authorize_buffer_drop(tmp_path, damage):
    agent, sink, store, thread, final = _completed_turn(tmp_path)
    snapshot = final.metadata["background_display_turn"]
    if damage == "incomplete":
        snapshot["complete"] = False
    elif damage in {"thread", "task", "request"}:
        snapshot[f"{damage}_id"] = "different"
    elif damage == "block":
        snapshot["events"][0]["block_id"] = "other:thinking"
    elif damage == "control":
        snapshot["events"][0]["kind"] = "permission_requested"
    else:
        snapshot["events"].append(copy.deepcopy(snapshot["events"][0]))
    assert background_display_turn_from_row(final) is None
    runtime = TuiRuntime("client")
    runtime.publish_recovered_history([], display_events=conversation_history_display_events([final]))
    assert sink.request_id not in runtime._recovered_background_turns
    runtime.publish_background_transcript_events(read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"])
    assert any(block.role == "tool" for block in runtime.store.snapshot().stable_blocks)


def test_display_metadata_does_not_change_provider_history_or_snapshot_source(tmp_path):
    _, sink, store, thread, _ = _completed_turn(tmp_path)
    rows = store.recent_messages(thread.thread_id, limit=0)
    plain = copy.deepcopy(rows)
    for row in plain:
        row.metadata.pop("background_display_turn", None)
        row.metadata.pop("background_transcript_request_id", None)
    assert provider_history_messages_from_rows(rows) == provider_history_messages_from_rows(plain)
    snapshot = sink.display_history_snapshot()
    snapshot["events"].clear()
    assert len(sink.display_history_snapshot()["events"]) == 3


def test_no_terminal_does_not_claim_work_succeeded_and_transient_input_is_not_history():
    sink = BackgroundTranscriptSink(SimpleNamespace(), thread_id="thread-a", task_id="task-a")
    sink._event("tool_input_started", "started", f"{sink.request_id}:tool-input:1", {"tool": "read_file"})
    sink.write_progress({"round": 1, "call_index": 1, "tool": "read_file", "phase": "started"})
    assert sink.display_history_snapshot()["complete"] is False
    sink.finish()
    events = sink.display_history_snapshot()["events"]
    assert len(events) == 1 and events[0]["kind"] == "system_message"
    assert "没有保存完整终态" in events[0]["payload"]["text"]


def test_compact_retry_keeps_tools_with_repeated_round_indices():
    sink = BackgroundTranscriptSink(SimpleNamespace(), thread_id="thread-a", task_id="task-a")
    for attempt in (1, 2):
        sink.begin_model_attempt(attempt)
        sink.write_progress({"round": 1, "call_index": 1, "tool": "read_file", "phase": "completed", "ok": True, "output": f"文件 {attempt}"})
    sink.finish()
    events = sink.display_history_snapshot()["events"]
    assert len(events) == 2 and events[0]["block_id"] != events[1]["block_id"]
    assert [event["payload"]["output"] for event in events] == ["文件 1", "文件 2"]


def test_background_invocation_snapshot_commits_only_with_canonical_final(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import runtime as runtime_module
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, GoalRuntimeContext

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({"canonical_user_id": "owner-a"})
    runtime = SimpleNamespace(agent=SimpleNamespace(), store=store)
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-a", reason="scheduled_progress_report")

    def invoke(*_args, activity_sink, **_kwargs):
        activity_sink.write_thinking_delta("后台思考")
        activity_sink.write_model("先看已有文件")
        activity_sink.write_progress({"round": 1, "call_index": 1, "tool": "read_file", "phase": "completed", "ok": True, "output": "读取结果"})
        assert not activity_sink.display_history_snapshot()["complete"]
        return SimpleNamespace(response="最终结果")

    monkeypatch.setattr(runtime_module, "_run_background_main_turn_with_compact", invoke)
    result, snapshot = runtime_module._invoke_background_main_agent(runtime, thread, request, GoalRuntimeContext(), (False, True))
    assert snapshot["complete"] is True
    assert not store.recent_messages(thread.thread_id)
    context = DeliveryContext(channel="internal", target="", thread_id=thread.thread_id)
    kwargs = {"receipt": SimpleNamespace(), "committed_content": result.response, "evidence_refs": (),
              "message_metadata": {"task_id": "task-a", "background_delivery_reason": "scheduled_progress_report"},
              "display_snapshot": snapshot}
    runtime_module._commit_background_response(runtime, request, context, delivery_status="failed", transcript_record=False, **kwargs)
    assert not store.recent_messages(thread.thread_id)
    runtime_module._commit_background_response(runtime, request, context, delivery_status="not_applicable", transcript_record=True, **kwargs)
    final = store.recent_messages(thread.thread_id)[0]
    assert background_display_turn_from_row(final) == snapshot
