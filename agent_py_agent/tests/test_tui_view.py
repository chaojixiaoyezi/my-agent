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


def test_view_select_all_routes_to_active_viewport_and_preserves_other_selection() -> None:
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState

    store = TuiStateStore()
    seq = TuiEventSequencer("select-all-view", clock=lambda: 1.0)
    store.publish(seq.emit("assistant_completed", "completed", "answer", {"text": "第一行\n第二行"}))
    mode = TuiTranscriptModeState()
    view = make_tui_transcript_view(
        store, lambda width: TuiRenderContext(width=width), transcript_state=mode,
    )
    view.control.create_content(80, 5)
    view.select_all()
    normal = view.selected_text()
    assert "第一行" in normal and "第二行" in normal

    view.clear_selection()
    mode.enter(store.snapshot())
    mode.toggle_show_all()
    view.modal_control.create_content(80, 5)
    view.select_all()
    assert "第一行" in view.selected_text() and "第二行" in view.selected_text()
    assert view.control.selected_text() == ""
    mode.exit()
    assert view.selected_text() == ""


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
    assert control.is_following() is True
    control.move(-4)
    anchored = control.create_content(40, 5).cursor_position.y
    assert control.is_following() is False
    assert control.scroll_indicator() == "Jump to bottom"
    store.publish(seq.emit("system_message", "completed", "system-new", {"text": "new"}))
    after = control.create_content(40, 5)
    assert after.cursor_position.y == anchored
    control.move_end()
    assert control.is_following() is True
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


def test_window_scroll_follows_each_agent_store_anchor_after_switch() -> None:
    root = TuiStateStore()
    child = TuiStateStore()
    root_seq = TuiEventSequencer("root-scroll", clock=lambda: 3.0)
    child_seq = TuiEventSequencer("child-scroll", clock=lambda: 4.0)
    for index in range(12):
        root.publish(
            root_seq.emit(
                "system_message",
                "completed",
                f"root-{index}",
                {"text": f"root line {index}"},
            )
        )
    for index in range(18):
        child.publish(
            child_seq.emit(
                "system_message",
                "completed",
                f"child-{index}",
                {"text": f"child line {index}"},
            )
        )
    view = make_tui_transcript_view(
        root,
        lambda width: TuiRenderContext(width=width),
    )

    root_content = view.control.create_content(60, 5)
    view.window._scroll(root_content, 60, 5)
    assert view.window.vertical_scroll == root_content.line_count - 5
    view.home()
    root_home = view.control.create_content(60, 5)
    view.window._scroll(root_home, 60, 5)
    assert view.window.vertical_scroll == 0

    view.set_state_store(child)
    child_content = view.control.create_content(60, 5)
    view.window._scroll(child_content, 60, 5)
    assert view.window.vertical_scroll == child_content.line_count - 5

    view.set_state_store(root)
    restored = view.control.create_content(60, 5)
    view.window._scroll(restored, 60, 5)
    assert view.window.vertical_scroll == 0


def test_agent_switch_rebinds_expanded_transcript_to_selected_store() -> None:
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState

    root, child = TuiStateStore(), TuiStateStore()
    root_seq = TuiEventSequencer("expanded-root", clock=lambda: 1.0)
    child_seq = TuiEventSequencer("expanded-child", clock=lambda: 2.0)
    root.publish(root_seq.emit("user_message", "completed", "root-1", {"text": "主代理需求"}))
    child.publish(child_seq.emit("user_message", "completed", "child-1", {"text": "子代理职责"}))
    mode = TuiTranscriptModeState()
    view = make_tui_transcript_view(root, lambda width: TuiRenderContext(width=width), transcript_state=mode)
    view.set_state_store(child)
    mode.enter(child.snapshot())
    mode.toggle_show_all()

    view.set_state_store(root)
    rendered = "\n".join(fragments_text(line) for line in view.provider.frame(80).transcript_lines)
    assert "主代理需求" in rendered
    assert "子代理职责" not in rendered
    assert mode.snapshot().active and mode.snapshot().show_all
    root.publish(root_seq.emit("user_message", "completed", "root-2", {"text": "刚到的新消息"}))
    view.set_state_store(root)  # 同页面刷新不能解冻或偷纳入后到内容。
    assert "刚到的新消息" not in "\n".join(
        fragments_text(line) for line in view.provider.frame(80).transcript_lines
    )
    view.set_state_store(child)
    assert "子代理职责" in "\n".join(
        fragments_text(line) for line in view.provider.frame(80).transcript_lines
    )
    mode.exit()
    view.set_state_store(root)
    assert "刚到的新消息" in "\n".join(
        fragments_text(line) for line in view.provider.frame(80).transcript_lines
    )


