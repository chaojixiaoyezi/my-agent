"""前台 Gateway 先保存思考全文再发送预览，后续 sink 不重存裁剪版本。"""

import json
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_transcript import (
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_events
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.foreground_transcript import GatewayForegroundTranscriptSink
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter


# LLM: The fixture connects the actual foreground writer/sink/archive/checkpoint chain in a
# temporary owner store; it never submits a model request or touches user configuration.
# 函数用途: 创建可读真实归档的前台显示夹具，检查 Gateway 之前是否已丢失长思考正文。
def _writer(root):
    store = ConversationStore(root / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner-a", "channel": "chat",
        "channel_conversation_id": "session", "channel_user_id": "owner-a",
    })
    agent = SimpleNamespace(conversation_store=store)
    sink = GatewayForegroundTranscriptSink(agent, thread_id=thread.thread_id, request_id="request-1", request={})
    writer = BufferedChunkStreamWriter(root / "chunks.jsonl", rich_transcript=True, transcript_sink=sink, delivery_channel="chat")
    return agent, store, thread, writer


def test_full_thinking_archives_before_gateway_clip_and_keeps_ref_on_replay(tmp_path):
    agent, store, thread, writer = _writer(tmp_path)
    text = "\n".join(f"第{index}行：这是一段显式公开思考🙂，中间信息必须能完整浏览。" for index in range(2000))
    writer.write_thinking(text, duration_seconds=3)
    chunk = json.loads(writer.chunk_path.read_text().splitlines()[-1])
    assert chunk["kind"] == "assistant_thinking" and len(chunk["text"]) < 12_100
    assert "第1000行" not in chunk["text"]
    reference = chunk["display_archive_ref"]
    all_parts = []
    for page_index in range(reference["page_count"]):
        all_parts.extend(read_display_archive_page(store, reference, page_index)["rows"])
    reconstructed = ["" for _ in range(reference["row_count"])]
    for part in all_parts:
        reconstructed[part["row_index"]] += part["text"]
    assert "\n".join(reconstructed) == text
    assert len(list((store.storage.root / "display_archives").iterdir())) == 1
    live = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    completed = next(event for event in live if event["kind"] == "thinking_completed")
    assert completed["payload"]["display_archive_ref"] == reference
    history = store.messages.page_after_offset_report(thread.thread_id, after=0)[0]
    persisted = next(event for event in display_checkpoint_events(history) if event["kind"] == "thinking_completed")
    assert persisted["payload"]["display_archive_ref"] == reference
    snapshot = writer.prepare_display_history()
    assert next(event for event in snapshot["events"] if event["kind"] == "thinking_completed")["payload"]["display_archive_ref"] == reference


def test_archive_failure_stays_explicit_and_does_not_rearchive_the_preview(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import display_archive

    agent, _store, thread, writer = _writer(tmp_path)
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise OSError("private-server-path")

    monkeypatch.setattr(display_archive, "archive_display_rows", failed)
    writer.write_thinking("公开内容" * 10000)
    assert len(calls) == 1
    chunk = json.loads(writer.chunk_path.read_text().splitlines()[-1])
    assert chunk["history_incomplete"] is True and "display_archive_ref" not in chunk
    assert "private-server-path" not in json.dumps(chunk)
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    completed = next(event for event in events if event["kind"] == "thinking_completed")
    assert completed["payload"]["history_incomplete"] is True
    assert "display_archive_ref" not in completed["payload"]


def test_no_bound_sink_marks_missing_full_text_and_nonrich_stays_private(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / "unbound.jsonl", rich_transcript=True)
    writer.write_thinking("很长" * 20000)
    chunk = json.loads(writer.chunk_path.read_text().splitlines()[-1])
    assert chunk["history_incomplete"] is True and "display_archive_ref" not in chunk
    private = BufferedChunkStreamWriter(tmp_path / "nonrich.jsonl")
    private.write_thinking("不向普通客户端展示" * 2000)
    assert not private.chunk_path.exists()


def test_long_commentary_is_already_complete_at_gateway_tool_boundary(tmp_path):
    agent, _store, thread, writer = _writer(tmp_path)
    text = "\n".join(f"第{index}段：完整模型正文" for index in range(2000))
    writer.write_model(text)
    writer.write_progress({"phase": "started", "tool": "read_file", "round": 1, "call_index": 1}, "")
    events = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()]
    assert next(event for event in events if event["kind"] == "assistant_commentary")["text"] == text
    live = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    assert next(event for event in live if event["kind"] == "assistant_completed")["payload"]["text"] == text
