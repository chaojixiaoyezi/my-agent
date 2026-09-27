"""配置、canonical 工具重放及 fake LLM 验证；不运行真实模型或业务检查器。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.settings import AgentConfig, load_config
from agent_py_agent.agent.settings.normalize import normalize_agent_config
from agent_py_agent.agent.tooling import file_syntax_diagnostics as diagnostics
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.content_transport_policy import FileSourceContent
from agent_py_agent.agent.tooling.models import ApprovalPolicy
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


@pytest.mark.parametrize("raw, expected", [(False, False), (True, True), ("false", False), ("true", True), ("bad-value", False)])
def test_file_syntax_switch_is_normalized(raw, expected):
    config, warnings = normalize_agent_config({"enable_file_syntax_diagnostics": raw})
    assert config["enable_file_syntax_diagnostics"] is expected
    assert any("enable_file_syntax_diagnostics" in warning for warning in warnings) == (raw == "bad-value")


def test_shipped_yaml_and_dataclass_disable_observation():
    path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    assert AgentConfig().enable_file_syntax_diagnostics is False
    assert load_config(path).enable_file_syntax_diagnostics is False


@pytest.mark.parametrize("syntax, recall, expected_syntax, expected_recall", [
    ("false", "false", False, False),
    ("true", "false", True, False),
    ("false", "true", False, True),
])
def test_file_and_compact_switches_are_independent_after_yaml_load(tmp_path, syntax, recall, expected_syntax, expected_recall):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f'enable_file_syntax_diagnostics: "{syntax}"\ncompact_recall_hint_enabled: "{recall}"\n'
        'compact_landmark_max_tokens: 1200\n', encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.enable_file_syntax_diagnostics is expected_syntax
    assert config.compact_recall_hint_enabled is expected_recall
    assert config.compact_landmark_max_tokens == 1200


@pytest.mark.parametrize("configured", ['"false"', "true"])
def test_loaded_config_reaches_all_registered_file_tools(tmp_path, configured):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"enable_file_syntax_diagnostics: {configured}\n", encoding="utf-8")
    config = load_config(config_path)
    config.my_agent_home = str(tmp_path / "home")
    config.enable_subagents = False
    config.enable_plugins = False
    config.home_context_enabled = False
    agent = SimpleAgent(config, tmp_path)
    assert {agent.tools.tools[name].enable_file_syntax_diagnostics
            for name in ("write_file", "edit_file", "apply_patch")} == {configured == "true"}


# LLM: 测试来源只返回明确字节，不执行脚本；后续必须经过 canonical ToolExecutor 才能断言权限或操作重放。
# 函数用途: 装配可统计读取次数的来源工具，验证原字节和原来源回执不受语法观察改变。
def source_writer(root, payload):
    reads = []
    reference = {"kind": "test_resource", "sha256": hashlib.sha256(payload).hexdigest()}

    def resolve(ref):
        reads.append(dict(ref))
        return FileSourceContent(payload, ref)

    schema = {"type": "object", "properties": {"kind": {"type": "string"}, "sha256": {"type": "string"}},
              "required": ["kind", "sha256"], "additionalProperties": False}
    tool = WriteFileTool(root, options=WriteFileToolOptions(
        source_resolver=resolve, source_ref_schema=schema,
        access_options=FileSystemAccessOptions(enable_file_syntax_diagnostics=True),
    ))
    return tool, reference, reads


# LLM: 保持原 ToolCall/Executor/operation 路径，工具观察不能代替审批、owner 或幂等事实。
# 函数用途: 以固定调用身份执行测试来源写入，让重放读取同一回执。
def invoke(root, tool, reference, **kwargs):
    return execute_canonical_test_call(root, tools={"write_file": tool}, tool_name="write_file",
                                       arguments={"path": "source.json", "source_ref": reference}, **kwargs).result


@pytest.mark.parametrize("payload", [b'{"value": ]}\r\n', b'\xef\xbb\xbftrue\r\n'])
def test_source_bytes_hash_and_diagnostics_replay_from_original_receipt(tmp_path, payload):
    tool, reference, reads = source_writer(tmp_path, payload)
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    first = invoke(tmp_path, tool, reference, operation_store=store, operation_store_required=True)
    assert first.ok, first.content
    assert (tmp_path / "source.json").read_bytes() == payload
    details = first.metadata["handler_details"]
    assert details["source_ref"] == reference and details["content_sha256"] == hashlib.sha256(payload).hexdigest()
    (tmp_path / "source.json").write_bytes(b"external replacement")
    replay = invoke(tmp_path, tool, reference, operation_store=store, operation_store_required=True)
    assert replay.ok and len(reads) == 1
    assert replay.metadata["handler_details"]["syntax_diagnostics"] == details["syntax_diagnostics"]
    assert (tmp_path / "source.json").read_bytes() == b"external replacement"


def test_original_approval_blocks_source_and_diagnostics(tmp_path, monkeypatch):
    tool, reference, reads = source_writer(tmp_path, b"{")
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))

    def forbidden(*_args):
        raise AssertionError("审批前不可观察")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "observe_candidate", forbidden)
    result = invoke(tmp_path, tool, reference)
    assert result.status == "approval_required" and not result.handler_executed
    assert reads == [] and not (tmp_path / "source.json").exists()


def test_control_cancellation_during_observation_stops_original_publish(tmp_path, monkeypatch):
    tool, reference, _ = source_writer(tmp_path, b"{")
    (tmp_path / "source.json").write_bytes(b"{}")

    def stop(*_args):
        raise ToolCancelled("test cancellation")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "observe_candidate", stop)
    result = invoke(tmp_path, tool, reference)
    assert not result.ok and result.status == "cancelled" and result.error_code == "CANCELLED"
    assert (tmp_path / "source.json").read_bytes() == b"{}"
    assert not list(tmp_path.glob(".source.json.*"))


# LLM: 假后端只根据真实模型输入选择后续动作；不能替生产工具写文件，也不能伪造回执。
# 类用途: 证明原模型链能读到语法错误，并自主修复或合法地保留坏夹具。
class SyntaxAwareBackend:
    name = "fake_syntax_feedback"

    # LLM: 测试明确声明是否修复，普通自然任务仍由模型返回 final，不引入宿主完成门。
    # 函数用途: 保存假模型的决策和每次真实输入。
    def __init__(self, repair):
        self.repair = repair
        self.prompts = []

    # LLM: 只声明本地假 native 能力，不探测供应商或发网络请求。
    # 函数用途: 让原运行循环使用结构化工具调用协议。
    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://fake", model="", stream=False,
                                      native_supported=True, evidence="test_fake_native")

    # LLM: 只返回工具调用和最终文字；后续断言来自实际 prompt 与原生 messages，不旁路造诊断或降回文本工具协议。
    # 函数用途: 先请求坏 JSON，再根据真实语法反馈修复或保留夹具。
    def generate(self, prompt, on_chunk=None, **kwargs):
        prompt += json.dumps(kwargs.get("messages"), ensure_ascii=False)
        self.prompts.append(prompt)
        number = len(self.prompts)
        if number == 1:
            content = '{"value": ]}'
        elif number == 2:
            assert "JSON_INVALID" in prompt
            if not self.repair:
                return ModelResponse(text="已保留故意无效的测试夹具。", backend=self.name)
            content = '{"value": 1}'
        else:
            assert "JSON_VALID" in prompt
            return ModelResponse(text="已修复并保存。", backend=self.name)
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
            "id": f"call_write_{number}", "name": "write_file", "input": {"path": "fixture.json", "content": content},
        }])


@pytest.mark.parametrize("repair", [True, False])
def test_fake_llm_receives_feedback_and_keeps_completion_authority(tmp_path, repair):
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), enable_file_syntax_diagnostics=True,
                         enable_plugins=False, enable_subagents=False, home_context_enabled=False,
                         tool_protocol="native", max_tool_rounds=4, auto_save_memory=False)
    agent = SimpleAgent(config, tmp_path)
    backend = SyntaxAwareBackend(repair)
    agent.backend = backend
    result = agent.run("保存一份测试夹具。", save=False, allowed_tools=["write_file"])
    expected = b'{"value": 1}' if repair else b'{"value": ]}'
    assert (Path(agent.effective_workspace_root) / "fixture.json").read_bytes() == expected
    assert result.response == ("已修复并保存。" if repair else "已保留故意无效的测试夹具。")
    assert len(backend.prompts) == (3 if repair else 2)
    assert len(result.executed_tools) == (2 if repair else 1)
