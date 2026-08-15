from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelSpec,
)
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class EchoTool(BaseTool):
    model_spec = make_test_model_spec(
        "echo",
        category="utility",
        description="Return params for tests.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome("echo", True, json.dumps(params, sort_keys=True))


class DangerousEchoTool(EchoTool):
    runtime_policy = make_test_runtime_policy("dangerous")


class ProtocolNameCollisionTool(BaseTool):
    model_spec = make_test_model_spec(
        "protocol_name_collision",
        category="utility",
        description="Validate arguments whose names also occur in outer protocols.",
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
    runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome(
            "protocol_name_collision",
            True,
            json.dumps(params, sort_keys=True),
        )


class LocalFileUrlTool(BaseTool):
    model_spec = make_test_model_spec(
        "local_file_url",
        category="utility",
        description="Read one explicitly declared local file URL for tests.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    )
    runtime_policy = replace(
        make_test_runtime_policy("read_only"),
        input_policy=ToolInputPolicy(local_file_url_parameters=("url",)),
    )

    def execute(self, params):
        return ToolHandlerOutcome("local_file_url", True, json.dumps(params, sort_keys=True))


def _execute(
    tmp_path,
    payload,
    *,
    tool=None,
    write_boundary=None,
    dangerous_roots=None,
    owner_scope_root="",
):
    selected = tool or EchoTool()
    arguments = dict(payload)
    tool_name = str(arguments.pop("tool"))
    return execute_canonical_test_call(
        tmp_path,
        tools={selected.model_spec.name: selected},
        tool_name=tool_name,
        arguments=arguments,
        write_boundary=write_boundary,
        path_dangerous_roots=tuple(dangerous_roots or ()),
        owner_scope_root=owner_scope_root,
    ).result


def test_registry_execution_returns_runtime_gate_denial_for_unknown_tool(tmp_path):
    result = _execute(tmp_path, {"tool": "magic_tool", "value": 1})

    assert result.ok is False
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
    assert result.handler_executed is False


def test_registry_execution_records_runtime_gate_allow_for_executed_tool(tmp_path):
    result = _execute(tmp_path, {"tool": "echo", "value": 1})

    assert result.ok is True
    gate = result.metadata["action_decision"]
    assert gate["status"] == "allow"
    assert gate["evidence"]["tool_name"] == "echo"
    assert gate["evidence"]["schema_hash"].startswith("sha256:")
    assert result.handler_executed is True


def test_registry_validates_protocol_named_tool_arguments_and_strips_outer_metadata(tmp_path):
    result = _execute(
        tmp_path,
        {
            "tool": "protocol_name_collision",
            "kind": "fact",
            "run_id": 7,
        },
        tool=ProtocolNameCollisionTool(),
    )

    assert result.ok is True
    assert json.loads(result.output) == {"kind": "fact", "run_id": 7}
    assert result.metadata["input_sources"] == [
        {
            "path": "$.kind",
            "source": "model_proposed",
            "source_ref": "tool_call:test-call#/input/kind",
        },
        {
            "path": "$.run_id",
            "source": "model_proposed",
            "source_ref": "tool_call:test-call#/input/run_id",
        },
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
    assert result.metadata["action_decision"]["evidence"]["issues"] == [
        {
            "keyword": "enum",
            "path": "$.kind",
            "expected": ["fact", "note"],
            "actual_type": "string",
        }
    ]


def test_registry_execution_blocks_when_runtime_rate_limit_is_exhausted(tmp_path):
    payload = {"tool": "echo", "value": 1}
    now = time.time()
    result = _execute(
        tmp_path,
        payload,
        write_boundary={
            "tool_rate_limit_policy": {"max_calls": 1, "window_seconds": 60},
            "tool_rate_limit_records": [
                {
                    "tool_name": "echo",
                    "args_hash": tool_arguments_hash({"value": 1}),
                    "attempt_timestamps": [now - 1],
                }
            ],
        },
    )

    assert result.ok is False
    assert result.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert result.metadata["action_decision"]["evidence"]["gate"]["gate"] == "tool_rate_limit"


def test_registry_execution_blocks_tool_with_incomplete_manifest(tmp_path):
    with pytest.raises(ValueError, match="description is required"):
        ToolModelSpec(
            name="missing_manifest",
            description="",
            input_schema={"type": "object"},
        )


def test_registry_execution_blocks_path_gate_before_tool_execute(tmp_path):
    workspace = tmp_path / "workspace"
    danger = tmp_path / "danger"
    workspace.mkdir()
    danger.mkdir()
    outside = danger / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    result = execute_canonical_test_call(
        workspace,
        tools={"echo": EchoTool()},
        tool_name="echo",
        arguments={"path": "link"},
        workspace_roots=(workspace,),
        path_dangerous_roots=(str(danger),),
    ).result

    assert result.ok is False
    assert result.handler_executed is False
    assert result.metadata["action_decision"]["evidence"]["gate"]["gate"] == "path_url_command"


def test_registry_execution_blocks_cross_owner_path_before_tool_execute(tmp_path):
    home = tmp_path / ".my-agent"
    owner_a = home / "owners" / "providers" / "feishu" / "users" / "user-a"
    owner_b = home / "owners" / "providers" / "feishu" / "users" / "user-b"
    workspace = owner_b / "tasks" / "task-b"
    workspace.mkdir(parents=True)
    owner_a.mkdir(parents=True)

    result = _execute(
        workspace,
        {"tool": "echo", "path": str(owner_a)},
        owner_scope_root=str(owner_b),
    )

    assert result.ok is False
    assert result.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert result.metadata["action_decision"]["evidence"]["gate"]["gate"] == "path_url_command"
    assert result.handler_executed is False


def test_registry_execution_applies_manifest_file_url_policy_without_bypass(tmp_path):
    owner_a = tmp_path / ".my-agent" / "owners" / "user-a"
    owner_b = tmp_path / ".my-agent" / "owners" / "user-b"
    owner_a.mkdir(parents=True)
    owner_b.mkdir(parents=True)
    own_source = owner_a / "events.jsonl"
    other_source = owner_b / "events.jsonl"
    own_source.write_text("{}\n", encoding="utf-8")
    other_source.write_text("{}\n", encoding="utf-8")

    allowed = _execute(
        owner_a,
        {"tool": "local_file_url", "url": own_source.as_uri()},
        tool=LocalFileUrlTool(),
        owner_scope_root=str(owner_a),
    )
    cross_owner = _execute(
        owner_a,
        {"tool": "local_file_url", "url": other_source.as_uri()},
        tool=LocalFileUrlTool(),
        owner_scope_root=str(owner_a),
    )

    assert allowed.ok is True
    assert cross_owner.ok is False
    assert cross_owner.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert cross_owner.handler_executed is False


def test_registry_execution_allows_trusted_workspace_outside_owner_home(tmp_path):
    home = tmp_path / ".my-agent"
    owner = home / "owners" / "providers" / "feishu" / "users" / "user-a"
    workspace = tmp_path / "explicit-cli-project"
    workspace.mkdir(parents=True)
    target = workspace / "README.md"
    target.write_text("trusted workspace", encoding="utf-8")

    result = _execute(
        workspace,
        {"tool": "echo", "path": str(target)},
        owner_scope_root=str(owner),
    )

    assert result.ok is True
    assert result.handler_executed is True


def test_registry_execution_blocks_dangerous_real_tool_without_approval(tmp_path):
    result = _execute(
        tmp_path,
        {"tool": "echo", "value": 1},
        tool=DangerousEchoTool(),
    )

    assert result.ok is False
    assert result.status == "approval_required"
    assert result.metadata["action_decision"]["status"] == "ask"
