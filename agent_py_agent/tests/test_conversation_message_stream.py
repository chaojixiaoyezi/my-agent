# LLM: canonical 消息流回归只使用临时 owner 存储与真实 TUI reducer；不执行被测任务或调用模型。
# 模块用途: 验证分页、恢复交接、重放幂等与损坏游标不会造成正文重复或遗漏。

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.history import load_gateway_chat_history
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import (
    _consume_background_notices,
    _publish_background_notice_row,
)


# LLM: 夹具创建独立的本地用户会话；所有文件均落 pytest 临时目录。
# 函数用途: 提供有真实 ID、行偏移和线程归属的消息源。
@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({
        "canonical_user_id": "local-agent", "channel": "chat",
        "channel_conversation_id": "session", "channel_user_id": "local-agent", "now": 1.0,
    })
    return store, thread.thread_id


# LLM: 此 helper 通过唯一 canonical append 入口落一条已提交 final；不触及旧 notices 文件。
# 函数用途: 为恢复和实时竞态测试追加稳定身份的后台回复。
def _append(store, thread_id, text="相同正文", *, metadata=None):
    return store.append_message({
        "thread_id": thread_id, "role": "assistant", "content": text, "now": 20.0,
        "metadata": {"background_delivery_reason": "root_subagents_terminal",
                     "assistant_part_id": "final", **(metadata or {})},
    })


def test_forward_pages_keep_distinct_ids_with_same_text_and_time(conversation):
    store, thread_id = conversation
    entries = [_append(store, thread_id) for _ in range(5)]
    cursor = 0
    found = []
    for expected_count in (2, 2, 1, 0):
        rows, next_cursor, errors = store.message_page_after_offset_report(thread_id, after=cursor, limit=2)
        assert not errors and len(rows) == expected_count
        assert next_cursor >= cursor
        cursor = next_cursor
        found.extend(row.message_id for row in rows)
    assert found == [entry.message_id for entry in entries]


@pytest.mark.parametrize("invalid", ["middle", "beyond", "foreign_row", "corrupt"])
def test_invalid_page_preserves_original_cursor(conversation, invalid):
    store, thread_id = conversation
    row = _append(store, thread_id)
    cursor = store.message_byte_offset_after(thread_id, row.message_id)
    if invalid == "middle":
        cursor -= 1
    elif invalid == "beyond":
        cursor += 1
    else:
        value = row.to_dict()
        value["thread_id"] = "other-thread"
        with store._message_path(thread_id).open("ab") as handle:
            handle.write((json.dumps(value).encode() if invalid == "foreign_row" else b"broken") + b"\n")
    assert read_background_response_page(store, thread_id, after=cursor) == ([], cursor, False)


def test_partial_last_row_is_read_on_next_complete_page(conversation):
    store, thread_id = conversation
    row = _append(store, thread_id)
    cursor = store.message_byte_offset_after(thread_id, row.message_id)
    value = {**row.to_dict(), "message_id": "msg-late", "content": "后到的完整回复"}
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
    path = store._message_path(thread_id)
    with path.open("ab") as handle:
        handle.write(encoded[:-2])
    assert read_background_response_page(store, thread_id, after=cursor) == ([], cursor, True)
    with path.open("ab") as handle:
        handle.write(encoded[-2:])
    rows, after, ok = read_background_response_page(store, thread_id, after=cursor)
    assert ok and after > cursor
    assert [(item["message_id"], item["content"]) for item in rows] == [("msg-late", "后到的完整回复")]


def test_forward_page_does_not_parse_prefix_again(conversation):
    store, thread_id = conversation
    first = _append(store, thread_id)
    cursor = store.message_byte_offset_after(thread_id, first.message_id)
    second = _append(store, thread_id)
    with store._message_path(thread_id).open("r+b") as handle:
        handle.write(b"!")
    rows, _, ok = read_background_response_page(store, thread_id, after=cursor)
    assert ok and [row["message_id"] for row in rows] == [second.message_id]


