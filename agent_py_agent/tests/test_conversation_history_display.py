from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.cli.chat_parts.history import load_gateway_chat_history
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


# LLM: 此 helper 只生成 canonical 结构夹具，不运行模型或写入会话；便于检查显示恢复不会污染原生历史。
# 函数用途: 模拟一次带思考、过程回复和工具结果的真实原生回合。
def _rows(*, failed: bool = False) -> list[SimpleNamespace]:
    native = [
        {"role": "user", "content": [{"type": "text", "text": "INTERNAL_RUNTIME_INJECTION"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "先查看项目。", "signature": "PRIVATE_SIGNATURE"},
            {"type": "text", "text": "我先检查。"},
            {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"secret": "PRIVATE_INPUT"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "is_error": failed, "content": "项目内容里写着 failed"},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "检查结束。"},
            {"type": "text", "text": "这是完整汇报。"},
        ]},
    ]
    rows = [
        SimpleNamespace(role="user", content="帮我检查项目", metadata={"gateway_request_id": "req-1"}),
        SimpleNamespace(role="assistant", content="我先检查。", metadata={"gateway_request_id": "req-1", "assistant_part_id": "commentary:1"}),
        SimpleNamespace(role="assistant", content="这是完整汇报。", metadata={
            "gateway_request_id": "req-1", "assistant_part_id": "final",
            "canonical_native_messages": canonical_native_messages_envelope(native),
        }),
    ]
    for index, row in enumerate(rows):
        row.thread_id = "thread-1"
        row.message_id = f"msg-source-{index}"
    return rows


@pytest.mark.parametrize("failed", [False, True])
def test_rich_restore_preserves_order_and_never_requeues_or_mutates(failed):
    rows = _rows(failed=failed)
    before = copy.deepcopy(rows)
    events = conversation_history_display_events(rows)
    runtime = TuiRuntime("history-rich")
    runtime.publish_recovered_history([], display_events=events)
    first = runtime.store.snapshot()
    assert [(block.role, block.text) for block in first.stable_blocks] == [
        ("user", "帮我检查项目"), ("thinking", "先查看项目。"),
        ("assistant", "我先检查。"), ("tool", ""),
        ("thinking", "检查结束。"), ("assistant", "这是完整汇报。"),
    ]
    tool = first.stable_blocks[3]
    assert tool.title == "read_file"
    assert "failed" in tool.detail
    assert tool.phase == ("failed" if failed else "completed")
    assert first.active_blocks == ()
    assert first.queued_inputs == ()
    assert first.pending_steers == ()
    runtime.publish_recovered_history([], display_events=events)
    assert runtime.store.snapshot().stable_blocks == first.stable_blocks
    assert rows == before
    encoded = json.dumps(events, ensure_ascii=False)
    for private in ("INTERNAL_RUNTIME_INJECTION", "PRIVATE_SIGNATURE", "PRIVATE_INPUT"):
        assert private not in encoded


def test_without_native_keep_commentary_final_and_unanswered_user():
    rows = _rows()
    rows[-1].metadata.pop("canonical_native_messages")
    rows.append(SimpleNamespace(role="user", content="还有一问", metadata={"gateway_request_id": "req-2"}))
    events = conversation_history_display_events(rows)
    assert [event["payload"]["text"] for event in events] == [
        "帮我检查项目", "我先检查。", "这是完整汇报。", "还有一问",
    ]


@pytest.mark.parametrize("result_change", ["missing", "untyped"])
def test_missing_tool_evidence_is_neutral_not_success(result_change):
    rows = _rows()
    result = rows[-1].metadata["canonical_native_messages"]["messages"][2]["content"]
    if result_change == "missing":
        result.clear()
    else:
        result[0].pop("is_error")
    events = conversation_history_display_events(rows)
    assert not any(event["kind"] in {"tool_completed", "tool_failed"} for event in events)
    assert any(event["kind"] == "system_message" and "没有完整" in event["payload"]["text"] for event in events)


def test_compacted_native_tail_does_not_hide_earlier_canonical_commentary():
    rows = _rows()
    rows.insert(1, SimpleNamespace(role="assistant", content="压缩前的过程回复", metadata={
        "gateway_request_id": "req-1", "assistant_part_id": "commentary:0",
    }))
    events = conversation_history_display_events(rows)
    texts = [event["payload"].get("text", "") for event in events]
    assert texts.count("压缩前的过程回复") == 1
    assert texts.count("我先检查。") == 1
    assert texts.count("这是完整汇报。") == 1


def test_audit_and_internal_roles_cannot_enter_resume():
    rows = _rows() + [
        SimpleNamespace(role="system", content="PRIVATE_SYSTEM", metadata={}),
        SimpleNamespace(role="assistant", content="DETACHED_AUDIT", metadata={
            "gateway_request_id": "audit-1", "task_id": "task-audit", "reason": "audit_finding", "background_delivery_reason": "scheduled",
        }),
    ]
    encoded = json.dumps(conversation_history_display_events(rows))
    assert "PRIVATE_SYSTEM" not in encoded
    assert "DETACHED_AUDIT" not in encoded


def test_thin_client_transports_display_separately_from_model_preview():
    events = conversation_history_display_events(_rows())
    agent = SimpleNamespace(gateway_client_only=True, request_chat_history=lambda *_args, **_kwargs: {
        "ok": True, "thread_id": "thread-1", "turns": [{"user_message": "问", "assistant_message": "答"}],
        "display_events": list(events), "load_errors": [],
    })
    snapshot = load_gateway_chat_history(agent, "session-1", max_turns=20)
    assert snapshot.turns == (("问", "答"),)
    assert snapshot.display_events == events
    assert snapshot.load_errors == ()


def test_replay_rejects_control_or_active_events():
    event = conversation_history_display_events(_rows())[0]
    runtime = TuiRuntime("display-only")
    runtime.publish_recovered_history([], display_events=(
        {**event, "kind": "permission_requested"}, {**event, "phase": "started"},
        {**event, "request_id": "other", "block_id": "other:1"},
    ))
    assert runtime.store.snapshot().stable_blocks == ()
    assert runtime.store.snapshot().active_blocks == ()


def test_canonical_final_is_authoritative_when_provider_text_was_projected():
    rows = _rows()
    rows[-1].content = "正式汇报含用户可见交付引用"
    events = conversation_history_display_events(rows)
    texts = [event["payload"].get("text", "") for event in events]
    assert texts[-1] == "正式汇报含用户可见交付引用"
    assert "这是完整汇报。" not in texts


def test_background_commentary_does_not_count_as_twenty_extra_user_turns():
    rows = _rows()
    rows.extend(SimpleNamespace(
        role="assistant", content=f"后台过程 {index}", message_id=f"msg-{index}", thread_id="thread-1",
        metadata={"assistant_part_id": f"commentary:{index}"},
    ) for index in range(32))
    events = conversation_history_display_events(rows)
    assert events[0]["kind"] == "user_message"
    assert events[0]["payload"]["text"] == "帮我检查项目"
    assert len([event for event in events if event["kind"] == "assistant_completed"]) == 34
