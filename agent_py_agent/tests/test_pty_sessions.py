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
    started = _payload(tool.execute({"action": "start", "command": command}))

    written = _payload(
        tool.execute(
            {
                "action": "write",
                "session_id": started["session_id"],
                "data": "print(6 * 7)",
                "append_newline": True,
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
                }
            )
        )
        cursor = read["cursor"]
        output += read["output"]

    assert "42" in output
    closed = _payload(tool.execute({"action": "close", "session_id": started["session_id"]}))
    assert closed["status"] == "closed"


def test_terminal_session_reuses_shell_command_policy(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "start", "command": "rm -rf /"})

    assert result.ok is False
    assert result.error_code == "COMMAND_POLICY_BLOCKED"


def test_terminal_session_requires_existing_session(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "read", "session_id": "pty-missing"})

    assert result.ok is False
    assert result.error_code == "PROCESS_NOT_FOUND"
