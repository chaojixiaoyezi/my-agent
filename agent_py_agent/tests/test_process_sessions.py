from __future__ import annotations

"""Managed background process session and multi-user scope contracts."""

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.process_session_store import (
    PROCESS_SESSION_SCHEMA,
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import (
    execute_approved_registry_test_call,
    execute_registry_test_call,
)


@pytest.fixture(autouse=True)
def _clear_process_registry():
    process_registry.clear()
    yield
    for row in process_registry.list():
        if row["status"] == "running":
            process_registry.kill(row["session_id"])
    process_registry.clear()


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=100,
            max_matches=50,
            web_max_chars=6000,
            http_timeout=30,
            catalog_limit=30,
            retrieval_limit=5,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            owner_scope_root=str(root),
            operation_store_required=False,
        )
    )


def _call(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, object],
    scope: dict[str, object],
):
    executor = (
        execute_approved_registry_test_call
        if tool_name == "run_command" and bool(arguments.get("run_in_background"))
        else execute_registry_test_call
    )
    return executor(
        registry,
        tool_name,
        arguments,
        trusted_run_context={"run_scope": scope},
        run_id=str(scope["run_id"]),
    )


def _payload(result) -> dict[str, object]:
    assert result.ok, result.output
    start = result.output.find("{")
    end = result.output.rfind("}")
    assert start >= 0 and end >= start, result.output
    return json.loads(result.output[start : end + 1])


def test_process_session_wait_replaces_shell_sleep_polling(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    command = (
        f"{shlex.quote(sys.executable)} -c "
        '"import time; print(\'started\', flush=True); '
        "time.sleep(0.15); print('done', flush=True)\""
    )

    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": command, "run_in_background": True},
            scope,
        )
    )
    assert started["status"] == "started"
    assert "process_session" in started["hint"]
    assert "sleep" in started["hint"]

    finished = _payload(
        _call(
            registry,
            "process_session",
            {
                "action": "wait",
                "session_id": started["session_id"],
                "timeout_seconds": 3,
            },
            scope,
        )
    )
    assert finished["status"] == "exited"
    assert finished["exit_code"] == 0
    assert finished["wait_timed_out"] is False
    assert "started" in finished["output_tail"]
    assert "done" in finished["output_tail"]


def test_process_session_hides_other_tui_session(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope_a = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    scope_b = {
        "owner_id": "owner-a",
        "session_id": "thread-b",
        "root_task_id": "task-b",
        "run_id": "run-b",
    }
    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": "echo scoped", "run_in_background": True},
            scope_a,
        )
    )

    denied = _call(
        registry,
        "process_session",
        {"action": "status", "session_id": started["session_id"]},
        scope_b,
    )
    assert denied.ok is False
    assert denied.error_code == "PROCESS_NOT_FOUND"
    assert _payload(_call(registry, "process_session", {"action": "list"}, scope_b)) == {
        "processes": []
    }


def test_process_session_stop_terminates_owned_process_tree(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    command = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(20)\""
    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": command, "run_in_background": True},
            scope,
        )
    )
    tool = ProcessSessionTool(str(tmp_path))

    stopped = tool.execute(
        {
            "action": "stop",
            "session_id": started["session_id"],
            "__run_scope": scope,
        }
    )

    assert stopped.ok is True
    payload = json.loads(stopped.output)
    assert payload["status"] == "killed"
    assert process_registry.status(started["session_id"])["status"] == "killed"


def test_process_session_requires_host_bound_scope(tmp_path: Path) -> None:
    result = ProcessSessionTool(str(tmp_path)).execute({"action": "list"})

    assert result.ok is False
    assert result.error_code == "OWNER_SCOPE_UNAVAILABLE"


