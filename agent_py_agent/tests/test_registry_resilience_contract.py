from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_py_agent.agent.memory_archive import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import (
    ReadFileTool,
    filesystem_access_options,
)
from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool
from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)
from agent_py_agent.agent.tooling.registry_invoke import (
    AuthorizedToolDispatchRequest,
    RegistryToolInvokeRequest,
    _sandbox_read_roots_for_invocation,
    _tool_params_with_runtime_boundary,
    _workspace_roots_for_invocation,
    invoke_registry_tool,
)


class FlakyReadTool(BaseTool):
    spec = ToolSpec(
        name="flaky_read",
        category="test",
        effect="read_only",
        description="Fails once then succeeds.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        if self.calls == 1:
            return ToolExecutionResult("flaky_read", False, "timeout", error_code="TOOL_TIMEOUT")
        return ToolExecutionResult("flaky_read", True, "ok-after-retry")


class FlakyWriteTool(BaseTool):
    spec = ToolSpec(
        name="flaky_write",
        category="test",
        effect="mutating",
        description="Fails once and should not be retried without idempotency.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolExecutionResult("flaky_write", False, "timeout", error_code="TOOL_TIMEOUT")


class HugeOutputTool(BaseTool):
    spec = ToolSpec(
        name="huge_output",
        category="test",
        effect="read_only",
        description="Returns large output.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("huge_output", True, "x" * 25000)


class HugeReadFileTool(BaseTool):
    spec = ToolSpec(
        name="read_file",
        category="test",
        effect="read_only",
        description="Returns large file content.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("read_file", True, "文件正文" * 9000)


class CountingReadFileTool(BaseTool):
    spec = ToolSpec(
        name="read_file",
        category="test",
        effect="read_only",
        description="Records whether the handler executed.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={"path": "path"},
        parameter_schema={"path": {"type": "string"}},
        required_parameters=["path"],
    )

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolExecutionResult("read_file", True, str(params.get("path") or ""))


class PreservedOutputTool(BaseTool):
    spec = ToolSpec(
        name="preserved_output",
        category="test",
        effect="read_only",
        description="Returns large machine-readable output.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult(
            "preserved_output",
            True,
            '{"items":[' + ",".join('"value"' for _ in range(3000)) + "]}",
            result_envelope={"tool_output_policy": {"preserve_prompt_output": True}},
        )


class ExternalOutputTool(BaseTool):
    spec = ToolSpec(
        name="external_output",
        category="test",
        effect="read_only",
        output_trust="external_data",
        description="Returns external data.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult(
            "external_output",
            True,
            "external",
            result_envelope={
                "tool_output_policy": {
                    "trust": "runtime",
                    "redaction": "source_code",
                }
            },
        )


class DynamicExternalReadTool(BaseTool):
    spec = ToolSpec(
        name="dynamic_external_read",
        category="test",
        effect="read_only",
        output_redaction="source_code",
        description="Usually reads source code but this result came from an external archive.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult(
            "dynamic_external_read",
            True,
            "external archive",
            result_envelope={
                "tool_output_policy": {
                    "trust": "external_data",
                    "redaction": "default",
                }
            },
        )


def test_registry_retries_retryable_read_only_tool_once(tmp_path: Path) -> None:
    tool = FlakyReadTool()

    result = execute_registry_call(_call({"tool": "flaky_read"}, {"flaky_read": tool}, tmp_path))

    assert result.ok is True
    assert result.output == "ok-after-retry"
    assert tool.calls == 2
    assert result.result_envelope["tool_resilience"]["retry_attempts"] == 1


def test_registry_does_not_retry_mutating_tool_without_idempotency(tmp_path: Path) -> None:
    tool = FlakyWriteTool()

    result = execute_registry_call(_call({"tool": "flaky_write"}, {"flaky_write": tool}, tmp_path))

    assert result.ok is False
    assert tool.calls == 0
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] in {
        "TOOL_IDEMPOTENCY_KEY_MISSING",
        "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",
    }


def test_registry_keeps_large_output_and_bounds_only_model_projection(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "huge_output"}, {"huge_output": HugeOutputTool()}, tmp_path))

    assert result.ok is True
    assert result.output == "x" * 25000
    payload = result.result_envelope["tool_output_policy"]
    assert payload["truncated"] is True
    assert len(payload["live_prompt_output"]) < 12000
    assert "artifact_ref" not in payload
    assert not (tmp_path / "work" / "blobs" / "tool_outputs").exists()
    assert not (tmp_path / ".agent_tool_outputs").exists()


def test_large_projection_archives_and_reads_the_complete_execution_output(
    tmp_path: Path,
) -> None:
    result = execute_registry_call(
        _call(
            {"tool": "huge_output"},
            {"huge_output": HugeOutputTool()},
            tmp_path,
        )
    )
    archived = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool=result.tool,
            call_id="call-large",
            output=result.output,
            ok=result.ok,
            run_id="run-large",
            task_id="task-large",
            min_chars=100,
            preview_chars=100,
            result_envelope=result.result_envelope,
        )
    )

    assert archived["output_size_bytes"] == 25000
    assert archived["output_externalized"] is True
    restored = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=tmp_path,
            artifact_ref=archived["scoped_call_id"],
            run_id="run-large",
            task_id="task-large",
            max_chars=0,
        )
    )
    assert restored["ok"] is True
    assert restored["content"] == "x" * 25000


