from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.cli.local_status_payload import StatusPayloadContext, build_status_payload
from agent_py_agent.cli.local_status_view import StatusPrintContext, print_status_human
from agent_py_agent.cli.subagents import cmd_subagents


def test_status_payload_surfaces_shared_progress_and_failure_handoff(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(goal="处理失败任务", thought="留下进度面板线索。", plan=["保存"])
    task.status = "FAILED"
    task.failure_type = "tool_output_context_overflow"
    task.latest_summary = "工具输出太大，已经停止展开。"
    task.current_step = "等待接管"
    manager.save(task)
    board = manager.build_board(recent_limit=5)

    payload = build_status_payload(_status_payload_context(tmp_path, store, manager, board))

    panels = payload["subagents"]["shared_progress"]
    assert panels[0]["root_task_id"] == task.id
    assert panels[0]["run_count"] == 1
    assert panels[0]["failure_handoff_refs"] == [task.failure_handoff_json]
    assert panels[0]["takeover_readiness_refs"] == [task.takeover_readiness_json]


def test_status_human_prints_shared_progress_failure_handoff(tmp_path, capsys) -> None:
    panel = {
        "root_task_id": "root-1",
        "run_count": 2,
        "blocked_count": 1,
        "failure_handoff_refs": ["reports/failure_handoff.json"],
        "takeover_readiness_refs": ["reports/takeover_readiness.json"],
    }
    ctx = StatusPrintContext(
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="demo", subagent_board_limit=5), root=tmp_path),
        paths=SimpleNamespace(root=tmp_path),
        local_stats={"record_count": 0, "event_count": 0, "fts5_enabled": True, "db_path": str(tmp_path / "db.sqlite")},
        board=SimpleNamespace(summary={"total": 1}, hot_list=[], recent=[], shared_progress=[panel]),
        timeline=[],
        gateway_status="stopped",
        pid=None,
        alive=False,
        heartbeat_age=0.0,
        active_work_summary=None,
        request_counts={},
        suggested_actions=[],
    )

    print_status_human(ctx)

    output = capsys.readouterr().out
    assert "Shared Progress" in output
    assert "root-1 runs=2 blocked=1 failure_handoffs=1 takeover_packets=1" in output


def test_subagents_board_prints_shared_progress_panel(tmp_path) -> None:
    args = SimpleNamespace(limit=10, all=False, status=None, owner=None, root_id=None)
    panel = {
        "root_task_id": "root-1",
        "run_count": 2,
        "blocked_count": 1,
        "failure_handoff_refs": ["reports/failure_handoff.json"],
        "takeover_readiness_refs": ["reports/takeover_readiness.json"],
    }
    board = SimpleNamespace(summary={"total": 0}, hot_list=[], recent=[], items=[], shared_progress=[panel])
    agent = SimpleNamespace(subagents=SimpleNamespace(write_board=lambda options: board, workspace=tmp_path))

    stdout = StringIO()
    with redirect_stdout(stdout):
        with _patch_board_agent(agent):
            result = cmd_subagents(args)

    assert result == 0
    assert "Shared Progress" in stdout.getvalue()
    assert "root-1 runs=2 blocked=1 failure_handoffs=1 takeover_packets=1" in stdout.getvalue()


def _status_payload_context(tmp_path, store, manager, board) -> StatusPayloadContext:
    agent = SimpleNamespace(
        config=SimpleNamespace(agent_name="demo", subagent_board_limit=5),
        root=tmp_path,
        local_store=store,
        subagents=manager,
    )
    return StatusPayloadContext(
        agent=agent,
        paths=SimpleNamespace(root=tmp_path),
        local_stats={"record_count": 0, "event_count": 0, "fts5_enabled": True, "db_path": str(tmp_path / "db.sqlite")},
        board=board,
        timeline=[],
        pid=None,
        alive=False,
        gateway_status="stopped",
        heartbeat_age=0.0,
        active_work_summary=None,
        request_counts={},
    )


def _patch_board_agent(agent):
    from unittest.mock import patch

    return patch("agent_py_agent.cli._board.make_agent", return_value=agent)
