"""真实 Gateway 媒体输入跨工具溢出恢复：只有供应商 HTTP 为测试替身。"""
from __future__ import annotations

import base64
import json

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult, UserTurn
from agent_py_agent.agent.conversation import active_turn_compact
from agent_py_agent.agent.conversation.input_media import import_input_media, input_media_root
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.agent.tooling import _filesystem_read
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_subagent_compact_recovery_continuation import _response


# LLM: 1px PNG 是固定离线测试字节，只经产品导入到已认证 owner 的目录；不伪造内部媒体块。
# 函数用途: 为真实 Gateway 入口准备同一用户文字、图片及稍后由 read_file 返回的长材料。
def _media_request(tmp_path, protocol):
    fixture = actual_request(tmp_path, mode="disabled", tools=True)
    agent = fixture.agent
    if protocol == "openai_compatible":
        profile, _ = add(agent, model_name="original-openai", model_backend=protocol,
                         model_context_window_tokens=250_000)
        model_profiles.execute_model_profile_operation(
            agent, "select", {"profile_id": profile}, thread_id=fixture.thread_id,
        )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    source = tmp_path / "one-pixel.png"
    source.write_bytes(png)
    ref = import_input_media(source, input_media_root(agent))
    fixture.context.request["prompt"] = "看图并依据已读取的文件整理结论"
    fixture.context.request["input_media"] = [ref]
    fixture.context.request_path.write_text(json.dumps(fixture.context.request), encoding="utf-8")
    full_body = "SOURCE-HEAD-" + "甲" * 2_500 + "EXACT-MEDIA-TOOL-MIDDLE" + "乙" * 2_500 + "-SOURCE-TAIL"
    material = agent.home_paths.owner_workspace_dir / "media-recovery-material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text(full_body, encoding="utf-8")
    return fixture, png, ref, material, full_body


# LLM: 只检查供应商实际载荷中的同一 user 消息；媒体字节必须由原 backend 在发送边界展开。
# 函数用途: 验证图片数据与用户文字在压缩前后均逐字可见，且不只剩本地路径引用。
def _assert_media_wire(wire, *, png, prompt):
    found = []
    for message in wire["messages"]:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        blocks = message["content"]
        images = [block for block in blocks if isinstance(block, dict)
                  and block.get("type") in {"image", "image_url"}]
        if images:
            text = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
            found.extend((image, text) for image in images)
    assert len(found) == 1, "真实出站请求必须恰好携带一个图片块"
    image, text = found[0]
    assert prompt in text
    if image["type"] == "image":
        assert image["source"]["type"] == "base64"
        encoded = image["source"]["data"]
    else:
        encoded = image["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == png


# LLM: 只按结构化探针工具名区分辅助调用，不借正文猜测业务或摘要阶段。
# 函数用途: 保留原 capability probe 回答，避免把它计入模型业务请求。
def _is_probe(wire):
    names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
    return names == ["my_agent_capability_probe"]


@pytest.mark.parametrize("protocol", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("overflow", [False, True])
def test_gateway_media_tool_round_and_unknown_capacity_recovery(tmp_path, monkeypatch, protocol, overflow):
    fixture, png, ref, material, full_body = _media_request(tmp_path, protocol)
    agent, context = fixture.agent, fixture.context
    calls, carries, reads, summaries, compacts = [], [], [], [], []
    original_generate = _tool_loop_service.generate_model_response
    original_overflow = request_execution._gateway_compact_overflowing_turn
    original_read = _filesystem_read.ReadFileTool.execute
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_compact = active_turn_compact.compact_carried_active_turn_archive

    def generate(request):
        calls.append(tuple(request.params.tool_ir_history or ()))
        return original_generate(request)

    def compact_overflow(*args, **kwargs):
        carry = args[3].native_compact_carry
        assert carry is not None
        carries.append(carry)
        return original_overflow(*args, **kwargs)

    def read(self, params):
        reads.append(dict(params))
        return original_read(self, params)

    def summarize(*args, **kwargs):
        summaries.append(args)
        return original_summary(*args, **kwargs)

    def compact(*args, **kwargs):
        compacts.append(args)
        return original_compact(*args, **kwargs)

    monkeypatch.setattr(_tool_loop_service, "generate_model_response", generate)
    monkeypatch.setattr(request_execution, "_gateway_compact_overflowing_turn", compact_overflow)
    monkeypatch.setattr(_filesystem_read.ReadFileTool, "execute", read)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summarize)
    monkeypatch.setattr(active_turn_compact, "compact_carried_active_turn_archive", compact)

    def on_business(_wire):
        number = len(business)
        if number == 2 and overflow:
            raise ProviderContextWindowError("测试图片请求已读文件后供应商上下文溢出")
        assert number <= 2, "媒体容量未知时不得发送摘要或恢复业务请求"

    business, _ = fake_http(monkeypatch, fixture, on_business=on_business)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if _is_probe(request.payload):
            return response
        if len(business) == 1:
            return _response(protocol, tool={
                "type": "tool_use", "id": "read-with-media", "name": "read_file",
                "input": {"path": str(material)},
            })
        return response

    monkeypatch.setattr(http, "post_json", send)
    if overflow:
        with pytest.raises(RuntimeError) as failure:
            request_execution._run_gateway_ask(context)
        assert getattr(failure.value, "error_code", None) == "COMPACT_REQUEST_NON_TEXT"
    else:
        result = request_execution._run_gateway_ask(context)
        assert result.response == "资料整理完成。"
        assert not carries
    assert [row["path"] for row in reads] == [str(material)]
    assert len(business) == len(calls) == 2
    _assert_media_wire(business[0][0], png=png, prompt=context.request["prompt"])
    _assert_media_wire(business[1][0], png=png, prompt=context.request["prompt"])
    assert full_body in json.dumps(business[1][0]["messages"], ensure_ascii=False)
    assert all(any(isinstance(item, UserTurn) and item.media and item.media[0]["sha256"] == ref["sha256"]
                   for item in history) for history in calls)
    assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0
    assert not summaries and not compacts
    if overflow:
        assert len(carries) == 1
        history = carries[0].history
        media_turns = [item for item in history if isinstance(item, UserTurn) and item.media]
        assert len(media_turns) == 1
        assert media_turns[0].media[0]["sha256"] == ref["sha256"]
        assert context.request["prompt"] in media_turns[0].text
        tools = [item for item in history if isinstance(item, (AssistantTurn, ToolResult))]
        assert len(tools) == 2 and isinstance(tools[0], AssistantTurn) and isinstance(tools[1], ToolResult)
        call = tools[0].tool_calls[0]
        assert call.call_id == tools[1].call_id == "read-with-media"
        assert call.run_id == carries[0].run_id and call.attempt_id == carries[0].source_attempt_id
        assert call.turn_id
        assert full_body in tools[1].output
