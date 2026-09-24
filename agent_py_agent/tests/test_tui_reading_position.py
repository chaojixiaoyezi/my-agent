from __future__ import annotations

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def _view(*, count=40, rows=4):
    store, state = TuiStateStore(), TuiTranscriptModeState()
    seq = TuiEventSequencer("reading", clock=lambda: 1.0)
    for index in range(count):
        store.publish(seq.emit("assistant_completed", "completed", f"block-{index}", {
            "text": "\n".join(f"message-{index}-row-{row}" for row in range(rows)),
        }))
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(
        width=width, detailed_transcript=state.snapshot().active,
        show_all=state.snapshot().show_all,
    ), transcript_state=state)
    view.control.create_content(80, 12)
    return store, state, view


def test_each_down_key_and_wheel_step_moves_viewport_immediately_from_top():
    _, _, view = _view()
    control = view.control
    control.jump_to(0)
    control.move(1)
    control.create_content(80, 12)
    assert control.preferred_vertical_scroll(view.window) == 1
    control.mouse_handler(MouseEvent(Point(1, 1), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()))
    control.create_content(80, 12)
    assert control.preferred_vertical_scroll(view.window) == 2


def test_enter_expand_and_collapse_preserve_visible_message_position():
    store, _, view = _view()
    frame = view.provider.frame(80)
    start = dict(frame.block_line_offsets)["block-18"]
    view.control.jump_to(start + 11)
    before = view.control.reading_anchor()
    view.enter_transcript(store.snapshot())
    view.modal_control.create_content(80, 12)
    assert view.modal_control.reading_anchor().block_id == before.block_id
    view.toggle_full_detail()
    view.modal_control.create_content(80, 12)
    assert view.modal_control.reading_anchor().block_id == before.block_id
    view.toggle_full_detail()
    view.modal_control.create_content(80, 12)
    assert view.modal_control.reading_anchor().block_id == before.block_id


def test_full_detail_scroll_crosses_internal_pages_without_page_keys():
    store, state, view = _view(count=1, rows=900)
    view.enter_transcript(store.snapshot())
    view.toggle_full_detail()
    control = view.modal_control
    control.create_content(80, 12)
    control.move_home()
    seen = set()
    for _ in range(950):
        content = control.create_content(80, 12)
        top = control.preferred_vertical_scroll(view.modal_window)
        for line in range(top, min(content.line_count, top + 12)):
            seen.add(fragments_text(content.get_line(line)))
        control.move(1)
    assert all(f"message-0-row-{row}" in seen for row in range(900))
    assert state.snapshot().complete_page > 0
    backwards = set()
    for _ in range(950):
        content = control.create_content(80, 12)
        top = control.preferred_vertical_scroll(view.modal_window)
        backwards.update(fragments_text(content.get_line(line)) for line in range(top, min(content.line_count, top + 12)))
        control.move(-1)
    assert all(f"message-0-row-{row}" in backwards for row in range(900))


def test_checkpoint_and_live_receipts_show_one_input_in_both_arrival_orders():
    for checkpoint_first in (False, True):
        runtime = TuiRuntime("reading-dedupe")
        runtime.enqueue_prompt("request", "开始", queued=False)
        turn = runtime.begin_turn("request")
        runtime.enqueue_active_turn_input("input", "用户补充")
        event = {"schema": "conversation_history_display.v1", "request_id": "bg-main:thread:piece",
                 "block_id": "bg-main:thread:piece:input-message:input", "kind": "user_message",
                 "phase": "completed", "payload": {"text": "用户补充", "message_id": "input"}}
        if checkpoint_first:
            runtime.publish_recovered_history([], display_events=[event])
        turn.on_gateway_event({"kind": "active_turn_input_submitted", "client_message_ids": ["input"], "provider_call_id": "call"})
        turn.on_gateway_event({"kind": "active_turn_input_consumed", "client_message_ids": ["input"]})
        runtime.publish_recovered_history([], display_events=[event])
        assert sum(block.text == "用户补充" for block in runtime.store.snapshot().stable_blocks) == 1


def test_gateway_submission_closes_previous_response_before_inserted_input():
    runtime = TuiRuntime("reading-steer")
    runtime.enqueue_prompt("request", "开始", queued=False)
    turn = runtime.begin_turn("request")
    turn.write_model("插话前的回答")
    runtime.enqueue_active_turn_input("input", "用户补充")
    event = {"kind": "active_turn_input_submitted", "client_message_ids": ["input"], "provider_call_id": "call-2"}
    turn.on_gateway_event(event)
    turn.write_model("收到补充后的回答")
    turn.on_gateway_event(event)  # 重复回执不能把同一段回答再切开。
    turn.on_gateway_event({"kind": "active_turn_input_consumed", "client_message_ids": ["input"]})
    turn.finalize(TuiTurnSummary(response_text="收到补充后的回答", ok=True))
    snapshot = runtime.store.snapshot()
    blocks = sorted((*snapshot.stable_blocks, *snapshot.active_blocks), key=lambda block: block.created_seq)
    assert [(block.role, block.text) for block in blocks if block.role in {"user", "assistant"}] == [
        ("user", "开始"), ("assistant", "插话前的回答"),
        ("user", "用户补充"), ("assistant", "收到补充后的回答"),
    ]