def test_registry_does_not_create_a_second_large_output_store(tmp_path: Path) -> None:
    source_workspace = tmp_path / "source"
    source_workspace.mkdir()
    task_root = tmp_path / "home" / "tasks" / "2026-06-07" / "demo"
    task_work = task_root / "work"
    result = execute_registry_call(
        _call(
            {"tool": "huge_output"},
            {"huge_output": HugeOutputTool()},
            source_workspace,
            write_boundary={"task_root": str(task_root), "task_work_dir": str(task_work)},
        )
    )

    assert result.ok is True
    payload = result.result_envelope["tool_output_policy"]
    assert result.output == "x" * 25000
    assert payload["truncated"] is True
    assert "artifact_ref" not in payload
    assert not (task_work / "blobs" / "tool_outputs").exists()
    assert not (source_workspace / ".agent_tool_outputs").exists()
    assert not (source_workspace / "work" / "blobs" / "tool_outputs").exists()


def test_registry_preserves_read_file_output_for_context_compaction(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "read_file"}, {"read_file": HugeReadFileTool()}, tmp_path))

    assert result.ok is True
    assert result.output == "文件正文" * 9000
    assert result.result_envelope["tool_output_policy"]["preserved"] is True


def test_registry_preserves_large_machine_output_when_declared(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "preserved_output"}, {"preserved_output": PreservedOutputTool()}, tmp_path))

    assert result.ok is True
    assert result.output.startswith('{"items":[')
    assert result.output.endswith("]}")
    assert result.result_envelope["tool_output_policy"]["preserved"] is True


def test_registry_tool_spec_owns_output_projection_policy(tmp_path: Path) -> None:
    result = execute_registry_call(
        _call(
            {"tool": "external_output"},
            {"external_output": ExternalOutputTool()},
            tmp_path,
        )
    )

    policy = result.result_envelope["tool_output_policy"]
    assert policy["trust"] == "external_data"
    assert policy["redaction"] == "default"


def test_registry_result_may_tighten_but_not_loosen_projection_policy(
    tmp_path: Path,
) -> None:
    result = execute_registry_call(
        _call(
            {"tool": "dynamic_external_read"},
            {"dynamic_external_read": DynamicExternalReadTool()},
            tmp_path,
        )
    )

    policy = result.result_envelope["tool_output_policy"]
    assert policy["trust"] == "external_data"
    assert policy["redaction"] == "default"


