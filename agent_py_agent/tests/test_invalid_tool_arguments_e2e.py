# LLM: 真实工具循环 + 假供应商 HTTP 的端到端验证：坏工具参数先有界纠正，超限才按原错误结束；
#   复用 test_gateway_model_adoption 的 actual_request/fake_http 夹具，不新造假网络。
# 模块用途: 验证“参数无效有界纠正”在真实 gateway/工具循环里生效：纠正回灌、零执行、执行一次、超限失败。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.agent.tooling import _filesystem_read
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_subagent_compact_recovery_continuation import _is_probe

_PROTOCOLS = ["anthropic_compatible", "openai_compatible"]


# LLM: 测试只构造供应商 HTTP 响应，不绕过原 backend 解析或工具执行；文件落在 owner 工作区供 read_file 真实读取。
# 函数用途: 准备真实 gateway 上下文与可读材料，按协议返回对应模型选择。
def _invalid_arguments_fixture(tmp_path, protocol):
    fixture = actual_request(tmp_path, mode="disabled", tools=True)
    agent = fixture.agent
    if protocol == "openai_compatible":
        profile, _ = add(agent, model_name="original-openai", model_backend=protocol,
                         model_context_window_tokens=250_000)
        model_profiles.execute_model_profile_operation(
            agent, "select", {"profile_id": profile}, thread_id=fixture.thread_id,
        )
    material = agent.home_paths.owner_workspace_dir / "invalid-args-material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("需要整理的资料。", encoding="utf-8")
    return fixture, material


# LLM: 坏参数响应必须贴真实协议形状：anthropic 的 tool_use.input 是半截 JSON 字符串（非 dict），
#   openai 的 function.arguments 是坏 JSON 字符串；两种都会被 backend 归一成 MODEL_TOOL_ARGUMENTS_INVALID。
# 函数用途: 返回第一次/连续坏参数时假供应商给出的工具调用响应。
def _bad_tool_response(protocol):
    if protocol == "anthropic_compatible":
        return {
            "content": [{"type": "tool_use", "id": "bad-call", "name": "read_file",
                         "input": '{"path": "a.md",,'}],
            "stop_reason": "tool_use",
        }
    return {
        "choices": [{"message": {"content": "",
                                 "tool_calls": [{"id": "bad-call", "type": "function",
                                                 "function": {"name": "read_file",
                                                             "arguments": '{"path": "a.md",,'}}]},
                     "finish_reason": "tool_calls"}],
    }


# LLM: 合法调用走 read_file，参数是 owner 工作区真实路径；后端解析后工具循环会真正执行一次。
# 函数用途: 返回第二次（纠正后）的合法 read_file 调用响应。
def _good_tool_response(protocol, material):
    if protocol == "anthropic_compatible":
        return {
            "content": [{"type": "tool_use", "id": "good-call", "name": "read_file",
                         "input": {"path": str(material)}}],
            "stop_reason": "tool_use",
        }
    return {
        "choices": [{"message": {"content": "",
                                 "tool_calls": [{"id": "good-call", "type": "function",
                                                 "function": {"name": "read_file",
                                                             "arguments": json.dumps({"path": str(material)})}}]},
                     "finish_reason": "tool_calls"}],
    }


# LLM: 第一次坏参数 → 有界纠正续跑；第二次合法调用 → read_file 真正执行一次；第三次正文 → 正常交付。
#   全程只有 HTTP 是替身，裁决、零执行、工具执行和最终答复都走真实工具循环。
# 函数用途: 验证坏参数不整轮失败，而是回灌纠正后同轮续跑成功，read_file 只执行一次。
@pytest.mark.parametrize("protocol", _PROTOCOLS)
def test_invalid_arguments_are_corrected_then_read_file_runs_once(tmp_path, monkeypatch, protocol):
    fixture, material = _invalid_arguments_fixture(tmp_path, protocol)
    agent, context = fixture.agent, fixture.context
    reads: list[dict[str, object]] = []
    original_read = _filesystem_read.ReadFileTool.execute

    def read(self, params):
        reads.append(dict(params))
        return original_read(self, params)

    monkeypatch.setattr(_filesystem_read.ReadFileTool, "execute", read)
    business, probes = fake_http(monkeypatch, fixture)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if _is_probe(request.payload):
            return response
        number = len(business)
        if number == 1:
            return _bad_tool_response(protocol)
        if number == 2:
            return _good_tool_response(protocol, material)
        return response

    monkeypatch.setattr(http, "post_json", send)
    result = request_execution._run_gateway_ask(context)

    assert result.response == "资料整理完成。"
    assert [row["path"] for row in reads] == [str(material)]
    assert len(business) == 3
    # 第二次请求（纠正后）必须带着宿主纠正指令，而不是把坏参数原样发回去。
    assert "[tool-arguments-invalid]" in json.dumps(business[1][0], ensure_ascii=False)


# LLM: 连续 4 次坏参数：前 3 次各消耗一次有界纠正（continue，零执行），第 4 次达到上限后
#   沿原无工具分支按 MODEL_TOOL_ARGUMENTS_INVALID 结束本轮；业务请求正好 4 次、零工具执行。
# 函数用途: 验证超限后失败投影与改动前一致，且没有偷偷执行任何工具。
@pytest.mark.parametrize("protocol", _PROTOCOLS)
def test_four_invalid_argument_responses_fail_with_typed_error_and_no_tool_execution(tmp_path, monkeypatch, protocol):
    fixture, material = _invalid_arguments_fixture(tmp_path, protocol)
    agent, context = fixture.agent, fixture.context
    reads: list[dict[str, object]] = []

    def read(self, params):
        reads.append(dict(params))
        return "不应执行"

    monkeypatch.setattr(_filesystem_read.ReadFileTool, "execute", read)
    business, probes = fake_http(monkeypatch, fixture)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if _is_probe(request.payload):
            return response
        return _bad_tool_response(protocol)

    monkeypatch.setattr(http, "post_json", send)
    result = request_execution._run_gateway_ask(context)

    assert result.runtime_reason == "MODEL_TOOL_ARGUMENTS_INVALID"
    assert result.runtime_status == "error"
    assert result.runtime_source == "model_provider"
    assert reads == []
    assert len(business) == 4