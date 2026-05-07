from __future__ import annotations

"""LLM: tests extracted chat helpers so chat.py can keep shrinking safely.

给人看的解释：
这些测试覆盖聊天历史上下文和长回复折叠，防止后续拆分交互代码时改变体验。
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.cli.chat_parts.history import (
    ConversationTurn,
    append_conversation_turn,
    build_history_context,
)
from agent_py_agent.cli.chat_parts.rendering import collapse_response_text, startup_banner


def test_history_context_keeps_recent_turns_in_reverse_order() -> None:
    history: list[tuple[str, str]] = []
    lock = threading.Lock()

    append_conversation_turn(history, lock, ConversationTurn("first", "answer-one"), max_turns=2)
    append_conversation_turn(history, lock, ConversationTurn("second", "answer-two"), max_turns=2)
    append_conversation_turn(history, lock, ConversationTurn("third", "answer-three"), max_turns=2)

    context = build_history_context(history, lock, max_turns=2)

    assert "用户: third" in context
    assert "用户: second" in context
    assert "用户: first" not in context
    assert context.index("用户: third") < context.index("用户: second")


def test_collapse_response_text_returns_preview_for_long_text() -> None:
    text = "\n".join(f"line {index}" for index in range(20))

    preview, collapsed = collapse_response_text(text)

    assert collapsed is True
    assert "line 0" in preview
    assert "..." in preview


def test_startup_banner_marks_gateway_mode() -> None:
    banner = startup_banner("AgentName", use_gateway=True)

    assert "AgentName" in banner
    assert "gateway client" in banner


def test_tui_stream_chunks_are_emitted_immediately(capsys) -> None:
    from agent_py_agent.cli.chat_parts.tui_worker_stream import (
        _append_stream_text,
        _flush_stream_buf,
    )

    pending = [""]
    _append_stream_text("hello", pending)
    first = capsys.readouterr().out
    _append_stream_text(" world", pending)
    second = capsys.readouterr().out
    _flush_stream_buf(pending)
    tail = capsys.readouterr().out

    assert "hello" in first
    assert " world" in second
    assert tail == "\n"


def test_cprint_uses_plain_print_when_stdout_is_not_tty(monkeypatch, capsys):
    """非 TTY 管道下不要调用 prompt_toolkit，避免 Windows console 报错。"""
    from agent_py_agent.cli.chat_parts import rendering

    def fail_print(_text):
        raise AssertionError("prompt_toolkit should not be used for non-tty stdout")

    monkeypatch.setattr(rendering, "_pt_print", fail_print)
    monkeypatch.setattr(rendering, "_PT_ANSI", lambda text: text)

    rendering._cprint("hello")

    assert "hello" in capsys.readouterr().out


def test_tui_stream_chunks_strip_ansi_but_keep_text(capsys):
    from agent_py_agent.cli.chat_parts.tui_worker_stream import _append_stream_text

    pending = [""]
    _append_stream_text("\033[38;2;34;197;94mhello\033[0m", pending)

    out = capsys.readouterr().out
    assert out == "hello"
    assert "\033[" not in out


def test_tui_default_mode_streams_visible_chunks() -> None:
    from agent_py_agent.cli.chat_parts.tui_worker import _make_stream_callbacks

    cfg = SimpleNamespace(
        args=SimpleNamespace(app_scrollback=False),
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="myagent")),
        stream_buf_ref=[""],
        stream_visible_text_ref=[""],
    )
    spinner = SimpleNamespace(stopped=False, stop=lambda: setattr(spinner, "stopped", True))

    with patch("agent_py_agent.cli.chat_parts.rendering._write_stream_text") as mock_write:
        _begin, on_chunk = _make_stream_callbacks(cfg, 1, spinner)
        visible = on_chunk("hello")

    assert visible is True
    assert spinner.stopped is True
    assert cfg.stream_visible_text_ref == ["hello"]
    mock_write.assert_called_once_with("hello")


def test_tui_input_prompt_is_stable_separate_window(tmp_path) -> None:
    pytest.importorskip("prompt_toolkit")
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _make_input_area,
        _make_input_prompt_window,
    )

    input_area = _make_input_area(str(tmp_path / ".chat_history"))
    prompt_window = _make_input_prompt_window()

    assert input_area.window.get_line_prefix is None
    assert prompt_window.width == 2
    assert prompt_window.content.text == [("class:prompt", "❯ ")]


def test_tui_transcript_sink_appends_and_follows(monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    from agent_py_agent.cli.chat_parts import rendering
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _install_transcript_sink,
        _make_transcript_area,
    )

    area = _make_transcript_area()
    follow = [True]
    app = type("App", (), {"invalidated": False, "invalidate": lambda self: setattr(self, "invalidated", True)})()

    _install_transcript_sink(area, follow, [app])
    try:
        rendering._cprint("\033[38;2;34;197;94mhello\033[0m")
    finally:
        rendering.set_tui_output_sink(None)
        rendering.set_tui_stream_sink(None)

    assert area.text == "hello\n"
    assert area.buffer.cursor_position == len(area.text)
    assert app.invalidated is True


def test_tui_transcript_stream_stays_live_until_finished(monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        TuiTranscriptStore,
        _make_transcript_area,
    )

    area = _make_transcript_area()
    follow = [True]
    app = type("App", (), {"invalidate": lambda self: None})()
    store = TuiTranscriptStore(area, follow, [app])

    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.tui_transcript_store.time.monotonic",
        lambda: 100.0,
    )
    store.append_history("myagent#1>\n")
    store.append_stream("hello")

    assert area.text == "myagent#1>\nhello"
    assert store.history == "myagent#1>\n"
    assert store.live_stream == "hello"

    store.finish_stream()

    assert area.text == "myagent#1>\nhello"
    assert store.history == "myagent#1>\nhello"
    assert store.live_stream == ""
