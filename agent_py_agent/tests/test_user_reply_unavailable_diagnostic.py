"""wsempty4：只用合成供应商响应，覆盖原工具循环和请求失败落账。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ProviderRequestOptions
from agent_py_agent.agent.gateway_parts import request_history
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.request_errors import UserReplyUnavailableError
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
from agent_py_agent.tests.test_gateway_request_runtime_errors import _make_agent
from agent_py_agent.tests.test_responses_backend import completed
from agent_py_agent.tests.test_responses_empty_recovery import probe_backend, run_real_loop
from agent_py_agent.tests.test_responses_websocket import FakeConnection


def reasoning_endings():
    obj = completed([{"type": "reasoning", "encrypted_content": "PRIVATE_REASONING", "summary": []}])
    event = json.dumps({"type": "response.completed", "response": obj})
    return [FakeConnection([event]) for _ in range(3)]


def test_real_loop_reads_existing_budget_without_changing_retries(monkeypatch, tmp_path):
    result, used = run_real_loop(monkeypatch, tmp_path, reasoning_endings())
    assert result.response == "" and result.tool_rounds == 1
    assert len(used) == 4 and result.executed_tools == ["read_file"]
    diagnostic = getattr(result, "user_reply_diagnostic", None)
    assert diagnostic is not None, "真实工具循环必须把最后调用和现有空正文预算带到运行结果"
    assert diagnostic["empty_text_repairs"] == 2
    assert diagnostic["empty_text_repair_limit"] == 2
    assert diagnostic["tool_rounds"] == 1
    call = diagnostic["last_model_call"]
    assert call["tools_count"] == 1 and call["is_probe"] is False
    assert call["compaction_trigger"] is False
    assert call["output_items"] == [{"type": "reasoning", "deliverable_text_chars": 0, "content_types": []}]
    assert call["response_status"] == "completed" and call["incomplete_reason"] is None
    assert "PRIVATE_REASONING" not in json.dumps(diagnostic)


@pytest.mark.parametrize("stream", [False, True])
def test_provider_projection_has_only_types_lengths_and_actual_request_facts(monkeypatch, stream):
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    output = [
        {"type": "message", "content": [{"type": "output_text", "text": "PRIVATE_TEXT"},
                                         {"type": "refusal", "refusal": "PRIVATE_REFUSAL"}]},
        {"type": "reasoning", "encrypted_content": "PRIVATE_REASONING", "summary": []},
        {"type": "function_call", "name": "read_file", "call_id": "private-id", "arguments": "{}"},
        {"type": "compaction", "encrypted_content": "PRIVATE_COMPACTION"},
    ]
    backend, _ = probe_backend(monkeypatch, stream, output)
    response = backend.generate("PRIVATE_PROMPT", tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
                                tool_choice=ToolChoice.none(),
                                request_options=ProviderRequestOptions(is_probe=True, compaction_trigger=True))
    call = getattr(response, "model_call_diagnostic", None)
    assert call is not None, "HTTP和SSE共用原始Responses结构投影，不能从归一块猜条目类型"
    assert call == {
        # none仅禁止调用，既有合同保留schema；诊断记真实出站数，不把none猜成零。
        "tools_count": 1, "is_probe": True, "compaction_trigger": True,
        "response_status": "completed", "incomplete_reason": None,
        "output_items": [
            {"type": "message", "deliverable_text_chars": 27, "content_types": ["output_text", "refusal"]},
            {"type": "reasoning", "deliverable_text_chars": 0, "content_types": []},
            {"type": "function_call", "deliverable_text_chars": 0, "content_types": []},
            {"type": "compaction", "deliverable_text_chars": 0, "content_types": []},
        ],
    }
    assert "PRIVATE" not in json.dumps(call) and "private-id" not in json.dumps(call)


def test_incomplete_reason_is_raw_and_does_not_change_provider_terminal(monkeypatch):
    backend, _ = probe_backend(monkeypatch, False, [])
    obj = {"status": "incomplete", "incomplete_details": {"reason": "stream_eof", "text": "PRIVATE"},
           "output": [], "instructions": "PRIVATE"}
    monkeypatch.setattr(backend, "_send_responses_request", lambda *_args: obj)
    response = backend.generate("PRIVATE")
    assert response.runtime_reason == "MODEL_STREAM_INCOMPLETE"
    call = getattr(response, "model_call_diagnostic", None)
    assert call is not None
    assert call["response_status"] == "incomplete" and call["incomplete_reason"] == "stream_eof"
    assert "PRIVATE" not in json.dumps(call)


def test_failure_diagnostic_uses_canonical_compaction_time_without_body(monkeypatch, tmp_path):
    result, _ = run_real_loop(monkeypatch, tmp_path, reasoning_endings())
    from dataclasses import replace

    from agent_py_agent.agent.conversation.native_history import (
        canonical_native_messages_from_metadata,
    )
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationContext,
    )

    # 新的失败前partial落账义务需要原store/context；保留原精确诊断断言，并核实空carrier保存真实native。
    agent, paths = _make_agent(tmp_path / "gateway")
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat"})
    agent.conversation_store.threads.write(replace(thread, compact_updated_at=40.0))
    context = GatewayAskRunContext(agent=agent, request={"metadata": {"channel": "chat"}}, request_id="diagnostic-partial",
        request_path=paths.processing / "diagnostic-partial.json", response_path=paths.responses / "diagnostic-partial.json", on_chunk=None)
    monkeypatch.setattr(request_history, "time", SimpleNamespace(time=lambda: 160.0), raising=False)
    with pytest.raises(UserReplyUnavailableError) as caught:
        request_history.persist_gateway_assistant_result(context, GatewayConversationContext(thread_id=thread.thread_id), result)
    diagnostic = getattr(caught.value, "user_reply_diagnostic", None)
    assert diagnostic is not None
    assert diagnostic["seconds_since_last_compaction"] == 120.0
    assert set(diagnostic) == {"last_model_call", "empty_text_repairs", "empty_text_repair_limit",
                               "tool_rounds", "seconds_since_last_compaction"}
    assert "PRIVATE" not in json.dumps(diagnostic)
    rows = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    assert len(rows) == 1 and rows[0].content == ""
    assert canonical_native_messages_from_metadata(rows[0].metadata)
    assert rows[0].metadata["turn_end_reason"] == "error"
    assert not getattr(result, "conversation_persist_degraded", False)
    with pytest.raises(UserReplyUnavailableError):
        request_history.persist_gateway_assistant_result(context, GatewayConversationContext(thread_id=thread.thread_id), result)
    assert len(agent.conversation_store.messages.recent(thread.thread_id, limit=0)) == 1


def test_gateway_empty_reply_failure_persists_diagnostic_in_response_and_terminal(monkeypatch, tmp_path):
    result, used = run_real_loop(monkeypatch, tmp_path / "sampling", reasoning_endings())
    agent, paths = _make_agent(tmp_path / "gateway")
    request_id = "gw-unavailable-diagnostic"
    request_path = paths.processing / f"{request_id}.json"
    request_path.write_text(json.dumps({
        "id": request_id, "kind": "ask", "status": "processing", "turn_phase": "open", "prompt": "PRIVATE_PROMPT",
        "execution_attempt_id": "attempt-diagnostic",
        "conversation": {"channel": "chat", "channel_conversation_id": "diagnostic-session",
                         "channel_user_id": "local-agent", "canonical_user_id": "local-agent"},
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(agent, "run", lambda *_args, **_kwargs: calls.append(1) or result)
    response = _handle_gateway_request(agent, request_path)
    assert response["error_code"] == "USER_REPLY_UNAVAILABLE"
    assert response["ok"] is False and response["status"] == "failed"
    assert response["response"] == "" and calls == [1] and len(used) == 4
    diagnostic = response.get("user_reply_diagnostic")
    assert diagnostic is not None, "空回复抛错不能让只存在运行结果的诊断丢失"
    assert diagnostic["empty_text_repairs"] == 2 and diagnostic["tool_rounds"] == 1
    assert diagnostic["seconds_since_last_compaction"] is None, "没有压缩时必须未知，不补零"
    assert "PRIVATE" not in json.dumps(diagnostic)
    _finish_claimed_gateway_request(paths, request_path, request_id, response,
                                    conversation_store=agent.conversation_store)
    terminal = read_json_file(paths.terminal / request_path.name)
    assert terminal["terminal_response"]["user_reply_diagnostic"] == diagnostic
    saved = read_json_file(paths.responses / request_path.name)
    assert saved["user_reply_diagnostic"] == diagnostic


def test_shared_backend_request_local_purpose_and_no_tools(monkeypatch):
    backend, _ = probe_backend(monkeypatch, False, [{"type": "reasoning", "summary": []}])
    probe = backend.generate("PRIVATE", request_options=ProviderRequestOptions(is_probe=True))
    business = backend.generate("PRIVATE")
    assert probe.model_call_diagnostic["is_probe"] is True
    assert business.model_call_diagnostic["is_probe"] is False
    assert probe.model_call_diagnostic["tools_count"] == business.model_call_diagnostic["tools_count"] == 0
    assert probe.model_call_diagnostic is not business.model_call_diagnostic


def test_diagnostic_public_projection_drops_body_and_invalid_codes():
    from agent_py_agent.agent.backends.reply_diagnostics import public_user_reply_diagnostic

    raw = {"last_model_call": {
        "tools_count": 0, "is_probe": False, "compaction_trigger": False,
        "response_status": "PRIVATE STATUS", "incomplete_reason": "PRIVATE REASON",
        "text": "PRIVATE_BODY", "prompt": "PRIVATE_PROMPT", "key": "PRIVATE_KEY",
        "output_items": [{"type": "message", "deliverable_text_chars": 0, "content_types": ["refusal"],
                          "text": "PRIVATE_BODY", "encrypted_content": "PRIVATE_REASONING"}],
    }, "empty_text_repairs": 2, "empty_text_repair_limit": 2, "tool_rounds": 1,
        "seconds_since_last_compaction": float("nan"), "persona": "PRIVATE_PERSONA"}
    safe = public_user_reply_diagnostic(raw)
    assert "PRIVATE" not in json.dumps(safe)
    assert safe["seconds_since_last_compaction"] is None
    assert safe["last_model_call"]["response_status"] is None
    assert safe["last_model_call"]["incomplete_reason"] is None
    assert set(safe["last_model_call"]["output_items"][0]) == {"type", "deliverable_text_chars", "content_types"}


def test_last_logical_call_and_unknown_output_are_not_inferred_from_normalized_body():
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.reply_diagnostics import remember_model_call

    params = SimpleNamespace(live_archive_state={})
    first = ModelResponse(text="PRIVATE_BODY", backend="legacy")
    remember_model_call(params, first, {"tools": [{"name": "read_file"}]})
    assert params.live_archive_state["_last_model_call_diagnostic"]["tools_count"] == 1
    remember_model_call(params, first, {})
    call = params.live_archive_state["_last_model_call_diagnostic"]
    assert call["tools_count"] == 0 and call["is_probe"] is False
    assert call["output_items"] is None and call["response_status"] is None
    assert "PRIVATE" not in json.dumps(call)


@pytest.mark.parametrize("updated", [float("inf"), float("nan"), -1.0, None])
def test_nonfinite_or_missing_compaction_clock_is_unknown(monkeypatch, updated):
    context = SimpleNamespace(agent=SimpleNamespace(conversation_store=SimpleNamespace(
        threads=SimpleNamespace(load=lambda _id: SimpleNamespace(compact_updated_at=updated)))))
    monkeypatch.setattr(request_history, "time", SimpleNamespace(time=lambda: 160.0))
    assert request_history._last_compaction_elapsed(context, SimpleNamespace(thread_id="t")) is None
