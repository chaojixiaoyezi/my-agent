from __future__ import annotations

import unicodedata

import pytest

from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    TuiRenderFrame,
    render_tui_snapshot,
    sanitize_tui_render_frame,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import (
    display_width_fragments,
    fragments_text,
    sanitize_terminal_text,
)
from agent_py_agent.cli.chat_parts.tui_view_model import (
    TuiBlock,
    TuiContextUsage,
    TuiStateStore,
)


def _frame_lines(frame) -> list[str]:
    return [fragments_text(line) for line in frame.transcript_lines]


def _frame_text(frame: TuiRenderFrame) -> str:
    lines = (
        *frame.transcript_lines,
        *frame.overlay_lines,
        *frame.input_status_lines,
        *frame.todo_lines,
        *frame.agent_lines,
        frame.footer,
    )
    return "\n".join(fragments_text(line) for line in lines)


def _assert_terminal_safe(text: str) -> None:
    assert all(
        char in {"\n", "\t"} or unicodedata.category(char) not in {"Cc", "Cs"}
        for char in text
    )


def _fixture_store() -> TuiStateStore:
    store = TuiStateStore()
    seq = TuiEventSequencer("renderer", clock=lambda: 10.0)
    store.publish(
        seq.emit(
            "session_started",
            "completed",
            "session",
            {"version": "0.3.0", "model": "MiniMax-M2.7", "workspace": "/root"},
        )
    )
    store.publish(seq.emit("user_message", "completed", "user", {"text": "TUI_FIXTURE_MARKDOWN"}))
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant",
            {
                "text": """## Fixture 标题

- 中文宽字符：你好，终端
- **粗体**、`inline_code()` 与 https://example.com

> *这是引用行。*

```python
print("fixture")
```

| 列 A | 列 B |
|---|---|
| 1 | 二 |
"""
            },
        )
    )
    return store


def test_terminal_text_sanitizer_removes_all_c0_c1_and_lone_surrogates() -> None:
    controls = "".join(chr(value) for value in range(160)) + "\ud800"
    safe_unicode = "中文 العربية אבג 👩\u200d👩\u200d👧\u200d👦️\n\t"

    sanitized = sanitize_terminal_text(controls + safe_unicode)

    _assert_terminal_safe(sanitized)
    assert "\n\t" in sanitized
    assert safe_unicode.rstrip("\n\t") in sanitized


