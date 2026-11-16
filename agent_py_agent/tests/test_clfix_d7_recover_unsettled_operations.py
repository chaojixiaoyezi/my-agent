"""D-7: with nothing blocking, /recover still lists this thread's unsettled UNKNOWN tool operations (read-only)
instead of only answering that there is nothing to recover."""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import turn_recovery_control
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

THREAD = "thread-d7"
VIEW = parse_conversation_control("/recover")


@pytest.fixture
def world(tmp_path):
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    owner = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    root = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-main", role="main",
                                    conversation_task_id="gwreq-d7", thread_id=THREAD)
    other = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-other", role="main",
                                     conversation_task_id="gwreq-d7-other", thread_id="thread-other")
    return SimpleNamespace(repo=repo, owner=owner, root=root, other=other,
                           thread=SimpleNamespace(thread_id=THREAD, workspace_task_id=""))


def _operation(world, run, *, status="UNKNOWN", settled_at=0.0, age=0.0, tool="run_command") -> str:
    operation_id = f"tool_operation:{uuid.uuid4().hex}"
    now = time.time() - age
    with world.repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, settled_at, created_at, updated_at) "
            "VALUES(?,?,?,1,1,?,?,?,?,?,?)",
            (operation_id, run["agent_run_id"], run["attempt_id"], tool, status, now, settled_at, now, now),
        )
    return operation_id


def _rows(world):
    return world.repo._runtime_connect().execute(
        "SELECT operation_id, status, settled_at, updated_at FROM tool_operations ORDER BY operation_id").fetchall()


def test_no_block_lists_unsettled_unknown_operations_without_writing(world):
    listed = [_operation(world, world.root, age=20, tool="scripts"), _operation(world, world.root, age=10)]
    hidden = [
        _operation(world, world.root, settled_at=time.time()),
        _operation(world, world.root, status="EXECUTING"),
        _operation(world, world.other),
    ]
    before = _rows(world)
    result = execute_turn_recovery_control(world.owner, world.thread, VIEW)
    assert result.ok and result.message.startswith(turn_recovery_control._NO_BLOCK)
    assert "另有 2 条工具操作结果未知" in result.message and "scripts" in result.message
    assert result.details == {"unsettled_operation_count": 2, "unsettled_operation_ids": listed[::-1]}
    assert all(operation_id not in result.message for operation_id in hidden)
    assert _rows(world) == before


def test_long_backlog_is_capped_newest_first(world, monkeypatch):
    monkeypatch.setattr(turn_recovery_control, "_UNSETTLED_OPERATION_LIST_MAX_COUNT", 3)
    created = [_operation(world, world.root, age=100 - number) for number in range(5)]
    result = execute_turn_recovery_control(world.owner, world.thread, VIEW)
    assert result.details["unsettled_operation_count"] == 5
    assert result.details["unsettled_operation_ids"] == created[::-1][:3]
    assert "仅列出最近 3 条" in result.message


def test_clean_thread_keeps_the_plain_reply(world):
    _operation(world, world.other)
    result = execute_turn_recovery_control(world.owner, world.thread, VIEW)
    assert (result.ok, result.message, result.details) == (True, turn_recovery_control._NO_BLOCK, None)
