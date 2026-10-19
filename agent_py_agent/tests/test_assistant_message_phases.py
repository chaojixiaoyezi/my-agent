"""逐消息来源、完整正文一致性与跨协议历史合同；全程不发网络请求。"""
import pytest

from agent_py_agent.agent.backends.assistant_messages import final_response_text, response_messages
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.openai_chat import _openai_message_from_native
from agent_py_agent.agent.backends.responses_wire import (
    collect_response,
    input_items,
    response_fields,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_envelope,
    canonical_native_messages_from_metadata,
)


def _message(text, phase=None, *, message_id="m1"):
    item = {"type": "message", "id": message_id, "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}
    if phase is not None:
        item["phase"] = phase
    return item


def _response(items):
    return ModelResponse(backend="openai_responses", **response_fields({"status": "completed", "output": items}, "model"))


def test_phases_are_per_message_and_final_does_not_include_commentary():
    result = _response([_message("先查入口。", "commentary"), _message("已经完成。", "final_answer", message_id="m2")])
    assert result.text == "先查入口。已经完成。"
    rows = response_messages(result)
    assert [(row["text"], row["phase"], row["message_id"]) for row in rows] == [
        ("先查入口。", "commentary", "m1"), ("已经完成。", "final_answer", "m2")]
    assert final_response_text(result) == "已经完成。"


@pytest.mark.parametrize("phase", [None, "Future_Phase"])
def test_unknown_phase_stays_exact_and_preserves_legacy_reply(phase):
    result = _response([_message("正常回答。", phase)])
    assert response_messages(result)[0]["phase"] == phase
    assert final_response_text(result) == "正常回答。"
    sent = input_items("", [{"role": "assistant", "content": result.assistant_content_blocks}], "", "model")
    assert "phase" not in sent[0]


@pytest.mark.parametrize("replacement", ["", "宿主替换的错误说明", "先查入口。已经完成。\n\n另一枪正文"])
def test_changed_public_text_never_resurrects_old_blocks(replacement):
    response = _response([_message("先查入口。", "commentary"), _message("已经完成。", "final_answer", message_id="m2")])
    response.text = replacement
    assert final_response_text(response) == replacement
    assert all(row["phase"] is None for row in response_messages(response))


def test_failed_equal_text_is_not_promoted_by_phase():
    response = _response([_message("错误候选", "commentary")])
    response.runtime_status = "failed"
    assert final_response_text(response) == "错误候选"


def test_commentary_only_has_no_final_without_changing_response_stop():
    response = _response([_message("我继续核对。", "commentary")])
    assert final_response_text(response) == ""
    assert response.text == "我继续核对。" and response.stop_reason == "end_turn"


def test_message_boundaries_survive_ir_envelope_and_responses_replay():
    response = _response([_message("过程一", "commentary"), _message("终答", "final_answer", message_id="m2")])
    native = AnthropicMessageAdapter().to_provider_messages([
        AssistantTurn(text=response.text, content_blocks=response.assistant_content_blocks)])
    envelope = canonical_native_messages_envelope(native)
    reloaded = canonical_native_messages_from_metadata({"canonical_native_messages": envelope})
    sent = input_items("", list(reloaded), "", "model")
    assert [(row.get("id"), row.get("phase"), row["content"]) for row in sent] == [
        ("m1", "commentary", "过程一"), ("m2", "final_answer", "终答")]
    assert input_items("", native, "", "model") == sent


@pytest.mark.parametrize("cache_enabled", [False, True])
def test_responses_metadata_does_not_escape_to_chat_or_anthropic(cache_enabled):
    from agent_py_agent.agent.backends.anthropic_prompt_cache import (
        anthropic_messages_with_optional_cache,
    )
    result = _response([_message("过程", "commentary")])
    native = AnthropicMessageAdapter().to_provider_messages([AssistantTurn(text=result.text, content_blocks=result.assistant_content_blocks)])
    chat = _openai_message_from_native(native[0])
    anth, _ = anthropic_messages_with_optional_cache(prompt="", messages=native, tools=[], cache_enabled=cache_enabled)
    assert chat == [{"role": "assistant", "content": "过程"}]
    assert anth[0]["content"][0]["text"] == "过程"
    assert set(anth[0]["content"][0]) <= {"type", "text", "cache_control"}
    assert "assistant_message" in native[0]["content"][0]


def test_terminal_message_corrects_late_phase_and_old_stream_text():
    import json
    events = [
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "delta": "旧候选"},
        {"type": "response.output_item.done", "item": _message("旧候选", "commentary")},
        {"type": "response.completed", "response": {"status": "completed", "output": [_message("终态正文", "final_answer")]}}]
    result = ModelResponse(backend="openai_responses", **response_fields(collect_response((json.dumps(e) for e in events), None, None), "model"))
    assert result.text == final_response_text(result) == "终态正文"
    assert response_messages(result)[0]["phase"] == "final_answer"