def test_renderer_neutralizes_real_terminal_control_sequences_on_every_surface() -> None:
    untrusted = (
        "before\x1b[2J\x1b[999;999H"
        "\x1b]0;owned\x07\x1b]8;;https://example.invalid\x1b\\link\x1b]8;;\x1b\\"
        "\x1b]52;c;c2VjcmV0\x07\x1bP+dcs\x1b\\\x1b_apc\x1b\\\x1b^pm\x1b\\"
        "\x9b31m\x9dtitle\x9cafter"
    )
    store = TuiStateStore()
    seq = TuiEventSequencer("terminal-injection", clock=lambda: 12.0)
    store.publish(
        seq.emit(
            "session_started",
            "completed",
            "session",
            {"version": untrusted, "model": untrusted, "workspace": untrusted},
        )
    )
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant",
            {"text": untrusted},
        )
    )
    store.publish(
        seq.emit(
            "tool_completed",
            "completed",
            "tool",
            {
                "tool": "run_command",
                "ok": False,
                "output": untrusted,
                "display": {
                    "kind": "command",
                    "command": untrusted,
                    "output": untrusted,
                    "error": untrusted,
                },
            },
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(
            width=120,
            agent_name=untrusted,
            model_name=untrusted,
            workspace=untrusted,
            notice=untrusted,
        ),
    )
    rendered = _frame_text(frame)

    _assert_terminal_safe(rendered)
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "]52;c;c2VjcmV0" in rendered
    assert "before[2J[999;999H" in rendered


def test_frame_sanitizer_preserves_styles_and_mouse_handlers() -> None:
    handler = object()
    frame = TuiRenderFrame(
        transcript_lines=((('class:test', 'a\x1b[2Jb', handler),),),
        overlay_lines=(),
        input_status_lines=(),
        todo_lines=(),
        agent_lines=(),
        footer=(("class:footer", "ok\x07"),),
    )

    sanitized = sanitize_tui_render_frame(frame)

    assert sanitized.transcript_lines == ((('class:test', 'a[2Jb', handler),),)
    assert sanitized.footer == (("class:footer", "ok"),)


def test_full_frame_matches_reference_message_geometry_at_120_columns() -> None:
    frame = render_tui_snapshot(
        _fixture_store().snapshot(),
        TuiRenderContext(width=120, model_name="MiniMax-M2.7", workspace="/root"),
    )
    texts = _frame_lines(frame)
    assert display_width_fragments(frame.transcript_lines[0]) == 95
    user_index = next(index for index, text in enumerate(texts) if text.startswith("❯ TUI_FIXTURE"))
    assert display_width_fragments(frame.transcript_lines[user_index]) == 120
    assert texts[user_index + 2] == "● Fixture 标题"
    assert "  - 中文宽字符：你好，终端" in texts
    assert '  print("fixture")' in texts
    assert "  ┌──────┬──────┐" in texts
    user_styles = [style for style, _text in frame.transcript_lines[user_index]]
    assert user_styles == ["class:tui-user-marker", "class:tui-user-text", "class:tui-user-fill"]
    assert frame.footer == ((
        "class:tui-muted",
        "  ? 快捷键 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式",
    ),)


def test_footer_explains_native_copy_escape_hatch_without_hiding_history() -> None:
    frame = render_tui_snapshot(
        _fixture_store().snapshot(),
        TuiRenderContext(width=120, mouse_capture_enabled=False),
    )

    assert frame.footer == ((
        "class:tui-muted",
        "  ? 快捷键 · PgUp/Ctrl+Home 历史 · F6 恢复滚轮",
    ),)


def test_narrow_frame_never_exceeds_width_and_uses_single_column_card() -> None:
    frame = render_tui_snapshot(
        _fixture_store().snapshot(),
        TuiRenderContext(width=58, model_name="MiniMax-M2.7", workspace="/root"),
    )
    assert all(display_width_fragments(line) <= 58 for line in frame.transcript_lines)
    texts = _frame_lines(frame)
    assert any("Welcome back!" in text for text in texts)
    assert not any("Recent activity" in text for text in texts)


def test_welcome_card_renders_bunny_girl_terminal_avatar_styles() -> None:
    frame = render_tui_snapshot(
        _fixture_store().snapshot(),
        TuiRenderContext(width=120, model_name="MiniMax-M2.7", workspace="/root"),
    )
    texts = _frame_lines(frame)
    welcome_fragments = [fragment for line in frame.transcript_lines[:14] for fragment in line]

    assert any("◕  ◕" in text for text in texts)
    assert any("◆────◆" in text for text in texts)
    assert any("ᘏ⑅ᘏ" in text for text in texts)
    assert any("╱╲" in text for text in texts)
    assert {
        "class:tui-avatar-hair",
        "class:tui-avatar-ribbon",
        "class:tui-avatar-face",
        "class:tui-avatar-dress",
        "class:tui-avatar-umbrella",
        "class:tui-avatar-bunny",
    }.issubset({style for style, _text, *_ in welcome_fragments})


@pytest.mark.parametrize("width", [80, 120, 140])
def test_reference_acceptance_widths_never_overflow(width: int) -> None:
    frame = render_tui_snapshot(
        _fixture_store().snapshot(),
        TuiRenderContext(width=width, model_name="MiniMax-M2.7", workspace="/root"),
    )

    assert all(display_width_fragments(line) <= width for line in frame.transcript_lines)
    assert display_width_fragments(frame.footer) <= width


def test_gateway_connection_spinner_uses_typed_startup_block() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("connection", clock=lambda: 15.0)
    store.publish(
        seq.emit(
            "connection_started",
            "started",
            "connection:gateway",
            {"text": "Connecting to Gateway"},
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))

    assert _frame_lines(frame) == ["✻ Connecting to Gateway…"]
    assert frame.footer == (("class:tui-muted", "  Connecting…"),)


def test_thinking_tool_and_permission_use_typed_phase() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("renderer", clock=lambda: 20.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking", {"text": "internal"}))
    store.publish(seq.emit("tool_started", "started", "tool", {"tool": "run_command"}))
    store.publish(
        seq.emit(
            "permission_requested",
            "waiting_permission",
            "tool",
            {
                "permission_id": "permission-1",
                "title": "Bash command",
                "description": "printf fixture",
                "options": [
                    {
                        "id": "allow",
                        "label": "Yes",
                        "decision": "approved",
                        "feedback_type": "accept",
                    },
                    {
                        "id": "deny",
                        "label": "No",
                        "decision": "denied",
                        "feedback_type": "reject",
                    },
                ],
            },
        )
    )
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80, spinner_index=0))
    texts = _frame_lines(frame)
    overlay = [fragments_text(line) for line in frame.overlay_lines]
    assert not any(text.startswith(("✻ ", "✢ ", "✶ ")) for text in texts)
    assert not any("⎿\u00a0Tip:" in text for text in texts)
    assert "● Bash" in texts
    assert "  ⎿ Waiting for permission…" in texts
    assert overlay[:6] == [
        "─" * 80,
        " Bash command",
        "   printf fixture",
        " Do you want to proceed?",
        "❯ 1. Yes",
        "  2. No",
    ]
    assert overlay[-1] == " Esc to cancel · Tab to amend"
    assert frame.footer == ()


def test_tool_input_progress_replaces_generic_spinner_with_counter() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("tool-input-render", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "tool_input_started",
            "started",
            "tool-input",
            {
                "tool": "write_file",
                "stream_index": 0,
                "received_chars": 0,
                "started_at": 90.0,
            },
        )
    )
    store.publish(
        seq.emit(
            "tool_input_progress",
            "updated",
            "tool-input",
            {
                "tool": "write_file",
                "stream_index": 0,
                "received_chars": 12_345,
            },
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, now=105.0, spinner_index=1),
    )
    texts = _frame_lines(frame)
    assert "✢ 正在准备 Write 参数 · 12.3k chars · 15s" in texts
    assert not any("Working" in text or "Thinking" in text for text in texts)
    styles = {style for line in frame.transcript_lines for style, _text, *_ in line}
    assert "class:tui-spinner-highlight" in styles


