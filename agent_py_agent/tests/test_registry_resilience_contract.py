from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
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
        expose_security_tools=False,
        security_tool_names=set(),
        write_boundary=write_boundary,
    )