def test_loading_preceding_archive_keeps_selected_message_on_screen():
    store, state = TuiStateStore(), TuiTranscriptModeState()
    seq = TuiEventSequencer("late-archive")
    reference = {"schema": "display_archive_ref.v1", "archive_id": "a" * 32,
                 "thread_id": "thread", "page_count": 2}
    store.publish(seq.emit("tool_completed", "completed", "archive", {
        "output": "\n".join(f"preview-{row}" for row in range(20)), "display_archive_ref": reference,
    }))
    store.publish(seq.emit("assistant_completed", "completed", "selected", {"text": "正在看的消息"}))
    store.publish(seq.emit("assistant_completed", "completed", "tail", {"text": "\n".join(f"tail-{row}" for row in range(40))}))
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(width=width,
        detailed_transcript=state.snapshot().active, show_all=state.snapshot().show_all), transcript_state=state)
    view.control.create_content(80, 12)
    start = dict(view.provider.frame(80).block_line_offsets)["selected"]
    view.control.jump_to(start + 11 - 3)
    view.enter_transcript(store.snapshot())
    view.modal_control.create_content(80, 12)
    before = view.modal_control.reading_anchor()
    assert before.block_id == "selected"
    view.toggle_full_detail()
    view.modal_control.create_content(80, 12)
    for page in (0, 1):
        state.accept_complete_page(reference, page, {"ok": True, "page_index": page,
            "rows": [{"text": f"archive-{page}-{row}", "row_index": page * 100 + row} for row in range(100)]})
        content = view.modal_control.create_content(80, 12)
    top = view.modal_control.preferred_vertical_scroll(view.modal_window)
    visible = [fragments_text(content.get_line(row)) for row in range(top, min(top + 12, content.line_count))]
    assert any("正在看的消息" in line for line in visible), visible
    assert view.modal_control.reading_anchor().block_id == "selected"


def test_loading_adjacent_archive_preserves_position_inside_wrapped_line():
    store, state = TuiStateStore(), TuiTranscriptModeState()
    seq = TuiEventSequencer("wrapped-archive")
    reference = {"schema": "display_archive_ref.v1", "archive_id": "b" * 32,
                 "thread_id": "thread", "page_count": 2}
    store.publish(seq.emit("tool_completed", "completed", "archive", {
        "output": "preview", "display_archive_ref": reference,
    }))
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(width=width), transcript_state=state)
    view.enter_transcript(store.snapshot())
    view.toggle_full_detail()
    view.modal_control.create_content(40, 12)
    state.accept_complete_page(reference, 0, {"ok": True, "page_index": 0, "rows": [
        {"text": "".join(f"{row:04d} " for row in range(400)), "row_index": 0},
    ]})
    control = view.modal_control
    control.create_content(40, 12)
    control.move_home()
    control.create_content(40, 12)
    control.move(25)
    content = control.create_content(40, 12)
    top = control.preferred_vertical_scroll(view.modal_window)
    before = fragments_text(content.get_line(top))
    state.accept_complete_page(reference, 1, {"ok": True, "page_index": 1,
        "rows": [{"text": f"next-{row}", "row_index": row + 1} for row in range(100)]})
    content = control.create_content(40, 12)
    top = control.preferred_vertical_scroll(view.modal_window)
    assert fragments_text(content.get_line(top)) == before


def test_short_archive_pages_scroll_continuously_before_long_page():
    store, state = TuiStateStore(), TuiTranscriptModeState()
    seq = TuiEventSequencer("short-pages")
    references = []
    for index in range(8):
        reference = {"schema": "display_archive_ref.v1", "archive_id": f"{index:032x}",
                     "thread_id": "thread", "page_count": 1}
        references.append(reference)
        store.publish(seq.emit("tool_completed", "completed", f"tool-{index}", {
            "output": "preview", "display_archive_ref": reference,
        }))
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(width=width), transcript_state=state)
    view.enter_transcript(store.snapshot())
    view.toggle_full_detail()
    control = view.modal_control
    control.create_content(80, 24)
    for index, ref in enumerate(references):
        state.accept_complete_page(ref, 0, {"ok": True, "page_index": 0,
            "rows": [{"text": f"page-{index}-row-{row}"} for row in range(24 if index == 0 else 6)]})
    control.create_content(80, 24)
    control.move_home()
    seen = set()
    for _ in range(100):
        content = control.create_content(80, 24)
        top = control.preferred_vertical_scroll(view.modal_window)
        seen.update(fragments_text(content.get_line(row)) for row in range(top, min(top + 24, content.line_count)))
        control.move(1)
    assert all(f"page-{page}-row-{row}" in seen for page in range(8) for row in range(24 if page == 0 else 6))
