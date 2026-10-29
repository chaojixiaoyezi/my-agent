# LLM: 完整目录由原快照生成；live 缩短不能改变原生 Schema、身份边界或外置归档内容。
# 模块用途: 验证工具 manifest 的完整合同、紧凑卡片及真实归档恢复。
from __future__ import annotations

import json

from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    SandboxPolicy,
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
    assert payload["permission_scope"] == "same_as_root_agent"
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
    assert policy["approval_policy"] == {"mode": "dangerous", "always_effects": [], "owner_grant_parameters": []}
    assert policy["sandbox_policy"] == {
        "mode": "required",
        "contained_by_parameter": [{"field": "path", "values": ["host"]}],
    }
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
        owner_type="user",
    )

    payload = tool_manifest_payload(snapshot)

    assert payload["permission_scope"] == "owner_scoped"
    assert payload["visible_tools"] == ["read_file"]
    assert payload["tools"][0]["permission_scope"] == "owner_scoped"


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
            sandbox_policy=SandboxPolicy(
                "required",
                contained_by_parameter=(("path", ("host",)),),
            ),
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


# LLM: 使用真实装配和同一请求快照；完整参数及身份留在归档，live 仅删逐工具重复描述。
# 函数用途: 固定 v4 短卡目录及正规详情入口，不把折叠误说成无权限。
def test_list_tools_live_cards_preserve_full_archive_and_authority(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    registry = SimpleAgent(AgentConfig(enable_tools=True, memory_path=str(tmp_path / "m.jsonl")), str(tmp_path)).tools
    snapshot = registry.runtime_snapshot()
    result = registry.tools["tool_search"]._execute_snapshot({"action": "catalog"}, snapshot)
    full = json.loads(result.output)
    live = json.loads(result.result_envelope["tool_output_policy"]["live_prompt_output"])
    policy = result.result_envelope["tool_output_policy"]
    assert policy["preserve_prompt_output"] is False
    assert len(policy["live_prompt_output"].encode("utf-8")) <= 12000
    assert "tool_search" in live["detail_ref"]
    assert live["schema_name"] == "tool_runtime_manifest_live" and live["schema_version"] == 4
    assert all("input_schema" in item for item in full["tools"])
    assert "tool_states" in full["self_context"] and "tool_states" not in live["self_context"]
    self_keys = {"schema_version", "run_id", "owner_type", "scope", "permissions", "identity", "filesystem", "network"}
    assert set(live["self_context"]) == self_keys
    assert live["self_context"] == {key: full["self_context"][key] for key in self_keys}
    direct = {spec.name for spec in registry.model_visible_specs(runtime_snapshot=snapshot)}
    assert live["counts"] == {"registered": len(snapshot.runtimes), "available": len(full["executable_tools"]), "direct": len(direct)}
    assert {item["name"] for item in live["tools"]} == set(full["executable_tools"])
    assert all(set(item) == {"name", "category", "purpose", "schema_visibility"} for item in live["tools"])
    assert all(0 < len(item["purpose"]) <= 90 for item in live["tools"])
    assert all(item["schema_visibility"] == ("direct" if item["name"] in direct else "discoverable") for item in live["tools"])
    assert {"tool_search", "self_map", "read_artifact"} <= set(live["detail_policy"])
    assert live["unavailable_tools"] == full["unavailable_tools"]


# LLM: 真实外置器及正式读口兼容内联/blob；逐字核对全文、SHA及定义，不以JSON包装的旧content槽位限定存储格式。
# 函数用途: 验证两种发现工具的完整归档可实际恢复，保留短卡和权限边界而不停用大正文blob。
def test_discovery_full_output_survives_real_externalization(tmp_path):
    import hashlib
    from pathlib import Path

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.memory_archive.artifact.reader import (
        ReadToolOutputArtifactRequest,
        read_tool_output_artifact,
    )
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )
    from agent_py_agent.agent.settings import AgentConfig

    registry = SimpleAgent(AgentConfig(enable_tools=True, memory_path=str(tmp_path / "m.jsonl")), str(tmp_path)).tools
    for index, (name, params) in enumerate((("tool_search", {"action": "catalog"}), ("tool_search", {"names": ["watch_stream"]}))):
        outcome = registry.tools[name].execute(params)
        assert outcome.ok
        record = externalize_tool_output_record(ExternalizeToolOutputRequest(
            root=tmp_path, tool=name, call_id=f"{name}-{index}", output=outcome.output, ok=True,
            result_envelope=outcome.result_envelope))
        assert record["output_externalized"] is True
        wrapper = json.loads(Path(record["output_path"]).read_text())
        if wrapper.get("artifact_format") == "blob_v1":
            assert "content" not in wrapper
            assert Path(record["output_path"]).with_suffix(".blob").is_file()
        stored = read_tool_output_artifact(ReadToolOutputArtifactRequest(
            root=tmp_path, artifact_ref=record["artifact_ref"], max_chars=0))
        assert stored["ok"] and stored["content_hash_verified"]
        assert stored["sha256"] == record["output_hash"] == hashlib.sha256(outcome.output.encode("utf-8")).hexdigest()
        assert stored["size_bytes"] == len(outcome.output.encode("utf-8"))
        assert stored["content"] == outcome.output
        assert all("input_schema" in item for item in json.loads(stored["content"])["tools"])
        live = outcome.result_envelope["tool_output_policy"]["live_prompt_output"]
        assert json.loads(live)["schema_version"] == 4
        assert "input_schema" not in live