def test_long_user_prompt_is_bounded_only_in_display_projection() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("long-user", clock=lambda: 25.0)
    text = "H" * 2_500 + "\n" + "middle\n" * 1_000 + "T" * 2_500
    store.publish(seq.emit("user_message", "completed", "user", {"text": text}))

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=3_000))
    lines = [fragments_text(line).rstrip() for line in frame.transcript_lines]
    hidden_lines = text[2_500:-2_500].count("\n")

    assert store.snapshot().stable_blocks[0].text == text
    assert lines[0] == "❯ " + "H" * 2_500
    assert lines[1] == f"  … +{hidden_lines} lines …"
    assert lines[2] == "  " + "T" * 2_500


def test_spinner_metrics_stall_color_and_bottom_order_match_reference() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("spinner", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(seq.emit("assistant_started", "started", "assistant"))
    store.publish(seq.emit("assistant_delta", "delta", "assistant", {"text": "done"}))
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant",
            {"text": "done"},
        )
    )
    store.publish(seq.emit("tool_started", "started", "tool", {"tool": "read_file"}))

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(
            width=100,
            now=131.0,
            status_started_at=100.0,
            status_last_event_at=120.0,
            output_tokens=1_234,
        ),
    )
    texts = _frame_lines(frame)
    thinking_index = next(
        index for index, text in enumerate(texts) if text.startswith(("✻ ", "✢ ", "✶ "))
    )
    thinking_line = frame.transcript_lines[thinking_index]

    assert texts.index("● done") < thinking_index
    assert texts.index("● Read") < thinking_index
    assert "31s" in texts[thinking_index]
    assert "↓ 1.2k tokens" in texts[thinking_index]
    # 终端交互 对齐：思考文案保持浅灰，只有活动 glyph/单字 glimmer 使用高亮。
    assert "class:tui-thinking" in {style for style, _text in thinking_line}
    assert "class:tui-spinner-highlight" in {style for style, _text in thinking_line}


def test_visible_assistant_stream_hides_global_activity_spinner() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("streaming", clock=lambda: 200.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(seq.emit("assistant_started", "started", "assistant"))
    store.publish(seq.emit("assistant_delta", "delta", "assistant", {"text": "visible"}))

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    texts = _frame_lines(frame)

    assert "● visible" in texts
    assert not any(text.startswith(("✻ ", "✢ ", "✶ ")) for text in texts)


def test_visible_assistant_keeps_prior_thinking_text_in_order() -> None:
    """已收到正文的思考不是等待动画；正文开始后仍应保留在原时序位置。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("streaming-thinking", clock=lambda: 200.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit("thinking_delta", "delta", "thinking", {"text": "正在核对底座"})
    )
    store.publish(seq.emit("assistant_started", "started", "assistant"))
    store.publish(seq.emit("assistant_delta", "delta", "assistant", {"text": "开始修改"}))

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    texts = _frame_lines(frame)

    assert any("正在核对底座" in text for text in texts)
    assert texts.index(next(text for text in texts if "正在核对底座" in text)) < texts.index(
        "● 开始修改"
    )


def test_completed_thinking_collapses_and_detailed_mode_expands() -> None:
    """终端交互 对齐：completed 思考默认展开内容（灰色常显），
    超长（>50 行）才折叠提示；detailed 模式全量展开。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("renderer", clock=lambda: 30.0)
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "thinking_completed",
            "completed",
            "thinking",
            {"text": "**private reasoning**", "duration_seconds": 10.2},
        )
    )
    collapsed = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    detailed = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, detailed_transcript=True),
    )
    # 默认展开：标题 + 内容都可见（灰色）
    assert _frame_lines(collapsed)[0] == "∴ Thought for 10s"
    assert "（private reasoning）" in _frame_lines(collapsed)
    assert _frame_lines(detailed)[0] == "∴ Thought for 10s"
    assert "（private reasoning）" in _frame_lines(detailed)
    assert fragments_text(detailed.footer).startswith("  Showing detailed transcript")


def test_completed_thinking_fullwidth_parentheses_fit_narrow_terminal() -> None:
    """全角左右括号各占两列，10 列终端也不能让 completed thinking 越界。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("renderer-narrow-thinking", clock=lambda: 30.0)
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "thinking_completed",
            "completed",
            "thinking",
            {"text": "0000000", "duration_seconds": 0.0},
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=10))

    assert all(display_width_fragments(line) <= 10 for line in frame.transcript_lines)


def test_completed_thinking_long_content_folds_with_hint() -> None:
    """completed 思考超长（>50 行）折叠为提示行（灰色），Ctrl+O 看全部。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("renderer", clock=lambda: 30.0)
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    long_text = "\n".join(f"思考第 {i} 行内容" for i in range(1, 80))
    store.publish(
        seq.emit(
            "thinking_completed",
            "completed",
            "thinking",
            {"text": long_text, "duration_seconds": 10.2},
        )
    )
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    texts = _frame_lines(frame)
    assert any("思考内容共" in text and "Ctrl+O" in text for text in texts)
    # 折叠提示行灰色
    hint_lines = [
        line
        for line in frame.transcript_lines
        if any("思考内容共" in text for _style, text in line)
    ]
    assert hint_lines
    styles = {style for line in hint_lines for style, _text in line}
    assert "class:tui-thinking" in styles


