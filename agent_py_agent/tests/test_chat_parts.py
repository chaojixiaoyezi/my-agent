from __future__ import annotations

from types import SimpleNamespace as _StoreDomain

"""LLM: tests extracted chat helpers so chat.py can keep shrinking safely.

给人看的解释：
这些测试覆盖聊天历史上下文和长回复折叠，防止后续拆分交互代码时改变体验。
"""

import threading
from contextlib import nullcontext
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.cli.chat_parts.history import (
    ConversationTurn,
    append_conversation_turn,
    build_history_context,
    load_gateway_chat_history,
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


def test_history_context_uses_configured_assistant_preview_chars() -> None:
    history = [("question", "abcdefghijklmnopqrstuvwxyz")]
    lock = threading.Lock()

    context = build_history_context(
        history,
        lock,
        max_turns=1,
        assistant_preview_chars=7,
    )

    assert "助手: abcdefg" in context
    assert "hijklmnop" not in context


def test_history_append_trims_to_configured_turn_count() -> None:
    history: list[tuple[str, str]] = []
    lock = threading.Lock()

    append_conversation_turn(history, lock, ConversationTurn("first", "a"), max_turns=2)
    append_conversation_turn(history, lock, ConversationTurn("second", "b"), max_turns=2)
    append_conversation_turn(history, lock, ConversationTurn("third", "c"), max_turns=2)

    assert history == [("second", "b"), ("third", "c")]


def test_gateway_history_restore_uses_only_complete_foreground_cli_turns() -> None:
    rows = [
        SimpleNamespace(
            channel="cli_chat",
            role="user",
            content="第一问",
            metadata={"gateway_request_id": "req-1"},
        ),
        SimpleNamespace(
            channel="cli_chat",
            role="assistant",
            content="第一答",
            metadata={"gateway_request_id": "req-1"},
        ),
        SimpleNamespace(
            channel="cli_chat",
            role="user",
            content="残缺问题",
            metadata={"gateway_request_id": "req-incomplete"},
        ),
        SimpleNamespace(
            channel="cli_chat",
            role="user",
            content="后台任务触发",
            metadata={"gateway_request_id": "req-background"},
        ),
        SimpleNamespace(
            channel="cli_chat",
            role="assistant",
            content="后台审计消息",
            metadata={
                "gateway_request_id": "req-background",
                "task_id": "task-audit",
                "reason": "audit_finding",
                "background_delivery_reason": "scheduled",
            },
        ),
        SimpleNamespace(
            channel="feishu",
            role="user",
            content="其它通道问题",
            metadata={"gateway_request_id": "req-other-channel"},
        ),
        SimpleNamespace(
            channel="feishu",
            role="assistant",
            content="其它通道回答",
            metadata={"gateway_request_id": "req-other-channel"},
        ),
    ]

    class Store:
        def __init__(self, *args, **kwargs):
            self.threads = _StoreDomain(resolve_report=self._fake_resolve_thread_report)
            self.messages = _StoreDomain(history_page_report=self._fake_history_page_report)

        def _fake_resolve_thread_report(self, **kwargs):
            assert kwargs == {
                "channel": "chat",
                "channel_conversation_id": "sess-resume",
                "channel_user_id": "local-agent",
            }
            return SimpleNamespace(thread_id="thread-resume"), None

        def _fake_history_page_report(self, thread_id, *, before, limit):
            assert thread_id == "thread-resume"
            assert before is None
            assert limit == 16
            return SimpleNamespace(rows=rows, errors=(), after=len(rows), before=0)

    for index, row in enumerate(rows):
        row.message_id = f"msg-{index}"
        row.thread_id = "thread-resume"
    snapshot = load_gateway_chat_history(
        SimpleNamespace(conversation_store=Store()),
        "sess-resume",
        max_turns=2,
    )

    assert snapshot.thread_id == "thread-resume"
    assert snapshot.turns == (("第一问", "第一答"),)
    assert snapshot.load_errors == ()


def test_gateway_history_restore_preserves_structured_load_error() -> None:
    load_error = {"error_code": "jsonl_corrupt", "line_number": 3}

    class Store:
        def __init__(self, *args, **kwargs):
            self.threads = _StoreDomain(resolve_report=self._fake_resolve_thread_report)
            self.messages = _StoreDomain(history_page_report=self._fake_history_page_report)

        def _fake_resolve_thread_report(self, **_kwargs):
            return SimpleNamespace(thread_id="thread-bad"), None

        def _fake_history_page_report(self, _thread_id, *, before, limit):
            assert before is None
            assert limit == 16
            return SimpleNamespace(rows=[], errors=(load_error,), after=0, before=0)

    snapshot = load_gateway_chat_history(
        SimpleNamespace(conversation_store=Store()),
        "sess-bad",
        max_turns=2,
    )

    assert snapshot.turns == ()
    assert snapshot.load_errors == (load_error,)


def test_plain_turn_inject_skips_client_history_for_gateway() -> None:
    from agent_py_agent.cli.chat_parts.plain_handlers import PlainJobContext, _turn_inject

    history_calls: list[str] = []
    ctx = PlainJobContext(
        job=SimpleNamespace(inject=["本轮附加"]),
        agent=object(),
        args=object(),
        paths=object(),
        assistant_outputs=[],
        build_history_context=lambda: history_calls.append("called") or "旧历史",
    )

    gateway_inject = _turn_inject(ctx, include_history=False)
    direct_inject = _turn_inject(ctx, include_history=True)

    assert history_calls == ["called"]
    assert gateway_inject[0] == "本轮附加"
    assert "旧历史" not in gateway_inject
    assert direct_inject[-1] == "旧历史"


def test_gateway_timing_reports_current_context_tokens_before_cumulative_ledger() -> None:
    from agent_py_agent.cli.chat_parts.gateway_client import (
        GatewayTimingContext,
        format_gateway_timing,
    )

    timing = format_gateway_timing(
        GatewayTimingContext(
            request_id="req-1",
            elapsed=1.25,
            response={
                "tool_rounds": 2,
                "current_context_token_estimate": 1200,
                "prompt_token_estimate": 1100,
                "cumulative_token_estimate": 9_400_000,
            },
            use_gateway=True,
        )
    )

    assert "ctx_tokens~1200" in timing
    assert "prompt_tokens~1100" in timing


def test_collapse_response_text_returns_preview_for_long_text() -> None:
    text = "\n".join(f"line {index}" for index in range(20))

    preview, collapsed = collapse_response_text(text)

    assert collapsed is True
    assert "line 0" in preview
    assert "..." in preview


def test_plain_stream_shows_tool_progress_after_long_response_is_collapsed(capsys) -> None:
    from agent_py_agent.cli.chat_parts.plain_ui import _make_chunk_handler

    on_chunk, _started = _make_chunk_handler("agent", 1, preview_chars=5)

    assert on_chunk("abcdef") is True
    assert on_chunk("[工具] #1 read_file 开始 path=report.md") is True

    out = capsys.readouterr().out
    assert "[回复较长，后续内容已折叠" in out
    assert "[工具] #1 read_file 开始" in out


def test_startup_banner_marks_gateway_mode() -> None:
    banner = startup_banner("AgentName", use_gateway=True)

    assert "AgentName" in banner
    assert "gateway client" in banner


def test_plain_eof_waits_for_queued_work_before_shutdown() -> None:
    from agent_py_agent.cli.chat_parts.plain import run_plain
    from agent_py_agent.cli.chat_parts.plain_state import RunPlainConfig

    jobs = MagicMock(spec=Queue)
    session_manager = MagicMock()
    cfg = RunPlainConfig(
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="myagent")),
        args=SimpleNamespace(memory_limit=None),
        use_gateway=True,
        paths=SimpleNamespace(),
        runtime_inject=[],
        prompt_files=[],
        conversation_history=[],
        history_lock=threading.Lock(),
        jobs=jobs,
        state_lock=threading.Lock(),
        build_history_context=lambda: "",
        session_manager=session_manager,
        current_session_id="sess-piped",
    )

    with (
        patch("agent_py_agent.cli.chat_parts.plain._start_plain_worker"),
        patch("agent_py_agent.cli.chat_parts.plain._print_plain_banner"),
        patch(
            "agent_py_agent.cli.chat_parts.plain._read_plain_user",
            side_effect=["执行这个任务", None],
        ),
    ):
        result = run_plain(cfg)

    assert result == 0
    jobs.put.assert_called_once()
    jobs.join.assert_called_once()
    session_manager.touch_session.assert_called_once_with("sess-piped", channel="chat")


