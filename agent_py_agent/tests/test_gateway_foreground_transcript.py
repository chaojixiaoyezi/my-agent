"""前台真实 writer → 共享会话显示 → canonical final/replay 的定向合同；不代替真 TUI 验收。"""

import copy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_history import background_display_turn_from_row
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.history_page import history_group_identity
from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.request_execution import _configure_gateway_main_activity
from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import (
    _consume_gateway_background_snapshot,
    _publish_background_notice_row,
)


# LLM: 使用真实 writer、显示 mapper 和临时 canonical store，不启用模型或业务工具。
# 函数用途: 建立一片带精确 Gateway 来源的前台公开流，供增量和重放组合验证。
def _foreground(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-a"})
    agent = SimpleNamespace(conversation_store=store)
    request = {"conversation_runtime": {"request_id": "gwreq-live", "thread_id": thread.thread_id, "task_id": "task-a"}}
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
    context = SimpleNamespace(agent=agent, on_chunk=writer, request_id="gwreq-live", request=request)
    _configure_gateway_main_activity(context, SimpleNamespace(thread_id=thread.thread_id, workspace_task=None))
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "帮我检查文件", "metadata": {
        "gateway_request_id": "gwreq-live", "conversation_request_id": "request-a",
    }})
    return agent, writer, store, thread, request


# LLM: 所有事件经真实 Gateway writer 的净化/排序入口；候选最终段保持活动，不能预写结果来掩盖去重问题。
# 函数用途: 发出思考、过程说明、工具结果和一段尚未最终提交的回复。
def _emit_work(writer):
    writer.write_thinking_delta("先检查")
    writer.write_thinking("先检查现有文件", duration_seconds=2)
    writer.write_model("我来读取文件。")
    progress = {"round": 1, "call_index": 0, "tool": "read_file"}
    writer.write_progress({**progress, "phase": "started", "detail": "src/main.py"}, "")
    writer.write_progress({**progress, "phase": "completed", "ok": True, "output": "完整正文"}, "")
    writer.write_model("最终回复的半句")
    writer.flush()


# LLM: final 复用产品元数据包；这是显示持久化夹具，不伪造真实任务执行或验收。
# 函数用途: 保存 user/commentary/final，验证显示分组与模型历史不会被新快照改变。
def _commit(writer, store, thread):
    meta = {"gateway_request_id": "gwreq-live", "conversation_request_id": "request-a"}
    user = store.messages.page_after_offset_report(thread.thread_id, after=0)[0][0]
    commentary = store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "我来读取文件。", "metadata": {**meta, "assistant_part_id": "commentary:1"}})
    snapshot = writer.prepare_display_history()
    final_meta = GatewayAssistantTurn(end_reason="completed", display_snapshot=snapshot).metadata()
    final = store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "这是完整的最终回复。", "metadata": {**meta, **final_meta, "assistant_part_id": "final"}})
    writer.close()
    return user, commentary, final


@pytest.mark.parametrize("live_prefix", ["all", "none", "tail"])
def test_live_and_canonical_final_converge_without_duplicate_or_active_candidate(tmp_path, live_prefix):
    agent, writer, store, thread, _ = _foreground(tmp_path)
    _emit_work(writer)
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    assert events and all(row["gateway_request_id"] == "gwreq-live" for row in events)
    runtime = TuiRuntime("observer")
    if live_prefix != "none":
        notices, cursor, ok = read_background_response_page(store, thread.thread_id, include_foreground=True)
        assert ok
        projection = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
        payload = {"ok": True, "notices": notices, "cursor": cursor, "transcript_events": events if live_prefix == "all" else events[-1:],
                   "event_cursor": projection["cursor"], "event_stream_id": projection["stream_id"]}
        client = SimpleNamespace(request_background_notices=lambda *_args, **_kwargs: payload)
        assert _consume_gateway_background_snapshot(client, "session", runtime, [None], set(), [0])
    if live_prefix == "all":
        before = runtime.store.snapshot()
        assert [block.text for block in before.active_blocks if block.role == "assistant"] == ["最终回复的半句"]
        assert {block.role for block in before.stable_blocks} == {"user", "thinking", "assistant", "tool"}
        assert before.stable_blocks[0].role == "user"
    rows = _commit(writer, store, thread)
    assert len({history_group_identity(row) for row in rows}) == 1
    notices, _, ok = read_background_response_page(store, thread.thread_id, include_foreground=True)
    assert ok and len(notices) == 2
    for notice in notices:
        assert _publish_background_notice_row(runtime, notice, set())
    after = runtime.store.snapshot()
    assert not after.active_blocks
    assert [block.text for block in after.stable_blocks if block.role == "assistant"] == ["我来读取文件。", "这是完整的最终回复。"]
    assert len(after.stable_blocks) == 5
    runtime.publish_background_transcript_events(events)
    for notice in notices:
        _publish_background_notice_row(runtime, notice, set())
    assert runtime.store.snapshot().stable_blocks == after.stable_blocks
    recovered = TuiRuntime("recovered")
    recovered._publish_recovered_display_events(conversation_history_display_events(rows))
    assert [(b.block_id, b.role, b.text, b.detail) for b in recovered.store.snapshot().stable_blocks] == [(b.block_id, b.role, b.text, b.detail) for b in after.stable_blocks]
    plain = copy.deepcopy(rows)
    for row in plain:
        row.metadata.pop("background_display_turn", None)
        row.metadata.pop("background_transcript_request_id", None)
    assert provider_history_messages_from_rows(rows) == provider_history_messages_from_rows(plain)


