"""已验响应→原CLI/TUI出口的合同与fake-loop测试，不访问真实模型或服务。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.responses_wire import response_fields
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.run_output import make_run_chunk_writer
from agent_py_agent.tests.test_assistant_message_phases import _message, _response
from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend


# LLM: 合成payload只用于显示入口单测；产品提交另由真实生成出口的sealed来源门负责。
# 函数用途: 构造带不同调用编号的已验消息显示夹具，不发网络或执行工具。
def _payload(messages, *, call_id="call-display", boundary="terminal", final="终答"):
    return {"schema": "assistant_messages.v1", "source": {"call_id": call_id, "request_id": "req-display", "run_id": "run-display"},
            "messages": messages, "boundary": boundary, "final_text": final}


# LLM: 仅纯writer/展示映射单测可构造已验输入；真实Agent接线必须由原core sealed-call提交，禁止该helper介入。
# 函数用途: 给纯显示单测完整响应及不同call身份并写原临时chunk，不能把raw flush冒充公开资格。
def _commit_writer_fixture(writer, text, *, call_id, boundary="tools"):
    writer.commit_model_response(_payload([{"text": text}], call_id=call_id,
                                        boundary=boundary, final=text if boundary == "terminal" else ""))


@pytest.mark.parametrize("stream", [False, True])
def test_cli_fake_tool_loop_commits_text_before_tool_and_keeps_final(tmp_path, capsys, stream):
    agent = _agent(tmp_path)
    path = agent.home_paths.owner_home_dir / "note.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("真实工具读取内容", encoding="utf-8")

    class ReadThenReply(_StaticBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if stream:
                    on_chunk("不应打印的旧候选。")
                return ModelResponse(text="先读取资料再核对。", backend=self.name, tool_use_blocks=[
                    {"id": "read-once", "name": "read_file", "input": {"path": str(path)}}])
            return ModelResponse(text="核对完成。", backend=self.name)

    agent.backend = ReadThenReply()
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {"seen": False, "text": ""})
    result = agent.run("读取资料并核对", allowed_tools=["read_file"], on_chunk=writer, save=False)
    output = capsys.readouterr().out
    assert output.index("先读取资料再核对。") < output.index("[工具]")
    assert "不应打印的旧候选" not in output
    assert result.response == "核对完成。"
    assert result.assistant_commentary_messages == ["先读取资料再核对。"]
    assert agent.backend.calls == 2 and result.tool_rounds == 1


def test_commentary_only_completed_stops_once_and_persists_cli_native(tmp_path):
    agent = _agent(tmp_path)

    class CommentaryOnly(_StaticBackend):
        def generate(self, prompt, **kwargs):
            self.calls += 1
            return ModelResponse(backend=self.name, **response_fields(
                {"status": "completed", "output": [_message("我继续核对。", "commentary")]}, "model"))

    agent.backend = CommentaryOnly()
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {})
    result = agent.run("核对当前说明", source="cli_run", on_chunk=writer, save=False)
    assert agent.backend.calls == 1 and result.tool_rounds == 0
    assert result.response == "" and result.runtime_reason == "USER_REPLY_UNAVAILABLE"
    assert result.assistant_commentary_messages == ["我继续核对。"]
    rows = agent.conversation_store.messages.recent(agent.conversation_store.threads.list()[0].thread_id, limit=0)
    assert any(row.content == "我继续核对。" for row in rows)
    assert "commentary" in json.dumps(rows[-1].metadata, ensure_ascii=False)


def test_tui_replaces_old_candidate_and_keeps_equal_text_distinct_messages():
    runtime = TuiRuntime("phase-commit")
    turn = runtime.begin_turn("req-display")
    turn.write_model("流里的旧正文")
    payload = _payload([{"text": "相同正文", "phase": "commentary", "message_id": "m1"},
                        {"text": "相同正文", "phase": "final_answer", "message_id": "m2"}], final="相同正文")
    turn.commit_model_response(payload)
    turn.commit_model_response(payload)
    runtime.complete_turn("req-display", TuiTurnSummary(response_text="相同正文"))
    blocks = [row for row in runtime.store.snapshot().stable_blocks if row.role == "assistant"]
    assert [row.text for row in blocks] == ["相同正文", "相同正文"]
    assert blocks[0].metadata.get("process") is True
    assert not blocks[1].metadata.get("process")
    assert blocks[0].block_id != blocks[1].block_id


def test_gateway_commit_uses_terminal_not_delta_and_keeps_final_only_im(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / "rich.jsonl", rich_transcript=True)
    writer.write_model("旧正文\n")
    writer.commit_model_response(_payload([{"text": "终态确认", "phase": "final_answer", "message_id": "m1"}], final="终态确认"))
    events = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()]
    assert events[-1]["kind"] == "assistant_final" and events[-1]["text"] == "终态确认"
    assert events[-1]["replace_candidate"] is True
    im = BufferedChunkStreamWriter(tmp_path / "im.jsonl")
    im.set_final_only()
    im.commit_model_response(_payload([{"text": "接单过程", "phase": None}], boundary="tools", final=""))
    assert not im.chunk_path.exists()


def test_tool_removal_keeps_text_source_but_drops_invalid_thinking():
    from agent_py_agent.agent.agent_core.tool_ir_history import _assistant_turn_without
    from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
    response = _response([_message("过程来源", "commentary")])
    call = canonical_history_call("read_file", {"path": "a"}, call_id="tool-a")
    turn = AssistantTurn(text=response.text, tool_calls=[call], content_blocks=[
        {"type": "thinking", "thinking": "不应重放的签名思考", "signature": "old"},
        *response.assistant_content_blocks, {"type": "tool_use", "id": "tool-a", "name": "read_file", "input": {"path": "a"}}])
    rewritten = _assistant_turn_without(turn, {"tool-a"})
    assert rewritten.content_blocks == response.assistant_content_blocks
    assert rewritten.tool_calls == []
    assert AnthropicMessageAdapter().to_provider_messages([rewritten])[0]["content"][0]["assistant_message"]["phase"] == "commentary"


def test_gateway_commentary_only_persists_partial_before_original_unavailable(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.native_history import (
        canonical_native_messages_from_metadata,
    )
    from agent_py_agent.agent.gateway_parts import request_history
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationContext,
    )
    from agent_py_agent.agent.gateway_parts.request_errors import UserReplyUnavailableError

    agent = _agent(tmp_path)
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat"})
    writer = BufferedChunkStreamWriter(tmp_path / "gateway-chunks.jsonl", rich_transcript=True)
    context = GatewayAskRunContext(agent=agent, request={"metadata": {"channel": "chat"}}, request_id="req-no-final",
        request_path=tmp_path / "request.json", response_path=tmp_path / "response.json", on_chunk=writer)
    response = _response([_message("只有确认过程。", "commentary")])

    class CommentaryOnly(_StaticBackend):
        def generate(self, prompt, **kwargs):
            self.calls += 1
            return response

    agent.backend = CommentaryOnly()
    result = agent.run("合成过程消息", source="gateway", request_id=context.request_id,
        task_attributes={"conversation_thread_id": thread.thread_id}, on_chunk=writer, save=False)
    assert result.response == "" and result.runtime_reason == "USER_REPLY_UNAVAILABLE"
    assert agent.backend.calls == 1 and result.tool_rounds == 0 and response.stop_reason == "end_turn"
    repairs = []
    monkeypatch.setattr(request_history, "_queue_gateway_conversation_repair", lambda *args, **kwargs: repairs.append(kwargs))
    for _ in range(2):
        with pytest.raises(UserReplyUnavailableError):
            request_history.persist_gateway_assistant_result(context, GatewayConversationContext(thread_id=thread.thread_id), result)
    rows = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    carriers = [row for row in rows if row.metadata.get("assistant_part_id") == "final"]
    assert len(carriers) == 1 and carriers[0].content == "" and carriers[0].metadata["turn_end_reason"] == "error"
    assert canonical_native_messages_from_metadata(carriers[0].metadata)
    assert [row.content for row in rows if row.metadata.get("assistant_part_id") == "commentary:1"] == [response.text]
    assert not repairs and not getattr(result, "conversation_persist_degraded", False)


@pytest.mark.parametrize("reason", ["", "structured_tool_call"])
def test_natural_reply_rejection_does_not_resurrect_original_phase_blocks(reason):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        finish_natural_user_reply,
    )
    from agent_py_agent.agent.backends.assistant_messages import final_response_text
    from agent_py_agent.tests.test_response_decision_native_tool_use import _params
    response = _response([_message("旧过程", "commentary"), _message("旧终答", "final_answer", message_id="m2")])
    rejected = finish_natural_user_reply(_params(), response, accepted=False, rejection_reason=reason)
    assert final_response_text(rejected) == rejected.text
    assert "旧终答" not in final_response_text(rejected)


def test_timeout_probe_composite_keeps_both_current_text_segments():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from agent_py_agent.agent.agent_core.tool_loop.response_decision import ToolLoopRepairCounters
    from agent_py_agent.agent.backends.assistant_messages import final_response_text
    from agent_py_agent.tests.test_provider_timeout_resume_probe import (
        _decide,
        _FakeAgent,
        _generate,
        _params,
        _RecordingPrompts,
        _ScriptedBackend,
    )
    with TemporaryDirectory() as directory:
        params = _params(run_id="run-composite-phase", executed_tools=["read_file"])
        backend = _ScriptedBackend(["TIMEOUT", "前一枪正文", "终态正文"])
        agent = _FakeAgent(backend, _RecordingPrompts(), Path(directory))
        prior = _generate(agent, params)
        first = _decide(params, prior, ToolLoopRepairCounters())
        terminal = _generate(agent, params)
        terminal.assistant_content_blocks = _response([_message(terminal.text, "final_answer")]).assistant_content_blocks
        result = _decide(params, terminal, first.counters).response
        assert result.text == "前一枪正文\n\n终态正文"
        assert final_response_text(result) == result.text


@pytest.mark.parametrize("boundary", ["tools", "terminal"])
@pytest.mark.parametrize("parts", [("pass", "word=SYNTHETIC_SECRET_515"), ("PASSWORD=", "SYNTHETIC_SECRET_515")])
def test_committed_projection_redacts_across_chunks_messages_and_phases(capsys, boundary, parts):
    from agent_py_agent.agent.backends.assistant_messages import (
        final_response_text,
        response_messages,
    )
    from agent_py_agent.agent.gateway_parts.stream_events import committed_model_events
    response = _response([_message(parts[0], "commentary"), _message(parts[1], "final_answer", message_id="m2")])
    payload = _payload(response_messages(response), boundary=boundary, final=final_response_text(response))
    events = committed_model_events(payload, delivery_channel="cli")
    assert len(events) == 1
    assert events[0]["projection"] == "full_response_security_context"
    assert "message_source" not in events[0]  # 合并投影不伪造provider消息phase或编号。
    assert "SYNTHETIC_SECRET_515" not in events[0]["text"]
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {})
    for part in parts:
        writer.write_model(part)
    writer.commit_model_response(payload)
    output = capsys.readouterr().out
    assert "SYNTHETIC_SECRET_515" not in output
    if boundary == "tools":
        assert "[REDACTED]" in output


@pytest.mark.parametrize("fault", ["none", "unsealed", "wrong_attempt", "wrong_purpose", "failed", "cleared", "interrupted", "cancelled"])
def test_public_commit_requires_accepted_same_sealed_call(monkeypatch, fault):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model import visible_response
    from agent_py_agent.tests.test_background_public_receipt_contract import _params
    from agent_py_agent.tests.test_background_public_receipt_contract import (
        _response as sealed_response,
    )
    delivered = []
    params = replace(_params(explicit=False), effective_on_chunk=SimpleNamespace(commit_model_response=delivered.append))
    response, _ = sealed_response(params, "已验公开正文", purpose="compact" if fault == "wrong_purpose" else "main")
    response.assistant_content_blocks = _response([_message(response.text, "commentary")]).assistant_content_blocks
    if fault == "unsealed":
        response.response_provenance = {"call_id": "call-1"}
    elif fault == "wrong_attempt":
        params = replace(params, attempt_id="other-attempt")
    elif fault == "failed":
        response.runtime_status = "failed"
    elif fault == "cleared":
        response.text = ""
    elif fault == "interrupted":
        monkeypatch.setattr(visible_response, "is_interrupted", lambda: True)
    elif fault == "cancelled":
        params.cancellation_token.cancel()
    visible_response.commit_visible_response(params, response, boundary="tools")
    assert bool(delivered) is (fault == "none")
    if delivered:
        assert delivered[0]["source"]["attempt_id"] == params.attempt_id
        assert delivered[0]["messages"][0]["phase"] == "commentary"