@pytest.mark.parametrize("gateway", [False, True])
def test_history_cursor_stops_at_last_read_row_not_later_append(conversation, monkeypatch, gateway):
    from agent_py_agent.agent.gateway_parts import client_service
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope

    store, thread_id = conversation
    first = _append(store, thread_id)
    exact_offset = store.message_byte_offset_after
    late = []

    # LLM: 模拟快照读取和取偏移之间到达新消息；只在临时 canonical 文件追加一次。
    # 函数用途: 构造不能使用稍后文件总长度跳过的真实读写竞态。
    def append_between_read_and_cursor(tid, mid):
        if not late:
            late.append(_append(store, thread_id))
        return exact_offset(tid, mid)

    monkeypatch.setattr(store, "message_byte_offset_after", append_between_read_and_cursor)
    agent = SimpleNamespace(conversation_store=store)
    if gateway:
        monkeypatch.setattr(client_service, "resolve_gateway_scope_agent", lambda *_args: agent)
        history = client_service.read_gateway_client_history(
            object(), scope=GatewayControlScope("local-agent", "chat", "session"), max_turns=20,
        )
        assert history.ok
    else:
        history = load_gateway_chat_history(agent, "session", max_turns=20)
        assert not history.load_errors
    assert history.message_cursor == exact_offset(thread_id, first.message_id)
    rows, _, ok = read_background_response_page(store, thread_id, after=history.message_cursor)
    assert ok and [row["message_id"] for row in rows] == [late[0].message_id]


@pytest.mark.parametrize("native", [False, True])
def test_history_and_live_final_share_one_block_and_keep_new_message(conversation, native):
    store, thread_id = conversation
    metadata = {"canonical_native_messages": canonical_native_messages_envelope([
        {"role": "assistant", "content": [{"type": "text", "text": "相同正文"}]},
    ])} if native else {}
    first = _append(store, thread_id, metadata=metadata)
    agent = SimpleNamespace(conversation_store=store)
    history = load_gateway_chat_history(agent, "session", max_turns=20)
    runtime = TuiRuntime("session")
    runtime.publish_recovered_history([], display_events=history.display_events, message_cursor=history.message_cursor)
    rows, _, ok = read_background_response_page(store, thread_id)
    seen = set()
    assert ok and _publish_background_notice_row(runtime, rows[0], seen)
    second = _append(store, thread_id)
    assert _consume_background_notices(agent, "session", runtime, [None], seen)
    assert _consume_background_notices(agent, "session", runtime, [None], seen)
    blocks = runtime.store.snapshot().stable_blocks
    assert [block.text for block in blocks] == ["相同正文", "相同正文"]
    assert [block.block_id for block in blocks] == [
        f"history:{thread_id}:{entry.message_id}:assistant" for entry in (first, second)
    ]


def test_failed_publish_does_not_acknowledge_message(conversation):
    store, thread_id = conversation
    _append(store, thread_id)
    rows, _, _ = read_background_response_page(store, thread_id)
    seen = set()

    # LLM: 只模拟界面发布失败，不修改会话或游标；确保下一次可以重试同一 ID。
    # 函数用途: 防止“先确认已读、后发布失败”丢失最终回复。
    def fail(*_args, **_kwargs):
        raise RuntimeError("display unavailable")

    with pytest.raises(RuntimeError, match="display unavailable"):
        _publish_background_notice_row(SimpleNamespace(publish_background_response=fail), rows[0], seen)
    assert seen == set()
    assert _publish_background_notice_row(TuiRuntime("session"), rows[0], seen)


@pytest.mark.parametrize("cursor", [True, -1, 0, 100.0, "100"])
def test_invalid_or_rewinding_remote_cursor_does_not_acknowledge(cursor):
    runtime = TuiRuntime("session")
    runtime.background_message_cursor = 10
    agent = SimpleNamespace(request_background_notices=lambda *_args, **_kwargs: {
        "ok": True, "cursor": cursor, "notices": [], "transcript_events": [], "event_cursor": 0,
    })
    assert _consume_background_notices(agent, "session", runtime, [None], set()) is False
    assert runtime.background_message_cursor == 10