def test_original_page_skips_both_live_and_committed_foreground(tmp_path):
    agent, writer, store, thread, _ = _foreground(tmp_path)
    _emit_work(writer)
    runtime = TuiRuntime("original")
    runtime.register_gateway_request("gwreq-live")
    page = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
    assert runtime.publish_background_transcript_events(page["events"]) == page["cursor"]
    _commit(writer, store, thread)
    notices, _, _ = read_background_response_page(store, thread.thread_id, include_foreground=True)
    for notice in notices:
        _publish_background_notice_row(runtime, notice, set())
    assert not runtime.store.snapshot().stable_blocks and not runtime.store.snapshot().active_blocks
    background = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-a")
    background.write_thinking("下一片独立后台思考")
    runtime.publish_background_transcript_events(read_background_transcript_events(agent, thread_id=thread.thread_id, after=page["cursor"], stream_id=page["stream_id"])["events"])
    assert runtime.store.snapshot().stable_blocks[-1].text == "下一片独立后台思考"


def test_steering_discards_uncommitted_candidate_and_confirms_exact_input(tmp_path):
    agent, writer, _, thread, _ = _foreground(tmp_path)
    writer.write_model("插话前半句")
    writer.flush()
    writer.begin_active_turn_input(("input-1",))
    writer.complete_active_turn_input(("input-1",), client_messages=(("input-1", "先看第二份"), ("other", "不能出现")))
    writer.write_model("按补充要求继续")
    writer.flush()
    runtime = TuiRuntime("observer")
    runtime.publish_background_transcript_events(read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"])
    snap = runtime.store.snapshot()
    assert [b.text for b in snap.active_blocks if b.role == "assistant"] == ["按补充要求继续"]
    assert [b.text for b in snap.stable_blocks if b.role == "user"] == ["先看第二份"]


def test_submitted_steering_is_replayable_before_consumption(tmp_path):
    """已提交是独立于已确认的持久事实:重连客户端必须立刻看到用户行,同时保留未确认状态。"""
    agent, writer, _, thread, _ = _foreground(tmp_path)
    writer.begin_active_turn_input(("input-1",))
    writer.submit_active_turn_input(
        ("input-1",),
        provider_call_id="call-7",
        client_messages=(("input-1", "先看第二份"), ("other", "不能出现")),
    )
    writer.write_model("正在生成")
    writer.flush()

    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    kinds = [item.get("kind") for item in events]
    assert "active_turn_input_submitted" in kinds
    assert "active_turn_input_consumed" not in kinds  # 未确认前不得出现消费事件

    runtime = TuiRuntime("observer")
    runtime.publish_background_transcript_events(events)
    snap = runtime.store.snapshot()
    assert [b.text for b in snap.stable_blocks if b.role == "user"] == ["先看第二份"]
    assert [item.state for item in snap.pending_steers] == ["submitted"]
    assert all("不能出现" not in b.text for b in snap.stable_blocks)


def test_close_without_final_settles_only_its_display_and_does_not_invent_tool_success(tmp_path):
    agent, writer, _, thread, _ = _foreground(tmp_path)
    writer.write_progress({"round": 1, "call_index": 0, "tool": "read_file", "phase": "started"}, "")
    writer.write_model("未提交")
    writer.flush()
    other = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-a")
    other.write_thinking_delta("另一片继续")
    writer.close()
    first = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
    writer.close()
    assert first == read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
    runtime = TuiRuntime("observer")
    runtime.publish_background_transcript_events(first["events"])
    snap = runtime.store.snapshot()
    assert all(b.block_id.startswith(other.request_id) for b in snap.active_blocks)
    assert len(snap.active_blocks) == 1
    assert [(b.role, b.text) for b in snap.stable_blocks] == [("system", "read_file：本工作片未保存完整结果。")]
    assert writer.prepare_display_history() == {}


@pytest.mark.parametrize("damage", ["gateway", "thread", "replacement", "incomplete", "background", "control"])
def test_invalid_foreground_snapshot_cannot_replace_candidates(tmp_path, damage):
    _, writer, store, thread, _ = _foreground(tmp_path)
    _emit_work(writer)
    final = _commit(writer, store, thread)[-1]
    assert background_display_turn_from_row(final)
    snapshot = final.metadata["background_display_turn"]
    if damage in {"gateway", "thread"}:
        snapshot[f"{damage}_id" if damage == "thread" else "gateway_request_id"] = "other"
    elif damage == "replacement":
        snapshot["live_final_block_id"] = "bg-main:other:assistant:1"
    elif damage == "incomplete":
        snapshot["complete"] = False
    elif damage == "background":
        final.metadata["background_delivery_reason"] = "wake"
    else:
        snapshot["events"][0]["kind"] = "permission_requested"
    assert background_display_turn_from_row(final) is None


def test_task_promotion_compact_repeated_tools_and_typed_progress(tmp_path):
    agent, writer, _, thread, request = _foreground(tmp_path)
    request["conversation_runtime"]["task_id"] = "task-promoted"
    tool = {"round": 1, "call_index": 0, "tool": "read_file", "phase": "completed", "ok": True}
    writer.write_progress({**tool, "output": "压缩前"}, "")
    writer.write_compact_boundary(1)
    writer.write_progress({**tool, "output": "压缩后"}, "")
    writer.write_tool_input_progress({"schema": "provider_tool_input_progress.v1", "phase": "streaming", "stream_index": 0, "tool": "read_file", "received_chars": 200, "arguments": "private"})
    writer.write_provider_retry(scope="transport", attempt=1, total=3, delay_seconds=2, error_type="private")
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    assert all(event["task_id"] == "task-promoted" for event in events)
    assert "private" not in str(events)
    assert len({event["block_id"] for event in events if event["kind"] == "tool_completed"}) == 2
    assert {"tool_input_started", "tool_input_completed", "compact_boundary", "system_message"} <= {event["kind"] for event in events}
    request["conversation_runtime"]["thread_id"] = "other"
    writer.write_thinking("不能投到另一会话")
    assert events == read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]


