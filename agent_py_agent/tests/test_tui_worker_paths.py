from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.cli.chat_parts.tui_worker_paths import _finish_gateway_response


def test_finish_gateway_response_does_not_reprint_streamed_text():
    cfg = SimpleNamespace(
        state_lock=threading.Lock(),
        last_token_estimate_ref=[0],
        assistant_outputs=[],
        stream_visible_text_ref=["streamed final"],
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="myagent")),
    )
    ctx = SimpleNamespace(cfg=cfg, job=SimpleNamespace(show_prompt=False), started_at=10.0)
    response = {"ok": True, "response": "streamed final", "prompt_token_estimate": 42}

    with patch("agent_py_agent.cli.chat_parts.tui_worker_paths.time.perf_counter", return_value=11.5), \
         patch("agent_py_agent.cli.chat_parts.rendering._cprint") as mock_print:
        text, recorded = _finish_gateway_response(ctx, "gw-hidden", response, True)

    assert text == "streamed final"
    assert recorded is True
    assert cfg.assistant_outputs == ["streamed final"]
    printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
    assert "streamed final" not in printed
    assert "gateway_request" not in printed
    assert "工具轮数" in printed


def test_finish_gateway_response_reprints_when_stream_text_missing(capsys):
    cfg = SimpleNamespace(
        state_lock=threading.Lock(),
        last_token_estimate_ref=[0],
        assistant_outputs=[],
        stream_visible_text_ref=[""],
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="myagent")),
    )
    ctx = SimpleNamespace(cfg=cfg, job=SimpleNamespace(show_prompt=False), started_at=10.0)
    response = {
        "ok": True,
        "response": "你好，有什么可以帮你的？",
        "prompt_token_estimate": 42,
    }

    with patch("agent_py_agent.cli.chat_parts.tui_worker_paths.time.perf_counter", return_value=11.5), \
         patch("agent_py_agent.cli.chat_parts.rendering._cprint") as mock_print:
        text, recorded = _finish_gateway_response(ctx, "gw-hidden", response, True)

    assert text == "你好，有什么可以帮你的？"
    assert recorded is True
    assert cfg.assistant_outputs == ["你好，有什么可以帮你的？"]
    assert "你好，有什么可以帮你的？" in capsys.readouterr().out
    printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
    assert "工具轮数" in printed