def test_process_tools_receive_structured_sandbox_write_roots(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    allowed = task_root / "work"
    readable = tmp_path / "shared-source"
    boundary = {
        "allowed_write_roots": [str(allowed)],
        "allowed_read_roots": [str(readable)],
        "shell_access_mode": "workspace-write",
        "task_root": str(task_root),
    }
    for tool_name in ("run_command", "terminal_session", "lsp"):
        tool_params = {"action": "start"} if tool_name == "terminal_session" else {"action": "status"}
        params = _tool_params_with_runtime_boundary(
            AuthorizedToolDispatchRequest(
                tool_name=tool_name,
                tool=HugeOutputTool(),
                tool_params=tool_params,
                workspace_root=tmp_path,
                write_boundary=boundary,
                sandbox_read_roots=(readable,),
            )
        )
        assert params["__sandbox_write_roots"] == [str(allowed)]
        assert params["__sandbox_read_roots"] == [str(readable)]
        if tool_name in {"run_command", "terminal_session"}:
            assert params["__access_mode"] == "workspace-write"
        else:
            assert "__access_mode" not in params
        assert "working_dir" not in params


def test_process_tools_can_enter_read_root_without_promoting_it_to_write(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    readable = tmp_path / "shared-source"
    request = RegistryToolInvokeRequest(
        tool_name="run_command",
        payload={"command": "pwd", "working_dir": str(readable)},
        tools={},
        workspace_root=task_root,
        workspace_roots=[task_root],
        allowed_tools=["run_command"],
        write_boundary={
            "task_root": str(task_root),
            "allowed_write_roots": [str(task_root)],
            "allowed_read_roots": [str(readable)],
        },
    )

    roots = _workspace_roots_for_invocation(request)
    assert readable.resolve() in roots
    assert _sandbox_read_roots_for_invocation(request) == (
        task_root.resolve(),
        readable.resolve(),
    )


def test_exact_read_scope_rejects_other_owner_file_before_handler(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "owner" / "docs" / "source.md"
    denied = tmp_path / "owner" / "private" / "user.md"
    allowed.parent.mkdir(parents=True)
    denied.parent.mkdir(parents=True)
    allowed.write_text("source", encoding="utf-8")
    denied.write_text("private", encoding="utf-8")
    tool = CountingReadFileTool()

    rejected = invoke_registry_tool(
        RegistryToolInvokeRequest(
            tool_name="read_file",
            payload={"tool": "read_file", "path": str(denied)},
            tools={"read_file": tool},
            workspace_root=tmp_path / "owner",
            workspace_roots=[tmp_path / "owner"],
            allowed_tools=["read_file"],
            write_boundary={
                "read_scope_mode": "exact",
                "allowed_read_roots": [str(allowed)],
            },
        )
    )
    accepted = invoke_registry_tool(
        RegistryToolInvokeRequest(
            tool_name="read_file",
            payload={"tool": "read_file", "path": str(allowed)},
            tools={"read_file": tool},
            workspace_root=tmp_path / "owner",
            workspace_roots=[tmp_path / "owner"],
            allowed_tools=["read_file"],
            write_boundary={
                "read_scope_mode": "exact",
                "allowed_read_roots": [str(allowed)],
            },
        )
    )

    assert rejected.ok is False
    assert rejected.error_code == "TOOL_PERMISSION_DENIED"
    assert rejected.handler_executed is False
    assert accepted.ok is True
    assert accepted.handler_executed is True
    assert tool.calls == 1


def test_read_artifact_receives_current_run_scope_from_boundary(
    tmp_path: Path,
) -> None:
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="read_artifact",
            tool=HugeOutputTool(),
            tool_params={"artifact_ref": "run-a:1-1"},
            workspace_root=tmp_path,
            write_boundary={
                "task_work_dir": str(tmp_path / "task" / "work"),
                "artifact_read_root": str(tmp_path / "task" / "work" / "agents" / "run-a"),
                "artifact_read_scope_mode": "current_run",
            },
        )
    )

    assert params["__artifact_read_root"] == str(
        tmp_path / "task" / "work" / "agents" / "run-a"
    )
    assert params["__artifact_read_scope_mode"] == "current_run"


def test_read_artifact_falls_back_to_task_work_root_for_legacy_boundary(
    tmp_path: Path,
) -> None:
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="read_artifact",
            tool=HugeOutputTool(),
            tool_params={"artifact_ref": "run-a:1-1"},
            workspace_root=tmp_path,
            write_boundary={"task_work_dir": str(tmp_path / "task" / "work")},
        )
    )

    assert params["__artifact_read_root"] == str(tmp_path / "task" / "work")