def test_plain_explicit_exit_does_not_run_shutdown_twice() -> None:
    from agent_py_agent.cli.chat_parts.plain import run_plain
    from agent_py_agent.cli.chat_parts.plain_state import RunPlainConfig

    cfg = RunPlainConfig(
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="myagent")),
        args=SimpleNamespace(memory_limit=None),
        use_gateway=True,
        paths=SimpleNamespace(),
        runtime_inject=[],
        prompt_files=[],
        conversation_history=[],
        history_lock=threading.Lock(),
        jobs=MagicMock(spec=Queue),
        state_lock=threading.Lock(),
        build_history_context=lambda: "",
        session_manager=MagicMock(),
        current_session_id="sess-exit",
    )

    with (
        patch("agent_py_agent.cli.chat_parts.plain._start_plain_worker"),
        patch("agent_py_agent.cli.chat_parts.plain._print_plain_banner"),
        patch(
            "agent_py_agent.cli.chat_parts.plain._read_plain_user",
            return_value="/exit",
        ),
        patch("agent_py_agent.cli.chat_parts.plain._wait_for_exit") as wait_for_exit,
    ):
        result = run_plain(cfg)

    assert result == 0
    wait_for_exit.assert_called_once()


def test_cprint_uses_plain_print_when_stdout_is_not_tty(monkeypatch, capsys):
    """非 TTY 管道下不要调用 prompt_toolkit，避免 Windows console 报错。"""
    from agent_py_agent.cli.chat_parts import rendering

    def fail_print(_text):
        raise AssertionError("prompt_toolkit should not be used for non-tty stdout")

    monkeypatch.setattr(rendering, "_pt_print", fail_print)
    monkeypatch.setattr(rendering, "_PT_ANSI", lambda text: text)

    rendering._cprint("hello")

    assert "hello" in capsys.readouterr().out


