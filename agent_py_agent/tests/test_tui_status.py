from __future__ import annotations

import threading
from unittest.mock import patch

from agent_py_agent.cli.chat_parts import tui
from agent_py_agent.cli.chat_parts.tui import TuiStatusRefs, _tui_get_status_text


def test_tui_status_includes_thinking_line_and_elapsed_after_context_percent():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[True],
        pending_jobs_ref=[0],
        running_started_at_ref=[90.0],
        last_token_estimate_ref=[2141],
        thinking_line_ref=["正在整理上下文"],
    )

    with patch.object(tui.time, "perf_counter", return_value=93.2):
        status = _tui_get_status_text(refs, "MiniMax-M2.7")

    assert "[" in status
    assert "1%" in status
    assert status.endswith("正在整理上下文 3.2s")


def test_tui_status_keeps_static_thinking_without_elapsed_when_idle():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_started_at_ref=[0.0],
        last_token_estimate_ref=[0],
        thinking_line_ref=["等待输入"],
    )

    status = _tui_get_status_text(refs, "MiniMax-M2.7")

    assert status.endswith("等待输入")
