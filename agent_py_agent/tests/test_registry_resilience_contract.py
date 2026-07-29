from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import filesystem_access_options
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


def test_registry_archives_large_tool_output(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "huge_output"}, {"huge_output": HugeOutputTool()}, tmp_path))

    assert result.ok is True
    assert len(result.output) < 12000
    payload = result.result_envelope["tool_output_policy"]
    artifact_ref = payload["artifact_ref"]
    assert payload["truncated"] is True
    assert artifact_ref.startswith("work/blobs/tool_outputs/")
    assert (tmp_path / artifact_ref).read_text(encoding="utf-8") == "x" * 25000
    assert not (tmp_path / ".agent_tool_outputs").exists()


def test_registry_archives_large_tool_output_under_task_work_dir(tmp_path: Path) -> None:
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
    artifact_ref = payload["artifact_ref"]
    assert artifact_ref.startswith("work/blobs/tool_outputs/")
    assert (task_root / artifact_ref).read_text(encoding="utf-8") == "x" * 25000
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