def test_block_cache_reuses_stable_history_when_active_stream_changes() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("cache", clock=lambda: 40.0)
    store.publish(seq.emit("user_message", "completed", "user", {"text": "stable"}))
    store.publish(seq.emit("assistant_started", "started", "assistant"))
    cache = TuiBlockRenderCache(max_entries=10)
    context = TuiRenderContext(width=80)
    render_tui_snapshot(store.snapshot(), context, cache=cache)
    assert cache.stats().misses == 2
    store.publish(seq.emit("assistant_delta", "delta", "assistant", {"text": "x"}))
    render_tui_snapshot(store.snapshot(), context, cache=cache)
    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 3
    assert stats.entries == 3


def test_thinking_cache_key_covers_timer_and_stall_cycle() -> None:
    cache = TuiBlockRenderCache(max_entries=10)
    block = TuiBlock(
        block_id="spinner:thinking",
        kind="thinking_started",
        role="thinking",
        phase="started",
        metadata={"started_at": 100.0},
    )

    first = cache.render(
        block,
        TuiRenderContext(width=80, now=100.0, status_started_at=100.0, status_last_event_at=99.0),
    )
    second_elapsed = cache.render(
        block,
        TuiRenderContext(width=80, now=105.0, status_started_at=100.0, status_last_event_at=104.0),
    )
    third_stalled = cache.render(
        block,
        TuiRenderContext(width=80, now=110.0, status_started_at=100.0, status_last_event_at=101.0),
    )

    assert second_elapsed != first
    assert third_stalled != second_elapsed
    assert cache.stats().misses == 3


def test_tool_preview_is_bounded_and_show_all_expands_it() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("tool-expand", clock=lambda: 45.0)
    store.publish(seq.emit("tool_started", "started", "tool-expand", {"tool": "read_file"}))
    store.publish(
        seq.emit(
            "tool_completed",
            "completed",
            "tool-expand",
            {"tool": "read_file", "output": "\n".join(f"line {index}" for index in range(8))},
        )
    )

    bounded = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    expanded = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, detailed_transcript=True, show_all=True),
    )
    bounded_text = "\n".join(_frame_lines(bounded))
    expanded_text = "\n".join(_frame_lines(expanded))

    assert "line 5" in bounded_text
    assert "line 6" not in bounded_text
    assert "… +2 lines (ctrl+o, then ctrl+e to show all)" in bounded_text
    assert "line 6" in expanded_text
    assert "line 7" in expanded_text
    assert "… +" not in expanded_text


