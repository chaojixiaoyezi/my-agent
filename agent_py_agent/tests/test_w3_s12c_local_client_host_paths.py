"""S12c: the local TUI, plain chat and `my-agent run` clients keep full host paths; credential masking is unchanged.

The gateway already decides path display per viewer. The local clients re-project text before showing it and used an
empty channel, which shortened every absolute path to its last segment. They now name their own local private channel.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary

_PATH = "/private/tmp/w3-clone/workspaces/a1"
# 形似 GitHub token 的合成值，只用来证明完整路径旁的凭据仍被遮蔽。
_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_TEXT = f"CLONE-MM-OK {_PATH}\ngateway_workspace: /Users/alice/.my-agent/gateway\ntoken {_TOKEN}"


# 函数用途: 断言一段已上屏正文保留完整路径且遮蔽凭据。
def _assert_full_and_masked(text: str) -> None:
    assert f"CLONE-MM-OK {_PATH}" in text, text
    assert "gateway_workspace: /Users/alice/.my-agent/gateway" in text, text
    assert _TOKEN not in text, text


# 函数用途: 取 TUI 当前全部助手块正文。
def _assistant_text(runtime: TuiRuntime) -> str:
    snapshot = runtime.store.snapshot()
    return "".join(block.text for block in (*snapshot.stable_blocks, *snapshot.active_blocks)
                   if block.role == "assistant")


def test_tui_gateway_final_keeps_the_full_path():
    runtime = TuiRuntime("session-s12c-final")
    runtime.enqueue_prompt("request-s12c", "gateway", queued=False)
    turn = runtime.begin_turn("request-s12c")
    assert turn.on_gateway_event({"kind": "assistant_final", "text": _TEXT, "model_source": {"call_id": "call-s12c"}})
    runtime.complete_turn("request-s12c", TuiTurnSummary(response_text=_TEXT, ok=True))
    _assert_full_and_masked(_assistant_text(runtime))


def test_tui_local_committed_reply_and_public_stream_keep_the_full_path():
    runtime = TuiRuntime("session-s12c-local")
    turn = runtime.begin_turn("request-s12c-local")
    turn.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-s12c-local"},
                                "boundary": "terminal", "messages": [{"text": _TEXT, "phase": "final_answer"}],
                                "final_text": _TEXT})
    _assert_full_and_masked(_assistant_text(runtime))
    streamed = TuiRuntime("session-s12c-stream")
    stream_turn = streamed.begin_turn("request-s12c-stream")
    assert stream_turn.write_model(_TEXT, public_stream=True)
    _assert_full_and_masked(_assistant_text(streamed))


def test_plain_chat_final_expand_and_commentary_keep_the_full_path(capsys):
    from agent_py_agent.cli.chat_parts.plain_ui import (
        AssistantResponseRenderRequest,
        _handle_expand_command,
        _make_chunk_handler,
        _render_assistant_response,
    )

    history: list[str] = []
    _render_assistant_response(AssistantResponseRenderRequest(_TEXT, history, "agent", preview_lines=50,
                                                              preview_chars=5000))
    _handle_expand_command("/expand last", history)
    callback, _streamed = _make_chunk_handler("agent", 1, committed_model_only=True)
    callback.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-s12c-plain"},
                                    "boundary": "tools", "messages": [{"text": _TEXT}], "final_text": ""})
    output = capsys.readouterr().out
    assert output.count(f"CLONE-MM-OK {_PATH}") == 3, output
    _assert_full_and_masked(output)
    _assert_full_and_masked(history[0])


def test_cli_run_final_and_commentary_keep_the_full_path(capsys):
    from agent_py_agent.cli.run_output import _unstreamed_final_response, make_run_chunk_writer

    result = SimpleNamespace(response=_TEXT, model_response=_TEXT)
    _assert_full_and_masked(_unstreamed_final_response(result, ""))
    assert _unstreamed_final_response(result, _unstreamed_final_response(result, "")) == ""
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {"text": "", "model_text": ""})
    writer.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-s12c-run"},
                                  "boundary": "tools", "messages": [{"text": _TEXT}], "final_text": ""})
    _assert_full_and_masked(capsys.readouterr().out)