def test_refusal_source_and_unknown_mix_keep_their_original_semantics():
    response = _response([_message("过程", "commentary"), _message("兼容终答", "Future_Phase", message_id="m2")])
    assert final_response_text(response) == "兼容终答"
    item = _message("", "final_answer")
    item["content"] = [{"type": "refusal", "refusal": "合法拒绝正文"}]
    response = _response([item])
    assert final_response_text(response) == "合法拒绝正文"
    assert response_messages(response)[0]["source_type"] == "refusal"


@pytest.mark.parametrize("parts", [("pass", "word=SYNTHETIC_SECRET_515"), ("PASSWORD=", "SYNTHETIC_SECRET_515")])
def test_final_phase_keeps_whole_response_redaction_context(parts):
    from agent_py_agent.agent.conversation.channels import project_user_reply
    response = _response([_message(parts[0], "commentary"), _message(parts[1], "final_answer", message_id="m2")])
    assert final_response_text(response) == project_user_reply("".join(parts)).content
    assert "SYNTHETIC_SECRET_515" not in final_response_text(response)
    assert response.text == "".join(parts)  # 原协议历史仍保存真实正文，公开投影不写回。


def test_sse_same_message_replaces_but_different_equal_text_is_not_deduplicated():
    import json
    events = [{"type": "response.output_item.done", "item": _message("旧正文", "commentary")},
              {"type": "response.output_item.done", "item": _message("相同正文", "final_answer")},
              {"type": "response.output_item.done", "item": _message("相同正文", "final_answer", message_id="m2")},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    response = _response(collect_response((json.dumps(event) for event in events), None, None)["output"])
    assert response.text == "相同正文相同正文"
    assert [(row["message_id"], row["phase"]) for row in response_messages(response)] == [("m1", "final_answer"), ("m2", "final_answer")]



def test_tool_interleaved_sources_survive_canonical_reload_and_pair_reclamation():
    from agent_py_agent.agent.agent_core.tool_ir_history import drop_tool_call_pairs
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )
    call = canonical_history_call("read_file", {"path": "note.txt"}, call_id="t1")
    response = _response([_message("读取前说明", "commentary"),
        {"type": "function_call", "call_id": "t1", "name": "read_file", "arguments": '{"path":"note.txt"}'},
        _message("供应商第二条正文", "Future_Phase", message_id="m2")])
    history = [AssistantTurn(text=response.text, tool_calls=[call], content_blocks=response.assistant_content_blocks),
               canonical_history_result(call, "读取结果")]
    native = AnthropicMessageAdapter().to_provider_messages(history)
    sent = input_items("", native, "", "model")
    assert [item.get("type", "message") for item in sent] == ["message", "function_call", "message", "function_call_output"]
    assert sent[0]["phase"] == "commentary" and sent[2]["id"] == "m2" and "phase" not in sent[2]
    from types import SimpleNamespace
    assert drop_tool_call_pairs(SimpleNamespace(tool_ir_history=history), {"t1"}) == 1
    rewritten = AnthropicMessageAdapter().to_provider_messages(history)
    envelope = canonical_native_messages_envelope(rewritten)
    restored = canonical_native_messages_from_metadata({"canonical_native_messages": envelope})
    assert input_items("", list(restored), "", "model") == input_items("", rewritten, "", "model")
    assert [(block["assistant_message"]["message_id"], block["assistant_message"]["phase"]) for block in rewritten[0]["content"]] == [
        ("m1", "commentary"), ("m2", "Future_Phase")]
