"""工具预览只整理公开展示，不改调用引用、原文或生命周期。"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    fragments_text,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def _stream(tool, detail=""):
    store = TuiStateStore()
    seq = TuiEventSequencer("preview", session_id="preview", request_id="request")
    store.publish(seq.emit("tool_started", "started", "tool:request:1:0", {"tool": tool, "detail": detail}))
    return store, seq


def _render(snapshot):
    frame = render_tui_snapshot(snapshot, TuiRenderContext(width=180))
    return "\n".join(fragments_text(line) for line in frame.transcript_lines)


@pytest.mark.parametrize("reference", [
    "gwreq-msg-26445af4d53b4c5f01213c8df7c367b6:call_00_MZxE5r8NAXvPKaF39oh80090",
    "request-other:call_short",
])
def test_read_artifact_logical_reference_is_a_short_label_in_every_phase(reference):
    store, seq = _stream("read_artifact", reference)
    started = store.snapshot()
    assert "已外置工具输出" in started.active_blocks[0].detail
    assert reference.split(":")[0] not in _render(started)
    for kind, phase in (("tool_progress", "updated"), ("tool_completed", "completed")):
        event = seq.emit(kind, phase, "tool:request:1:0", {"detail": reference, "ok": True})
        store.publish(event)
        snapshot = store.snapshot()
        block = (snapshot.stable_blocks or snapshot.active_blocks)[0]
        assert "已外置工具输出" in block.detail and "call_" in block.detail
        assert len(block.detail) <= 60
        assert reference.split(":")[0] not in _render(snapshot)
        assert event.payload["detail"] == reference


def test_read_artifact_page_preview_shows_mode_and_offset_without_internal_reference():
    reference = "request-page:call_read"
    raw = json.dumps({"artifact_ref": reference, "read_mode": "slice", "content_offset": 0,
        "content_chars": 6, "content": "正文内容示例"}, ensure_ascii=False)
    store, seq = _stream("read_artifact", reference)
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": raw, "ok": True}))
    block = store.snapshot().stable_blocks[0]
    assert "call_read" in block.detail and "slice" in block.detail and "0" in block.detail
    assert reference not in _render(store.snapshot())
    assert "artifact_ref" not in _render(store.snapshot())


def test_read_artifact_reference_field_and_large_offsets_stay_bounded():
    store, seq = _stream("read_artifact")
    store.publish(seq.emit("tool_progress", "updated", "tool:request:1:0", {"artifact_ref": "request:call_field"}))
    assert "call_field" in store.snapshot().active_blocks[0].detail
    raw = json.dumps({"artifact_ref": "request:call_field", "content_offset": 10 ** 1000, "content_chars": 10 ** 1000})
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": raw}))
    assert len(store.snapshot().stable_blocks[0].detail) < 150


@pytest.mark.parametrize("tool,key,label", [
    ("workflows", "workflows", "工作流"),
    ("scripts", "scripts", "脚本"),
    ("schedule", "jobs", "计划任务"),
    ("spaces", "items", "空间"),
    ("business_spaces", "spaces", "空间"),
    ("background_agents", "items", "后台代理"),
])
@pytest.mark.parametrize("encoded_twice", [False, True])
def test_management_tool_json_becomes_named_collection_preview(tool, key, label, encoded_twice):
    names = ["模型 API 交付检查", "模型部署开机检查"]
    raw = json.dumps({"action": "list", key: [
        {"descriptor": {"schema_version": 1, "name": name}, "definition": {"internal": "raw-data"}}
        for name in names
    ]}, ensure_ascii=False)
    if encoded_twice:
        raw = json.dumps(raw, ensure_ascii=False)
    store, seq = _stream(tool, "action=list")
    for kind, phase in (("tool_progress", "updated"), ("tool_completed", "completed")):
        event = seq.emit(kind, phase, "tool:request:1:0", {"output": raw, "ok": True})
        store.publish(event)
        block = (store.snapshot().stable_blocks or store.snapshot().active_blocks)[0]
        assert label in block.detail and "2" in block.detail
        assert all(name in block.detail for name in names)
        assert "action=list" in block.detail
        assert "schema_version" not in _render(store.snapshot())
        assert "raw-data" not in _render(store.snapshot())
        assert event.payload["output"] == raw
    assert store.snapshot().stable_blocks[0].phase == "completed"


@pytest.mark.parametrize("tool,label", [("workflows", "工作流"), ("read_artifact", "工具输出")])
@pytest.mark.parametrize("raw", [
    '{"workflows": [{"descriptor": {"schema_version": 1, "name": "未完整返回',
    '{"workflows": ' + ' ' * 20_000,
    '[' * 1100,
], ids=["truncated", "oversized", "deep"])
def test_incomplete_or_oversized_json_has_bounded_neutral_preview(raw, tool, label):
    store, seq = _stream(tool)
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": raw, "ok": True}))
    block = store.snapshot().stable_blocks[0]
    assert label in block.detail and "预览" in block.detail
    assert len(block.detail) < 100 and "{" not in block.detail
    assert "共" not in block.detail  # 不完整结果不能编造总数或成功状态。


def test_empty_and_many_items_have_bounded_safe_labels():
    store, seq = _stream("workflows")
    raw = json.dumps({"workflows": [
        {"descriptor": {"name": "\u001b[31m标题\n" + "长" * 1000}} for _ in range(5)
    ]}, ensure_ascii=False)
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": raw}))
    block = store.snapshot().stable_blocks[0]
    assert "5" in block.detail and len(block.detail) < 300
    assert "\u001b" not in block.detail and "\n" not in block.detail
    empty, seq = _stream("workflows")
    empty.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": '{"workflows": []}'}))
    assert "0" in empty.snapshot().stable_blocks[0].detail


@pytest.mark.parametrize("tool,data,expected", [
    ("scripts", {"scripts": [{"name": "脚本甲"}, {"title": "脚本乙"}]}, "脚本乙"),
    ("spaces", {"items": [{"display_name": "空间甲"}]}, "空间甲"),
    ("workflows", {"workflow": {"descriptor": {"name": "单个工作流"}}}, "单个工作流"),
    ("background_agents", [{"name": "代理甲"}], "代理甲"),
    ("workflows", {"entries": [{"name": "通用集合"}]}, "通用集合"),
])
def test_preview_supports_names_single_entities_and_open_collection_keys(tool, data, expected):
    store, seq = _stream(tool)
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": json.dumps(data)}))
    assert expected in store.snapshot().stable_blocks[0].detail


@pytest.mark.parametrize("tool,output", [
    ("workflows", "读取失败，请稍后重试"),
    ("read_file", '{"workflows": [{"descriptor": {"name": "文件原文"}}]}'),
    ("run_command", "request:call_this_is_command_output"),
    ("read_artifact", "artifacts/example.txt"),
])
def test_other_tools_and_plain_text_keep_original_output(tool, output):
    store, seq = _stream(tool)
    store.publish(seq.emit("tool_completed", "completed", "tool:request:1:0", {"output": output}))
    assert store.snapshot().stable_blocks[0].detail == output


@pytest.mark.parametrize("error_code,expected", [
    ("APPROVAL_REJECTED", "用户拒绝了工具调用"), ("CANCELLED", "已中断"), ("TOOL_ERROR", "权限不足"),
])
def test_structured_preview_preserves_rejection_cancellation_and_errors(error_code, expected):
    store, seq = _stream("workflows")
    store.publish(seq.emit("tool_failed", "failed", "tool:request:1:0",
        {"output": '{"error": "权限不足"}', "ok": False, "error_code": error_code}))
    block = store.snapshot().stable_blocks[0]
    assert block.phase == "failed" and expected in block.detail


def test_history_restore_uses_same_preview_without_changing_native_messages():
    raw = json.dumps({"workflows": [{"descriptor": {"name": "工作流甲"}}, {"descriptor": {"name": "工作流乙"}}]},
                     ensure_ascii=False)
    native = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_list", "name": "workflows", "input": {"action": "list"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_list", "is_error": False, "content": raw}]},
    ]
    rows = [SimpleNamespace(role="assistant", content="列表已读取", thread_id="thread", message_id="message",
        metadata={"gateway_request_id": "request", "canonical_native_messages": canonical_native_messages_envelope(native)})]
    before = copy.deepcopy(rows)
    events = conversation_history_display_events(rows)
    runtime = TuiRuntime("preview-history")
    runtime.publish_recovered_history([], display_events=events)
    block = next(block for block in runtime.store.snapshot().stable_blocks if block.role == "tool")
    assert "工作流甲" in block.detail and "工作流乙" in block.detail and "2" in block.detail
    assert "descriptor" not in _render(runtime.store.snapshot())
    assert rows == before
    assert next(event for event in events if event["kind"] == "tool_completed")["payload"]["output"] == raw


def test_real_background_transcript_uses_same_preview_and_preserves_original_output(tmp_path):
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore

    conversation = ConversationStore(tmp_path / "conversations")
    thread = conversation.threads.get_or_create({"canonical_user_id": "preview-owner"})
    agent = SimpleNamespace(conversation_store=conversation)
    sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="preview-task")
    raw = json.dumps({"workflows": [{"descriptor": {"name": "后台工作流"}}]}, ensure_ascii=False)
    progress = {"tool": "workflows", "round": 1, "call_index": 0}
    sink.write_progress({**progress, "phase": "started"})
    sink.write_progress({**progress, "phase": "completed", "ok": True, "output": raw})
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    before = copy.deepcopy(events)
    runtime = TuiRuntime("background-preview")
    runtime.publish_conversation_reset(thread.thread_id)
    assert runtime.publish_background_transcript_events(events) > 0
    block = next(block for block in runtime.store.snapshot().stable_blocks if block.role == "tool")
    assert "后台工作流" in block.detail and "descriptor" not in _render(runtime.store.snapshot())
    assert next(event for event in events if event["kind"] == "tool_completed")["payload"]["output"] == raw
    assert events == before
