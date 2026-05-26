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
    params = _params(task_attributes={"repeat_fail_threshold": 1})
    payload = {"tool": "read_file", "path": "outputs/source_index.json"}

    for _ in range(3):
        record_tool_guard_observation(agent, params, payload, ToolExecutionResult("read_file", True, "same"))

    result = maybe_block_repeated_tool_no_progress(agent, params, payload)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"


# LLM: zero runtime repeat threshold should disable the repeated-read cap.
# 函数用途: 验证 repeat_fail_threshold=0 时同一只读结果不会因计数门被阻断。
def test_runtime_no_progress_threshold_zero_is_unlimited() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_no_progress,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 0})
    payload = {"tool": "read_file", "path": "outputs/source_index.json"}

    for _ in range(4):
        record_tool_guard_observation(agent, params, payload, ToolExecutionResult("read_file", True, "same"))

    assert maybe_block_repeated_tool_no_progress(agent, params, payload) is None


# LLM: A local write changes the no-progress state so repeated reads after progress are allowed again.
# 函数用途: 验证写入/构建类工具成功后会清理只读重复计数，避免误杀正常“写后复查”。
def test_runtime_repeated_read_guard_resets_after_local_progress() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_no_progress,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 1})
    read_payload = {"tool": "read_file", "path": "outputs/source_index.json"}
    write_payload = {"tool": "write_structured_json", "path": "outputs/source_index.json", "rows": [{"a": 1}]}

    for _ in range(3):
        record_tool_guard_observation(agent, params, read_payload, ToolExecutionResult("read_file", True, "same"))
    assert maybe_block_repeated_tool_no_progress(agent, params, read_payload) is not None

    record_tool_guard_observation(agent, params, write_payload, ToolExecutionResult("write_structured_json", True, "{}"))

    assert maybe_block_repeated_tool_no_progress(agent, params, read_payload) is None


# LLM: Repeated failure guard warns at N/2N and blocks only the next unchanged call at 3N.
# 函数用途: 验证同工具同参数同类失败按一个阈值派生两次提示和一次动作级拦截，不杀任务。
def test_runtime_same_args_same_failure_warns_then_blocks_next_call_only() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_failure,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(9):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "3 次" in warnings[0]
    assert "6 次" in warnings[1]

    result = maybe_block_repeated_tool_failure(agent, params, payload)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
    assert "换关键词、换参数、换工具或换数据来源" in result.output


# LLM: Threshold 0 means unlimited: reminders still exist but no same-call block is produced.
# 函数用途: 验证 repeat_fail_threshold=0 时 50/100 次只给软提示，不会拦截后续工具调用。
def test_runtime_repeat_fail_threshold_zero_is_unlimited_with_fixed_hints() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_failure,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 0})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(100):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "50 次" in warnings[0]
    assert "100 次" in warnings[1]
    assert maybe_block_repeated_tool_failure(agent, params, payload) is None


# LLM: Failure class is part of the loop identity so different failures do not compound.
# 函数用途: 验证同工具同参数但错误类型变化时不会被当作同一条撞墙路径累计到 3N。
def test_runtime_same_args_different_failure_class_does_not_compound() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_failure,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}

    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "permission", error_code="WRITE_FORBIDDEN"),
        )

    assert maybe_block_repeated_tool_failure(agent, params, payload) is None


# LLM: Read-only progress is based on unchanged results, not just same args.
# 函数用途: 验证同参数分页/游标类读取只要结果持续变化，就不会被无进展门误拦。
def test_runtime_same_args_read_with_changing_results_is_progress() -> None:
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        maybe_block_repeated_tool_no_progress,
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tools import ToolExecutionResult

    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "read_artifact", "artifact_ref": "large-source"}

    for index in range(9):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("read_artifact", True, f'{{"cursor_after": "{index}", "rows": [{index}]}}'),
        )

    assert maybe_block_repeated_tool_no_progress(agent, params, payload) is None


def _params(*, task_attributes: dict[str, object] | None = None):
    return SimpleNamespace(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        task_attributes=task_attributes or {},
    )
