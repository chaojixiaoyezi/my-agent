from __future__ import annotations

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_view import (
    TuiFrameProvider,
    TuiTranscriptControl,
    make_tui_transcript_view,
)
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def _provider(store: TuiStateStore) -> TuiFrameProvider:
    return TuiFrameProvider(store, lambda width: TuiRenderContext(width=width))


def test_control_reads_formatted_lines_without_building_transcript_string() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("view", clock=lambda: 1.0)
    store.publish(seq.emit("user_message", "completed", "user", {"text": "hello"}))
    control = TuiTranscriptControl(_provider(store))
    content = control.create_content(20, 5)
    assert content.line_count == 1
    assert "".join(text for _style, text, *_handler in content.get_line(0)).startswith("❯ hello")
    assert content.cursor_position.y == 0
    assert control.preferred_height(20, 10, False, None) == 1


def test_control_preferred_height_tracks_inline_transcript_and_terminal_cap() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("height", clock=lambda: 1.0)
    for index in range(6):
        store.publish(
            seq.emit(
                "system_message",
                "completed",
                f"line-{index}",
                {"text": f"line-{index}"},
            )
        )
    control = TuiTranscriptControl(_provider(store))

    assert control.preferred_height(40, 50, False, None) == 11
    assert control.preferred_height(40, 4, False, None) == 4


def test_manual_scroll_anchor_does_not_jump_when_new_event_arrives() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("view", clock=lambda: 2.0)
    for index in range(8):
        store.publish(
            seq.emit(
                "system_message",
                "completed",
                f"system-{index}",
                {"text": f"line-{index}"},
            )
        )
    provider = _provider(store)
    control = TuiTranscriptControl(provider)
    first = control.create_content(40, 5)
    assert first.cursor_position.y == first.line_count - 1
    control.move(-4)
    anchored = control.create_content(40, 5).cursor_position.y
    assert control.scroll_indicator() == "Jump to bottom"
    store.publish(seq.emit("system_message", "completed", "system-new", {"text": "new"}))
    after = control.create_content(40, 5)
    assert after.cursor_position.y == anchored
    control.move_end()
    assert control.create_content(40, 5).cursor_position.y == after.line_count - 1
    assert control.scroll_indicator() is None


def test_scrolling_down_to_last_line_restores_sticky_follow() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("sticky-return", clock=lambda: 2.25)
    for index in range(8):
        store.publish(
            seq.emit(
                "system_message",
                "completed",
                f"system-{index}",
                {"text": f"line-{index}"},
            )
        )
    control = TuiTranscriptControl(_provider(store))
    content = control.create_content(40, 5)
    control.move(-4)
    assert control.follow is False

    control.move(content.line_count)

    assert control.follow is True
    assert control.scroll_indicator() is None
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-new",
            {"text": "new answer"},
        )
    )
    updated = control.create_content(40, 5)
    assert updated.cursor_position.y == updated.line_count - 1


def test_manual_scroll_counts_new_typed_messages_without_counting_tool_blocks() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("unseen", clock=lambda: 2.5)
    store.publish(seq.emit("user_message", "completed", "user-1", {"text": "one"}))
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.move(-1)

    store.publish(seq.emit("tool_started", "started", "tool-1", {"tool_name": "Read"}))
    assert control.scroll_indicator() == "Jump to bottom"
    store.publish(seq.emit("assistant_started", "started", "assistant-1"))
    store.publish(
        seq.emit("assistant_delta", "delta", "assistant-1", {"text": "answer"})
    )
    assert control.scroll_indicator() == "1 new message"
    store.publish(
        seq.emit("assistant_completed", "completed", "assistant-1", {"text": "answer"})
    )
    assert control.scroll_indicator() == "1 new message"


def test_new_messages_footer_pill_click_restores_follow_tail() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("unseen-click", clock=lambda: 2.75)
    store.publish(seq.emit("user_message", "completed", "user-1", {"text": "one"}))
    view = make_tui_transcript_view(
        store,
        lambda width: TuiRenderContext(width=width),
    )
    view.control.create_content(40, 5)
    view.control.move(-1)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-1",
            {"text": "answer"},
        )
    )
    assert view.control.scroll_indicator() == "1 new message"
    footer = view.footer_control.create_content(40, 1).get_line(0)
    pill_start = sum(len(text) for _style, text in footer[:-1])

    result = view.footer_control.mouse_handler(
        MouseEvent(
            Point(x=pill_start + 1, y=0),
            MouseEventType.MOUSE_UP,
            MouseButton.LEFT,
            frozenset(),
        )
    )

    assert result is None
    assert view.control.scroll_indicator() is None
    assert view.control.create_content(40, 5).cursor_position.y > 0


