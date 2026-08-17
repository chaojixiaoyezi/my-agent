from __future__ import annotations

import threading
from unittest.mock import patch

from agent_py_agent.cli.chat_parts import tui
from agent_py_agent.cli.chat_parts.tui import (
    TuiStatusRefs,
    _format_tokens_compact,
    _tui_status_fragments,
)


def _fragment_text(fragments):
    return "".join(text for _style, text in fragments)


def test_status_shows_brand_model_workspace_when_idle():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_started_at_ref=[0.0],
        last_token_estimate_ref=[0],
        thinking_line_ref=[""],
    )
    fragments = _tui_status_fragments(refs, "MiniMax-M2.7", workspace="my-agent-dsh")
    text = _fragment_text(fragments)
    assert "my-agent" in text
    assert "MiniMax-M2.7" in text
    assert "my-agent-dsh" in text
    assert "ctx" not in text  # 无 token 不显示


def test_status_shows_spinner_elapsed_and_activity_while_running():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[True],
        pending_jobs_ref=[0],
        running_started_at_ref=[90.0],
        last_token_estimate_ref=[2141],
        thinking_line_ref=["正在整理上下文"],
    )
    with patch.object(tui.time, "perf_counter", return_value=93.2):
        fragments = _tui_status_fragments(refs, "MiniMax-M2.7", workspace="w")
    text = _fragment_text(fragments)
    assert "3.2s" in text
    assert "正在整理上下文" in text
    assert any(style == "class:activity" for style, _ in fragments)


def test_status_compact_token_suffix():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_started_at_ref=[0.0],
        last_token_estimate_ref=[2141],
        thinking_line_ref=[""],
    )
    fragments = _tui_status_fragments(refs, "m", workspace="w")
    text = _fragment_text(fragments)
    assert "⟿" in text
    assert "2.14K" in text


def test_format_tokens_compact_matches_codex_convention():
    assert _format_tokens_compact(0) == "0"
    assert _format_tokens_compact(999) == "999"
    assert _format_tokens_compact(2141) == "2.14K"
    assert _format_tokens_compact(8500) == "8.50K"
    assert _format_tokens_compact(123456) == "123K"
    assert _format_tokens_compact(3450000) == "3.45M"


def test_tui_response_state_uses_current_context_tokens():
    from agent_py_agent.cli.chat_parts.tui_worker_stream import _update_response_state

    token_ref = [0]

    text = _update_response_state(
        {"response": "ok", "prompt_token_estimate": 1200, "cumulative_token_estimate": 9400},
        threading.Lock(),
        token_ref,
    )

    assert text == "ok"
    assert token_ref[0] == 1200
