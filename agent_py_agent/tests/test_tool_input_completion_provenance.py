from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import (
    _compact_result_envelope,
)
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_SOURCE_OPEN_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
)
from agent_py_agent.agent.contracts.tool_input_schema import normalize_tool_input
from agent_py_agent.agent.ingestion.watch_tool_spec import (
    build_watch_stream_model_spec,
    build_watch_stream_runtime_policy,
)
from agent_py_agent.agent.tooling.artifact import ReadArtifactTool
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)
from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.tooling.tool_input_completion import (
    ToolInputCompletionContext,
    complete_tool_arguments,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class _CaptureTool(BaseTool):
    def __init__(self, model_spec: ToolModelSpec, runtime_policy: ToolRuntimePolicy) -> None:
        self.model_spec = model_spec
        self.runtime_policy = runtime_policy
        self.last_params: dict[str, object] = {}

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.last_params = dict(params)
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(params, ensure_ascii=False, sort_keys=True),
        )


def _contract(
    *,
    input_schema: dict[str, object] | None = None,
    safe_defaults: dict[str, object] | None = None,
    trusted_bindings: dict[str, TrustedParameterBinding] | None = None,
) -> tuple[ToolModelSpec, ToolRuntimePolicy]:
    schema = input_schema or {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "action"},
            "query": {"type": "string", "description": "query"},
            "limit": {"type": "integer", "minimum": 1, "description": "limit"},
            "working_dir": {"type": "string", "description": "working dir"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    model_spec = make_test_model_spec(
        "capture",
        description="capture",
        input_schema=schema,
    )
    runtime_policy = replace(
        make_test_runtime_policy("read_only"),
        input_policy=ToolInputPolicy(
            safe_parameter_defaults=tuple((safe_defaults or {}).items()),
            trusted_parameter_bindings=tuple((trusted_bindings or {}).items()),
        ),
    )
    return model_spec, runtime_policy


def _complete(
    arguments: dict[str, object],
    model_spec: ToolModelSpec,
    runtime_policy: ToolRuntimePolicy,
    context: ToolInputCompletionContext,
):
    tool = _CaptureTool(model_spec, runtime_policy)
    completed = complete_tool_arguments(
        arguments,
        ToolRuntime(model_spec, runtime_policy, tool),
        context,
    )
    normalized = normalize_tool_input(completed.value, model_spec.input_schema)
    return normalized.value, completed.sources


def test_safe_default_is_applied_from_runtime_policy_before_validation() -> None:
    model_spec, runtime_policy = _contract(safe_defaults={"limit": 25})

    value, sources = _complete(
        {"query": "demo"},
        model_spec,
        runtime_policy,
        ToolInputCompletionContext(
            call_id="call-1",
            call_source="native",
        ),
    )

    assert value["limit"] == 25
    assert [item.to_dict() for item in sources] == [
        {
            "path": "$.query",
            "source": "model_proposed",
            "source_ref": "tool_call:call-1#/input/query",
        },
        {
            "path": "$.limit",
            "source": "safe_default",
            "source_ref": "tool_runtime:capture#/input_policy/safe_parameter_defaults/limit",
        },
    ]


def test_explicit_value_is_never_overwritten_by_default_or_trusted_context() -> None:
    model_spec, runtime_policy = _contract(
        safe_defaults={"limit": 25},
        trusted_bindings={
            "working_dir": TrustedParameterBinding(source_refs=("write_boundary.task_root",))
        },
    )

    value, input_sources = _complete(
        {
            "query": "demo",
            "limit": 3,
            "working_dir": "",
        },
        model_spec,
        runtime_policy,
        ToolInputCompletionContext(
            call_id="call-2",
            trusted_context={
                "write_boundary": {"task_root": "/trusted/task"},
            },
        ),
    )

    assert value["limit"] == 3
    assert value["working_dir"] == ""
    sources = {item.path: item.source for item in input_sources}
    assert sources["$.limit"] == "model_proposed"
    assert sources["$.working_dir"] == "model_proposed"


def test_host_authoritative_binding_replaces_model_value_before_handler(
    tmp_path: Path,
) -> None:
    model_spec, runtime_policy = _contract(
        trusted_bindings={
            "working_dir": TrustedParameterBinding(
                source_refs=("registry.effective_cwd",),
                authority="host_authoritative",
            )
        },
    )
    tool = _CaptureTool(model_spec, runtime_policy)

    execution = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo", "working_dir": "/model/guess"},
        trusted_run_context={},
    )

    assert execution.result.ok is True
    assert tool.last_params["working_dir"] == str(tmp_path)
    sources = {
        item["path"]: item for item in execution.result.metadata["input_sources"]
    }
    assert sources["$.working_dir"] == {
        "path": "$.working_dir",
        "source": "trusted_context",
        "source_ref": "registry.effective_cwd",
    }


