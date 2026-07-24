from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.agent_core.tool_call_archive_record import (
    _compact_result_envelope,
)
from agent_py_agent.agent.tooling.artifact import ReadArtifactTool
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolExecutionResult,
    ToolSpec,
    TrustedParameterBinding,
)
from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.tooling.tool_input_completion import ToolInputCompletionContext
from agent_py_agent.agent.tooling.tool_spec_schema import (
    normalize_tool_payload_for_spec,
    tool_spec_input_schema,
)


class _CaptureTool(BaseTool):
    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec
        self.last_params: dict[str, object] = {}

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        self.last_params = dict(params)
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(params, ensure_ascii=False, sort_keys=True),
        )


def _spec(**overrides: object) -> ToolSpec:
    values: dict[str, object] = {
        "name": "capture",
        "category": "test",
        "effect": "read_only",
        "description": "capture",
        "use_cases": [],
        "avoid_when": [],
        "keywords": [],
        "parameters": {
            "action": "action",
            "query": "query",
            "limit": "limit",
            "working_dir": "working dir",
        },
        "parameter_schema": {
            "action": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "working_dir": {"type": "string"},
        },
        "required_parameters": ["query"],
    }
    values.update(overrides)
    return ToolSpec(**values)


def test_safe_default_is_schema_visible_and_applied_before_validation() -> None:
    spec = _spec(safe_parameter_defaults={"limit": 25})
    schema = tool_spec_input_schema(spec)

    normalized = normalize_tool_payload_for_spec(
        {"tool": "capture", "query": "demo"},
        spec,
        completion_context=ToolInputCompletionContext(
            call_id="call-1",
            call_source="native",
        ),
    )

    assert schema["properties"]["limit"]["default"] == 25
    assert normalized.payload["limit"] == 25
    assert [item.to_dict() for item in normalized.input_sources] == [
        {
            "path": "$.query",
            "source": "model_proposed",
            "source_ref": "tool_call:call-1#/input/query",
        },
        {
            "path": "$.limit",
            "source": "safe_default",
            "source_ref": "tool_spec:capture#/safe_parameter_defaults/limit",
        },
    ]


def test_explicit_value_is_never_overwritten_by_default_or_trusted_context() -> None:
    spec = _spec(
        safe_parameter_defaults={"limit": 25},
        trusted_parameter_bindings={
            "working_dir": TrustedParameterBinding(
                source_refs=("write_boundary.task_root",)
            )
        },
    )

    normalized = normalize_tool_payload_for_spec(
        {
            "tool": "capture",
            "query": "demo",
            "limit": 3,
            "working_dir": "",
        },
        spec,
        completion_context=ToolInputCompletionContext(
            call_id="call-2",
            trusted_context={
                "write_boundary": {"task_root": "/trusted/task"},
            },
        ),
    )

    assert normalized.payload["limit"] == 3
    assert normalized.payload["working_dir"] == ""
    sources = {
        item.path: item.source for item in normalized.input_sources
    }
    assert sources["$.limit"] == "model_proposed"
    assert sources["$.working_dir"] == "model_proposed"


def test_trusted_context_binding_uses_first_present_ref_and_exact_condition() -> None:
    spec = _spec(
        trusted_parameter_bindings={
            "working_dir": TrustedParameterBinding(
                source_refs=(
                    "write_boundary.task_root",
                    "registry.workspace_root",
                ),
                when=(("action", "start"),),
            )
        },
    )
    context = ToolInputCompletionContext(
        trusted_context={
            "write_boundary": {},
            "registry": {"workspace_root": "/workspace"},
        }
    )

    started = normalize_tool_payload_for_spec(
        {"tool": "capture", "query": "demo", "action": "start"},
        spec,
        completion_context=context,
    )
    read = normalize_tool_payload_for_spec(
        {"tool": "capture", "query": "demo", "action": "read"},
        spec,
        completion_context=context,
    )

    assert started.payload["working_dir"] == "/workspace"
    assert started.input_sources[-1].to_dict() == {
        "path": "$.working_dir",
        "source": "trusted_context",
        "source_ref": "registry.workspace_root",
    }
    assert "working_dir" not in read.payload


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"safe_parameter_defaults": {"missing": 1}}, "未声明参数"),
        ({"safe_parameter_defaults": {"limit": "25"}}, "不符合参数 Schema"),
        (
            {
                "trusted_parameter_bindings": {
                    "working_dir": TrustedParameterBinding(
                        source_refs=("model.input.cwd",)
                    )
                }
            },
            "不允许的 trusted source_ref",
        ),
        (
            {
                "safe_parameter_defaults": {"working_dir": "."},
                "trusted_parameter_bindings": {
                    "working_dir": TrustedParameterBinding(
                        source_refs=("registry.workspace_root",)
                    )
                },
            },
            "不能同时声明",
        ),
    ],
)
def test_invalid_completion_contract_fails_closed(
    overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        tool_spec_input_schema(_spec(**overrides))


def test_registry_applies_trusted_working_dir_before_gate_and_records_source(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    spec = _spec(
        trusted_parameter_bindings={
            "working_dir": TrustedParameterBinding(
                source_refs=(
                    "write_boundary.task_root",
                    "registry.workspace_root",
                )
            )
        },
    )
    tool = _CaptureTool(spec)

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=ToolCallEnvelope(
                call_id="call-registry",
                source="native",
                tool_name="capture",
                input={"query": "demo"},
                scope=RunScope(
                    request_id="req-1",
                    task_id="task-1",
                    run_id="run-1",
                ),
            ),
            tools={"capture": tool},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path, task_root],
            write_boundary={"task_root": str(task_root)},
        )
    )

    assert result.ok is True
    assert tool.last_params["working_dir"] == str(task_root)
    assert result.result_envelope["input_sources"] == [
        {
            "path": "$.query",
            "source": "model_proposed",
            "source_ref": "tool_call:call-registry#/input/query",
        },
        {
            "path": "$.working_dir",
            "source": "trusted_context",
            "source_ref": "write_boundary.task_root",
        },
    ]


