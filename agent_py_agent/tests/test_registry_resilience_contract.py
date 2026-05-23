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


# LLM: registry gateway should absorb one safe transient read-only failure.
# 函数用途: 参考 长期助手 的有限 retry/backoff 思路，验证只读工具可重试一次并留下机器证据。
def test_registry_retries_retryable_read_only_tool_once(tmp_path: Path) -> None:
    tool = FlakyReadTool()

    result = execute_registry_call(_call({"tool": "flaky_read"}, {"flaky_read": tool}, tmp_path))

    assert result.ok is True
    assert result.output == "ok-after-retry"
    assert tool.calls == 2
    assert result.result_envelope["tool_resilience"]["retry_attempts"] == 1


# LLM: mutating tools should be blocked before execution when idempotency is missing.
# 函数用途: 防止工具韧性层绕过副作用门；没有幂等键时不能执行，更不能重放。
def test_registry_does_not_retry_mutating_tool_without_idempotency(tmp_path: Path) -> None:
    tool = FlakyWriteTool()

    result = execute_registry_call(_call({"tool": "flaky_write"}, {"flaky_write": tool}, tmp_path))

    assert result.ok is False
    assert tool.calls == 0
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] in {
        "TOOL_IDEMPOTENCY_KEY_MISSING",
        "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",
    }


# LLM: large tool outputs should be archived and truncated before entering the prompt.
# 函数用途: 验证大输出有 refs-only artifact，完整内容落盘，给模型的是有界摘要。
def test_registry_archives_large_tool_output(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "huge_output"}, {"huge_output": HugeOutputTool()}, tmp_path))

    assert result.ok is True
    assert len(result.output) < 12000
    payload = result.result_envelope["tool_output_policy"]
    artifact_ref = payload["artifact_ref"]
    assert payload["truncated"] is True
    assert (tmp_path / artifact_ref).read_text(encoding="utf-8") == "x" * 25000


# LLM: large machine-readable outputs can opt into preservation through structured envelope.
# 函数用途: 防止工具清单/schema 这类机器 JSON 被大输出策略截成不可解析文本。
def test_registry_preserves_large_machine_output_when_declared(tmp_path: Path) -> None:
    result = execute_registry_call(_call({"tool": "preserved_output"}, {"preserved_output": PreservedOutputTool()}, tmp_path))

    assert result.ok is True
    assert result.output.startswith('{"items":[')
    assert result.output.endswith("]}")
    assert result.result_envelope["tool_output_policy"]["preserved"] is True


def _call(payload: dict[str, object], tools: dict[str, BaseTool], workspace: Path) -> ExecuteRegistryCallParams:
    return ExecuteRegistryCallParams(
        payload=payload,
        tools=tools,
        workspace_root=workspace,
        workspace_roots=[workspace],
        expose_security_tools=False,
        security_tool_names=set(),
    )