def test_must_match_binding_rejects_conflict_before_handler(tmp_path: Path) -> None:
    model_spec, runtime_policy = _contract(
        trusted_bindings={
            "working_dir": TrustedParameterBinding(
                source_refs=("registry.effective_cwd",),
                authority="must_match",
            )
        },
    )
    tool = _CaptureTool(model_spec, runtime_policy)

    execution = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo", "working_dir": "/other"},
    )

    assert execution.result.ok is False
    assert execution.result.error_code == "TOOL_TRUSTED_PARAMETER_CONFLICT"
    assert execution.result.handler_executed is False
    assert execution.result.failure_stage == "validation"
    assert tool.last_params == {}


def test_internal_trusted_binding_is_injected_but_cannot_be_model_supplied(
    tmp_path: Path,
) -> None:
    model_spec = make_test_model_spec(
        "capture",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    runtime_policy = replace(
        make_test_runtime_policy("read_only"),
        input_policy=ToolInputPolicy(
            internal_parameters=("run_id",),
            trusted_parameter_bindings=((
                "run_id",
                TrustedParameterBinding(
                    source_refs=("run_scope.run_id",),
                    authority="host_authoritative",
                ),
            ),),
        ),
    )
    tool = _CaptureTool(model_spec, runtime_policy)

    injected = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo"},
        run_id="trusted-run",
    )
    spoofed = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo", "run_id": "spoofed"},
        run_id="trusted-run",
    )

    assert injected.result.ok is True
    assert tool.last_params["run_id"] == "trusted-run"
    assert spoofed.result.error_code == "TOOL_INTERNAL_PARAMETER_FORBIDDEN"
    assert spoofed.result.handler_executed is False