def test_original_nonrich_chunk_contract_and_display_failures_do_not_break_execution(tmp_path):
    class Broken:
        def begin_active_turn_input(self, _ids):
            raise OSError("projection")

        def prepare_final(self):
            raise OSError("projection")

        def close(self):
            raise OSError("projection")

    writer = BufferedChunkStreamWriter(tmp_path / "plain.jsonl", transcript_sink=Broken())
    writer.begin_active_turn_input(("input",))
    assert writer.prepare_display_history() == {}
    writer.close()


def test_notices_negotiate_live_stream_separately_from_committed_messages(tmp_path):
    from agent_py_agent.agent.conversation.message_stream import NoticeDisplayCapabilities
    from agent_py_agent.agent.gateway_parts.foreground_transcript import (
        GatewayForegroundTranscriptSink,
    )
    from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree

    agent, scope, _ = _bound_agent_tree(tmp_path)
    thread, _ = agent.conversation_store.threads.resolve_report(channel="chat", channel_conversation_id="session-a", channel_user_id="local-agent")
    foreground = GatewayForegroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-root", request_id="gwreq-live", request={})
    foreground({"kind": "thinking_delta", "text": "前台显式思考"})
    background = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-root")
    background.write_thinking("独立后台过程")
    old = read_gateway_client_notices(agent, scope=scope, after=0, display=NoticeDisplayCapabilities(foreground_messages=True))
    new = read_gateway_client_notices(agent, scope=scope, after=0, display=NoticeDisplayCapabilities(foreground_messages=True, foreground_transcript=True))
    assert all(not row.get("gateway_request_id") for row in old["transcript_events"])
    assert any(row.get("gateway_request_id") == "gwreq-live" for row in new["transcript_events"])
    assert old["event_cursor"] == new["event_cursor"] > 0
    assert old["event_stream_id"] == new["event_stream_id"]
    assert any(row["request_id"] == background.request_id for row in old["transcript_events"])
