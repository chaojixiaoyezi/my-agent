from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool, pty_session_registry
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")


@pytest.fixture(autouse=True)
def _clear_pty_sessions():
    pty_session_registry.clear()
    yield
    pty_session_registry.clear()


def _tool(root: Path) -> TerminalSessionTool:
    shell = ShellTool(root, options=ShellToolOptions(workspace_roots=[root]))
    return TerminalSessionTool(shell)


def _payload(result):
    assert result.ok, result.output
    return json.loads(result.output)


def test_terminal_session_runs_real_interactive_pty(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    command = f"{shlex.quote(sys.executable)} -q"
    run_scope = {"owner_id": "owner-a", "session_id": "thread-a"}
    started = _payload(
        tool.execute({"action": "start", "command": command, "__run_scope": run_scope})
    )
    assert "pid" not in started

    listed = _payload(tool.execute({"action": "list", "__run_scope": run_scope}))
    assert listed["count"] == 1
    assert listed["sessions"][0]["session_id"] == started["session_id"]
    assert "pid" not in listed["sessions"][0]

    resized = _payload(
        tool.execute(
            {
                "action": "resize",
                "session_id": started["session_id"],
                "columns": 100,
                "rows": 30,
                "__run_scope": run_scope,
            }
        )
    )
    assert (resized["columns"], resized["rows"]) == (100, 30)

    written = _payload(
        tool.execute(
            {
                "action": "write",
                "session_id": started["session_id"],
                "data": "print(6 * 7)",
                "append_newline": True,
                "__run_scope": run_scope,
            }
        )
    )
    assert written["bytes"] > 0

    deadline = time.time() + 5
    output = ""
    cursor = 0
    while time.time() < deadline and "42" not in output:
        time.sleep(0.05)
        read = _payload(
            tool.execute(
                {
                    "action": "read",
                    "session_id": started["session_id"],
                    "cursor": cursor,
                    "__run_scope": run_scope,
                }
            )
        )
        cursor = read["cursor"]
        output += read["output"]

    assert "42" in output
    closed = _payload(
        tool.execute(
            {
                "action": "close",
                "session_id": started["session_id"],
                "__run_scope": run_scope,
            }
        )
    )
    assert closed["status"] == "closed"


def test_terminal_session_reuses_shell_command_policy(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "start", "command": "rm -rf /"})

    assert result.ok is False
    assert result.error_code == "COMMAND_POLICY_BLOCKED"


def test_terminal_session_requires_existing_session(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "read", "session_id": "pty-missing"})

    assert result.ok is False
    assert result.error_code == "PROCESS_NOT_FOUND"


# LLM: 使用真实 ToolExecutor 与耐久操作账本；不能用 handler 的局部结果代替整链错误归类。
# 函数用途: 将 PTY 控制调用送过授权和副作用核对，验证零写入不会变成 UNKNOWN。
def _execute_transport(tmp_path, tool, arguments):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    snapshot = runtime_snapshot_for_tools({"terminal_session": tool})
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    execution = ToolExecutor().execute(ToolExecutorRequest(
        call=canonical_test_call(snapshot, "terminal_session", arguments),
        runtime_snapshot=snapshot, workspace_root=tmp_path, workspace_roots=(tmp_path,),
        approval_mode="auto", operation_store=store, operation_store_required=True,
        operation_owner_id="test-owner",
    ))
    rows = store.list_tool_operations(owner_id="test-owner", run_id=snapshot.run_id)
    return execution.result, rows


@pytest.mark.parametrize("action", ["write", "resize", "close"])
def test_missing_pty_transport_is_not_an_unknown_effect(tmp_path, action):
    result, operations = _execute_transport(tmp_path, _tool(tmp_path), {
        "action": action, "session_id": "bg-old-handle",
        **({"data": "hello"} if action == "write" else {}),
        **({"columns": 80, "rows": 24} if action == "resize" else {}),
    })

    assert result.handler_executed
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert result.effect_outcome == "not_started"
    assert len(operations) == 1
    assert operations[0].status == "failed"
    assert operations[0].result["effect_outcome"] == "not_started"


