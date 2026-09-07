from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.cli.chat_parts.history import GatewayChatHistorySnapshot
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_history import TuiHistoryPager
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


# LLM: helper 只构造公开静态显示事件，使用持久消息形状的稳定 ID，无模型或控制动作。
# 函数用途: 为分页显示和重复响应测试生成一组多行消息。
def _events(label, count=5):
    return tuple({
        "schema": "conversation_history_display.v1", "kind": "assistant_completed", "phase": "completed",
        "request_id": f"history:{label}-{index}", "block_id": f"history:{label}-{index}:assistant",
        "payload": {"text": f"{label}-{index}\n中文正文第二行\n第三行"},
    } for index in range(count))


# LLM: 使用真实 runtime/provider/reducer，fake app 只负责 UI 调度，不替代历史或权限实现。
# 函数用途: 构造正在浏览最近记录、尚有旧页的客户端。
def _pager():
    runtime = TuiRuntime("session")
    runtime.publish_session(version="test", model="test", workspace="test")
    runtime.publish_recovered_history([], display_events=_events("recent"), message_cursor=1000, before_message_cursor=500)
    view = make_tui_transcript_view(runtime.store, lambda width: TuiRenderContext(width=width))
    view.control.create_content(60, 5)
    view.control.jump_to(10)
    app = SimpleNamespace(invalidate=lambda: None, loop=SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)))
    params = SimpleNamespace(tui_runtime=runtime, stop_event=threading.Event(), current_session_id="session",
                             agent=SimpleNamespace(config=SimpleNamespace()))
    pager = TuiHistoryPager(app, params, view)
    return pager, runtime, view


def test_older_page_renders_before_recent_and_keeps_anchor_and_live_cursor():
    pager, runtime, view = _pager()
    anchor = view.control.history_anchor()
    phase = runtime.store.snapshot().status.phase
    page = GatewayChatHistorySnapshot(display_events=_events("older"), before_message_cursor=200)
    pager._apply(500, False, page)
    assert runtime.history_before_cursor == 200 and runtime.background_message_cursor == 1000
    assert view.control.history_anchor() == anchor
    text = "\n".join(map(fragments_text, view.provider.frame(60).transcript_lines))
    assert text.index("older-0") < text.index("recent-0")
    assert runtime.store.snapshot().status.phase == phase
    before = runtime.store.snapshot().stable_blocks
    pager._apply(500, False, page)
    assert runtime.store.snapshot().stable_blocks == before


def test_child_switch_and_read_error_preserve_root_cursor_and_blocks():
    pager, runtime, view = _pager()
    previous = runtime.store.snapshot().stable_blocks
    page = GatewayChatHistorySnapshot(display_events=_events("older"), before_message_cursor=0)
    view.set_state_store(TuiStateStore())
    pager._apply(500, False, page)
    assert runtime.history_before_cursor == 500 and runtime.store.snapshot().stable_blocks == previous
    view.set_state_store(runtime.store)
    pager._apply(500, False, GatewayChatHistorySnapshot(load_errors=({"error_code": "failed"},)))
    assert runtime.history_before_cursor == 500 and runtime.store.snapshot().stable_blocks == previous


def test_frozen_transcript_gets_old_page_but_not_new_live_response():
    pager, runtime, view = _pager()
    view.transcript_state.enter(runtime.store.snapshot())
    runtime._publish("assistant_completed", "completed", "new-live", {"text": "new-live-response"})
    page = GatewayChatHistorySnapshot(display_events=_events("older"), before_message_cursor=0)
    pager._apply(500, False, page)
    text = "\n".join(map(fragments_text, view.provider.frame(60).transcript_lines))
    assert "older-0" in text and "recent-0" in text and "new-live-response" not in text
    assert view.transcript_state.snapshot().active


def test_background_reorder_survives_actual_renderer_sorting():
    pager, runtime, view = _pager()
    rid, final = "bg-main:root:attempt", "history:thread:final:assistant"
    runtime._publish("tool_completed", "completed", rid + ":tool", {"tool": "first"}, request_id=rid)
    runtime._publish("assistant_completed", "completed", final, {"text": "FINAL"}, request_id="history:thread:final")
    runtime._publish("thinking_completed", "completed", rid + ":think", {"text": "THINK"}, request_id=rid)
    runtime._publish("history_blocks_reordered", "completed", rid + ":order", {
        "block_ids": [rid + ":tool", rid + ":think", final], "final_block_id": final,
    }, request_id=rid)
    offsets = dict(view.provider.frame(60).block_line_offsets)
    assert offsets[rid + ":think"] < offsets[final]


def test_page_reader_does_not_spawn_twice_or_run_for_child(monkeypatch):
    pager, runtime, view = _pager()
    starts = []
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_history.threading.Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: starts.append(kwargs)))
    pager.request()
    pager.request()
    assert len(starts) == 1
    pager._loading = False
    view.set_state_store(TuiStateStore())
    pager.request()
    assert len(starts) == 1


def test_older_page_is_not_counted_as_new_message():
    pager, runtime, view = _pager()
    view.control.move(-1)
    view.control.create_content(60, 5)
    before = view.control._unseen_block_ids
    pager._apply(500, False, GatewayChatHistorySnapshot(display_events=_events("older"), before_message_cursor=0))
    view.control.create_content(60, 5)
    assert view.control._unseen_block_ids == before


def test_late_home_response_does_not_override_subsequent_scroll():
    pager, runtime, view = _pager()
    view.control.move_home()
    view.control.move(8)
    anchor = view.control.history_anchor()
    pager._apply(500, True, GatewayChatHistorySnapshot(display_events=_events("older"), before_message_cursor=0))
    assert view.control.history_anchor() == anchor
