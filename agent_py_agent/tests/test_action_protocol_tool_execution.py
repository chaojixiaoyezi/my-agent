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