def test_trusted_context_binding_uses_first_present_ref_and_exact_condition() -> None:
    model_spec, runtime_policy = _contract(
        trusted_bindings={
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

    started, started_sources = _complete(
        {"query": "demo", "action": "start"},
        model_spec,
        runtime_policy,
        context,
    )
    read, _read_sources = _complete(
        {"query": "demo", "action": "read"},
        model_spec,
        runtime_policy,
        context,
    )

    assert started["working_dir"] == "/workspace"
    assert started_sources[-1].to_dict() == {
        "path": "$.working_dir",
        "source": "trusted_context",
        "source_ref": "registry.workspace_root",
    }
    assert "working_dir" not in read


def test_audit_source_open_uses_host_selected_transport_without_model_rewrite(
    tmp_path: Path,
) -> None:
    binding = {
        "source_id": "source-a",
        "url": "https://source.example.invalid/events",
        "mode": "cursor",
        "http_request": {
            "method": "GET",
            "cursor_binding": {
                "location": "query",
                "name": "position",
                "initial": 0,
            },
        },
        "record_list_field": "items",
        "cursor_field": "next_position",
    }
    tool = _CaptureTool(
        build_watch_stream_model_spec(surface="audit_binding"),
        build_watch_stream_runtime_policy(surface="audit_binding"),
    )

    result = execute_canonical_test_call(
        tmp_path,
        tools={"watch_stream": tool},
        tool_name="watch_stream",
        arguments={"action": "open"},
        operation_store_required=False,
        trusted_run_context={
            "task_attributes": {AUDIT_SOURCE_OPEN_ATTR: binding}
        },
        call_id="call-audit-source-open",
    ).result

    assert result.ok is True
    assert {
        name: tool.last_params[name]
        for name in ("action", *binding)
    } == {"action": "open", **binding}
    sources = {
        item["path"]: item
        for item in result.metadata["input_sources"]
    }
    assert sources["$.action"]["source"] == "model_proposed"
    for name in binding:
        assert sources[f"$.{name}"] == {
            "path": f"$.{name}",
            "source": "trusted_context",
            "source_ref": f"run_scope.task_attributes.audit_source_open.{name}",
        }


def test_audit_source_worker_uses_host_bound_watch_id_when_model_omits_it(
    tmp_path: Path,
) -> None:
    tool = _CaptureTool(
        build_watch_stream_model_spec(surface="audit_source"),
        build_watch_stream_runtime_policy(surface="audit_source"),
    )

    result = execute_canonical_test_call(
        tmp_path,
        tools={"watch_stream": tool},
        tool_name="watch_stream",
        arguments={"action": "status"},
        operation_store_required=False,
        trusted_run_context={
            "task_attributes": {AUDIT_SOURCE_WATCH_ID_ATTR: "ws-0123456789"}
        },
        call_id="call-audit-source-status",
    ).result

    assert result.ok is True
    assert {
        name: tool.last_params[name]
        for name in ("action", "watch_id")
    } == {
        "action": "status",
        "watch_id": "ws-0123456789",
    }
    sources = {
        item["path"]: item
        for item in result.metadata["input_sources"]
    }
    assert sources["$.watch_id"] == {
        "path": "$.watch_id",
        "source": "trusted_context",
        "source_ref": "run_scope.task_attributes.audit_source_watch_id",
    }


@pytest.mark.parametrize(
    "surface",
    ["ordinary", "audit_prepare", "audit_coordinator"],
)
def test_non_source_watch_surfaces_do_not_inherit_source_worker_bindings(
    surface: str,
) -> None:
    policy = build_watch_stream_runtime_policy(surface=surface)

    assert policy.input_policy.trusted_parameter_bindings == ()


def test_audit_source_binding_surface_has_only_its_required_host_facts() -> None:
    first_open = build_watch_stream_runtime_policy(surface="audit_binding")
    continuing = build_watch_stream_runtime_policy(surface="audit_source")

    first_bindings = dict(first_open.input_policy.trusted_parameter_bindings)
    continuing_bindings = dict(continuing.input_policy.trusted_parameter_bindings)
    assert "watch_id" in first_bindings
    assert "url" in first_bindings
    assert set(continuing_bindings) == {"watch_id"}


@pytest.mark.parametrize(
    ("safe_defaults", "trusted_bindings", "error"),
    [
        ({"missing": 1}, {}, "undeclared parameter"),
        ({"limit": "25"}, {}, "violates schema"),
        (
            {},
            {"working_dir": TrustedParameterBinding(source_refs=("model.input.cwd",))},
            "invalid trusted source_ref",
        ),
        (
            {"working_dir": "."},
            {"working_dir": TrustedParameterBinding(source_refs=("registry.workspace_root",))},
            "both default and trusted binding",
        ),
    ],
)
def test_invalid_completion_contract_fails_closed(
    safe_defaults: dict[str, object],
    trusted_bindings: dict[str, TrustedParameterBinding],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        model_spec, runtime_policy = _contract(
            safe_defaults=safe_defaults,
            trusted_bindings=trusted_bindings,
        )
        ToolRuntime(
            model_spec,
            runtime_policy,
            _CaptureTool(model_spec, runtime_policy),
        )


def test_registry_uses_workspace_as_effective_cwd_without_task_write_scope(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    model_spec, runtime_policy = _contract(
        trusted_bindings={
            "working_dir": TrustedParameterBinding(source_refs=("registry.effective_cwd",))
        },
    )
    tool = _CaptureTool(model_spec, runtime_policy)

    result = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo"},
        workspace_roots=(tmp_path, task_root),
        write_boundary={"task_root": str(task_root)},
        call_id="call-registry",
    ).result

    assert result.ok is True
    assert tool.last_params["working_dir"] == str(tmp_path)
    assert result.metadata["input_sources"] == [
        {
            "path": "$.query",
            "source": "model_proposed",
            "source_ref": "tool_call:call-registry#/input/query",
        },
        {
            "path": "$.working_dir",
            "source": "trusted_context",
            "source_ref": "registry.effective_cwd",
        },
    ]


def test_registry_keeps_user_workspace_as_cwd_for_task_scoped_writes(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    task_output = task_root / "output"
    task_output.mkdir(parents=True)
    model_spec, runtime_policy = _contract(
        trusted_bindings={
            "working_dir": TrustedParameterBinding(source_refs=("registry.effective_cwd",))
        },
    )
    tool = _CaptureTool(model_spec, runtime_policy)

    result = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo"},
        workspace_roots=(tmp_path, task_root),
        write_boundary={
            "task_root": str(task_root),
            "allowed_write_roots": [str(task_output)],
        },
        call_id="call-task-registry",
    ).result

    assert result.ok is True
    assert tool.last_params["working_dir"] == str(tmp_path)
    assert result.metadata["input_sources"][-1] == {
        "path": "$.working_dir",
        "source": "trusted_context",
        "source_ref": "registry.effective_cwd",
    }


def test_missing_unsafe_required_parameter_still_fails_schema_gate(
    tmp_path: Path,
) -> None:
    model_spec, runtime_policy = _contract()
    tool = _CaptureTool(model_spec, runtime_policy)

    result = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={},
    ).result

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert tool.last_params == {}


def test_schema_default_annotation_alone_never_authorizes_completion(
    tmp_path: Path,
) -> None:
    model_spec, runtime_policy = _contract(
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
    tool = _CaptureTool(model_spec, runtime_policy)

    result = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={"query": "demo"},
    ).result

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert tool.last_params == {}


def test_model_cannot_spoof_parameter_source_metadata(tmp_path: Path) -> None:
    model_spec, runtime_policy = _contract()
    tool = _CaptureTool(model_spec, runtime_policy)

    result = execute_canonical_test_call(
        tmp_path,
        tools={"capture": tool},
        tool_name="capture",
        arguments={
            "query": "demo",
            "input_sources": [
                {
                    "path": "$.query",
                    "source": "trusted_context",
                    "source_ref": "registry.workspace_root",
                }
            ],
        },
    ).result

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert tool.last_params == {}


def test_archive_compaction_preserves_value_free_input_audit() -> None:
    call = canonical_history_call(
        "capture",
        {},
        source_protocol="native",
    )
    result = canonical_history_result(
        call,
        "ok",
        handler_details={
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
            "registry": {
                "workspace_root": str(tmp_path),
                "effective_cwd": str(tmp_path / "task"),
            },
            "run_scope": {
                "request_id": "req-builtins",
                "task_id": "task-builtins",
                "run_id": "run-builtins",
            },
        },
    )

    def complete(tool: BaseTool, arguments: dict[str, object]):
        return complete_tool_arguments(
            arguments,
            ToolRuntime(
                model_spec=tool.model_spec,
                runtime_policy=tool.runtime_policy,
                handler=tool,
                availability=ToolAvailability.ready(),
            ),
            context,
        )

    shell_call = complete(shell, {"command": "pwd"})
    terminal_start = complete(
        terminal,
        {"action": "start", "command": "python -q"},
    )
    terminal_read = complete(
        terminal,
        {"action": "read", "session_id": "pty-1"},
    )
    artifact_call = complete_tool_arguments(
        {"artifact_ref": "run-builtins:1-1"},
        ToolRuntime(
            model_spec=artifact.model_spec,
            runtime_policy=artifact.runtime_policy,
            handler=artifact,
            availability=ToolAvailability.ready(),
        ),
        context,
    )

    assert shell_call.value["timeout"] == 47
    assert shell_call.value["run_in_background"] is False
    assert shell_call.value["working_dir"] == str(tmp_path / "task")
    assert terminal_start.value["working_dir"] == str(tmp_path / "task")
    assert "working_dir" not in terminal_read.value
    assert {
        key: artifact_call.value[key]
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
