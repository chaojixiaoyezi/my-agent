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


def test_background_length_notice_survives_live_and_history_handoff(conversation):
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )

    store, thread_id = conversation
    sink = BackgroundTranscriptSink(SimpleNamespace(), thread_id=thread_id, task_id="task-1")
    sink.write_thinking("继续核对当前任务")
    sink.finish()
    row = _append(store, thread_id, "接下来要修改", metadata={
        "task_id": "task-1",
        "turn_end_reason": "max-tokens",
        "background_transcript_request_id": sink.request_id,
        "background_display_turn": sink.display_history_snapshot(),
    })
    before = store._message_path(thread_id).read_bytes()
    notices, cursor, ok = read_background_response_page(store, thread_id)
    assert ok and cursor > 0
    events = notices[0]["display_events"]
    assert events[-1]["kind"] == "system_message"
    assert "长度限制" in events[-1]["payload"]["text"]
    assert any(event.get("covered_background_request_id") == sink.request_id for event in events)
    runtime = TuiRuntime("truncated-background")
    runtime.publish_recovered_history([], display_events=events)
    runtime.publish_recovered_history([], display_events=conversation_history_display_events([row]))
    snapshot = runtime.store.snapshot()
    assert sum(block.text == "接下来要修改" for block in snapshot.stable_blocks) == 1
    assert sum("长度限制" in block.text for block in snapshot.stable_blocks) == 1
    assert not snapshot.has_active_work
    assert store._message_path(thread_id).read_bytes() == before


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
    exact_page = store.history_page_report
    late = []

    # LLM: 模拟快照读取和取偏移之间到达新消息；只在临时 canonical 文件追加一次。
    # 函数用途: 构造不能使用稍后文件总长度跳过的真实读写竞态。
    def append_between_read_and_cursor(tid, **kwargs):
        page = exact_page(tid, **kwargs)
        if not late:
            late.append(_append(store, thread_id))
        return page

    monkeypatch.setattr(store, "history_page_report", append_between_read_and_cursor)
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


def test_foreground_messages_reach_observer_without_repeating_origin(conversation):
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary

    store, thread_id = conversation
    origin, observer = TuiRuntime("origin"), TuiRuntime("observer")
    origin.register_gateway_request("gwreq-live")
    origin.enqueue_prompt("chat-local", "检查后汇报", queued=False)
    origin.begin_turn("chat-local")
    metadata = {"gateway_request_id": "gwreq-live"}
    user = store.append_message({
        "thread_id": thread_id, "role": "user", "content": "检查后汇报", "metadata": metadata,
    })
    final = store.append_message({
        "thread_id": thread_id, "role": "assistant", "content": "检查完成",
        "metadata": {**metadata, "assistant_part_id": "final"},
    })
    before = store._message_path(thread_id).read_bytes()
    rows, cursor, ok = read_background_response_page(store, thread_id, include_foreground=True)
    assert ok and [row["message_id"] for row in rows] == [user.message_id, final.message_id]
    assert read_background_response_page(store, thread_id)[0] == []
    seen_origin, seen_observer = set(), set()
    for row in rows:
        assert _publish_background_notice_row(origin, row, seen_origin)
        assert _publish_background_notice_row(observer, row, seen_observer)
    origin.complete_turn("chat-local", TuiTurnSummary(response_text="检查完成", ok=True))
    history = conversation_history_display_events([user, final])
    for runtime in (origin, observer):
        runtime.publish_recovered_history([], display_events=history)
        texts = [block.text for block in runtime.store.snapshot().stable_blocks]
        assert texts.count("检查后汇报") == 1
        assert texts.count("检查完成") == 1
        assert not runtime.store.snapshot().has_active_work
    assert cursor == len(before) and store._message_path(thread_id).read_bytes() == before


def test_owned_foreground_does_not_hide_background_continuation(conversation):
    store, thread_id = conversation
    runtime = TuiRuntime("origin")
    runtime.register_gateway_request("gwreq-live")
    final = _append(store, thread_id, "子代理返回后的汇报", metadata={"gateway_request_id": "gwreq-live"})
    rows, _, ok = read_background_response_page(store, thread_id, include_foreground=True)
    assert ok and _publish_background_notice_row(runtime, rows[0], set())
    blocks = runtime.store.snapshot().stable_blocks
    assert any(block.text == final.content for block in blocks)


def test_foreground_user_ids_stay_stable_across_partial_pages(conversation):
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )

    store, thread_id = conversation
    entries = [store.append_message({
        "thread_id": thread_id, "role": "user", "content": "继续检查",
        "metadata": {"gateway_request_id": "gwreq-live"},
    }) for _ in range(2)]
    runtime = TuiRuntime("observer")
    rows, _, ok = read_background_response_page(store, thread_id, include_foreground=True)
    assert ok
    for row in rows:
        assert _publish_background_notice_row(runtime, row, set())
    runtime.publish_recovered_history([], display_events=conversation_history_display_events(entries))
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == ["继续检查", "继续检查"]


def test_foreground_page_excludes_other_thread_and_unknown_message_roles(conversation):
    store, thread_id = conversation
    other = store.get_or_create_thread({
        "canonical_user_id": "another-user", "channel": "chat", "channel_conversation_id": "other",
        "channel_user_id": "another-user",
    })
    store.append_message({
        "thread_id": other.thread_id, "role": "user", "content": "另一个用户",
        "metadata": {"gateway_request_id": "gwreq-other"},
    })
    store.append_message({
        "thread_id": thread_id, "role": "assistant", "content": "还没确认的过程",
        "metadata": {"gateway_request_id": "gwreq-live", "assistant_part_id": "commentary:1"},
    })
    assert read_background_response_page(store, thread_id, include_foreground=True)[0] == []


@pytest.mark.parametrize("cursor", [True, -1, 0, 100.0, "100"])
def test_invalid_or_rewinding_remote_cursor_does_not_acknowledge(cursor):
    runtime = TuiRuntime("session")
    runtime.background_message_cursor = 10
    agent = SimpleNamespace(request_background_notices=lambda *_args, **_kwargs: {
        "ok": True, "cursor": cursor, "notices": [], "transcript_events": [], "event_cursor": 0, "event_stream_id": "",
    })
    assert _consume_background_notices(agent, "session", runtime, [None], set()) is False
    assert runtime.background_message_cursor == 10
