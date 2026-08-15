from __future__ import annotations

import json

from test_tools.backends import make_tool_registry

from agent_py_agent.agent.tooling.registry import ToolRegistry
from agent_py_agent.tests._tool_runtime_harness import (
    execute_approved_registry_test_call,
    execute_registry_test_call,
)


def _execute(registry, arguments, *, boundary):
    return execute_registry_test_call(
        registry,
        "controlled_exec",
        arguments,
        allowed_tools=["controlled_exec"],
        write_boundary=boundary,
    )


def _execute_approved(registry, arguments, *, boundary):
    return execute_approved_registry_test_call(
        registry,
        "controlled_exec",
        arguments,
        allowed_tools=["controlled_exec"],
        write_boundary=boundary,
    )


def test_controlled_exec_tool_plans_from_write_boundary_grant(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = make_tool_registry(workspace)
    boundary = _controlled_exec_boundary(workspace)

    result = _execute(
        registry,
        {
            "grant_id": "grant-shell-1",
            "command": "pwd",
            "cwd": str(workspace),
        },
        boundary=boundary,
    )

    payload = json.loads(result.output)
    assert result.ok
    assert payload["mode"] == "dry_run"
    assert payload["allowed"] is True
    assert payload["action"] == "execute_shell"
    assert payload["grant_id"] == "grant-shell-1"


def test_controlled_exec_tool_rejects_self_authored_scope(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        {
            "command": "pwd",
            "cwd": str(workspace),
            "command_allowlist": ["pwd"],
            "path_scope": [str(workspace)],
        },
        boundary={"task_dir": str(workspace / "task")},
    )

    assert not result.ok
    assert result.error_code == "TOOL_INTERNAL_PARAMETER_FORBIDDEN"


def test_controlled_exec_tool_accepts_duplicate_equivalent_grants_without_grant_id(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    boundary = _controlled_exec_boundary(workspace)
    duplicate = dict(boundary["controlled_exec_grants"][0])
    duplicate["grant_id"] = "grant-shell-duplicate"
    duplicate["request_id"] = "req-shell-duplicate"
    boundary["controlled_exec_grants"].append(duplicate)
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        {
            "command": "pwd",
            "cwd": str(workspace),
        },
        boundary=boundary,
    )

    payload = json.loads(result.output)
    assert result.ok
    assert payload["action"] == "execute_shell"
    assert payload["grant_id"] == "grant-shell-1"


def test_controlled_exec_tool_apply_runs_with_bounded_audit_refs(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    boundary = _controlled_exec_boundary(workspace)
    boundary["controlled_exec_artifact_dir"] = str(workspace / "task" / "artifacts" / "controlled_exec")
    registry = make_tool_registry(workspace)

    result = _execute_approved(
        registry,
        {
            "grant_id": "grant-shell-1",
            "command": "pwd",
            "cwd": str(workspace),
            "apply": True,
        },
        boundary=boundary,
    )

    payload = json.loads(result.output)
    assert result.ok
    assert payload["mode"] == "execute"
    assert payload["execution"]["executed"] is True
    assert payload["execution"]["exit_code"] == 0
    assert str(workspace) in payload["execution"]["stdout_preview"]
    assert payload["execution"]["audit_ref"].endswith("shell_gateway_audit.jsonl")


def test_controlled_exec_tool_apply_keeps_large_stdout_externalized(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    boundary = _controlled_exec_boundary(workspace)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = ["python3"]
    boundary["controlled_exec_grants"][0]["output_budget"] = {"stdout_bytes": 8, "stderr_bytes": 8}
    registry = make_tool_registry(workspace)

    result = _execute_approved(
        registry,
        {
            "command": ["python3", "-c", "print('x' * 2000)"],
            "cwd": str(workspace),
            "apply": True,
        },
        boundary=boundary,
    )

    payload = json.loads(result.output)
    stdout_ref = payload["execution"]["stdout_ref"]
    assert result.ok
    assert payload["execution"]["stdout_truncated"] is True
    assert payload["execution"]["stdout_bytes"] > 8
    assert len(payload["execution"]["stdout_preview"]) <= 8
    assert len((workspace / stdout_ref).read_text(encoding="utf-8")) <= 8


def test_controlled_exec_tool_apply_routes_delete_to_task_trash(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "task"
    task_dir.mkdir(parents=True)
    stale = task_dir / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    boundary = _controlled_exec_boundary(workspace)
    boundary["task_dir"] = str(task_dir)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = ["rm"]
    boundary["controlled_exec_grants"][0]["path_scope"] = [str(task_dir)]
    registry = make_tool_registry(workspace)

    result = _execute_approved(
        registry,
        {
            "command": "rm stale.txt",
            "cwd": str(task_dir),
            "apply": True,
        },
        boundary=boundary,
    )

    payload = json.loads(result.output)
    destination = payload["trash"]["destination"]
    assert result.ok
    assert payload["mode"] == "task_trash"
    assert payload["trash"]["moved"] is True
    assert not stale.exists()
    assert destination.endswith("stale.txt")
    assert (workspace / destination).exists()
    assert payload["trash"]["manifest_ref"].endswith("trash/manifest.jsonl")


def test_controlled_exec_tool_blocks_delete_without_rm_shell_grant(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "task"
    task_dir.mkdir(parents=True)
    stale = task_dir / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    boundary = _controlled_exec_boundary(workspace)
    boundary["task_dir"] = str(task_dir)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = ["pwd"]
    boundary["controlled_exec_grants"][0]["path_scope"] = [str(task_dir)]
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        {
            "command": "rm stale.txt",
            "cwd": str(task_dir),
            "apply": True,
        },
        boundary=boundary,
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_DESTRUCTIVE_DELETE_BLOCKED"
    assert stale.exists()


def test_controlled_exec_tool_blocks_relative_delete_without_rm_grant(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "task"
    deliverables = workspace / "deliverables"
    task_dir.mkdir(parents=True)
    deliverables.mkdir(parents=True)
    sentinel = deliverables / "sentinel.txt"
    sentinel.write_text("old", encoding="utf-8")
    boundary = _controlled_exec_boundary(workspace)
    boundary["task_dir"] = str(task_dir)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = ["pwd", "python3"]
    boundary["controlled_exec_grants"][0]["path_scope"] = [str(deliverables)]
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        {
            "command": "rm sentinel.txt",
            "cwd": str(deliverables),
            "apply": True,
        },
        boundary=boundary,
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_DESTRUCTIVE_DELETE_BLOCKED"
    assert sentinel.exists()


def test_controlled_exec_tool_dry_run_delete_still_requires_rm_grant(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "task"
    task_dir.mkdir(parents=True)
    boundary = _controlled_exec_boundary(workspace)
    boundary["task_dir"] = str(task_dir)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = ["pwd"]
    boundary["controlled_exec_grants"][0]["path_scope"] = [str(task_dir)]
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        {
            "command": "rm stale.txt",
            "cwd": str(task_dir),
        },
        boundary=boundary,
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_DESTRUCTIVE_DELETE_BLOCKED"


def test_controlled_exec_tool_is_registered_in_catalog(tmp_path) -> None:
    registry: ToolRegistry = make_tool_registry(tmp_path)

    names = {spec.name for spec in registry.specs(allowed_tools=["controlled_exec"])}

    assert names == {"controlled_exec"}


def _controlled_exec_boundary(workspace):
    return {
        "task_dir": str(workspace / "task"),
        "controlled_exec_grants": [
            {
                "grant_id": "grant-shell-1",
                "request_id": "req-shell-1",
                "run_id": "run-1",
                "command_allowlist": ["pwd"],
                "path_scope": [str(workspace)],
                "network_scope": [],
                "output_budget": {"timeout_seconds": 5, "stdout_bytes": 1024},
                "constraints": {"delete_policy": "trash_only"},
            }
        ],
    }