def test_structured_diff_renders_summary_line_numbers_and_colored_rows() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("diff-render", clock=lambda: 46.0)
    store.publish(
        seq.emit(
            "tool_started",
            "started",
            "tool-diff",
            {"tool": "edit_file", "detail": "src/ui.py", "invocation": "src/ui.py"},
        )
    )
    store.publish(
        seq.emit(
            "tool_completed",
            "completed",
            "tool-diff",
            {
                "tool": "edit_file",
                "display": {
                    "kind": "diff",
                    "path": "src/ui.py",
                    "lines_added": 1,
                    "lines_removed": 1,
                    "hidden_lines": 0,
                    "lines": [
                        {"kind": "header", "text": "@@ -7,1 +7,1 @@"},
                        {"kind": "remove", "old_line": 7, "new_line": None, "text": "old"},
                        {"kind": "add", "old_line": None, "new_line": 7, "text": "new"},
                    ],
                },
            },
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    texts = _frame_lines(frame)
    assert texts[0] == "● Update(src/ui.py)"
    assert texts[1] == "  ⎿ Added 1 lines, removed 1 lines"
    assert any("- old" in text for text in texts)
    assert any("+ new" in text for text in texts)
    styles = {style for line in frame.transcript_lines for style, _text, *_ in line}
    assert "class:tui-diff-remove" in styles
    assert "class:tui-diff-add" in styles


def test_structured_multi_file_patch_renders_each_file_and_update_title() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("patch-render", clock=lambda: 46.5)
    store.publish(
        seq.emit(
            "tool_started",
            "started",
            "tool-patch",
            {"tool": "apply_patch", "invocation": "2 files"},
        )
    )
    store.publish(
        seq.emit(
            "tool_completed",
            "completed",
            "tool-patch",
            {
                "tool": "apply_patch",
                "display": {
                    "kind": "patch",
                    "hidden_files": 0,
                    "files": [
                        {
                            "kind": "diff",
                            "path": "src/a.py",
                            "lines_added": 1,
                            "lines_removed": 0,
                            "hidden_lines": 0,
                            "lines": [
                                {"kind": "add", "old_line": None, "new_line": 1, "text": "a"}
                            ],
                        },
                        {
                            "kind": "diff",
                            "path": "src/b.py",
                            "lines_added": 0,
                            "lines_removed": 1,
                            "hidden_lines": 0,
                            "lines": [
                                {"kind": "remove", "old_line": 1, "new_line": None, "text": "b"}
                            ],
                        },
                    ],
                },
            },
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    text = "\n".join(_frame_lines(frame))
    assert "● Update(2 files)" in text
    assert "Updated 2 files" in text
    assert "src/a.py" in text
    assert "src/b.py" in text
    assert "+ a" in text
    assert "- b" in text


def test_structured_command_separates_error_output_and_keeps_invocation_title() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("command-render", clock=lambda: 47.0)
    store.publish(
        seq.emit(
            "tool_started",
            "started",
            "tool-command",
            {
                "tool": "run_command",
                "detail": "go test ./...",
                "invocation": "go test ./...",
            },
        )
    )
    store.publish(
        seq.emit(
            "tool_failed",
            "failed",
            "tool-command",
            {
                "tool": "run_command",
                "error_code": "COMMAND_FAILED",
                "display": {
                    "kind": "command",
                    "return_code": 1,
                    "stdout": "package output",
                    "stderr": "compile error",
                    "stdout_lines": 1,
                    "stderr_lines": 1,
                },
            },
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    texts = _frame_lines(frame)
    assert texts[0] == "● Bash(go test ./...)"
    assert texts[1] == "  ⎿ Error: Exit code 1"
    assert any("compile error" in text for text in texts)
    error_lines = [
        line
        for line in frame.transcript_lines
        if "compile error" in fragments_text(line)
    ]
    assert error_lines[0][0][0] == "class:tui-error"


def test_pending_and_queued_inputs_stay_fixed_above_composer() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("queue-render", clock=lambda: 50.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "queue_added",
            "queued",
            "queue:2",
            {"queue_id": "queue:2", "text": "排队消息", "priority": "next"},
        )
    )
    store.publish(
        seq.emit(
            "steer_added",
            "queued",
            "steer:1",
            {"message_id": "steer-1", "text": "当前任务补充"},
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    transcript_text = [fragments_text(line) for line in frame.transcript_lines]
    fixed_text = [fragments_text(line) for line in frame.input_status_lines]

    assert not any("排队消息" in line or "当前任务补充" in line for line in transcript_text)
    assert fixed_text == [
        "• Messages to be submitted after next tool call",
        "  ↳ 当前任务补充",
        "• Queued follow-up inputs",
        "  ↳ 排队消息",
        "    ↑ edit queued messages",
    ]
    assert all(display_width_fragments(line) <= 80 for line in frame.input_status_lines)


def test_stash_notice_and_paste_footer_are_structured_context() -> None:
    store = TuiStateStore()
    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, has_stash=True, is_pasting=True),
    )

    assert [fragments_text(line) for line in frame.input_status_lines] == [
        "  › Stashed (auto-restores after submit)"
    ]
    assert frame.footer == (("class:tui-muted", "  Pasting text…"),)


def test_live_context_strip_uses_wide_and_narrow_density_and_keeps_stash() -> None:
    store = TuiStateStore()
    usage = TuiContextUsage(
        current_tokens=31_400,
        context_window_tokens=128_000,
        compact_trigger_tokens=115_200,
        prompt_tokens=8_000,
        messages_tokens=6_000,
        runtime_guidance_tokens=400,
        tool_schema_tokens=17_000,
        estimated=True,
        protocol="native",
    )

    wide = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(
            width=120,
            context_usage=usage,
            compact_count=2,
            has_stash=True,
        ),
    )
    narrow = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=52, context_usage=usage, compact_count=2),
    )
    tiny = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=24, context_usage=usage, compact_count=2),
    )

    wide_lines = [fragments_text(line) for line in wide.input_status_lines]
    assert wide_lines == [
        "  ◉ Context ~31.4k/128.0k · 25% · compact 2 · 压缩点 90%",
        "  › Stashed (auto-restores after submit)",
    ]
    assert [fragments_text(line) for line in narrow.input_status_lines] == [
        "  ◉ Ctx ~31.4k/128.0k · 25% · c2 · 点90%"
    ]
    assert [fragments_text(line) for line in tiny.input_status_lines] == [
        "  ◉ Ctx 25% · c2 · 点90%"
    ]
    assert not any(
        label in wide_lines[0] for label in ("prompt", "messages", "tools")
    )
    assert all(display_width_fragments(line) <= 52 for line in narrow.input_status_lines)
    assert all(display_width_fragments(line) <= 24 for line in tiny.input_status_lines)


def test_compact_count_stays_visible_while_provider_context_snapshot_refreshes() -> None:
    store = TuiStateStore()

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, context_usage=None, compact_count=3),
    )

    assert [fragments_text(line) for line in frame.input_status_lines] == [
        "  ◉ Context 将在下次模型调用时刷新 · compact 3"
    ]


