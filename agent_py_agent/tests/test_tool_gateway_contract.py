"""Canonical Tool Gateway execution, path, and bounded-output contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_invoke import (
    RegistryToolInvokeRequest,
    _request_local_tool_for_invocation,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


def _registry(
    root: Path, *, shell_output_max_chars: int = 80, access_mode: str = "workspace-write"
) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=shell_output_max_chars,
            access_mode=access_mode,
            # 这些合同测试直接验证工具本体；权威操作账本由专门测试覆盖。
            operation_store_required=False,
        )
    )


def _execute(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, object],
    *,
    allowed_tools: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
) -> ToolResult:
    return execute_registry_test_call(
        registry,
        tool_name,
        arguments,
        allowed_tools=allowed_tools,
        write_boundary=write_boundary,
    )


def _invoke_request_for_scope(
    registry: ToolRegistry,
    root: Path,
    owner_scope_root: Path,
) -> RegistryToolInvokeRequest:
    return RegistryToolInvokeRequest(
        tool_name="run_command",
        arguments={},
        tools=registry.tools,
        workspace_root=root,
        workspace_roots=[root],
        allowed_tools=None,
        write_boundary={},
        owner_scope_root=str(owner_scope_root),
    )


def test_tool_gateway_rejects_shell_alias(tmp_path: Path):
    result = _execute(
        _registry(tmp_path),
        "shell",
        {
            "cmd": f'{sys.executable} -c "print(123)"',
            "cwd": str(tmp_path),
        },
    )

    assert result.ok is False
    assert result.tool_name == "shell"
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"


def test_concurrent_invocations_use_request_local_shell_and_terminal_scopes(tmp_path: Path):
    owner_a = tmp_path / "owners" / "a"
    owner_b = tmp_path / "owners" / "b"
    owner_a.mkdir(parents=True)
    owner_b.mkdir(parents=True)
    registry = _registry(owner_a)
    shared_shell = registry.tools["run_command"]
    shared_terminal = registry.tools["terminal_session"]

    request_a = _invoke_request_for_scope(registry, owner_a, owner_a)
    request_b = _invoke_request_for_scope(registry, owner_b, owner_b)
    shell_a = _request_local_tool_for_invocation(
        shared_shell,
        request=request_a,
        workspace_roots=[owner_a],
    )
    shell_b = _request_local_tool_for_invocation(
        shared_shell,
        request=request_b,
        workspace_roots=[owner_b],
    )
    terminal_request = RegistryToolInvokeRequest(
        **{
            **request_b.__dict__,
            "tool_name": "terminal_session",
        }
    )
    terminal_b = _request_local_tool_for_invocation(
        shared_terminal,
        request=terminal_request,
        workspace_roots=[owner_b],
    )

    assert shell_a is not shared_shell and shell_b is not shared_shell
    assert shell_a.path_access_policy.owner_scope_root == owner_a.resolve()
    assert shell_b.path_access_policy.owner_scope_root == owner_b.resolve()
    assert shared_shell.path_access_policy.owner_scope_root is None
    assert terminal_b is not shared_terminal
    assert terminal_b.shell_tool is not shared_terminal.shell_tool
    assert terminal_b.shell_tool.path_access_policy.owner_scope_root == owner_b.resolve()


def test_concurrent_web_invocations_do_not_mutate_shared_private_host_grants(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    shared_web = registry.tools["web_fetch"]
    request = RegistryToolInvokeRequest(
        tool_name="web_fetch",
        arguments={},
        tools=registry.tools,
        workspace_root=tmp_path,
        workspace_roots=[tmp_path],
        allowed_tools=None,
        write_boundary={},
    )

    scoped_web = _request_local_tool_for_invocation(
        shared_web,
        request=request,
        workspace_roots=[tmp_path],
    )
    scoped_web.allowed_private_hosts = ("192.168.1.7",)
    scoped_web.allow_private_resolution = True

    assert scoped_web is not shared_web
    assert tuple(shared_web.allowed_private_hosts) == ()
    assert shared_web.allow_private_resolution is None


def test_tool_gateway_hides_controlled_exec_from_default_catalog(tmp_path: Path):
    registry = _registry(tmp_path)

    names = {spec.name for spec in registry.specs(include_orchestration=True)}
    list_result = _execute(registry, "list_tools", {})
    live_manifest = list_result.metadata["handler_details"]["tool_output_policy"][
        "live_prompt_output"
    ]
    manifest = json.loads(live_manifest)
    manifest_names = {item["name"] for item in manifest["tools"]}
    retired_names = {
        "append_file",
        "replace_in_file",
        "write_structured_json",
        "data_to_workbook",
        "markdown_to_pdf",
        "file_write_session",
    }

    assert "run_command" in names
    assert "controlled_exec" not in names
    assert retired_names.isdisjoint(names)
    assert "run_command" in manifest_names
    assert "controlled_exec" not in manifest_names
    assert retired_names.isdisjoint(manifest_names)


def test_tool_gateway_can_still_expose_controlled_exec_when_explicitly_allowed(tmp_path: Path):
    names = {spec.name for spec in _registry(tmp_path).specs(allowed_tools=["controlled_exec"])}

    assert names == {"controlled_exec"}


def _controlled_exec_boundary(root: Path) -> dict[str, object]:
    return {
        "controlled_exec_grants": [
            {
                "grant_id": "grant-shell-1",
                "request_id": "req-1",
                "run_id": "child-1",
                "command_allowlist": ["pwd"],
                "path_scope": [str(root)],
                "output_budget": {"stdout_bytes": 1000, "stderr_bytes": 1000},
            }
        ],
    }


def test_tool_gateway_rejects_hidden_controlled_exec_when_not_explicitly_allowed(tmp_path: Path):
    result = _execute(
        _registry(tmp_path),
        "controlled_exec",
        {
            "command": "pwd",
            "cwd": str(tmp_path),
            "apply": False,
        },
        write_boundary=_controlled_exec_boundary(tmp_path),
    )

    assert result.ok is False
    assert result.tool_name == "controlled_exec"
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"


def test_tool_gateway_executes_controlled_exec_only_when_explicitly_allowed(tmp_path: Path):
    result = _execute(
        _registry(tmp_path),
        "controlled_exec",
        {
            "command": "pwd",
            "cwd": str(tmp_path),
            "apply": False,
        },
        allowed_tools=["controlled_exec"],
        write_boundary=_controlled_exec_boundary(tmp_path),
    )

    assert result.ok is True
    assert '"mode": "dry_run"' in result.output
    assert '"allowed": true' in result.output


def test_run_command_output_is_bounded_by_gateway_budget(tmp_path: Path):
    for index in range(12):
        (tmp_path / f"very-long-output-name-{index:02d}.txt").write_text(
            "fixture",
            encoding="utf-8",
        )
    result = _execute(
        _registry(tmp_path, shell_output_max_chars=40),
        "run_command",
        {
            "command": "find . -maxdepth 1 -print",
        },
    )

    assert result.ok
    assert "stdout_truncated=True" in result.output
    assert "stdout_chars=" in result.output
    assert "very-long-output-name-11.txt" not in result.output


def test_relative_file_and_shell_tools_share_workspace_cwd(tmp_path: Path):
    registry = _registry(tmp_path)
    written = _execute(
        registry,
        "write_file",
        {
            "path": "cwd-contract/value.txt",
            "content": "same-cwd",
        },
    )
    read_from_shell = _execute(
        registry,
        "run_command",
        {
            "command": "cat cwd-contract/value.txt",
        },
    )

    assert written.ok is True
    assert read_from_shell.ok is True
    assert "same-cwd" in read_from_shell.output


def test_explicit_execution_cwd_keeps_file_alias_and_shell_aligned(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_root = tmp_path / "tasks" / "task-a"
    task_output = task_root / "output"
    workspace.mkdir()
    task_output.mkdir(parents=True)
    registry = _registry(workspace)
    boundary = {
        "execution_cwd": str(task_root),
        "task_root": str(task_root),
        "task_output_dir": str(task_output),
        "task_work_dir": str(task_root / "work"),
        "allowed_write_roots": [str(task_output)],
    }
    written = _execute(
        registry,
        "write_file",
        {
            "path": "output/project/value.txt",
            "content": "task-cwd",
        },
        write_boundary=boundary,
    )
    read_from_shell = _execute(
        registry,
        "run_command",
        {
            "command": "cat output/project/value.txt",
        },
        write_boundary=boundary,
    )

    assert written.ok is True
    assert read_from_shell.ok is True
    assert "task-cwd" in read_from_shell.output


def test_run_command_shell_access_override_uses_normal_path_policy(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    registry = _registry(workspace, access_mode="full-access")

    result = _execute(
        registry,
        "run_command",
        {
            "command": "pwd",
            "working_dir": str(external),
        },
        write_boundary={"shell_access_mode": "workspace-write"},
    )

    assert result.ok is True
    assert "return_code=0" in result.output


def test_tool_gateway_executes_write_file_overwrite_and_append_modes(tmp_path: Path):
    registry = _registry(tmp_path)
    output_root = tmp_path / "task" / "output"
    boundary = {
        "allowed_write_roots": [str(output_root)],
        "task_output_dir": str(output_root),
        "forbidden_write_roots": [],
        "locked_files": [],
    }
    target = output_root / "report.md"

    first = _execute(
        registry,
        "write_file",
        {
            "path": str(target),
            "mode": "overwrite",
            "content": "# Report\n",
        },
        write_boundary=boundary,
    )
    second = _execute(
        registry,
        "write_file",
        {
            "path": str(target),
            "mode": "append",
            "content": "\nbody\n",
        },
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert target.read_text(encoding="utf-8") == "# Report\n\nbody\n"


def test_tool_gateway_overwrites_existing_task_output_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {
        "task_output_dir": str(task_output),
        "task_work_dir": str(task_output.parent / "work"),
    }

    first = _execute(
        registry,
        "write_file",
        {"path": "output/report.md", "content": "# Report\n"},
        write_boundary=boundary,
    )
    second = _execute(
        registry,
        "write_file",
        {"path": "output/report.md", "content": "## Section\n"},
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert (task_output / "report.md").read_text(encoding="utf-8") == "## Section\n"


def test_tool_gateway_overwrites_existing_task_work_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_work)}

    first = _execute(
        registry,
        "write_file",
        {"path": "work/facts.md", "content": "# Facts\n"},
        write_boundary=boundary,
    )
    second = _execute(
        registry,
        "write_file",
        {"path": "work/facts.md", "content": "- detail\n"},
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert (task_work / "facts.md").read_text(encoding="utf-8") == "- detail\n"


def test_tool_gateway_overwrites_execution_output_json_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    output_json = task_work / "agents" / "run-1" / "output.json"
    output_json.parent.mkdir(parents=True)
    output_json.write_text('{"status":"PLANNING"}', encoding="utf-8")
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {
        "task_output_dir": str(task_output),
        "task_work_dir": str(task_work),
        "output_json": str(output_json),
        "allowed_write_roots": [str(task_work)],
    }

    result = _execute(
        registry,
        "write_file",
        {"path": str(output_json), "content": '{"status":"DONE"}'},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert output_json.read_text(encoding="utf-8") == '{"status":"DONE"}'


def test_tool_gateway_explicit_overwrite_still_replaces_task_output(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {
        "task_output_dir": str(task_output),
        "task_work_dir": str(task_output.parent / "work"),
    }

    _execute(
        registry,
        "write_file",
        {"path": "output/report.md", "content": "# Report\n"},
        write_boundary=boundary,
    )
    result = _execute(
        registry,
        "write_file",
        {
            "path": "output/report.md",
            "mode": "overwrite",
            "content": "# Replacement\n",
        },
        write_boundary=boundary,
    )

    assert result.ok is True
    assert (task_output / "report.md").read_text(encoding="utf-8") == "# Replacement\n"


def test_tool_gateway_omitted_mode_still_overwrites_non_task_output_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = _registry(workspace)
    target = workspace / "notes.md"
    target.write_text("old\n", encoding="utf-8")

    result = _execute(
        registry,
        "write_file",
        {"path": "notes.md", "content": "new\n"},
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_tool_gateway_maps_task_output_alias_from_write_boundary(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_work)}

    result = _execute(
        registry,
        "write_file",
        {"path": "output/report.md", "content": "hello"},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert (task_output / "report.md").read_text(encoding="utf-8") == "hello"
    assert not (workspace / "output" / "report.md").exists()
    readback = _execute(
        registry,
        "read_file",
        {"path": "output/report.md"},
        write_boundary=boundary,
    )
    assert readback.ok is True
    assert "hello" in readback.output


def test_tool_gateway_maps_current_owner_relative_task_alias_once(tmp_path: Path):
    owner_home = tmp_path / "owners" / "user-a"
    task_root = owner_home / "tasks" / "2026-08-28" / "task-a"
    task_root.mkdir(parents=True)
    registry = _registry(task_root)
    owner_relative = "tasks/2026-08-28/task-a/result.txt"
    boundary = {
        "task_root": str(task_root),
        "execution_cwd": str(task_root),
        "allowed_write_roots": [str(owner_home)],
    }

    result = _execute(
        registry,
        "write_file",
        {"path": owner_relative, "content": "one canonical file\n"},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert (task_root / "result.txt").read_text(encoding="utf-8") == "one canonical file\n"
    assert not (task_root / owner_relative).exists()


def test_tool_gateway_maps_current_owner_relative_task_alias_inside_patch(tmp_path: Path):
    owner_home = tmp_path / "owners" / "user-a"
    task_root = owner_home / "tasks" / "2026-08-28" / "task-a"
    task_root.mkdir(parents=True)
    target = task_root / "result.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = _registry(task_root)
    boundary = {
        "task_root": str(task_root),
        "execution_cwd": str(task_root),
        "allowed_write_roots": [str(owner_home)],
    }

    result = _execute(
        registry,
        "apply_patch",
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: tasks/2026-08-28/task-a/result.txt\n"
                "@@\n"
                "-before\n"
                "+after\n"
                "*** End Patch"
            )
        },
        write_boundary=boundary,
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "after\n"
    assert not (task_root / "tasks").exists()


def test_tool_gateway_maps_owner_workspace_alias_without_granting_write(
    tmp_path: Path,
):
    task_root = tmp_path / "owners" / "user-a" / "tasks" / "task-a"
    task_root.mkdir(parents=True)
    owner_workspace = tmp_path / "owners" / "user-a" / "workspace"
    source = owner_workspace / "input" / "reference_repos" / "project-a" / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("project-a\n", encoding="utf-8")
    registry = _registry(task_root)
    boundary = {
        "owner_workspace_dir": str(owner_workspace),
        "task_output_dir": str(task_root / "output"),
        "task_work_dir": str(task_root / "work"),
        "allowed_write_roots": [str(task_root / "output"), str(task_root / "work")],
    }

    readback = _execute(
        registry,
        "read_file",
        {
            "path": "workspace/input/reference_repos/project-a/README.md",
        },
        write_boundary=boundary,
    )
    write = _execute(
        registry,
        "write_file",
        {
            "path": "workspace/input/reference_repos/project-a/changed.txt",
            "content": "must not write",
        },
        write_boundary=boundary,
    )

    assert readback.ok is True
    assert "project-a" in readback.output
    assert write.ok is False
    assert write.error_code == "WRITE_FORBIDDEN"
    assert not (source.parent / "changed.txt").exists()


def test_tool_gateway_round_trips_public_owner_home_alias_through_policy(
    tmp_path: Path,
):
    owner_home = tmp_path / "owners" / "user-a"
    task_root = owner_home / "tasks" / "2026-08-28" / "task-a"
    source = task_root / "input" / "brief.md"
    source.parent.mkdir(parents=True)
    source.write_text("owner alias input\n", encoding="utf-8")
    registry = _registry(task_root)
    boundary = {
        "task_root": str(task_root),
        "execution_cwd": str(task_root),
        "effective_owner_scope_root": str(owner_home),
        "allowed_read_roots": [str(task_root)],
        "allowed_write_roots": [str(task_root / "output")],
    }

    readback = _execute(
        registry,
        "read_file",
        {"path": "~/.my-agent/owner/tasks/2026-08-28/task-a/input/brief.md"},
        write_boundary=boundary,
    )
    forbidden_write = _execute(
        registry,
        "write_file",
        {
            "path": "~/.my-agent/owner/tasks/2026-08-28/task-a/input/changed.md",
            "content": "must not write",
        },
        write_boundary=boundary,
    )

    assert readback.ok is True
    assert "owner alias input" in readback.output
    assert forbidden_write.ok is False
    assert forbidden_write.error_code == "WRITE_FORBIDDEN"
    assert not (source.parent / "changed.md").exists()


def test_tool_gateway_public_owner_home_alias_cannot_traverse_owner_wall(
    tmp_path: Path,
):
    owner_home = tmp_path / "owners" / "user-a"
    task_root = owner_home / "tasks" / "task-a"
    task_root.mkdir(parents=True)
    registry = _registry(task_root)
    boundary = {
        "task_root": str(task_root),
        "execution_cwd": str(task_root),
        "effective_owner_scope_root": str(owner_home),
        "allowed_read_roots": [str(owner_home)],
        "allowed_write_roots": [str(task_root)],
    }

    result = _execute(
        registry,
        "read_file",
        {"path": "~/.my-agent/owner/../other-user/private.txt"},
        write_boundary=boundary,
    )

    assert result.ok is False
