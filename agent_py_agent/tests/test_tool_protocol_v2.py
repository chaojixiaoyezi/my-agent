import json

from agent_py_agent.agent.contracts.tool_protocol_v2 import (
    ArtifactRef,
    ToolCallEnvelope,
    ToolError,
    ToolResultEnvelope,
    deserialize_tool_call,
    deserialize_tool_result,
    normalize_tool_call,
    normalize_tool_result,
    serialize_tool_call,
    serialize_tool_result,
    validate_tool_call,
    validate_tool_result,
)


def test_success_result_round_trips_with_operation_and_status():
    call = ToolCallEnvelope(
        operation_id="op-read-1",
        tool_name="read_file",
        input={"path": "README.md"},
        idempotency_key="idem-read-1",
    )
    result = ToolResultEnvelope.success(
        call,
        output={"text_ref": "archive/tool/op-read-1.txt"},
    )

    decoded = deserialize_tool_result(serialize_tool_result(result))

    assert decoded.schema_version == "tool_protocol.v2"
    assert decoded.operation_id == "op-read-1"
    assert decoded.tool_name == "read_file"
    assert decoded.status == "succeeded"
    assert decoded.error is None
    assert decoded.output == {"text_ref": "archive/tool/op-read-1.txt"}
    assert decoded.operation_ref.operation_id == "op-read-1"
    assert validate_tool_call(call) == []
    assert validate_tool_result(decoded) == []


def test_failure_result_classifies_error_and_retry_hint():
    call = normalize_tool_call(
        {
            "operation_id": "op-write-1",
            "tool_name": "write_file",
            "input": {"path": "../outside.txt"},
            "idempotency_key": "idem-write-1",
        }
    )
    result = normalize_tool_result(
        {
            "operation_id": call.operation_id,
            "tool_name": call.tool_name,
            "status": "failed",
            "error": {"message": "write forbidden: outside workspace"},
            "idempotency_key": call.idempotency_key,
        }
    )

    assert result.error is not None
    assert result.error.error_type == "PATH_OUTSIDE_WORKSPACE"
    assert result.error.retry_hint == "fix_path_within_allowed_roots"
    assert result.error.retryable is False
    assert result.status == "failed"


def test_artifact_refs_are_structured_and_not_inferred_from_natural_language():
    result = normalize_tool_result(
        {
            "operation_id": "op-render-1",
            "tool_name": "render_report",
            "status": "succeeded",
            "output": "Created artifact at reports/summary.html",
            "artifact_refs": [
                {
                    "artifact_id": "artifact-1",
                    "path": "reports/summary.html",
                    "kind": "html",
                    "hash": "sha256:abc",
                    "size_bytes": 42,
                }
            ],
        }
    )
    no_refs = normalize_tool_result(
        {
            "operation_id": "op-render-2",
            "tool_name": "render_report",
            "status": "succeeded",
            "output": "Created artifact at reports/from-natural-language.html",
        }
    )

    assert result.artifact_refs == [
        ArtifactRef(
            artifact_id="artifact-1",
            path="reports/summary.html",
            kind="html",
            hash="sha256:abc",
            reserved={"size_bytes": 42},
        )
    ]
    assert no_refs.artifact_refs == []


def test_idempotency_key_is_stable_for_equivalent_call_inputs():
    left = normalize_tool_call({"tool": "read_file", "args": {"path": "README.md", "limit": 20}})
    right = normalize_tool_call({"tool_name": "read_file", "input": {"limit": 20, "path": "README.md"}})

    assert left.idempotency_key == right.idempotency_key
    assert left.operation_id == right.operation_id


def test_legacy_fields_convert_to_v2_envelopes():
    call = normalize_tool_call(
        {
            "call_id": "call-123",
            "tool": "read_file",
            "args": {"path": "README.md"},
        }
    )
    result = normalize_tool_result(
        {
            "call_id": "call-123",
            "tool": "read_file",
            "ok": True,
            "output_ref": "memory_archive/artifacts/tool-call-123.txt",
        }
    )

    assert call.schema_version == "tool_protocol.v2"
    assert call.operation_id == "tool_call:call-123"
    assert call.tool_name == "read_file"
    assert call.input == {"path": "README.md"}
    assert result.operation_id == "tool_call:call-123"
    assert result.status == "succeeded"
    assert result.output == {"output_ref": "memory_archive/artifacts/tool-call-123.txt"}
    assert result.idempotency_key.startswith("idem:read_file:")


def test_legacy_flat_tool_status_argument_does_not_become_protocol_status():
    call = normalize_tool_call(
        {
            "tool": "update_collaboration",
            "case_id": "case-1",
            "status": "needs_replan",
        }
    )

    assert call.status == "pending"
    assert call.input["status"] == "needs_replan"
    assert validate_tool_call(call) == []


def test_unknown_error_downgrades_to_unknown_error():
    error = ToolError.from_payload({"error_type": "ALIEN_SIGNAL", "message": "strange failure"})

    assert error.error_type == "UNKNOWN_ERROR"
    assert error.retry_hint == "stop_and_record_diagnostic"
    assert error.retryable is False


def test_serialized_payloads_are_json_objects_not_free_text():
    call = normalize_tool_call({"tool": "read_file", "args": {"path": "README.md"}})
    result = ToolResultEnvelope.success(call, output={"ok": True})

    call_payload = json.loads(serialize_tool_call(call))
    result_payload = json.loads(serialize_tool_result(result))
    decoded_call = deserialize_tool_call(json.dumps(call_payload))

    assert call_payload["schema_version"] == "tool_protocol.v2"
    assert result_payload["schema_version"] == "tool_protocol.v2"
    assert decoded_call.tool_name == "read_file"