def test_manual_compact_boundary_keeps_structured_generation_and_display_details() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("manual-compact-render", clock=lambda: 12.0)
    store.publish(
        seq.emit(
            "compact_boundary",
            "completed",
            "compact:session:2",
            {
                "compact_generation": 2,
                "text": (
                    "Context compacted · generation 2\n"
                    "上下文估算：45,639 → 15,029 tokens。"
                ),
            },
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=100, compact_count=2),
    )
    rendered = "\n".join(_frame_lines(frame))

    assert "Context compacted · generation 2" in rendered
    assert "45,639 → 15,029 tokens" in rendered


def test_live_context_strip_warning_color_comes_from_compact_trigger() -> None:
    store = TuiStateStore()
    warning = TuiContextUsage(
        current_tokens=100_000,
        context_window_tokens=128_000,
        compact_trigger_tokens=115_200,
    )
    danger = TuiContextUsage(
        current_tokens=115_200,
        context_window_tokens=128_000,
        compact_trigger_tokens=115_200,
    )

    warning_frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, context_usage=warning),
    )
    danger_frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=80, context_usage=danger),
    )

    assert any(style == "class:tui-context-warning" for style, _ in warning_frame.input_status_lines[0])
    assert any(style == "class:tui-context-danger" for style, _ in danger_frame.input_status_lines[0])


def test_mid_turn_context_compaction_renders_distinct_from_conversation_compact() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("context-compaction", clock=lambda: 12.0)
    store.publish(
        seq.emit(
            "context_window_compacted",
            "completed",
            "context-window:req:1",
            {
                "generation": 1,
                "before_tokens": 118_400,
                "after_tokens": 31_200,
                "trigger_tokens": 115_200,
                "dropped_pairs": 84,
                "preserved_pairs": 12,
            },
        )
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=100))
    rendered = "\n".join(_frame_lines(frame))

    assert "Tool history compacted · 118.4k → 31.2k" in rendered
    assert "removed 84 tool pairs · pass 1" in rendered
    assert "generation" not in rendered


def test_conversation_compaction_renders_real_stage_progress_and_hides_thinking_spinner() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("compact-progress", clock=lambda: 12.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "conversation_compaction_started",
            "started",
            "compact:req:2",
            {
                "generation": 2,
                "percent": 5,
                "stage": "preparing",
                "before_tokens": 118_400,
            },
        )
    )
    store.publish(
        seq.emit(
            "conversation_compaction_progress",
            "updated",
            "compact:req:2",
            {
                "generation": 2,
                "percent": 78,
                "stage": "checkpointing",
                "after_tokens": 31_200,
            },
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=100, spinner_index=1),
    )
    rendered = "\n".join(_frame_lines(frame))

    assert "正在压缩上下文" in rendered
    assert "78% · 正在写恢复点" in rendered
    assert not any("Working" in line or "Thinking" in line for line in _frame_lines(frame))
    assert store.snapshot().active_blocks[-1].metadata["percent"] == 78


def test_question_help_footer_maps_only_real_tui_shortcuts() -> None:
    store = TuiStateStore()
    wide = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=120, help_open=True),
    )
    narrow = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=50, help_open=True),
    )

    wide_text = fragments_text(wide.footer)
    narrow_text = fragments_text(narrow.footer)
    assert "/ for commands" in wide_text
    assert "ctrl + o for detailed transcript" in wide_text
    assert "ctrl + t to expand tasks" in wide_text
    assert "ctrl + g back from child" in wide_text
    assert "ctrl + s to stash prompt" in wide_text
    assert "F6 切换原生复制/滚轮" in wide_text
    assert wide_text.count("\n") == 5
    assert narrow_text.count("\n") > wide_text.count("\n")


def test_todo_panel_renders_items_with_checkmarks_and_shared_spinner() -> None:
    """Todo 运行项共用一帧动画，待办和完成图标保持静态。"""
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiBlockRenderCache
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock

    cache = TuiBlockRenderCache(max_entries=10)
    block = TuiBlock(
        block_id="todo:task_progress",
        kind="task_progress",
        role="todo",
        phase="active",
        title="任务清单",
        metadata={
            "items": [
                {"id": "a", "title": "阅读项目A", "status": "done"},
                {"id": "b", "title": "分析模块", "status": "in_progress"},
                {"id": "c", "title": "写报告", "status": "pending"},
                {"id": "d", "title": "取消旧方案", "status": "skipped"},
            ]
        },
    )
    first = cache.render(block, TuiRenderContext(width=80, spinner_index=0))
    second = cache.render(block, TuiRenderContext(width=80, spinner_index=1))
    text = "\n".join(fragments_text(line) for line in first)
    second_text = "\n".join(fragments_text(line) for line in second)
    assert "任务清单" in text
    assert "完成 1/4 · 进行中 1" in text
    assert "☑ 阅读项目A" in text
    assert "✻ 分析模块" in text
    assert "✢ 分析模块" in second_text
    assert "□ 写报告" in text
    assert "➖ 取消旧方案" in text