@pytest.mark.parametrize("written", [0, 2])
def test_pty_partial_input_retains_unknown_effect(tmp_path, monkeypatch, written):
    from agent_py_agent.agent.tooling.pty_sessions import PtyWriteError

    def fail_write(*args):
        raise PtyWriteError("TOOL_TIMEOUT", written)

    monkeypatch.setattr(pty_session_registry, "write", fail_write)
    result, operations = _execute_transport(tmp_path, _tool(tmp_path), {
        "action": "write", "session_id": "pty-existing", "data": "hello",
    })
    expected = "unknown" if written else "not_started"
    assert result.effect_outcome == expected
    assert operations[0].status == ("unknown" if written else "failed")
    assert (result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN") is bool(written)


def test_pty_exited_before_write_reports_zero_bytes(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(pty_session_registry, "get", lambda *args: SimpleNamespace(
        closed=True, process=SimpleNamespace(poll=lambda: 0), write_lock=threading.Lock(),
    ))
    result = _tool(tmp_path).execute({"action": "write", "session_id": "exited", "data": "x"})
    assert not result.ok
    assert result.effect_outcome == "not_started"
    assert result.result_envelope["bytes_written"] == 0


def test_pty_exit_after_completed_write_does_not_erase_delivery(tmp_path, monkeypatch):
    # 成功返回代表字节已交给内核；进程随即退出不能反向变成未执行或 UNKNOWN。
    monkeypatch.setattr(pty_session_registry, "write", lambda *args: SimpleNamespace(
        closed=True, process=SimpleNamespace(poll=lambda: 0),
    ))
    result = _tool(tmp_path).execute({"action": "write", "session_id": "finished", "data": "x"})
    assert _payload(result)["bytes"] == 1


def test_other_conversation_cannot_write_pty_and_does_not_halt_turn(tmp_path):
    tool = _tool(tmp_path)
    started = _payload(tool.execute({
        "action": "start", "command": f"{shlex.quote(sys.executable)} -q",
        "__run_scope": {"owner_id": "owner-a", "session_id": "thread-a"},
    }))
    result = tool.execute({
        "action": "write", "session_id": started["session_id"], "data": "print(42)",
        "__run_scope": {"owner_id": "owner-a", "session_id": "thread-b"},
    })
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert result.effect_outcome == "not_started"
    assert pty_session_registry.get(started["session_id"]).process.poll() is None


def test_terminal_session_carries_structured_write_roots_to_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    captured: dict[str, object] = {}

    def fake_start(
        command,
        target,
        owner_home=None,
        write_roots=None,
        read_roots=None,
        protected_write_paths=None,
        run_scope=None,
    ):
        captured.update(
            command=command,
            target=target,
            owner_home=owner_home,
            write_roots=write_roots,
            read_roots=read_roots,
            protected_write_paths=protected_write_paths,
            run_scope=run_scope,
        )
        raise OSError("captured")

    monkeypatch.setattr(pty_session_registry, "start", fake_start)
    result = _tool(tmp_path).execute(
        {
            "action": "start",
            "command": "python -q",
            "__sandbox_write_roots": [str(task_root)],
            "__sandbox_read_roots": [str(tmp_path / "shared")],
        }
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert captured["write_roots"] == (task_root.resolve(),)
    assert captured["read_roots"] == ((tmp_path / "shared").resolve(),)


def test_terminal_session_scope_rejects_other_task(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.pty_sessions import _pty_access_scope

    first = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    same = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    other = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-b",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    other_conversation = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-b"},
    )
    other_read = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        (tmp_path / "shared-b",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )

    assert first == same
    assert first != other
    assert first != other_conversation
    assert first != other_read
