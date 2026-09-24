from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 测试仅创建临时 canonical 会话，直接使用产品追加和分页入口。
# 函数用途: 构造每回合多条中文记录、跨 64K 块和分页边界的真实文件。
def _conversation(tmp_path, turns=7, rows_per_turn=3, long=False):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat",
        "channel_conversation_id": "session", "channel_user_id": "local-agent",
    })
    rows = []
    for turn in range(turns):
        for index in range(rows_per_turn):
            rows.append(store.messages.append({
                "thread_id": thread.thread_id, "role": "user" if index == 0 else "assistant",
                "content": f"第{turn}轮第{index}条中文" + ("中文🙂" * 30000 if long else ""),
                "metadata": {"gateway_request_id": f"request-{turn}", "assistant_part_id": "final"},
            }))
    return store, thread.thread_id, rows


@pytest.mark.parametrize("long", [False, True])
def test_backward_pages_keep_complete_turns_and_exact_utf8_offsets(tmp_path, long):
    store, thread, rows = _conversation(tmp_path, long=long)
    before, found, page_sizes = None, [], []
    while True:
        page = store.messages.history_page_report(thread, before=before, limit=4)
        assert not page.errors
        assert page.after == (before if before is not None else store.storage.message_path(thread).stat().st_size)
        page_sizes.append(len(page.rows))
        found[:0] = [row.message_id for row in page.rows]
        if not page.before:
            break
        assert before is None or page.before < before
        before = page.before
    assert found == [row.message_id for row in rows]
    assert page_sizes == [6, 6, 6, 3]
    assert len(set(found)) == len(found)


def test_partial_tail_and_late_append_do_not_advance_live_cursor(tmp_path):
    store, thread, rows = _conversation(tmp_path, turns=2)
    path = store.storage.message_path(thread)
    complete_end = path.stat().st_size
    late = {**rows[-1].to_dict(), "message_id": "msg-late", "content": "后来的中文🙂"}
    encoded = (json.dumps(late, ensure_ascii=False) + "\n").encode()
    with path.open("ab") as handle:
        handle.write(encoded[:-3])
    page = store.messages.history_page_report(thread, limit=2)
    assert page.after == complete_end and not page.errors
    with path.open("ab") as handle:
        handle.write(encoded[-3:])
    messages, _, errors = store.messages.page_after_offset_report(thread, after=page.after)
    assert not errors and [row.message_id for row in messages] == ["msg-late"]


@pytest.mark.parametrize("invalid", ["middle", "beyond", "negative", "corrupt", "foreign"])
def test_bad_cursor_or_message_is_explicit_failure(tmp_path, invalid):
    store, thread, rows = _conversation(tmp_path)
    path = store.storage.message_path(thread)
    end = path.stat().st_size
    before = {"middle": end - 2, "beyond": end + 1, "negative": -1}.get(invalid)
    if invalid in {"corrupt", "foreign"}:
        raw = {**rows[-1].to_dict(), "thread_id": "foreign-thread"}
        with path.open("ab") as handle:
            handle.write((b"bad" if invalid == "corrupt" else json.dumps(raw).encode()) + b"\n")
    result = store.messages.history_page_report(thread, before=before)
    assert result.errors and not result.rows


def test_latest_page_does_not_decode_unselected_old_prefix(tmp_path):
    store, thread, _ = _conversation(tmp_path, turns=100)
    with store.storage.message_path(thread).open("r+b") as handle:
        handle.write(b"!")
    page = store.messages.history_page_report(thread, limit=4)
    assert not page.errors and len(page.rows) == 6 and page.before > 0


def test_zero_cursor_means_beginning_not_latest(tmp_path):
    store, thread, _ = _conversation(tmp_path)
    page = store.messages.history_page_report(thread, before=0)
    assert not page.rows and not page.errors and page.before == page.after == 0


def test_one_very_long_turn_still_pages_without_losing_rows(tmp_path):
    store, thread, rows = _conversation(tmp_path, turns=1, rows_per_turn=1701)
    before, found, sizes = None, [], []
    while True:
        page = store.messages.history_page_report(thread, before=before, limit=4)
        assert not page.errors
        sizes.append(len(page.rows))
        found[:0] = [row.message_id for row in page.rows]
        if not page.before:
            break
        before = page.before
    assert sizes == [800, 800, 101]
    assert found == [row.message_id for row in rows]
