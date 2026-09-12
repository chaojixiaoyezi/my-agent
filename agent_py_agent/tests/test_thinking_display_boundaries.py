from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.gateway_parts.foreground_transcript import GatewayForegroundTranscriptSink
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def test_local_tool_start_closes_thinking_without_assistant_or_full_terminal():
    runtime = TuiRuntime("boundary")
    turn = runtime.begin_turn("request")
    turn.write_thinking_delta("第一轮")
    turn.write_progress({"phase": "started", "round": 1, "call_index": 0, "tool": "read_file"})
    turn.write_thinking_delta("第二轮")
    snapshot = runtime.store.snapshot()
    assert [(b.role, b.text) for b in snapshot.stable_blocks] == [("thinking", "第一轮")]
    assert [(b.role, b.text) for b in snapshot.active_blocks] == [("tool", ""), ("thinking", "第二轮")]


@pytest.mark.parametrize("foreground", [False, True])
def test_shared_sink_tool_boundary_closes_old_thinking_and_keeps_order(foreground):
    agent = SimpleNamespace()
    sink = (GatewayForegroundTranscriptSink(agent, thread_id="thread", request_id="request", request={})
            if foreground else BackgroundTranscriptSink(agent, thread_id="thread", task_id="task"))
    sink.write_thinking_delta("第一轮")
    sink.write_progress({"phase": "started", "round": 1, "call_index": 0, "tool": "read_file"})
    sink.write_thinking_delta("第二轮")
    sink.finish()
    events = read_background_transcript_events(agent, thread_id="thread", after=0)["events"]
    visible = [(e["kind"], e["payload"].get("text")) for e in events
               if e["kind"] in {"thinking_completed", "tool_started"}]
    assert visible == [("thinking_completed", "第一轮"), ("tool_started", None), ("thinking_completed", "第二轮")]


def test_model_attempt_separates_thinking_even_without_tool_or_assistant():
    sink = BackgroundTranscriptSink(SimpleNamespace(), thread_id="thread", task_id="task")
    sink.write_thinking_delta("旧调用")
    sink.begin_model_attempt(2)
    sink.write_thinking_delta("新调用")
    assert sink._thinking_text == "新调用"


def test_thinking_preview_bounds_text_before_markdown_without_mutating_source(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_block_renderer as renderer
    runtime = TuiRuntime("performance")
    turn = runtime.begin_turn("request")
    text = "一" * 100_000
    turn.write_thinking_delta(text)
    calls = []
    original = renderer._thinking_content_lines
    def record(detail, context, *, closed):
        calls.append(len(detail))
        return original(detail, context, closed=closed)
    monkeypatch.setattr(renderer, "_thinking_content_lines", record)
    renderer.render_tui_snapshot(runtime.store.snapshot(), renderer.TuiRenderContext(width=80))
    assert max(calls) <= 12_000
    assert next(b.text for b in runtime.store.snapshot().active_blocks if b.role == "thinking") == text


def test_cli_keeps_original_reference_and_late_ref_does_not_add_another_block():
    runtime = TuiRuntime("late-archive")
    turn = runtime.begin_turn("request")
    reference = {"schema": "display_archive_ref.v1", "archive_id": "a" * 32, "thread_id": "thread", "page_count": 2}
    turn.write_thinking_delta("先前预览")
    turn.write_model("正在处理")
    assert not turn.on_gateway_event({"kind": "assistant_thinking", "text": "完整预览", "display_archive_ref": reference})
    thinking = [b for b in runtime.store.snapshot().stable_blocks if b.role == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].metadata["display_archive_ref"] == reference
    turn.write_thinking_delta("新一轮")
    assert next(b.text for b in runtime.store.snapshot().active_blocks if b.role == "thinking") == "新一轮"


def test_normal_cli_thinking_preserves_ref_and_history_gap_marker():
    runtime = TuiRuntime("reference")
    turn = runtime.begin_turn("request")
    turn.on_gateway_event({"kind": "assistant_thinking", "text": "旧内容", "history_incomplete": True})
    thinking = next(b for b in runtime.store.snapshot().stable_blocks if b.role == "thinking")
    assert thinking.metadata["history_incomplete"] is True
