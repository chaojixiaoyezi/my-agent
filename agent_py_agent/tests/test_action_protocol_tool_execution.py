from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_tools.backends import make_tool_registry


def test_registry_executes_only_canonical_tool_call_path(tmp_path):
    (tmp_path / "notes.txt").write_text("hello canonical protocol", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = execute_registry_test_call(
        registry,
        "read_file",
        {"path": "notes.txt"},
        run_id="run-1",
        call_id="call-read-1",
    )

    assert result.ok is True
    assert result.tool_name == "read_file"
    assert result.call_id == "call-read-1"
    assert len(result.metadata["raw_output_sha256"]) == 64
    assert result.metadata["action_decision"]["status"] == "allow"


def test_canonical_result_preserves_handler_structured_metadata(tmp_path):
    (tmp_path / "notes.txt").write_text(
        "hello canonical protocol\nsecond line",
        encoding="utf-8",
    )
    registry = make_tool_registry(tmp_path)

    result = execute_registry_test_call(
        registry,
        "read_file",
        {"path": "notes.txt"},
        operation_id="op:read_file:structured",
    )

    assert result.ok is True
    assert result.metadata["handler_details"]["read_window"] == {
        "kind": "line_window",
        "start_line": 1,
        "end_line": 2,
        "next_start_line": 0,
        "total_lines": 2,
        "complete": True,
    }


def test_canonical_result_exposes_typed_error_contract(tmp_path):
    registry = make_tool_registry(tmp_path)

    result = execute_registry_test_call(
        registry,
        "read_file",
        {"path": "missing.txt"},
        call_id="call-missing-1",
    )

    assert result.ok is False
    assert result.error_code == "PATH_NOT_FOUND"
    assert result.error_category == "path"
    assert result.recommended_action == "retry"
    assert "不存在" in result.recovery_hint
    assert result.handler_executed is True
    assert result.failure_stage == "execution"
