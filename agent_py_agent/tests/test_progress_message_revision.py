"""独立公开展示反例；仅内存TUI与合成已验消息，不访问模型、服务或工具。"""
import pytest

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


@pytest.mark.parametrize("phase,boundary", [("final_answer", "terminal"), ("commentary", "tools")])
def test_typed_committed_messages_keep_original_show_prompt_defer(phase, boundary):
    runtime = TuiRuntime("review-show-prompt")
    turn = runtime.begin_turn("request-review", defer_assistant_display=True)
    payload = {"schema": "assistant_messages.v1", "source": {"call_id": "call-review"},
        "boundary": boundary, "messages": [{"text": "模型正文", "phase": phase, "message_id": "m-review"}],
        "final_text": "模型正文"}
    turn.commit_model_response(payload)
    snap = runtime.store.snapshot()
    assert not [block for block in (*snap.stable_blocks, *snap.active_blocks) if block.role == "assistant"]
    runtime.write_console("===== FINAL PROMPT =====\nprompt\n===== RESPONSE =====")
    runtime.complete_turn("request-review", TuiTurnSummary(response_text="最终正文", ok=True))
    rows = runtime.store.snapshot().stable_blocks
    prompt_index = next(i for i, block in enumerate(rows) if "FINAL PROMPT" in block.text)
    final_index = next(i for i, block in enumerate(rows) if block.text == "最终正文")
    assert prompt_index < final_index


def test_same_call_summary_correction_replaces_the_committed_final_block():
    runtime = TuiRuntime("review-final-correction")
    turn = runtime.begin_turn("request-review")
    turn.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-review"},
        "boundary": "terminal", "messages": [{"text": "原终答", "phase": "final_answer", "message_id": "m-review"}],
        "final_text": "原终答"})
    before_snapshot = runtime.store.snapshot()
    before = [block for block in (*before_snapshot.active_blocks, *before_snapshot.stable_blocks) if block.role == "assistant"]
    assert len(before) == 1 and before[0].text == "原终答"
    runtime.complete_turn("request-review", TuiTurnSummary(response_text="后续公开校正终答", ok=True))
    after = [block for block in runtime.store.snapshot().stable_blocks if block.role == "assistant"]
    assert len(before) == len(after) == 1
    assert after[0].block_id == before[0].block_id
    assert after[0].text == "后续公开校正终答"
    assert not runtime.store.snapshot().active_blocks
    assert not any(row.code == "TERMINAL_BLOCK_REPLAY" for row in runtime.store.snapshot().diagnostics)