def test_tui_input_prompt_is_stable_separate_window(tmp_path) -> None:
    pytest.importorskip("prompt_toolkit")
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _make_input_area,
        _make_input_prompt_window,
    )

    input_area = _make_input_area(
        str(tmp_path / ".chat_history"),
        TuiRuntime("input-test"),
        tmp_path,
    )
    prompt_window = _make_input_prompt_window()

    assert input_area.window.get_line_prefix is None
    assert prompt_window.width == 2
    assert prompt_window.content.text == [("class:tui-input-marker", "❯ ")]


def test_tui_escape_timeouts_keep_meta_window_short() -> None:
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        ESCAPE_SEQUENCE_TIMEOUT_SECONDS,
        TERMINAL_ESCAPE_PREFIX_TIMEOUT_SECONDS,
        _configure_escape_timeouts,
    )

    application = SimpleNamespace(timeoutlen=1.0, ttimeoutlen=0.5)

    _configure_escape_timeouts(application)

    assert application.timeoutlen == ESCAPE_SEQUENCE_TIMEOUT_SECONDS == 0.1
    assert application.ttimeoutlen == TERMINAL_ESCAPE_PREFIX_TIMEOUT_SECONDS == 0.05


def test_tui_loop_exit_stops_client_and_prints_exact_resume_command(
    monkeypatch,
    capsys,
) -> None:
    """普通退出结束当前 TUI，但保留 durable session 并给出精确恢复命令。"""
    from agent_py_agent.cli.chat_parts import tui

    refresh_stop = threading.Event()
    stop_event = threading.Event()
    session_manager = MagicMock()
    app = SimpleNamespace(run=lambda *, pre_run: 0)
    monkeypatch.setattr(tui, "patch_stdout", lambda: nullcontext())

    result = tui._run_tui_loop(
        tui.TuiLoopContext(
            app=app,
            refresh_stop=refresh_stop,
            stop_event=stop_event,
            session_manager=session_manager,
            current_session_id="sess-exit-resume",
        )
    )

    assert result == 0
    assert refresh_stop.is_set()
    assert stop_event.is_set()
    session_manager.touch_session.assert_called_once_with(
        "sess-exit-resume",
        channel="chat",
    )
    output = capsys.readouterr().out
    assert "已退出界面" in output
    assert "my-agent resume sess-exit-resume" in output
