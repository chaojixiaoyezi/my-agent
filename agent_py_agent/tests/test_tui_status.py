from __future__ import annotations

import threading

from agent_py_agent.cli.chat_parts.tui import TuiStatusRefs, _tui_get_status_text


def test_tui_status_includes_thinking_line_after_context_percent():
    refs = TuiStatusRefs(
        state_lock=threading.Lock(),
        is_running_ref=[True],
        pending_jobs_ref=[0],
        running_started_at_ref=[0.0],
        last_token_estimate_ref=[2141],
        thinking_line_ref=["正在整理上下文"],
    )

    status = _tui_get_status_text(refs, "MiniMax-M2.7")

    assert "[" in status
    assert "1%" in status
    assert status.endswith("正在整理上下文")
