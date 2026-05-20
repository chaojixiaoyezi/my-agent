from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.tests.test_tools.backends import make_tool_registry


def test_tool_registry_executes_typed_tool_call_envelope(tmp_path):
    (tmp_path / "notes.txt").write_text("hello typed protocol", encoding="utf-8")
    registry = make_tool_registry(tmp_path)
    envelope = ToolCallEnvelope(
        call_id="call-read-1",
        source="legacy_text_protocol",
        tool="read_file",
        args={"path": "notes.txt"},
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    result = registry.execute_call(envelope)

    assert result.ok is True
    assert result.tool == "read_file"
    assert result.call_id == "call-read-1"
    assert result.result_envelope["kind"] == "tool_call_result"
    assert result.result_envelope["call_id"] == "call-read-1"
    assert result.result_envelope["tool"] == "read_file"
    assert result.result_envelope["scope"]["run_id"] == "run-1"
    assert result.result_envelope["tool_protocol_v2"]["schema_version"] == "tool_protocol.v2"
    assert result.result_envelope["tool_protocol_v2"]["operation_id"] == result.result_envelope["operation_id"]


# LLM: Tool result envelopes must keep the same operation id as the call envelope.
# 函数用途: 验证工具调用和工具结果在机器层属于同一个操作，恢复/重放时不能只靠 call_id 猜。
def test_tool_registry_result_envelope_preserves_call_operation_id(tmp_path):
    (tmp_path / "notes.txt").write_text("hello typed protocol", encoding="utf-8")
    registry = make_tool_registry(tmp_path)
    envelope = ToolCallEnvelope(
        call_id="call-read-2",
        source="legacy_text_protocol",
        tool="read_file",
        args={"path": "notes.txt"},
        operation_id="op:read_file:stable-123",
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    result = registry.execute_call(envelope)

    assert result.ok is True
    assert result.result_envelope["operation_id"] == "op:read_file:stable-123"


def test_tool_registry_rejects_non_tool_call_envelope_kind(tmp_path):
    registry = make_tool_registry(tmp_path)
    envelope = {
        "kind": "subagent_result",
        "schema_version": 1,
        "result_id": "result-1",
        "run_id": "run-1",
        "status": "DONE",
    }

    result = registry.execute_call(envelope)

    assert result.ok is False
    assert result.tool == "unknown"
    assert "tool_call envelope" in result.output


# LLM: Tool result envelopes should carry Error Taxonomy fields for recovery logic.
# 函数用途: typed 工具执行失败时，result_envelope 也要带 error_code/recommended_action，不能只给自然语言。
def test_tool_registry_error_envelope_includes_error_contract(tmp_path):
    registry = make_tool_registry(tmp_path)
    envelope = ToolCallEnvelope(
        call_id="call-missing-1",
        source="legacy_text_protocol",
        tool="read_file",
        args={"path": "missing.txt"},
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    result = registry.execute_call(envelope)

    assert result.ok is False
    assert result.error_code == "PATH_INVALID"
    assert result.result_envelope["error_code"] == "PATH_INVALID"
    assert result.result_envelope["error_category"] == "path"
    assert result.result_envelope["recommended_action"] == "fix_path_or_read_refs"
    assert "修正路径" in result.result_envelope["recovery_hint"]
    assert result.result_envelope["tool_protocol_v2"]["error_type"] == "PATH_INVALID"
    assert result.result_envelope["tool_protocol_v2"]["retry_hint"] == "fix_path_or_read_refs"
