from __future__ import annotations

"""Managed background process session and multi-user scope contracts."""

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


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
    return execute_registry_test_call(
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


def test_configured_background_shell_profile_adds_session_companion() -> None:
    """用户只列 run_command 时也不能得到一张无法续接后台命令的残缺快照。"""
    from agent_py_agent.agent.conversation.runtime import (
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        config=SimpleNamespace(background_main_agent_allowed_tools=["run_command"])
    )

    assert decision.allowed_tools == ("run_command", "process_session")