def test_agent_store_switch_restores_each_viewport_without_cross_view_selection() -> None:
    root_store = TuiStateStore()
    child_store = TuiStateStore()
    root_seq = TuiEventSequencer("root-viewport", clock=lambda: 2.3)
    child_seq = TuiEventSequencer("child-viewport", clock=lambda: 2.4)
    for index in range(10):
        root_store.publish(
            root_seq.emit(
                "system_message",
                "completed",
                f"root-{index}",
                {"text": f"root line {index}"},
            )
        )
    for index in range(7):
        child_store.publish(
            child_seq.emit(
                "system_message",
                "completed",
                f"child-{index}",
                {"text": f"child line {index}"},
            )
        )
    view = make_tui_transcript_view(
        root_store,
        lambda width: TuiRenderContext(width=width),
    )

    root_tail = view.control.create_content(40, 5)
    view.scroll(-6)
    root_anchor = view.control.create_content(40, 5).cursor_position.y
    view.control.mouse_handler(
        MouseEvent(
            Point(x=0, y=root_anchor),
            MouseEventType.MOUSE_DOWN,
            MouseButton.LEFT,
            frozenset(),
        )
    )
    view.control.mouse_handler(
        MouseEvent(
            Point(x=3, y=root_anchor),
            MouseEventType.MOUSE_UP,
            MouseButton.LEFT,
            frozenset(),
        )
    )
    assert root_anchor < root_tail.line_count - 1
    assert view.control.selected_text()

    view.set_state_store(child_store)
    child_tail = view.control.create_content(40, 5)
    assert child_tail.cursor_position.y == child_tail.line_count - 1
    assert view.control.selected_text() == ""
    view.scroll(-4)
    child_anchor = view.control.create_content(40, 5).cursor_position.y

    view.set_state_store(root_store)
    restored_root = view.control.create_content(40, 5)
    assert restored_root.cursor_position.y == root_anchor
    assert view.control.follow is False

    view.set_state_store(child_store)
    restored_child = view.control.create_content(40, 5)
    assert restored_child.cursor_position.y == child_anchor
    assert view.control.follow is False


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


def test_right_click_without_selection_does_not_interrupt_the_event_loop() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("right-copy-no-selection", clock=lambda: 5.94)
    store.publish(seq.emit("user_message", "completed", "user", {"text": "没有选区也可以右键"}))
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    copied: list[str] = []
    control.set_copy_on_select(copied.append)
    for event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
        result = control.mouse_handler(
            MouseEvent(Point(x=4, y=0), event_type, MouseButton.RIGHT, frozenset())
        )
        assert result is None
    assert copied == []
    assert control.selected_text() == ""


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


def test_right_release_without_down_still_copies_existing_selection_once() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("selection-right-release-only", clock=lambda: 5.97)
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant-selection-right-release-only",
            {"text": "远端右键复制"},
        )
    )
    control = TuiTranscriptControl(_provider(store))
    control.create_content(40, 5)
    control.mouse_handler(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    control.mouse_handler(
        MouseEvent(Point(x=7, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )
    copied: list[str] = []
    control.set_copy_on_select(copied.append)

    result = control.mouse_handler(
        MouseEvent(Point(x=5, y=0), MouseEventType.MOUSE_UP, MouseButton.RIGHT, frozenset())
    )

    assert result is None
    assert copied == ["远端右键复制"]
    assert control.selected_text() == "远端右键复制"


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