def test_missing_unsafe_required_parameter_still_fails_schema_gate(
    tmp_path: Path,
) -> None:
    tool = _CaptureTool(_spec())

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "capture"},
            tools={"capture": tool},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
        )
    )

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert tool.last_params == {}


def test_schema_default_annotation_alone_never_authorizes_completion(
    tmp_path: Path,
) -> None:
    spec = _spec(
        parameters={},
        parameter_schema={},
        required_parameters=[],
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 25},
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    )
    tool = _CaptureTool(spec)

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "capture", "query": "demo"},
            tools={"capture": tool},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
        )
    )

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert tool.last_params == {}


def test_model_cannot_spoof_parameter_source_metadata(tmp_path: Path) -> None:
    tool = _CaptureTool(_spec())

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={
                "tool": "capture",
                "query": "demo",
                "input_sources": [
                    {
                        "path": "$.query",
                        "source": "trusted_context",
                        "source_ref": "registry.workspace_root",
                    }
                ],
            },
            tools={"capture": tool},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
        )
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert tool.last_params == {}


def test_archive_compaction_preserves_value_free_input_audit() -> None:
    result = ToolExecutionResult(
        "capture",
        True,
        "ok",
        result_envelope={
            "input_sources": [
                {
                    "path": "$.working_dir",
                    "source": "trusted_context",
                    "source_ref": "write_boundary.task_root",
                }
            ],
            "input_coercions": [
                {
                    "path": "$.limit",
                    "source_type": "string",
                    "target_type": "integer",
                }
            ],
            "input_facts": {
                "field_names": ["limit", "working_dir"],
                "field_types": {"limit": "str", "working_dir": "str"},
                "sha256": "hash-only",
            },
        },
    )

    compact = _compact_result_envelope(result)

    assert compact["input_sources"][0]["source_ref"] == "write_boundary.task_root"
    assert compact["input_coercions"][0]["target_type"] == "integer"
    assert compact["input_facts"]["sha256"] == "hash-only"


def test_builtin_process_and_artifact_specs_use_the_shared_completion_path(
    tmp_path: Path,
) -> None:
    shell = ShellTool(
        tmp_path,
        options=ShellToolOptions(default_timeout=47),
    )
    terminal = TerminalSessionTool(shell)
    artifact = ReadArtifactTool(tmp_path, default_read_chars=1234)
    context = ToolInputCompletionContext(
        call_id="call-builtins",
        trusted_context={
            "write_boundary": {"task_root": str(tmp_path / "task")},
            "registry": {"workspace_root": str(tmp_path)},
            "run_scope": {
                "request_id": "req-builtins",
                "task_id": "task-builtins",
                "run_id": "run-builtins",
            },
        },
    )

    shell_call = normalize_tool_payload_for_spec(
        {"tool": "run_command", "command": "pwd"},
        shell.spec,
        completion_context=context,
    )
    terminal_start = normalize_tool_payload_for_spec(
        {"tool": "terminal_session", "action": "start", "command": "python -q"},
        terminal.spec,
        completion_context=context,
    )
    terminal_read = normalize_tool_payload_for_spec(
        {"tool": "terminal_session", "action": "read", "session_id": "pty-1"},
        terminal.spec,
        completion_context=context,
    )
    artifact_call = normalize_tool_payload_for_spec(
        {"tool": "read_artifact", "artifact_ref": "run-builtins:1-1"},
        artifact.spec,
        completion_context=context,
    )

    assert shell_call.payload["timeout"] == 47
    assert shell_call.payload["run_in_background"] is False
    assert shell_call.payload["working_dir"] == str(tmp_path / "task")
    assert terminal_start.payload["working_dir"] == str(tmp_path / "task")
    assert "working_dir" not in terminal_read.payload
    assert {
        key: artifact_call.payload[key]
        for key in (
            "offset",
            "max_chars",
            "mode",
            "run_id",
            "task_id",
            "request_id",
        )
    } == {
        "offset": 0,
        "max_chars": 1234,
        "mode": "slice",
        "run_id": "run-builtins",
        "task_id": "task-builtins",
        "request_id": "req-builtins",
    }