def test_resize_rewraps_without_changing_state_or_losing_user_background() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("view", clock=lambda: 3.0)
    text = "中文宽字符与 emoji 👩\u200d💻 repeated repeated"
    store.publish(seq.emit("user_message", "completed", "user", {"text": text}))
    provider = _provider(store)
    control = TuiTranscriptControl(provider)
    wide = control.create_content(40, 10)
    narrow = control.create_content(16, 10)
    assert narrow.line_count > wide.line_count
    assert store.snapshot().stable_blocks[0].text == text
    for index in range(narrow.line_count):
        line = narrow.get_line(index)
        assert fragments_text(tuple((style, value) for style, value, *_ in line))
        assert sum(len(value) for _style, value, *_ in line) >= 8


def test_provider_shares_frame_and_block_cache_between_controls() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("view", clock=lambda: 4.0)
    store.publish(seq.emit("user_message", "completed", "user", {"text": "stable"}))
    provider = _provider(store)
    first = provider.frame(80)
    second = provider.frame(80)
    assert first is second
    assert provider.block_cache.stats().misses == 1
    store.publish(seq.emit("assistant_started", "started", "assistant"))
    third = provider.frame(80)
    assert third is not second
    assert provider.block_cache.stats().hits == 1


def test_idle_animation_tick_reuses_static_long_transcript_frame() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("idle-cache", clock=lambda: 4.5)
    for index in range(200):
        store.publish(
            seq.emit(
                "assistant_completed",
                "completed",
                f"assistant-{index}",
                {"text": f"answer {index}"},
            )
        )
    clock = {"now": 10.0, "spinner": 0}
    provider = TuiFrameProvider(
        store,
        lambda width: TuiRenderContext(
            width=width,
            now=clock["now"],
            spinner_index=clock["spinner"],
        ),
    )
    first = provider.frame(120)

    clock.update(now=20.0, spinner=7)
    second = provider.frame(120)

    assert second is first
    assert provider.block_cache.stats().misses == 200


def test_full_transcript_view_computes_footer_height_from_rendered_fragments() -> None:
    store = TuiStateStore()
    view = make_tui_transcript_view(
        store,
        lambda width: TuiRenderContext(width=width),
    )

    assert view.footer_line_count() == 1


def test_mouse_selection_highlights_and_copies_visible_columns() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection", clock=lambda: 5.0)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection",
            {"text": "abcdef"},
        )
    )
    provider = _provider(store)
    control = TuiTranscriptControl(provider)
    content = control.create_content(40, 5)

    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )
    selected_line = control.create_content(40, 5).get_line(0)

    assert control.selected_text() == "abcd"
    assert any("class:tui-selection" in style for style, _text, *_ in selected_line)


def test_mouse_move_after_button_release_does_not_expand_selection() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-release", clock=lambda: 5.5)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection-release",
            {"text": "abcdefghijklmnopqrstuvwxyz"},
        )
    )
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )

    result = control.mouse_handler(
        MouseEvent(Point(x=24, y=0), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
    )

    assert result is NotImplemented
    assert control.selected_text() == "abcd"


def test_no_button_motion_finishes_lost_release_and_copies_once() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-lost-release", clock=lambda: 5.75)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection-lost-release",
            {"text": "abcdefghijklmnopqrstuvwxyz"},
        )
    )
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    copied: list[str] = []
    control.set_copy_on_select(copied.append)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
    )

    result = control.mouse_handler(
        MouseEvent(Point(x=18, y=0), MouseEventType.MOUSE_MOVE, MouseButton.NONE, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=24, y=0), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
    )

    assert result is None
    assert control.selected_text() == "abcd"
    assert copied == ["abcd"]


def test_selection_uses_prompt_toolkit_source_indexes_for_wide_characters() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-wide", clock=lambda: 5.9)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection-wide",
            {"text": "甲乙丙丁"},
        )
    )
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )
    selected_line = control.create_content(40, 5).get_line(0)
    highlighted = "".join(
        text for style, text, *_ in selected_line if "class:tui-selection" in style
    )

    assert control.selected_text() == "甲乙丙丁"
    assert highlighted == "甲乙丙丁"


def test_right_click_copies_wide_selection_once_and_keeps_highlight() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-right-copy", clock=lambda: 5.95)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection-right-copy",
            {"text": "甲乙丙丁"},
        )
    )
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )
    copied: list[str] = []
    control.set_copy_on_select(copied.append)

    down_result = control.mouse_handler(
        MouseEvent(Point(x=4, y=0), MouseEventType.MOUSE_DOWN, MouseButton.RIGHT, frozenset())
    )
    up_result = control.mouse_handler(
        MouseEvent(Point(x=4, y=0), MouseEventType.MOUSE_UP, MouseButton.RIGHT, frozenset())
    )
    selected_line = control.create_content(40, 5).get_line(0)

    assert down_result is None
    assert up_result is None
    assert copied == ["甲乙丙丁"]
    assert control.selected_text() == "甲乙丙丁"
    assert any("class:tui-selection" in style for style, _text, *_ in selected_line)


def test_resize_clears_viewport_selection() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-resize", clock=lambda: 6.0)
    store.publish(seq.emit("user_message", "completed", "user", {"text": "abcdef"}))
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )

    control.create_content(20, 5)

    assert control.selected_text() == ""