def test_done_fallback_preserves_actual_output_indices_and_provider_order():
    import json

    from agent_py_agent.agent.backends.assistant_messages import response_messages
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    from agent_py_agent.tests.test_assistant_message_phases import _message
    events = [{"type": "response.output_item.done", "output_index": 1, "item": _message("FINAL", "final_answer", message_id="m-final")},
              {"type": "response.output_item.done", "output_index": 0, "item": _message("PROGRESS", "commentary", message_id="m-progress")},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    obj = collect_response((json.dumps(event) for event in events), None, None)
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert response.text == "PROGRESSFINAL"
    assert [(row["message_id"], row["output_index"], row["phase"]) for row in response_messages(response)] == [
        ("m-progress", 0, "commentary"), ("m-final", 1, "final_answer")]


@pytest.mark.parametrize("output_index", [None, 8])
def test_done_fallback_does_not_fabricate_missing_or_sparse_index(output_index):
    import json

    from agent_py_agent.agent.backends.assistant_messages import response_messages
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    from agent_py_agent.tests.test_assistant_message_phases import _message
    event = {"type": "response.output_item.done", "item": _message("正文", "final_answer")}
    if output_index is not None:
        event["output_index"] = output_index
    events = [event, {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    obj = collect_response((json.dumps(row) for row in events), None, None)
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert response_messages(response)[0]["output_index"] == output_index


def test_eof_synthetic_text_has_unknown_index_and_cannot_restore_done_tool():
    import json

    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    events = [{"type": "response.output_text.delta", "delta": "未确认正文"},
              {"type": "response.output_item.done", "output_index": 0, "item": {
                  "type": "function_call", "id": "f1", "call_id": "c1", "name": "read_file", "arguments": "{}"}}]
    obj = collect_response((json.dumps(row) for row in events), None, None)
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert response.truncated and not response.tool_use_blocks
    assert response.assistant_content_blocks[0]["assistant_message"].get("output_index") is None



def test_typed_final_replaces_only_active_delta_and_stable_replay_stays_rejected():
    runtime = TuiRuntime("revision-active-final")
    turn = runtime.begin_turn("request-revision")
    turn.write_model("原始半句")
    turn.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-revision"},
        "boundary": "terminal", "messages": [{"text": "完整候选", "phase": "final_answer", "message_id": "m-revision"}],
        "final_text": "完整候选"})
    snapshot = runtime.store.snapshot()
    assert not [row for row in snapshot.stable_blocks if row.role == "assistant"]
    candidate = next(row for row in snapshot.active_blocks if row.role == "assistant")
    assert candidate.text == "完整候选"
    runtime.complete_turn("request-revision", TuiTurnSummary(response_text="最终安全正文", ok=True))
    runtime._publish("assistant_delta", "updated", candidate.block_id, {"text": "迟到旧内容", "replace_text": True}, request_id="request-revision")
    rows = [row for row in runtime.store.snapshot().stable_blocks if row.role == "assistant"]
    assert len(rows) == 1 and rows[0].block_id == candidate.block_id and rows[0].text == "最终安全正文"
    assert not runtime.store.snapshot().active_blocks


@pytest.mark.parametrize("kind", ["assistant_commentary", "assistant_final"])
def test_gateway_typed_events_also_respect_deferred_display(kind):
    runtime = TuiRuntime("revision-defer-gateway")
    turn = runtime.begin_turn("request-revision", defer_assistant_display=True)
    turn.on_gateway_event({"kind": kind, "text": "不应提前显示", "replace_candidate": True, "model_source": {"call_id": "c1"}})
    snapshot = runtime.store.snapshot()
    assert not [row for row in (*snapshot.active_blocks, *snapshot.stable_blocks) if row.role == "assistant"]
    runtime.write_console("===== FINAL PROMPT =====")
    runtime.complete_turn("request-revision", TuiTurnSummary(response_text="最终正文", ok=True))
    rows = runtime.store.snapshot().stable_blocks
    assert next(i for i, row in enumerate(rows) if row.text == "===== FINAL PROMPT =====") < next(i for i, row in enumerate(rows) if row.text == "最终正文")


def test_done_duplicate_identity_and_unknown_content_parts_keep_exact_source():
    import json

    from agent_py_agent.agent.backends.assistant_messages import response_messages
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import (
        collect_response,
        input_items,
        response_fields,
    )
    from agent_py_agent.tests.test_assistant_message_phases import _message
    item = _message("旧候选", "commentary")
    corrected = _message("新正文", "final_answer")
    corrected["content"].append({"type": "output_text", "text": "续段"})
    events = [{"type": "response.output_item.done", "item": item},
              {"type": "response.output_item.done", "item": corrected},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    obj = collect_response((json.dumps(row) for row in events), None, None)
    assert set(obj) == {"status", "output"}  # host索引只在私有属性，不加入供应商JSON。
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert response.text == "新正文续段"
    assert [(row["message_id"], row["output_index"], row["phase"]) for row in response_messages(response)] == [("m1", None, "final_answer")]
    assert input_items("", [{"role": "assistant", "content": response.assistant_content_blocks}], "", "model") == [
        {"role": "assistant", "id": "m1", "phase": "final_answer", "content": "新正文续段"}]


@pytest.mark.parametrize("conflict", [False, True])
def test_done_tool_replay_executes_at_most_one_or_rejects_conflicting_call_ids(conflict):
    import json

    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    first = {"type": "function_call", "id": "f1", "call_id": "c1", "name": "read_file", "arguments": "{}"}
    second = {**first, **({"id": "f2"} if conflict else {})}
    events = [{"type": "response.output_item.done", "output_index": 3, "item": first},
              {"type": "response.output_item.done", "output_index": 5 if conflict else 3, "item": second},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    response = ModelResponse(backend="openai_responses", **response_fields(collect_response((json.dumps(row) for row in events), None, None), "model"))
    if conflict:
        assert response.truncated and response.tool_use_blocks == []
    else:
        assert not response.truncated and [row["id"] for row in response.tool_use_blocks] == ["c1"]



def test_missing_indices_keep_unknown_anchor_and_phase_without_message_id():
    import json

    from agent_py_agent.agent.backends.assistant_messages import (
        final_response_text,
        response_messages,
    )
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    from agent_py_agent.tests.test_assistant_message_phases import _message
    first = _message("P", "commentary", message_id="p")
    unknown = _message("U", "Future_Phase")
    unknown.pop("id")
    last = _message("F", "final_answer", message_id="f")
    events = [{"type": "response.output_item.done", "output_index": 8, "item": last},
              {"type": "response.output_item.done", "item": unknown},
              {"type": "response.output_item.done", "output_index": 2, "item": first},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    obj = collect_response((json.dumps(row) for row in events), None, None)
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert response.text == "PUF"
    assert [row["output_index"] for row in response_messages(response)] == [2, None, 8]
    assert final_response_text(response) == "F"
    for message, phase in [(first, "commentary"), (last, "final_answer")]:
        message.pop("id")
        assert message["phase"] == phase
    events = [{"type": "response.output_item.done", "item": item} for item in (first, last)] + [events[-1]]
    response = ModelResponse(backend="openai_responses", **response_fields(collect_response((json.dumps(row) for row in events), None, None), "model"))
    assert [row["phase"] for row in response_messages(response)] == ["commentary", "final_answer"]
    assert all(row["output_index"] is None for row in response_messages(response))
    assert final_response_text(response) == "F"


def test_nonempty_terminal_remains_original_authority_over_done_conflict():
    import json

    from agent_py_agent.agent.backends.assistant_messages import response_messages
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
    from agent_py_agent.tests.test_assistant_message_phases import _message
    terminal = {"status": "completed", "output": [_message("真正终态", "final_answer", message_id="terminal")]}
    events = [{"type": "response.output_item.done", "output_index": 9, "item": _message("旧文", "commentary", message_id="old1")},
              {"type": "response.output_item.done", "output_index": 9, "item": _message("冲突旧文", "final_answer", message_id="old2")},
              {"type": "response.completed", "response": terminal}]
    obj = collect_response((json.dumps(row) for row in events), None, None)
    assert obj == terminal and type(obj) is dict
    response = ModelResponse(backend="openai_responses", **response_fields(obj, "model"))
    assert not response.truncated and response.text == "真正终态"
    assert [(row["message_id"], row["output_index"]) for row in response_messages(response)] == [("terminal", 0)]



def test_foreground_observer_keeps_typed_final_active_until_canonical_projection(tmp_path):
    from agent_py_agent.agent.conversation.background_transcript import (
        read_background_transcript_events,
    )
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page
    from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn
    from agent_py_agent.cli.chat_parts.tui_threading import _publish_background_notice_row
    from agent_py_agent.tests.test_gateway_foreground_transcript import _foreground
    agent, writer, store, thread, _ = _foreground(tmp_path)
    writer.write_model("旧候选\n")
    writer.flush()
    writer.commit_model_response({"schema": "assistant_messages.v1", "source": {"call_id": "call-observer"},
        "boundary": "terminal", "messages": [{"text": "供应商完整候选", "phase": "final_answer", "message_id": "m-observer"}],
        "final_text": "供应商完整候选"})
    runtime = TuiRuntime("revision-observer")
    runtime.publish_background_transcript_events(read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"])
    before = runtime.store.snapshot()
    assert not [row for row in before.stable_blocks if row.role == "assistant"]
    assert [row.text for row in before.active_blocks if row.role == "assistant"] == ["供应商完整候选"]
    snapshot = writer.prepare_display_history()
    final_meta = GatewayAssistantTurn(end_reason="completed", display_snapshot=snapshot).metadata()
    store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "宿主最终安全正文", "metadata": {
        "gateway_request_id": "gwreq-live", "conversation_request_id": "request-a", **final_meta, "assistant_part_id": "final"}})
    writer.close()
    notices, _, ok = read_background_response_page(store, thread.thread_id, include_foreground=True)
    assert ok
    for notice in notices:
        assert _publish_background_notice_row(runtime, notice, set())
    after = runtime.store.snapshot()
    assert [row.text for row in after.stable_blocks if row.role == "assistant"] == ["宿主最终安全正文"]
    assert not after.active_blocks
    assert not any(row.code == "TERMINAL_BLOCK_REPLAY" for row in after.diagnostics)
