"""session_search 的会话原文形态：压缩后按消息编号分段读回原话、只在当前会话内检索或按页浏览。

背景（2026-09-26）：用户问“10 段各 1 万 token 的需求压缩后会不会丢”。对照 Hermes（session_search 可搜到本会话已压缩
原文）、OpenClaw（sessions_search + 按消息编号读回）与 Claude Code（摘要末尾给出完整记录路径），我们缺的是“压缩后查回原话”。
本测试锁定：原文只取 canonical 消息文件；长消息按字符游标分段且能拼回原文；会话身份只取宿主可信上下文；
当前会话检索不混入其它会话；翻看窗口每条正文有上限。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.session_history_read import READ_MAX_CHARS, READ_MIN_CHARS
from agent_py_agent.agent.capability.session_search_tool import (
    SessionSearchTool,
    build_session_search_model_spec,
)
from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_metadata
from agent_py_agent.agent.conversation.history_index import index_conversation_message
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.local_storage.search import ThreadSearch, search_records_in_thread
from agent_py_agent.agent.local_storage.store import LocalStore

_LONG = "开头标记-规则一：金额保留两位小数。" + "中段数据行，" * 2400 + "中段唯一标记" + "后段数据行，" * 2400 + "结尾标记"


# 函数用途: 造一个带两个会话的 owner：当前会话含长需求、短回复、过程片段与显示记录，另一会话含同名关键词。
def _owner(tmp_path: Path, *, trusted: bool = True) -> SimpleNamespace:
    store = ConversationStore(tmp_path / "conversation")
    identity = {"canonical_user_id": "owner", "channel": "tui", "channel_user_id": "owner"}
    current = store.threads.get_or_create({**identity, "channel_conversation_id": "conv-current", "now": 1})
    other = store.threads.get_or_create({**identity, "channel_conversation_id": "conv-other", "now": 2})
    assert current.thread_id != other.thread_id
    agent = SimpleNamespace(conversation_store=store, local_store=LocalStore(tmp_path / "local.db"))
    if trusted:
        agent._current_task_attributes = {"conversation_thread_id": current.thread_id}
    rows = [
        store.messages.append({"thread_id": current.thread_id, "role": "user", "content": _LONG}),
        store.messages.append({"thread_id": current.thread_id, "role": "assistant", "content": "收到，规则已记下。",
                               "metadata": {"assistant_part_id": "final"}}),
        store.messages.append({"thread_id": current.thread_id, "role": "assistant", "content": "我先查一下……",
                               "metadata": {"assistant_part_id": "commentary:1"}}),
        store.messages.append({"thread_id": current.thread_id, "role": "user", "content": "第二条要求：日期写成 YYYY-MM-DD。"}),
        store.messages.append({"thread_id": other.thread_id, "role": "user", "content": "别的会话也提到金额保留两位小数。"}),
    ]
    request_id = f"bg-main:{current.thread_id}:one"
    store.messages.append_display_checkpoint(current.thread_id, display_checkpoint_metadata(
        thread_id=current.thread_id, task_id="", request_id=request_id, gateway_request_id="",
        kind="thinking_completed", phase="completed", block_id=f"{request_id}:thinking:1", payload={"text": "公开思考"}))
    for row in rows:
        index_conversation_message(agent, store, row)
    return SimpleNamespace(agent=agent, store=store, current=current, other=other, rows=rows)


# 函数用途: 执行一次 session_search 并解析成功结果。
def _call(agent: object, **params) -> dict:
    result = SessionSearchTool(agent).execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


def test_long_message_is_read_back_exactly_in_bounded_chunks(tmp_path):
    owner = _owner(tmp_path)
    message_id = owner.rows[0].message_id
    chunks, offset = [], 0
    while offset is not None:
        page = _call(owner.agent, message_id=message_id, offset=offset, max_chars=6_000)
        assert page["found"] and page["role"] == "user" and len(page["content"]) <= 6_000
        assert page["total_chars"] == len(_LONG) and page["scope_resolution"]["thread_source"] == "current_runner_context"
        chunks.append(page["content"])
        offset = page["next_offset"]
    # 分段拼回的正文与 canonical 原文逐字一致，最后一段标记读完。
    assert "".join(chunks) == _LONG and page["complete"] is True and len(chunks) == -(-len(_LONG) // 6_000)


def test_read_bounds_offsets_and_projects_assistant_replies(tmp_path):
    owner = _owner(tmp_path)
    tail = _call(owner.agent, message_id=owner.rows[0].message_id, offset=10**9, max_chars=10**9)
    assert tail["content"] == "" and tail["complete"] is True and tail["offset"] == len(_LONG)
    tiny = _call(owner.agent, message_id=owner.rows[0].message_id, max_chars=1)
    assert len(tiny["content"]) == READ_MIN_CHARS
    reply = _call(owner.agent, message_id=owner.rows[1].message_id)
    assert reply["role"] == "assistant" and reply["content"] == "收到，规则已记下。"
    missing = _call(owner.agent, message_id="msg-not-here")
    assert missing["found"] is False and "message_id" in missing["hint"]


def test_thread_identity_comes_from_the_host_and_explicit_threads_stay_in_the_owner_store(tmp_path):
    owner = _owner(tmp_path, trusted=False)
    refused = SessionSearchTool(owner.agent).execute({"message_id": owner.rows[0].message_id})
    assert refused.ok is False and refused.error_code == "TOOL_INVALID_ARGUMENTS"
    # 检索结果给出的其它会话编号可以显式读取，结果如实标明来源不是当前会话。
    other = _call(owner.agent, message_id=owner.rows[4].message_id, thread_id=owner.other.thread_id)
    assert other["found"] and other["scope_resolution"] == {
        "thread_source": "explicit_parameter", "current_thread_id": "", "same_as_current": False}
    # current_thread 只认宿主上下文，没有可信会话时拒绝，不接受模型自报会话。
    browse = SessionSearchTool(owner.agent).execute({"current_thread": True, "thread_id": owner.other.thread_id})
    assert browse.ok is False and browse.error_code == "TOOL_INVALID_ARGUMENTS"


def test_reading_original_text_does_not_need_the_derived_index(tmp_path):
    owner = _owner(tmp_path)
    owner.agent.local_store = None
    page = _call(owner.agent, message_id=owner.rows[3].message_id)
    assert page["content"] == "第二条要求：日期写成 YYYY-MM-DD。"
    listing = _call(owner.agent, current_thread=True)
    assert [item["message_id"] for item in listing["messages"]] == [owner.rows[i].message_id for i in (0, 1, 3)]
    refused = SessionSearchTool(owner.agent).execute({"query": "金额"})
    assert refused.ok is False and refused.error_code == "TOOL_UNAVAILABLE"


def test_current_thread_browse_lists_only_visible_messages_page_by_page(tmp_path):
    owner = _owner(tmp_path)
    store, thread_id = owner.store, owner.current.thread_id
    for index in range(30):
        store.messages.append({"thread_id": thread_id, "role": "user", "content": f"补充要求-{index:02d}"})
    seen, items, cursor, pages = [], {}, None, 0
    while True:
        page = _call(owner.agent, current_thread=True, limit=10, **({"cursor": cursor} if cursor is not None else {}))
        seen = [item["message_id"] for item in page["messages"]] + seen
        items.update({item["message_id"]: item for item in page["messages"]})
        pages += 1
        assert all(len(item["preview"]) <= 160 for item in page["messages"])
        cursor = page["older_cursor"]
        if cursor is None:
            break
    rows, _ = store.messages.recent_report(thread_id, limit=0)
    visible = [row.message_id for row in rows if row.role == "user" or row.metadata.get("assistant_part_id") == "final"]
    # 过程片段、显示记录不出现；翻页覆盖全部可见消息，顺序为时间正序，不重不漏。
    assert seen == visible and pages > 1
    assert items[owner.rows[0].message_id]["chars"] == len(_LONG)
    # 单页条数与其它形态同一上限（20），模型要更多就按 older_cursor 翻页。
    assert len(_call(owner.agent, current_thread=True, limit=50)["messages"]) <= 20
    bad = SessionSearchTool(owner.agent).execute({"current_thread": True, "cursor": "7"})
    assert bad.ok is False and bad.error_code == "TOOL_INVALID_ARGUMENTS"


def test_current_thread_search_never_returns_other_conversations(tmp_path):
    owner = _owner(tmp_path)
    everywhere = _call(owner.agent, query="金额保留两位小数")
    assert {hit["scope"]["thread_id"] for hit in everywhere["results"]} == {owner.current.thread_id, owner.other.thread_id}
    mine = _call(owner.agent, query="金额保留两位小数", current_thread=True)
    assert mine["mode"] == "thread_discover" and mine["results"]
    assert {hit["scope"]["thread_id"] for hit in mine["results"]} == {owner.current.thread_id}
    assert mine["results"][0]["scope"]["message_id"] == owner.rows[0].message_id
    # 命中中段也能找到（全文索引覆盖整条长消息），再用 message_id 读回。
    middle = _call(owner.agent, query="中段唯一标记", current_thread=True)
    assert [hit["scope"]["message_id"] for hit in middle["results"]] == [owner.rows[0].message_id]


def test_thread_filter_applies_before_the_limit_and_in_the_like_fallback(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    for index in range(30):
        store.upsert_record(source_type="conversation_message", source_id=f"other-{index}", title="Conversation user",
                            content=f"验收口径说明 {index}", metadata={"thread_id": "thread-other"})
    for index in range(2):
        store.upsert_record(source_type="conversation_message", source_id=f"mine-{index}", title="Conversation user",
                            content=f"验收口径说明 mine-{index}", metadata={"thread_id": "thread-mine"})
    store.upsert_record(source_type="gateway_request", source_id="req", title="Gateway request", content="验收口径说明 请求",
                        metadata={"conversation_runtime": {"thread_id": "thread-mine"}})
    hits = search_records_in_thread(store, "验收口径说明", ThreadSearch("thread-mine", limit=5))
    assert {hit.source_id for hit in hits} == {"mine-0", "mine-1", "req"}
    only_messages = search_records_in_thread(store, "验收口径说明", ThreadSearch("thread-mine", "conversation_message", 5))
    assert {hit.source_id for hit in only_messages} == {"mine-0", "mine-1"}
    # 两字查询走 LIKE 兜底，会话条件同样生效；会话编号为空不退化成全库检索。
    like_hits = search_records_in_thread(store, "口径", ThreadSearch("thread-mine", limit=5))
    assert {hit.source_id for hit in like_hits} == {"mine-0", "mine-1", "req"}
    assert search_records_in_thread(store, "验收口径说明", ThreadSearch("", limit=5)) == []


def test_scroll_window_bounds_each_record_and_points_to_the_full_text(tmp_path):
    owner = _owner(tmp_path)
    anchor = _call(owner.agent, query="金额保留两位小数", current_thread=True)["results"][0]["around_id"]
    window = _call(owner.agent, around_id=anchor, window=3)
    long_entry = next(item for item in window["messages"] if item["id"] == anchor)
    assert len(long_entry["content"]) == 2_000 and long_entry["content_truncated"] is True
    assert long_entry["chars"] == len(_LONG) and long_entry["scope"]["message_id"] == owner.rows[0].message_id
    assert "message_id" in window["hint"]
    # 显式锚点压过 current_thread：带着 current_thread=true 翻看仍是翻看，而不是浏览最新一页。
    both = _call(owner.agent, around_id=anchor, window=3, current_thread=True)
    assert both["mode"] == "scroll" and both["messages"] == window["messages"]


def test_schema_exposes_the_new_modes_with_shared_bounds():
    schema = build_session_search_model_spec().input_schema["properties"]
    assert schema["message_id"]["type"] == "string" and schema["current_thread"]["type"] == "boolean"
    assert (schema["max_chars"]["minimum"], schema["max_chars"]["maximum"]) == (READ_MIN_CHARS, READ_MAX_CHARS)
    assert schema["cursor"]["type"] == "integer" and schema["offset"]["minimum"] == 0
