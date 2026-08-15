from __future__ import annotations

from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    TimeoutPolicy,
    ToolRuntimePolicy,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    runtime_snapshot_for_model_specs,
)


def test_tool_manifest_payload_includes_failure_contracts() -> None:
    payload = _tool_manifest_payload()

    assert payload["visible_tools"] == ["read_file", "write_file"]
    assert payload["executable_tools"] == ["read_file", "write_file"]
    assert payload["permission_mode"] == "same_as_root_agent"
    assert payload["failure_taxonomy"] == sorted(payload["failure_taxonomy"])
    assert "PATH_INVALID" in payload["failure_taxonomy"]
    failure_contracts = {item["code"]: item for item in payload["failure_contracts"]}
    assert failure_contracts["TOOL_UNAVAILABLE"]["recommended_action"] == "request_capability"
    assert failure_contracts["APPROVAL_REQUIRED"]["retryable"] is True
    assert payload["tools"][0]["visible_in_context"] is True


def test_tool_manifest_projects_runtime_policy_without_duplicate_approval_flag() -> None:
    payload = _tool_manifest_payload()
    write_manifest = payload["tools"][1]
    policy = write_manifest["runtime_policy"]

    assert policy["effect_resolver"]["default_effect"] == "mutating"
    assert policy["approval_policy"] == {"mode": "dangerous"}
    assert policy["idempotency_policy"] == {"scope": "operation"}
    assert policy["timeout_policy"] == {"seconds": 20}
    assert policy["output_policy"]["refs"] == ["file"]
    assert "requires_approval" not in write_manifest
    assert write_manifest["schema_hash"].startswith("sha256:")
    assert set(write_manifest["input_schema"]["required"]) == {"content", "path"}


def test_tool_manifest_requires_runtime_snapshot() -> None:
    try:
        tool_manifest_payload([{"name": "read_file"}])
    except TypeError as exc:
        assert "ToolRuntimeSnapshot" in str(exc)
    else:  # pragma: no cover - explicit authority assertion
        raise AssertionError("dict/spec manifests must not be accepted")


def test_tool_manifest_payload_uses_owner_scoped_permission_mode_for_non_root_owner() -> None:
    read_spec = make_test_model_spec("read_file")
    snapshot = runtime_snapshot_for_model_specs(
        (read_spec,),
        owner_type="task_local",
    )

    payload = tool_manifest_payload(snapshot)

    assert payload["permission_mode"] == "owner_scoped"
    assert payload["tools"][0]["permission_mode"] == "owner_scoped"


def _tool_manifest_payload() -> dict[str, object]:
    read_spec = make_test_model_spec(
        "read_file",
        description="Read a file",
        category="filesystem",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "绝对路径"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        examples=('{"tool":"read_file"}',),
    )
    write_spec = make_test_model_spec(
        "write_file",
        description="Write a file",
        category="filesystem",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "绝对路径"},
                "content": {"type": "string", "description": "内容"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        examples=('{"tool":"write_file"}',),
    )
    policies = {
        "read_file": ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("read_only"),
        ),
        "write_file": ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"),
            approval_policy=ApprovalPolicy("dangerous"),
            idempotency_policy=IdempotencyPolicy("operation"),
            timeout_policy=TimeoutPolicy(20),
            output_policy=OutputPolicy(refs=("file",)),
        ),
    }
    snapshot = runtime_snapshot_for_model_specs(
        (read_spec, write_spec),
        policies=policies,
        allowed_tools=["read_file", "write_file"],
    )
    return tool_manifest_payload(snapshot)
