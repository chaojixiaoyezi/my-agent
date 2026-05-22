from __future__ import annotations

from types import SimpleNamespace


# LLM: Runtime tool guardrails must enforce the same no-progress contract as offline traces.
# 函数用途: 验证同一个只读工具同参数同结果重复成功后，下一次相同调用会被运行时挡住。
def test_runtime_blocks_repeated_identical_read_only_successes() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_no_progress,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params()
    payload = {"tool": "read_file", "path": "outputs/source_index.json"}

    for _ in range(2):
        record_tool_guard_observation(agent, params, payload, ToolExecutionResult("read_file", True, "same"))

    result = maybe_block_repeated_tool_no_progress(agent, params, payload)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "TOOL_REPEATED_NO_PROGRESS"


# LLM: A local write changes the no-progress state so repeated reads after progress are allowed again.
# 函数用途: 验证写入/构建类工具成功后会清理只读重复计数，避免误杀正常“写后复查”。
def test_runtime_repeated_read_guard_resets_after_local_progress() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_no_progress,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params()
    read_payload = {"tool": "read_file", "path": "outputs/source_index.json"}
    write_payload = {"tool": "write_structured_json", "path": "outputs/source_index.json", "rows": [{"a": 1}]}

    for _ in range(2):
        record_tool_guard_observation(agent, params, read_payload, ToolExecutionResult("read_file", True, "same"))
    assert maybe_block_repeated_tool_no_progress(agent, params, read_payload) is not None

    record_tool_guard_observation(agent, params, write_payload, ToolExecutionResult("write_structured_json", True, "{}"))

    assert maybe_block_repeated_tool_no_progress(agent, params, read_payload) is None


def _params():
    return SimpleNamespace(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        task_attributes={},
    )
