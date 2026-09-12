from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolProgressEvent,
    _structured_tool_progress,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_events
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.tests.test_tool_round_execution import _canonical_calls, _round_request


# LLM: 真实canonical store+ToolResult生成展示事件，不调用LLM、不执行任务内容。
# 函数用途: 建立可验证源快照与页式归档的工具结果夹具。
def _event(tmp_path, display=None, output="done", *, child=False):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({"canonical_user_id": "owner"})
    child_thread = store.ensure_agent_thread({"thread_id": "child", "canonical_user_id": "owner",
        "agent_run_id": "child-run", "parent_agent_thread_id": thread.thread_id,
        "root_agent_thread_id": thread.thread_id}) if child else thread
    agent = SimpleNamespace(conversation_store=store)
    attrs = {"conversation_thread_id": thread.thread_id, "agent_thread_id": child_thread.thread_id}
    call = _canonical_calls([{"tool": "write_file", "path": "file.py"}])[0]
    request = _round_request(agent=agent, params=SimpleNamespace(tool_context=[], task_attributes=attrs),
        tool_rounds=1, response=ModelResponse(text="", backend="test"),
        calls=[{"tool": "write_file", "path": "file.py"}], execute_one=lambda _: None, record_one=lambda _: None)
    result = ToolResult.succeeded(call, output, facts=ToolSuccessFacts(
        metadata={"handler_details": {"display": display}} if display else {}))
    return ToolProgressEvent(request, 0, call, "finished", "完成", result=result), child_thread


def test_tool_original_is_complete_immutable_public_and_events_remain_bounded(tmp_path):
    source = [f"    line-{index}: " + "界" * 600 for index in range(400)]
    display = {"kind": "write", "path": "file.py", "lines": list(source), "total_lines": 400}
    event, thread = _event(tmp_path, display, child=True)
    payload = _structured_tool_progress(event, "write_file", "file.py")
    assert len(payload["display"]["lines"]) == 180
    assert payload["display"]["hidden_lines"] == 220
    ref = payload["display_archive_ref"]
    assert ref["thread_id"] == thread.thread_id == "child"
    display["lines"][:] = ["new file content must not replace old snapshot"]
    rows = []
    for index in range(ref["page_count"]):
        rows.extend(read_display_archive_page(event.request.agent.conversation_store, ref, index)["rows"])
    text = "\n".join(row["text"] for row in rows)
    assert all(f"{index + 1} {line}" in text for index, line in enumerate(source))
    assert "new file content" not in text
    assert event.result.output == "done"
    sink = BackgroundTranscriptSink(event.request.agent, thread_id=thread.thread_id, task_id="task")
    sink.write_progress(payload)
    history, _, _ = event.request.agent.conversation_store.message_page_after_offset_report(thread.thread_id, after=0)
    recovered = display_checkpoint_events(history)
    assert any(row["payload"].get("display_archive_ref") == ref for row in recovered)


def test_display_archive_failure_does_not_change_tool_success_or_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import display_archive
    event, _ = _event(tmp_path, output="result")
    def fail(*args, **kwargs):
        raise OSError("private path must not leak")
    monkeypatch.setattr(display_archive, "archive_display_rows", fail)
    payload = _structured_tool_progress(event, "write_file", "file.py")
    assert event.result.ok and payload["ok"]
    assert payload["output"] == "result"
    assert payload["display_archive_ref"]["schema"] == "display_archive_error.v1"
    assert "private path" not in str(payload)


def test_plain_original_preserves_indentation_and_long_lines(tmp_path):
    text = "  first\n\n" + "中" * 5000 + "\n   end  "
    event, _ = _event(tmp_path, output=text)
    payload = _structured_tool_progress(event, "read_file", "file.py")
    ref = payload["display_archive_ref"]
    rows = [row for index in range(ref["page_count"])
            for row in read_display_archive_page(event.request.agent.conversation_store, ref, index)["rows"]]
    restored = ["" for _ in range(ref["row_count"])]
    for row in rows:
        restored[row["row_index"]] += row["text"]
    assert "\n".join(restored) == text


def test_long_thinking_checkpoint_has_complete_original_archive(tmp_path):
    event, thread = _event(tmp_path)
    sink = BackgroundTranscriptSink(event.request.agent, thread_id=thread.thread_id, task_id="task")
    text = "\n".join(f"想法{index}" for index in range(5000))
    sink.write_thinking(text)
    history, _, _ = event.request.agent.conversation_store.message_page_after_offset_report(thread.thread_id, after=0)
    payload = next(row["payload"] for row in display_checkpoint_events(history) if row["kind"] == "thinking_completed")
    assert len(payload["text"]) < len(text)
    ref = payload["display_archive_ref"]
    restored = [row["text"] for index in range(ref["page_count"])
                for row in read_display_archive_page(event.request.agent.conversation_store, ref, index)["rows"]]
    assert "\n".join(restored) == text


def test_late_full_thinking_attaches_to_closed_block_after_tool_boundary(tmp_path):
    from agent_py_agent.agent.conversation.background_transcript import (
        read_background_transcript_events,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    event, thread = _event(tmp_path)
    sink = BackgroundTranscriptSink(event.request.agent, thread_id=thread.thread_id, task_id="task")
    text = "完整思考" * 5000
    sink.write_thinking_delta(text)
    old_id = sink._thinking_block_id
    sink.write_progress({"phase": "started", "tool": "read_file", "round": 1, "call_index": 1})
    assert not sink.write_thinking(text)
    rows = read_background_transcript_events(event.request.agent, thread_id=thread.thread_id, after=0)["events"]
    ref_row = next(row for row in reversed(rows) if row["payload"].get("display_archive_ref"))
    assert ref_row["block_id"] == old_id
    runtime = TuiRuntime("reader")
    runtime.publish_background_transcript_events(rows)
    thinking = [block for block in runtime.store.snapshot().stable_blocks if block.role == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].metadata["display_archive_ref"] == ref_row["payload"]["display_archive_ref"]
    assert thinking[0].metadata["history_incomplete"] is False