def test_process_session_is_registered_in_model_manifest(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    names = {spec.name for spec in registry.model_visible_specs()}

    assert "process_session" in names


def test_process_session_follows_background_command_tool_profiles() -> None:
    """能启动后台命令的主/子代理都必须同时拿到唯一的续接工具。"""
    from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
        CODING_SUBAGENT_TOOLS,
        READ_ONLY_SUBAGENT_TOOLS,
    )
    from agent_py_agent.agent.conversation.runtime import (
        DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    )

    assert "process_session" in DEFAULT_BACKGROUND_ALLOWED_TOOLS
    assert "process_session" in CODING_SUBAGENT_TOOLS
    assert "process_session" not in READ_ONLY_SUBAGENT_TOOLS


def test_background_session_outlives_one_shot_launcher_and_is_rehydrated(tmp_path: Path) -> None:
    """子代理式短命 Python 进程退出后，另一个进程仍能查询并停止同一后台会话。"""

    scope = {
        "owner_id": "owner-cross-process",
        "session_id": "thread-cross-process",
        "root_task_id": "task-cross-process",
        "run_id": "subagent-cross-process",
    }
    command = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(20)\""
    script = "\n".join(
        (
            "import json",
            "from pathlib import Path",
            "from agent_py_agent.agent.tooling.process_registry import ProcessAccessScope",
            "from agent_py_agent.agent.tooling.shell import ShellTool",
            f"root = Path({str(tmp_path)!r})",
            f"command = {command!r}",
            "tool = ShellTool(root)",
            "result = tool._start_background_command(",
            "    command, root, None, None,",
            "    ProcessAccessScope('owner-cross-process', 'thread-cross-process', ''),",
            ")",
            "print(result.output, flush=True)",
            "raise SystemExit(0 if result.ok else 1)",
        )
    )
    launcher = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert launcher.returncode == 0, launcher.stderr or launcher.stdout
    started = json.loads(launcher.stdout.strip().splitlines()[-1])

    tool = ProcessSessionTool("", tmp_path)
    deadline = time.monotonic() + 3
    status = None
    while time.monotonic() < deadline:
        status = tool.execute(
            {
                "action": "status",
                "session_id": started["session_id"],
                "__run_scope": scope,
            }
        )
        if status.ok and json.loads(status.output)["status"] == "running":
            break
        time.sleep(0.05)
    assert status is not None and status.ok is True, getattr(status, "output", "")
    assert json.loads(status.output)["status"] == "running"

    stopped = tool.execute(
        {
            "action": "stop",
            "session_id": started["session_id"],
            "__run_scope": scope,
        }
    )
    assert stopped.ok is True
    assert json.loads(stopped.output)["status"] == "killed"


def test_process_session_authority_store_is_outside_owner_sandbox(tmp_path: Path) -> None:
    owner_home = tmp_path / "owners" / "alice"
    workspace = owner_home / "task"
    workspace.mkdir(parents=True)

    store_root = process_session_store_root(workspace, owner_home)

    assert not store_root.is_relative_to(owner_home)
    assert store_root.parent.parent == owner_home.parent / ".my-agent-runtime"


def test_process_session_terminal_state_never_regresses_to_running(tmp_path: Path) -> None:
    store = ProcessSessionStore(tmp_path / "authority")
    running = {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": "bg-terminal-monotonic",
        "pid": 12345,
        "pid_birth_token": "birth-a",
        "started_at": 1.0,
        "status": "running",
        "access_scope": {
            "owner_id": "owner-a",
            "conversation_id": "thread-a",
            "owner_home": "/owners/alice",
        },
    }
    store.write(running)
    store.write({**running, "status": "killed", "finished_at": 2.0})

    effective = store.write(running)

    assert effective["status"] == "killed"
    assert store.load("bg-terminal-monotonic").record["status"] == "killed"


def test_configured_background_shell_profile_adds_session_companion() -> None:
    """用户只列 run_command 时也不能得到一张无法续接后台命令的残缺快照。"""
    from agent_py_agent.agent.conversation.runtime import (
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        config=SimpleNamespace(background_main_agent_allowed_tools=["run_command"])
    )

    assert decision.allowed_tools == ("run_command", "process_session")
