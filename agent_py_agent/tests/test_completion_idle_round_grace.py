"""auto-closeout 空转轮判据(终端应用 对齐:模型还在动手就不当轮抢收口)。

真机形态(A1-u1 整合轮):第一轮写完 README 当轮即被自动收口切走,后续建造再没机会发生。
契约:收口提示已注入后,①本轮有写/跑/派/记账 → 不收口(继续让模型干);②本轮纯读看(空转)
→ 收口;③模型自己列的 task_progress 还有开放项 → 无论如何不当轮收口。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    _round_delivery_auto_closeout_ready,
)
from agent.task_progress import write_task_progress

_HINT = "[delivery-completion-soft-hint]\n{}"


def _request(tmp_path, executed_tools, run_id="run-grace-1"):
    params = SimpleNamespace(
        executed_tools=list(executed_tools),
        tool_context=[_HINT],
        delivery_contract=None,
        run_id=run_id,
        archive_tool_calls=[],
    )
    agent = SimpleNamespace(root=tmp_path, home_paths=None)
    return ToolRoundCompletionRequest(
        agent=agent,
        params=params,
        response=SimpleNamespace(backend="test"),
        before_executed_count=0,
        subagent_output_written=False,
    )


def test_active_write_round_blocks_auto_close(tmp_path):
    assert _round_delivery_auto_closeout_ready(_request(tmp_path, ["write_file"])) is False


def test_active_run_command_round_blocks_auto_close(tmp_path):
    assert _round_delivery_auto_closeout_ready(_request(tmp_path, ["run_command"])) is False


def test_idle_read_round_allows_auto_close(tmp_path):
    assert _round_delivery_auto_closeout_ready(_request(tmp_path, ["read_file"])) is True


def test_open_task_progress_blocks_even_idle_round(tmp_path):
    write_task_progress(
        tmp_path, "run-grace-1", {"items": [{"id": "backend", "title": "建后端", "status": "in_progress"}]}
    )
    assert _round_delivery_auto_closeout_ready(_request(tmp_path, ["read_file"])) is False


def test_closed_task_progress_allows_idle_auto_close(tmp_path):
    write_task_progress(
        tmp_path, "run-grace-1", {"items": [{"id": "backend", "title": "建后端", "status": "done", "notes": "跑通"}]}
    )
    assert _round_delivery_auto_closeout_ready(_request(tmp_path, ["read_file"])) is True
