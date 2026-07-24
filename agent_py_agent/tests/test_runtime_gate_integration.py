from __future__ import annotations

import json

from agent_py_agent.agent.contracts.gates.tool_effects import args_hash_for_call
from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        category="utility",
        effect="read_only",
        description="Return params for tests.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
        input_schema={"type": "object", "additionalProperties": True},
    )

    def execute(self, params):
        return ToolExecutionResult("echo", True, json.dumps(params, sort_keys=True))


class MissingManifestTool(BaseTool):
    spec = ToolSpec(
        name="missing_manifest",
        category="utility",
        description="Intentionally incomplete manifest for tests.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
        input_schema={"type": "object", "additionalProperties": True},
    )

    def execute(self, params):
        return ToolExecutionResult("missing_manifest", True, "should not execute")


class ProtocolNameCollisionTool(BaseTool):
    spec = ToolSpec(
        name="protocol_name_collision",
        category="utility",
        effect="read_only",
        description="Validate arguments whose names also occur in outer protocols.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={"kind": "kind", "run_id": "run id"},
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "note"]},
                "run_id": {"type": "integer", "minimum": 1},
            },
            "required": ["kind", "run_id"],
            "additionalProperties": False,
        },
    )

    def execute(self, params):
        return ToolExecutionResult(
            "protocol_name_collision",
            True,
            json.dumps(params, sort_keys=True),
        )


def _execute(tmp_path, payload, *, tool=None, write_boundary=None, dangerous_roots=None):
    selected = tool or EchoTool()
    return execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={selected.spec.name: selected},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            write_boundary=write_boundary,
            path_dangerous_roots=dangerous_roots or [],
        )
    )


def test_registry_execution_returns_runtime_gate_denial_for_unknown_tool(tmp_path):
    result = _execute(tmp_path, {"tool": "magic_tool", "value": 1})

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "DENY"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_NOT_REGISTERED"


def test_registry_execution_records_runtime_gate_allow_for_executed_tool(tmp_path):
    result = _execute(tmp_path, {"tool": "echo", "value": 1})

    assert result.ok is True
    gate = result.result_envelope["runtime_gate"]
    assert gate["status"] == "ALLOW"
    assert gate["evidence"]["tool_name"] == "echo"
    assert "tool_rate_limit" in gate["evidence"]["executed_gates"]


def test_registry_validates_protocol_named_tool_arguments_and_strips_outer_metadata(tmp_path):
    result = _execute(
        tmp_path,
        {
            "tool": "protocol_name_collision",
            "kind": "fact",
            "run_id": "7",
            "idempotency_key": "outer-only",
        },
        tool=ProtocolNameCollisionTool(),
    )

    assert result.ok is True
    assert json.loads(result.output) == {"kind": "fact", "run_id": 7}
    assert result.result_envelope["input_coercions"] == [
        {"path": "$.run_id", "source_type": "string", "target_type": "integer"}
    ]


def test_registry_rejects_invalid_protocol_named_tool_argument_before_execute(tmp_path):
    result = _execute(
        tmp_path,
        {
            "tool": "protocol_name_collision",
            "kind": "unknown",
            "run_id": 7,
        },
        tool=ProtocolNameCollisionTool(),
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.result_envelope["runtime_gate"]["findings"][0]["evidence"]["issues"] == [
        {
            "keyword": "enum",
            "path": "$.kind",
            "expected": ["fact", "note"],
            "actual_type": "string",
        }
    ]


def test_registry_execution_blocks_when_runtime_rate_limit_is_exhausted(tmp_path):
    payload = {"tool": "echo", "value": 1}
    result = _execute(
        tmp_path,
        payload,
        write_boundary={
            "now": 10.0,
            "tool_rate_limit_policy": {"max_calls": 1, "window_seconds": 60},
            "tool_rate_limit_records": [
                {
                    "tool_name": "echo",
                    "args_hash": args_hash_for_call({"value": 1}),
                    "attempt_timestamps": [9.0],
                }
            ],
        },
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_rate_limit"


def test_registry_execution_blocks_tool_with_incomplete_manifest(tmp_path):
    result = _execute(
        tmp_path,
        {"tool": "missing_manifest", "value": 1},
        tool=MissingManifestTool(),
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_manifest"


def test_registry_execution_blocks_path_gate_before_tool_execute(tmp_path):
    workspace = tmp_path / "workspace"
    danger = tmp_path / "danger"
    workspace.mkdir()
    danger.mkdir()
    outside = danger / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "path": "link"},
            tools={"echo": EchoTool()},
            workspace_root=workspace,
            workspace_roots=[workspace],
            path_dangerous_roots=[str(danger)],
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "path_url_command"


def test_registry_execution_blocks_dangerous_real_tool_without_approval(tmp_path):
    result = _execute(
        tmp_path,
        {
            "tool": "echo",
            "value": 1,
            "mode": "real",
            "idempotency_key": "idem-echo-dangerous",
        },
        write_boundary={"tool_effects": {"echo": "dangerous"}},
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "NEED_APPROVAL"


def test_registry_execution_blocks_duplicate_idempotency_key(tmp_path):
    result = _execute(
        tmp_path,
        {
            "tool": "echo",
            "value": 1,
            "mode": "real",
            "operation_id": "op-2",
            "idempotency_key": "idem-echo-mutating",
        },
        write_boundary={
            "tool_effects": {"echo": "mutating"},
            "idempotency_ledger": [
                {
                    "idempotency_key": "idem-echo-mutating",
                    "args_hash": args_hash_for_call({"value": 1, "mode": "real"}),
                    "operation_id": "op-1",
                    "status": "DONE",
                }
            ],
        },
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "idempotency_ledger"
