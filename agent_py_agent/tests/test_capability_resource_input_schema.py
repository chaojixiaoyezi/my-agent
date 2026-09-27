"""包资源的模型声明与原执行器校验同源；只用临时安装和本地组件，不调用模型。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_anthropic_tool
from agent_py_agent.agent.capability.package_resources import package_resource_reference
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_capability_package_task_refs import _agent

_REFERENCE_FIELDS = (
    "kind", "stable_id", "name", "source", "content_sha256", "package_id",
    "activation_id", "resource_path", "resource_sha256",
)


# LLM: 通过真实 core/registry 装配来源和 schema，计数仅观察 handler/resolver；不能给工具补测试专用字段声明。
# 函数用途: 在隔离 owner 安装包并返回原工具，检验模型参数是否在来源读取之前被原执行器拒绝。
@pytest.fixture
def resource_tool(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path, method_body=b"\x00exact\xff\r\n")
    package = agent.current_skill_snapshot().resolve_package("story-a")
    reference = package_resource_reference(package, "methods/SKILL.md")
    tool = agent.tools.tools["write_file"]
    calls = {"handler": 0, "resolver": 0}
    execute, resolve = tool.execute, tool.source_resolver

    # LLM: 只观察原 handler，不补参数或替换执行结果。
    # 函数用途: 证明结构错误在文件操作开始前被拒绝。
    def observe_execute(params):
        calls["handler"] += 1
        return execute(params)

    # LLM: 计数后仍调用 core 绑定的原解析器，不能扩大当前 owner 的包范围。
    # 函数用途: 区分 schema 门拒绝与来源校验拒绝。
    def observe_resolve(value):
        calls["resolver"] += 1
        return resolve(value)

    monkeypatch.setattr(tool, "execute", observe_execute)
    monkeypatch.setattr(tool, "source_resolver", observe_resolve)
    return agent, tool, reference, calls


# LLM: 所有权限、参数和 handler 是否执行的事实来自原 ToolExecutor，不直接调用解析器伪装集成通过。
# 函数用途: 在 Agent 的当前授权工作区提交一次规范写调用，目标只落到 pytest 临时目录。
def _invoke(agent, tool, reference):
    return execute_canonical_test_call(
        agent.tools.workspace_root, tools={"write_file": tool}, tool_name="write_file",
        arguments={"path": "copied-resource.bin", "source_ref": reference},
        workspace_roots=tuple(agent.tools.workspace_roots),
    )


@pytest.mark.parametrize("missing", _REFERENCE_FIELDS)
def test_missing_reference_field_is_rejected_before_handler_with_exact_schema_issue(resource_tool, missing):
    agent, tool, reference, calls = resource_tool
    del reference[missing]
    execution = _invoke(agent, tool, reference)
    assert execution.result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert execution.result.failure_stage == "validation"
    assert not execution.result.handler_executed
    assert execution.result.effect_outcome == "not_started"
    assert calls == {"handler": 0, "resolver": 0}
    assert execution.decision.evidence["issues"] == [{
        "keyword": "required", "path": f"$.source_ref.{missing}",
        "expected": True, "actual_type": "missing",
    }]
    assert not (agent.tools.workspace_root / "copied-resource.bin").exists()


def test_core_schema_and_provider_projection_describe_the_complete_returned_reference(resource_tool):
    _agent_view, tool, reference, calls = resource_tool
    rendered = tool_model_spec_to_anthropic_tool(tool.model_spec)
    source_schema = rendered["input_schema"]["properties"]["source_ref"]
    assert set(source_schema["properties"]) == set(reference) == set(_REFERENCE_FIELDS)
    assert set(source_schema["required"]) == set(reference)
    assert source_schema["additionalProperties"] is False
    assert all(item["type"] == "string" for item in source_schema["properties"].values())
    assert source_schema["properties"]["kind"]["const"] == "capability_package"
    assert source_schema["properties"]["source"]["const"] == "capability_package"
    source_schema["required"].remove("name")
    source_schema["properties"]["name"]["type"] = "integer"
    assert "name" in tool.model_spec.input_schema["properties"]["source_ref"]["required"]
    assert tool.model_spec.input_schema["properties"]["source_ref"]["properties"]["name"]["type"] == "string"
    tool.model_spec.assert_schema_hash()
    assert calls == {"handler": 0, "resolver": 0}


@pytest.mark.parametrize(("field", "value", "keyword", "code"), [
    ("name", True, "type", "TOOL_PARAMETER_TYPE_INVALID"),
    ("resource_sha256", "A" * 64, "pattern", "TOOL_INVALID_ARGUMENTS"),
    ("owner_id", "different-owner", "additionalProperties", "TOOL_INVALID_ARGUMENTS"),
])
def test_invalid_reference_shape_uses_existing_input_gate(resource_tool, field, value, keyword, code):
    agent, tool, reference, calls = resource_tool
    reference[field] = value
    execution = _invoke(agent, tool, reference)
    assert execution.result.error_code == code
    assert not execution.result.handler_executed
    assert calls == {"handler": 0, "resolver": 0}
    assert any(issue["path"] == f"$.source_ref.{field}" and issue["keyword"] == keyword
               for issue in execution.decision.evidence["issues"])
    assert not (agent.tools.workspace_root / "copied-resource.bin").exists()


@pytest.mark.parametrize(("field", "value", "code"), [
    ("name", "other-name", "TOOL_INVALID_ARGUMENTS"),
    ("activation_id", "f" * 64, "TOOL_UNAVAILABLE"),
    ("content_sha256", "f" * 64, "TOOL_UNAVAILABLE"),
    ("resource_sha256", "f" * 64, "TOOL_UNAVAILABLE"),
])
def test_valid_shape_does_not_weaken_original_identity_and_generation_checks(resource_tool, field, value, code):
    agent, tool, reference, calls = resource_tool
    reference[field] = value
    result = _invoke(agent, tool, reference).result
    assert not result.ok and result.error_code == code
    assert result.effect_outcome == "not_started"
    assert calls == {"handler": 1, "resolver": 1}
    assert not (agent.tools.workspace_root / "copied-resource.bin").exists()


def test_full_reference_materializes_exact_binary_bytes_with_original_receipt(resource_tool):
    agent, tool, reference, calls = resource_tool
    result = _invoke(agent, tool, reference).result
    assert result.ok, result.content
    assert (agent.tools.workspace_root / "copied-resource.bin").read_bytes() == b"\x00exact\xff\r\n"
    assert result.metadata["handler_details"]["source_ref"] == reference
    assert calls == {"handler": 1, "resolver": 1}


def test_disabling_plugins_keeps_the_previous_write_schema_and_text_path(resource_tool):
    enabled, _tool, _reference, _calls = resource_tool
    agent = SimpleAgent(replace(enabled.config, enable_plugins=False), enabled.root)
    tool = agent.tools.tools["write_file"]
    plain = WriteFileTool(agent.tools.workspace_root)
    assert tool.source_resolver is None
    assert tool.model_spec.input_schema == plain.model_spec.input_schema
    assert tool.model_spec.description == plain.model_spec.description
    execution = execute_canonical_test_call(
        agent.tools.workspace_root, tools={"write_file": tool}, tool_name="write_file",
        arguments={"path": "ordinary.txt", "content": "ordinary\n"},
        workspace_roots=tuple(agent.tools.workspace_roots),
    )
    assert execution.result.ok, execution.result.content
    assert (agent.tools.workspace_root / "ordinary.txt").read_bytes() == b"ordinary\n"


def test_enabled_plugins_without_installed_packages_still_declare_the_host_source_contract(tmp_path):
    agent = SimpleAgent(AgentConfig(enable_plugins=True, prompt_files=[]), tmp_path / "repo")
    assert agent.current_skill_snapshot().packages == ()
    tool = agent.tools.tools["write_file"]
    schema = tool.model_spec.input_schema["properties"]["source_ref"]
    assert set(schema["required"]) == set(_REFERENCE_FIELDS)
    assert tool.source_resolver is not None
