"""恢复后的工具往返继续使用同一已提交 Compact 请求；只有 HTTP 是测试替身。"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent import gateway_compact_recovery as recovery
from agent_py_agent.agent.agent_core import (
    _tool_loop_service,
    compact_request_recovery,
    runtime_mixin,
)
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult
from agent_py_agent.agent.conversation import active_turn_compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_model_adoption import _payload
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.agent.tooling import _filesystem_read
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_gateway_model_observation import install_backend


# LLM: Observe real runtime preparation and model-call params while the original tool loop and
# provider adapter execute; the HTTP stub alone chooses overflow, tool_use, and final responses.
# 函数用途: 验证摘要恢复后的两次业务模型请求沿同一参数继续读文件并结束，不重新准备运行上下文。
def test_recovered_tool_round_keeps_committed_history_and_boundary_order(tmp_path, monkeypatch) -> None:
    fixture = _history_request(tmp_path, tools=True)
    agent, context = fixture.agent, fixture.context
    material = agent.home_paths.owner_workspace_dir / "material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("继续轮需要读取的原始材料。", encoding="utf-8")
    install_backend(monkeypatch, fixture)
    events: list[object] = []
    prepared = []
    model_calls = []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_generate = _tool_loop_service.generate_model_response

    def prepare(*args, **kwargs):
        prepared.append((args, kwargs))
        return original_prepare(*args, **kwargs)

    def generate(request):
        params = request.params
        seed = params.conversation_history_seed
        model_calls.append({
            "generation": seed.compact_generation if seed is not None else None,
            "injections": tuple(params.runtime_injections),
            "provider_history": deepcopy(params.provider_history_messages),
            "params": params,
        })
        return original_generate(request)

    class Sink:
        def __call__(self, _chunk):
            return None

        def write_compact_boundary(self, generation):
            events.append(("boundary", generation))

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(_tool_loop_service, "generate_model_response", generate)
    context = replace(context, on_chunk=Sink())

    def on_business(_wire):
        host = _HOST.get()
        if not events:
            events.append("overflow")
            raise ProviderContextWindowError("测试首次业务请求上下文溢出")
        if host.compact_recovery is not None and not host.compact_recovery.committed:
            events.append("summary")
            return
        events.append(f"restored-{sum(str(item).startswith('restored-') for item in events) + 1}")

    business, _probes = fake_http(monkeypatch, fixture, on_business=on_business)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if events and events[-1] == "restored-1":
            wire = request.payload
            names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
            if names != ["my_agent_capability_probe"]:
                tool = {
                    "type": "tool_use",
                    "id": "read-material-after-compact",
                    "name": "read_file",
                    "input": {"path": str(material)},
                }
                if "system" in wire:
                    return {"content": [tool], "stop_reason": "tool_use",
                            "usage": {"input_tokens": 101, "output_tokens": 5}}
                return {"choices": [{"message": {"content": "", "tool_calls": [
                    {"id": tool["id"], "type": "function", "function": {
                        "name": tool["name"], "arguments": json.dumps(tool["input"]),
                    }}]}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 101, "completion_tokens": 5}}
        return response

    monkeypatch.setattr(http, "post_json", send)
    result = request_execution._run_gateway_ask(context)

    assert result.response == "资料整理完成。"
    assert events == ["overflow", "summary", ("boundary", 1), "restored-1", "restored-2"]
    assert len(business) == 4
    assert len(prepared) == 2
    assert len(model_calls) == 3
    original, restored_first, restored_second = model_calls
    assert original["generation"] == 0
    assert restored_first["generation"] == restored_second["generation"] == 1
    assert restored_first["params"] is restored_second["params"]
    assert restored_first["injections"] == restored_second["injections"]
    assert restored_first["injections"] != original["injections"]
    assert restored_first["provider_history"] == restored_second["provider_history"]
    assert restored_first["provider_history"] != original["provider_history"]
    assert original["params"].compact_context is not None
    assert original["params"].compact_context.view.checkpoint_id == ""
    committed_context = restored_first["params"].compact_context
    assert committed_context is not None
    assert committed_context == restored_second["params"].compact_context
    assert committed_context.view.checkpoint_id == agent.conversation_store.threads.require(
        fixture.thread_id,
    ).compact_checkpoint_id
    assert committed_context.view.source_message_ids
    assert "generation 1" in json.dumps(restored_second["provider_history"], ensure_ascii=False)
    assert any(
        block.get("type") == "tool_result"
        for message in business[-1][0]["messages"]
        for block in message["content"]
        if isinstance(block, dict)
    )
    assert "继续轮需要读取的原始材料" in json.dumps(business[-1][0]["messages"], ensure_ascii=False), [
        block for message in business[-1][0]["messages"] for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 1


# LLM: 首个真实工具对后由供应商触发溢出；原生IR连完整正文跨外层恢复，候选经同次请求计量并发送。
# 函数用途: 两协议验证空 transcript 的下一次准备发生在CAS前，完整工具正文进入摘要且不会重复旧读取。
@pytest.mark.parametrize("mode,candidate_backend", [
    ("disabled", "anthropic_compatible"), ("apply", "openai_compatible"),
])
@pytest.mark.parametrize("fail_projection", [False, True])
def test_empty_transcript_overflow_compacts_active_turn_before_reprepare(
    tmp_path, monkeypatch, mode, candidate_backend, fail_projection,
) -> None:
    fixture = actual_request(tmp_path, mode=mode, tools=True, candidate_backend=candidate_backend)
    agent, context = fixture.agent, fixture.context
    material = agent.home_paths.owner_workspace_dir / "active-turn-material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    full_body = "SOURCE-HEAD-" + "甲" * 2_500 + "EXACT-IR-MIDDLE-GATEWAY" + "乙" * 2_500 + "-SOURCE-TAIL"
    material.write_text(full_body, encoding="utf-8")
    following_material = agent.home_paths.owner_workspace_dir / "following-turn-material.txt"
    following_material.write_text("恢复后继续读取的新材料。", encoding="utf-8")
    decision = install_backend(monkeypatch, fixture)
    events: list[object] = []
    preparations = []
    loop_params = []
    compact_results = []
    candidates = []
    source_plans = []
    reads = []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_loop_params = loop_support._tool_loop_execute_params
    original_compact = active_turn_compact.compact_carried_active_turn_archive
    original_project = recovery._project_gateway_active_candidate
    original_mixed = compact_request_recovery._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_read = _filesystem_read.ReadFileTool.execute

    def prepare(*args, **kwargs):
        preparations.append((args, kwargs))
        if len(preparations) == 2:
            assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0
            events.append("prepare-before-cas")
        return original_prepare(*args, **kwargs)

    def build_loop_params(*args, **kwargs):
        value = original_loop_params(*args, **kwargs)
        loop_params.append(value)
        if len(loop_params) == 2:
            assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0
            events.append("loop-params-before-cas")
        return value

    def compact(*args):
        assert len(args[3]) == 1
        result = original_compact(*args)
        compact_results.append(result)
        events.append("active-compact-cas")
        return result

    def project(*args):
        if fail_projection:
            raise ConversationCompactError("测试活动候选无法投影", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        return original_project(*args)

    def mixed(material, view, max_chars):
        selected = original_mixed(material, view, max_chars)
        if view.is_candidate:
            candidates.append(selected)
        return selected

    def summarize(*args, **kwargs):
        source_plans.append(args[1])
        return original_summary(*args, **kwargs)

    def read(self, params):
        reads.append(dict(params))
        outcome = original_read(self, params)
        assert outcome.ok
        return outcome

    class Sink:
        def __call__(self, _chunk):
            return None

        def write_compact_boundary(self, generation):
            events.append(("boundary", generation))

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(loop_support, "_tool_loop_execute_params", build_loop_params)
    monkeypatch.setattr(active_turn_compact, "compact_carried_active_turn_archive", compact)
    monkeypatch.setattr(recovery, "_project_gateway_active_candidate", project)
    monkeypatch.setattr(compact_request_recovery, "_project_mixed_recovery_material", mixed)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summarize)
    monkeypatch.setattr(_filesystem_read.ReadFileTool, "execute", read)
    context = replace(context, on_chunk=Sink())

    def on_business(_wire):
        number = len(business)
        if number == 1:
            events.append("first-tool-request")
        elif number == 2:
            events.append("overflow-after-tool")
            raise ProviderContextWindowError("测试单次工具读取后的供应商上下文溢出")
        elif number == 3:
            events.append("active-summary")
        elif number == 4:
            events.append("restored-business")
            projection = candidates[-1].projection
            expected = _payload(
                agent.backend, projection.provider_prompt,
                list(candidates[-1].request_input.native_tools) or None,
                projection.tool_choice, projection.messages, projection.system_instruction,
            )
            assert _wire == expected
        elif number == 5:
            events.append("following-tool-business")
        else:
            raise AssertionError(f"unexpected business request {number}")

    business, _probes = fake_http(monkeypatch, fixture, on_business=on_business)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if events and events[-1] == "active-summary":
            if "system" in request.payload:
                return {"content": [{"type": "text", "text": "资料整理完成。"}],
                        "stop_reason": "end_turn", "usage": {"input_tokens": 101, "output_tokens": 5}}
            return {"choices": [{"message": {"content": "资料整理完成。"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 101, "completion_tokens": 5}}
        if len(business) == 1 and events[-1] == "first-tool-request":
            wire = request.payload
            names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
            if names != ["my_agent_capability_probe"]:
                tool = {"type": "tool_use", "id": "active-read-once", "name": "read_file",
                        "input": {"path": str(material)}}
                if "system" in wire:
                    return {"content": [tool], "stop_reason": "tool_use",
                            "usage": {"input_tokens": 101, "output_tokens": 5}}
                return {"choices": [{"message": {"content": "", "tool_calls": [
                    {"id": tool["id"], "type": "function", "function": {
                        "name": tool["name"], "arguments": json.dumps(tool["input"]),
                    }}]}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 101, "completion_tokens": 5}}
        if events and events[-1] == "restored-business":
            tool = {"type": "tool_use", "id": "read-following-tool", "name": "read_file",
                    "input": {"path": str(following_material)}}
            if "system" in request.payload:
                return {"content": [tool], "stop_reason": "tool_use",
                        "usage": {"input_tokens": 101, "output_tokens": 5}}
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": tool["id"], "type": "function", "function": {
                    "name": tool["name"], "arguments": json.dumps(tool["input"]),
                }}]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 101, "completion_tokens": 5}}
        return response

    monkeypatch.setattr(http, "post_json", send)
    if fail_projection:
        with pytest.raises(RuntimeError):
            request_execution._run_gateway_ask(context)
        assert len(business) == 3 and len(preparations) == len(loop_params) == 2
        assert [row["path"] for row in reads] == [str(material)]
        assert not compact_results and not candidates
        assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0
        return
    result = request_execution._run_gateway_ask(context)

    assert result.response == "资料整理完成。"
    assert [row["path"] for row in reads] == [str(material), str(following_material)]
    assert len(preparations) == len(loop_params) == 2
    assert len(compact_results) == len(candidates) == 1 and compact_results[0].compacted
    assert compact_results[0].source_call_ids == ("active-read-once",)
    assert events == [
        "first-tool-request", "overflow-after-tool", "prepare-before-cas", "loop-params-before-cas",
        "active-summary", "active-compact-cas", ("boundary", 1),
        "restored-business", "following-tool-business",
    ]
    stored = agent.conversation_store.threads.require(fixture.thread_id)
    assert stored.compact_generation == 1
    assert stored.compact_source_tool_pairs == 1
    assert stored.compacted_through_byte_offset == 0
    assert candidates[0].params.compact_context.view.checkpoint_id == ""
    assert len(decision.calls) == (1 if mode == "apply" else 0)
    source_ir = source_plans[0].source_ir_history
    assert len(source_plans) == len(source_ir) // 2 == 1
    assert isinstance(source_ir[0], AssistantTurn) and isinstance(source_ir[1], ToolResult)
    assert source_ir[0].tool_calls[0].call_id == source_ir[1].call_id == "active-read-once"
    source_call = source_ir[0].tool_calls[0]
    assert len(source_plans[0].source_records) == len(source_plans[0].source_tool_refs) == 1
    assert all(source_plans[0].source_records[0][field] == getattr(source_call, field)
               for field in ("run_id", "attempt_id", "turn_id", "call_id"))
    assert full_body in source_ir[1].output
    assert len(loop_params[1].archive_tool_calls[0]["output_preview"]) < len(source_ir[1].output)
    summary_wire = json.dumps(business[2][0]["messages"], ensure_ascii=False)
    assert "EXACT-IR-MIDDLE-GATEWAY" in summary_wire
    assert summary_wire.count("EXACT-IR-MIDDLE-GATEWAY") == 1
    assert not any(isinstance(item, (AssistantTurn, ToolResult)) for item in candidates[0].request_input.tool_ir_history)
    assert not any(
        message.get("tool_call_id") == "active-read-once"
        or any(call.get("id") == "active-read-once" for call in message.get("tool_calls") or [])
        or any(
            block.get("type") in {"tool_use", "tool_result"}
            and (block.get("id") == "active-read-once" or block.get("tool_use_id") == "active-read-once")
            for block in (message.get("content") if isinstance(message.get("content"), list) else [])
            if isinstance(block, dict)
        )
        for message in business[3][0]["messages"]
    )
    assert "恢复后继续读取的新材料" in json.dumps(business[-1][0], ensure_ascii=False)