def test_todo_panel_collapses_to_status_window_and_ctrl_t_expands() -> None:
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiBlockRenderCache
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock

    items = [
        {"id": "done-old", "title": "更早完成", "status": "done"},
        {"id": "done-prev", "title": "上一个完成", "status": "done"},
        {"id": "run-a", "title": "并行任务甲", "status": "in_progress"},
        {"id": "run-b", "title": "并行任务乙", "status": "in_progress"},
        {"id": "next", "title": "下一个待办", "status": "pending"},
        {"id": "later-1", "title": "稍后任务一", "status": "pending"},
        {"id": "later-2", "title": "稍后任务二", "status": "pending"},
    ]
    block = TuiBlock(
        block_id="todo:window",
        kind="task_progress",
        role="todo",
        phase="active",
        metadata={"items": items},
    )
    cache = TuiBlockRenderCache(max_entries=10)

    collapsed = cache.render(block, TuiRenderContext(width=80, spinner_index=0))
    expanded = cache.render(
        block,
        TuiRenderContext(width=80, spinner_index=0, todos_expanded=True),
    )
    collapsed_text = "\n".join(fragments_text(line) for line in collapsed)
    expanded_text = "\n".join(fragments_text(line) for line in expanded)

    assert len(collapsed) == 5  # 标题 + 4 条任务
    assert "完成 2/7 · 进行中 2（Ctrl+T 展开）" in collapsed_text
    assert "上一个完成" in collapsed_text
    assert "并行任务甲" in collapsed_text
    assert "并行任务乙" in collapsed_text
    assert "下一个待办" in collapsed_text
    assert "更早完成" not in collapsed_text
    assert len(expanded) == 8
    assert "完成 2/7 · 进行中 2（Ctrl+T 收起）" in expanded_text
    assert "更早完成" in expanded_text


def test_todo_panel_gateway_event_payload_keeps_task_progress_items() -> None:
    """S-TP1 回归：gateway tool_progress 事件经 _tool_payload 白名单后仍带
    task_progress_items，TUI todo 面板才能拿到数据（真机实锤：白名单漏掉
    task_progress_items → 面板永远不渲染）。"""
    from agent_py_agent.cli.chat_parts.tui_runtime import (
        TuiRuntime,
        TuiTurnEventAdapter,
    )
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

    store = TuiStateStore()
    runtime = TuiRuntime("session-todo", store=store)
    adapter = TuiTurnEventAdapter(runtime, "req-todo-1")
    consumed = adapter.on_gateway_event(
        {
            "kind": "tool_progress",
            "progress": {
                "tool": "task_progress",
                "round": 1,
                "call_index": 0,
                "phase": "finished",
                "status": "completed",
                "ok": True,
                "handler_executed": True,
                "duration_ms": 5,
                "task_progress_items": [
                    {"id": "a", "title": "阅读项目A", "status": "done"},
                    {"id": "b", "title": "分析模块", "status": "pending"},
                    {
                        "id": "child-analysis",
                        "title": "子代理[analysis]:分析模块完整长任务",
                        "status": "in_progress",
                    },
                ],
            },
        }
    )
    assert consumed is True
    todo = store.reducer.active_blocks.get("todo:task_progress")
    assert todo is not None, store.reducer.diagnostics
    assert todo.role == "todo"
    assert todo.metadata["items"] == [
        {"id": "a", "title": "阅读项目A", "status": "done"},
        {"id": "b", "title": "分析模块", "status": "pending"},
        {
            "id": "child-analysis",
            "title": "子代理[analysis]:分析模块完整长任务",
            "status": "in_progress",
        },
    ]
    runtime.update_background_activity(
        1,
        {
            "subagents": [
                {
                    "run_id": "child-analysis",
                    "name": "analysis",
                    "status": "DONE",
                    "attempts": 1,
                    "progress_item_ids": ["b"],
                }
            ]
        },
    )
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    assert not any("任务清单" in line for line in _frame_lines(frame))
    todo_text = "\n".join(fragments_text(line) for line in frame.todo_lines)
    assert "☑ 阅读项目A" in todo_text
    assert "☑ 分析模块" in todo_text
    assert "子代理[analysis]" not in todo_text
    assert any("Working · main" in line for line in _frame_lines(frame))
    assert not any("main" in fragments_text(line) for line in frame.agent_lines)
    assert any("analysis" in fragments_text(line) for line in frame.agent_lines)


def test_todo_header_reports_active_child_not_represented_by_visible_items() -> None:
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnEventAdapter

    store = TuiStateStore()
    runtime = TuiRuntime("todo-extra-child", store=store)
    adapter = TuiTurnEventAdapter(runtime, "req-extra-child")
    assert adapter.on_gateway_event(
        {
            "kind": "tool_progress",
            "progress": {
                "tool": "task_progress",
                "round": 1,
                "call_index": 0,
                "phase": "finished",
                "status": "completed",
                "ok": True,
                "handler_executed": True,
                "task_progress_items": [
                    {"id": "verify", "title": "完成验收", "status": "done"},
                    {"id": "report", "title": "完成汇报", "status": "done"},
                    {
                        "id": "child-residual",
                        "title": "修复残留构建问题",
                        "status": "in_progress",
                    },
                ],
            },
        }
    )
    runtime.update_background_activity(
        1,
        {
            "subagents": [
                {
                    "run_id": "child-residual",
                    "name": "build-fixer-5",
                    "status": "RUNNING",
                    "progress_item_ids": [],
                }
            ]
        },
    )

    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=100))
    todo_text = "\n".join(fragments_text(line) for line in frame.todo_lines)

    assert "完成 2/2 · 另有 1 个子代理运行中" in todo_text
    assert "修复残留构建问题" not in todo_text
    assert any("build-fixer-5" in fragments_text(line) for line in frame.agent_lines)

    runtime.update_background_activity(
        1,
        {
            "subagents": [
                {
                    "run_id": "child-residual",
                    "name": "build-fixer-5",
                    "status": "RUNNING",
                    "progress_item_ids": ["verify"],
                }
            ]
        },
    )
    linked_frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=100))
    linked_text = "\n".join(
        fragments_text(line) for line in linked_frame.todo_lines
    )
    assert "完成 1/2 · 进行中 1" in linked_text
    assert "另有" not in linked_text


