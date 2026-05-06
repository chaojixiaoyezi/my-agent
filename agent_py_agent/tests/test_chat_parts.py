from __future__ import annotations

"""LLM: tests extracted chat helpers so chat.py can keep shrinking safely.

给人看的解释：
这些测试覆盖聊天历史上下文和长回复折叠，防止后续拆分交互代码时改变体验。
"""

import threading

from agent_py_agent.cli.chat_parts.history import append_conversation_turn, build_history_context
from agent_py_agent.cli.chat_parts.rendering import collapse_response_text, startup_banner


def test_history_context_keeps_recent_turns_in_reverse_order() -> None:
    history: list[tuple[str, str]] = []
    lock = threading.Lock()

    append_conversation_turn(history, lock, "first", "answer-one", max_turns=2)
    append_conversation_turn(history, lock, "second", "answer-two", max_turns=2)
    append_conversation_turn(history, lock, "third", "answer-three", max_turns=2)

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


def test_cprint_uses_plain_print_when_stdout_is_not_tty(monkeypatch, capsys):
    """非 TTY 管道下不要调用 prompt_toolkit，避免 Windows console 报错。"""
    from agent_py_agent.cli.chat_parts import rendering

    def fail_print(_text):
        raise AssertionError("prompt_toolkit should not be used for non-tty stdout")

    monkeypatch.setattr(rendering, "_pt_print", fail_print)
    monkeypatch.setattr(rendering, "_PT_ANSI", lambda text: text)

    rendering._cprint("hello")

    assert "hello" in capsys.readouterr().out
