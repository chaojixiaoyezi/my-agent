from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

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