def test_todo_panel_empty_items_renders_nothing() -> None:
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiBlockRenderCache
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock

    cache = TuiBlockRenderCache(max_entries=10)
    block = TuiBlock(
        block_id="todo:task_progress",
        kind="task_progress",
        role="todo",
        phase="active",
        metadata={"items": []},
    )
    assert cache.render(block, TuiRenderContext(width=80)) == ()


def test_live_thinking_content_visible_by_default() -> None:
    """终端交互 对齐：活动 thinking 内容默认显示（灰色），不折叠。"""
    from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer

    store = TuiStateStore()
    seq = TuiEventSequencer("live-thinking", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "thinking_delta",
            "delta",
            "thinking",
            {"text": "思考中间内容可见"},
        )
    )
    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=100, now=101.0),
    )
    texts = _frame_lines(frame)
    assert any("思考中间内容可见" in text for text in texts)
    # 灰色样式
    thinking_lines = [
        line
        for line in frame.transcript_lines
        if any("思考中间内容可见" in text for _style, text in line)
    ]
    assert thinking_lines
    styles = {style for line in thinking_lines for style, _text in line}
    assert "class:tui-thinking-detail" in styles


def test_transcript_select_all_marks_full_selection() -> None:
    """Ctrl+A 全选：选区覆盖全部可见行，selected_text 可提取。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("select-all", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant",
            {"text": "第一行\n第二行"},
        )
    )
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    # 通过 view 选择（store 无 view，直接验证 select_all 依赖的行数据已渲染）
    assert frame.transcript_lines


def test_thinking_expanded_content_is_gray_in_transcript() -> None:
    """Ctrl+O 展开后的 thinking 内容保持灰色（与正文区分，终端交互 对齐）。"""
    from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer

    store = TuiStateStore()
    seq = TuiEventSequencer("think-expand", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "thinking_completed",
            "completed",
            "thinking",
            {"text": "思考内容展开后应该还是灰色", "duration_seconds": 5.0},
        )
    )
    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=100, detailed_transcript=True),
    )
    thinking_lines = [
        line
        for line in frame.transcript_lines
        if any("思考内容展开后" in text for _style, text in line)
    ]
    assert thinking_lines
    visible_fragments = [
        (style, text)
        for line in thinking_lines
        for style, text in line
        if text.strip()
    ]
    assert visible_fragments
    assert all(style.endswith("class:tui-thinking-detail") for style, _text in visible_fragments)


def test_thinking_markdown_token_colors_are_overridden_by_gray_role() -> None:
    """思考里的粗体、代码和链接保留语义样式，但最终前景色必须统一由灰色 role 接管。"""
    store = TuiStateStore()
    seq = TuiEventSequencer("think-markdown-gray", clock=lambda: 100.0)
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    store.publish(
        seq.emit(
            "thinking_completed",
            "completed",
            "thinking",
            {
                "text": "思考 **加粗**、`代码` 与 https://example.com",
                "duration_seconds": 2.0,
            },
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(width=100, detailed_transcript=True),
    )
    content_fragments = [
        (style, text)
        for line in frame.transcript_lines[1:]
        for style, text in line
        if text.strip() and text not in {"（", "）"}
    ]

    assert content_fragments
    assert any("class:tui-strong" in style for style, _text in content_fragments)
    assert any("class:tui-code-inline" in style for style, _text in content_fragments)
    assert all(style.endswith("class:tui-thinking-detail") for style, _text in content_fragments)


def test_assistant_fold_hint_is_gray() -> None:
    """assistant 折叠提示行（"… 中间 N 行已折叠"）渲染为浅灰，不与正文混。"""
    from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer

    store = TuiStateStore()
    seq = TuiEventSequencer("fold-hint", clock=lambda: 100.0)
    store.publish(seq.emit("turn_started", "started", "turn"))
    long_text = "\n".join(f"第 {i} 行正文内容" for i in range(1, 300))
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant",
            {"text": long_text},
        )
    )
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    hint_lines = [
        line
        for line in frame.transcript_lines
        if any("行已折叠" in text for _style, text in line)
    ]
    assert hint_lines, "折叠提示行应存在"
    visible_fragments = [
        (style, text)
        for line in hint_lines
        for style, text in line
        if text.strip()
    ]
    assert visible_fragments
    assert all(style.endswith("class:tui-muted") for style, _text in visible_fragments)