def test_explicit_process_working_dir_overrides_selected_task_root(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    explicit = task_root / "output" / "project"
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="run_command",
            tool=HugeOutputTool(),
            tool_params={"command": "pwd", "working_dir": str(explicit)},
            workspace_root=tmp_path,
            write_boundary={
                "task_root": str(task_root),
                "allowed_write_roots": [str(task_root)],
            },
        )
    )

    assert params["working_dir"] == str(explicit)


def _task_scoped_apply_patch_request(
    *,
    owner_root: Path,
    task_root: Path,
    patch: str,
) -> RegistryToolInvokeRequest:
    tool = ApplyPatchTool(
        owner_root,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )
    return RegistryToolInvokeRequest(
        tool_name="apply_patch",
        payload={"tool": "apply_patch", "patch": patch},
        tools={"apply_patch": tool},
        workspace_root=owner_root,
        workspace_roots=[owner_root],
        allowed_tools=["apply_patch"],
        write_boundary={
            "task_root": str(task_root),
            "task_dir": str(task_root),
            "task_output_dir": str(task_root / "output"),
            "task_work_dir": str(task_root / "work"),
            "allowed_write_roots": [
                str(task_root / "output"),
                str(task_root / "work"),
            ],
        },
    )


def test_task_scoped_apply_patch_resolves_work_delete_to_current_task(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = task_root / "work" / "delete-smoke.txt"
    target.parent.mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("remove me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch=(
                "*** Begin Patch\n"
                "*** Delete File: work/delete-smoke.txt\n"
                "*** End Patch\n"
            ),
        )
    )

    assert result.ok is True
    assert result.handler_executed is True
    assert not target.exists()


def test_task_scoped_apply_patch_rejects_malformed_delete_without_side_effect(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = task_root / "work" / "keep.txt"
    target.parent.mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("keep me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch="*** Delete File: work/keep.txt\n",
        )
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.handler_executed is True
    assert target.read_text(encoding="utf-8") == "keep me"


def test_task_scoped_apply_patch_rejects_work_path_escape_before_handler(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = owner_root / "outside.txt"
    (task_root / "work").mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("keep me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch=(
                "*** Begin Patch\n"
                "*** Delete File: work/../../../outside.txt\n"
                "*** End Patch\n"
            ),
        )
    )

    assert result.ok is False
    assert result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False
    assert target.read_text(encoding="utf-8") == "keep me"


def _task_scoped_filesystem_request(
    *,
    owner_root: Path,
    task_root: Path,
    tool: BaseTool,
    payload: dict[str, object],
) -> RegistryToolInvokeRequest:
    return RegistryToolInvokeRequest(
        tool_name=tool.spec.name,
        payload={"tool": tool.spec.name, **payload},
        tools={tool.spec.name: tool},
        workspace_root=owner_root,
        workspace_roots=[owner_root],
        allowed_tools=[tool.spec.name],
        write_boundary={
            "task_root": str(task_root),
            "task_dir": str(task_root),
            "task_output_dir": str(task_root / "output"),
            "task_work_dir": str(task_root / "work"),
            "allowed_write_roots": [
                str(task_root / "output"),
                str(task_root / "work"),
            ],
        },
    )


def test_task_scoped_relative_file_tools_share_one_effective_cwd(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "audits" / "audit-a"
    task_root.mkdir(parents=True)
    (task_root / "output").mkdir()
    (task_root / "work").mkdir()
    (task_root / "task-only.md").write_text("TASK_SCOPE_MARKER", encoding="utf-8")
    (owner_root / "owner-only.md").write_text("OWNER_SCOPE_MARKER", encoding="utf-8")
    access = filesystem_access_options(owner_scope_root=str(owner_root))
    tools_and_payloads = (
        (ReadFileTool(owner_root, 10_000, access_options=access), {"path": "task-only.md"}),
        (ListFilesTool(owner_root, 100, access_options=access), {"path": "."}),
        (FindFilesTool(owner_root, 100, access_options=access), {"pattern": "task-only.md", "path": "."}),
        (SearchTextTool(owner_root, 100, access_options=access), {"query": "TASK_SCOPE_MARKER", "path": "."}),
    )

    for tool, payload in tools_and_payloads:
        result = invoke_registry_tool(
            _task_scoped_filesystem_request(
                owner_root=owner_root,
                task_root=task_root,
                tool=tool,
                payload=payload,
            )
        )
        assert result.ok is True, (tool.spec.name, result.output)
        assert "task-only.md" in result.output or "TASK_SCOPE_MARKER" in result.output
        assert "owner-only.md" not in result.output
        assert tool.workspace_root == owner_root.resolve()


def test_shared_file_tool_does_not_leak_task_cwd_between_concurrent_calls(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    first_root = owner_root / "audits" / "first"
    second_root = owner_root / "audits" / "second"
    for root, marker in ((first_root, "FIRST_MARKER"), (second_root, "SECOND_MARKER")):
        (root / "work").mkdir(parents=True)
        (root / "output").mkdir()
        (root / "marker.txt").write_text(marker, encoding="utf-8")
    tool = ReadFileTool(
        owner_root,
        10_000,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )

    def read(root: Path) -> str:
        result = invoke_registry_tool(
            _task_scoped_filesystem_request(
                owner_root=owner_root,
                task_root=root,
                tool=tool,
                payload={"path": "marker.txt"},
            )
        )
        assert result.ok is True
        return result.output

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read, first_root)
        second = pool.submit(read, second_root)
        assert "FIRST_MARKER" in first.result()
        assert "SECOND_MARKER" in second.result()
    assert tool.workspace_root == owner_root.resolve()


def test_task_scoped_file_tool_keeps_owner_wall_and_ordinary_cwd(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "audits" / "task"
    outsider = tmp_path / "other-owner" / "secret.txt"
    owner_root.mkdir()
    (task_root / "work").mkdir(parents=True)
    (task_root / "output").mkdir()
    outsider.parent.mkdir()
    outsider.write_text("OTHER_OWNER_SECRET", encoding="utf-8")
    (owner_root / "ordinary.txt").write_text("ORDINARY_MARKER", encoding="utf-8")
    tool = ReadFileTool(
        owner_root,
        10_000,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )

    denied = invoke_registry_tool(
        _task_scoped_filesystem_request(
            owner_root=owner_root,
            task_root=task_root,
            tool=tool,
            payload={"path": str(outsider)},
        )
    )
    ordinary = invoke_registry_tool(
        RegistryToolInvokeRequest(
            tool_name="read_file",
            payload={"tool": "read_file", "path": "ordinary.txt"},
            tools={"read_file": tool},
            workspace_root=owner_root,
            workspace_roots=[owner_root],
            allowed_tools=["read_file"],
            write_boundary=None,
        )
    )

    assert denied.ok is False
    assert "OTHER_OWNER_SECRET" not in denied.output
    assert ordinary.ok is True
    assert "ORDINARY_MARKER" in ordinary.output


def _call(
    payload: dict[str, object],
    tools: dict[str, BaseTool],
    workspace: Path,
    *,
    write_boundary: dict[str, object] | None = None,
) -> ExecuteRegistryCallParams:
    return ExecuteRegistryCallParams(
        payload=payload,
        tools=tools,
        workspace_root=workspace,
        workspace_roots=[workspace],
        write_boundary=write_boundary,
    )
