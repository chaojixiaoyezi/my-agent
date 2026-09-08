"""前台真实事件与同会话观察窗口共用 main 活动和上下文，不增发模型请求。"""

from types import SimpleNamespace

from agent_py_agent.agent.conversation.agent_activity import (
    BackgroundMainActivitySink,
    background_main_activity,
)
from agent_py_agent.agent.gateway_parts import request_execution


def test_foreground_writer_replaces_stale_background_activity(tmp_path):
    agent = SimpleNamespace()
    background = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    background.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 8_000})
    background.finish()
    writer = request_execution.BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
    context = SimpleNamespace(
        agent=agent, on_chunk=writer, request_id="gwreq-live",
        request={"conversation_runtime": {"request_id": "gwreq-live", "thread_id": "thread-a", "task_id": "task-a"}},
    )
    conversation = SimpleNamespace(thread_id="thread-a", workspace_task=None)
    request_execution._configure_gateway_main_activity(context, conversation)
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 62_300, "prompt": "private"})
    writer.write_progress({"tool": "search_text", "phase": "started", "round": 1, "call_index": 0}, "")
    activity = background_main_activity(agent, "thread-a", {"task-a"})
    assert activity["phase"] == "tool"
    assert activity["activity"] == "正在使用 search_text"
    assert activity["context_usage"]["current_tokens"] == 62_300
    assert "prompt" not in activity["context_usage"]
    assert background_main_activity(agent, "thread-b", {"task-a"}) == {}
    writer.close()
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "waiting"


def test_foreground_projection_follows_exact_promoted_task_and_not_conflicting_binding(tmp_path):
    agent = SimpleNamespace()
    request = {}
    writer = request_execution.BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
    context = SimpleNamespace(agent=agent, on_chunk=writer, request_id="gwreq-live", request=request)
    conversation = SimpleNamespace(thread_id="thread-a", workspace_task=SimpleNamespace(task_id="task-old"))
    request_execution._configure_gateway_main_activity(context, conversation)
    request["conversation_runtime"] = {"request_id": "gwreq-live", "thread_id": "thread-a", "task_id": "task-new"}
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 24_000})
    assert background_main_activity(agent, "thread-a", {"task-new"})["context_usage"]["current_tokens"] == 24_000
    request["conversation_runtime"] = {"request_id": "gwreq-other", "thread_id": "thread-b", "task_id": "task-other"}
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 99_000})
    assert background_main_activity(agent, "thread-a", {"task-new"})["context_usage"]["current_tokens"] == 24_000
    assert background_main_activity(agent, "thread-b", {"task-other"}) == {}


def test_plain_writer_does_not_publish_private_model_data(tmp_path):
    agent = SimpleNamespace()
    writer = request_execution.BufferedChunkStreamWriter(tmp_path / "chunks.jsonl")
    context = SimpleNamespace(agent=agent, on_chunk=writer, request_id="gwreq-plain", request={})
    conversation = SimpleNamespace(thread_id="thread-a", workspace_task=None)
    request_execution._configure_gateway_main_activity(context, conversation)
    writer.write_thinking_delta("不得公开的正文")
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 88_888})
    assert background_main_activity(agent, "thread-a", set()) == {}


def test_old_foreground_close_cannot_overwrite_new_background_phase(tmp_path):
    agent = SimpleNamespace()
    writer = request_execution.BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
    context = SimpleNamespace(agent=agent, on_chunk=writer, request_id="gwreq-old", request={})
    conversation = SimpleNamespace(thread_id="thread-a", workspace_task=SimpleNamespace(task_id="task-a"))
    request_execution._configure_gateway_main_activity(context, conversation)
    current = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    current.write_progress({"tool": "read_file", "phase": "started"})
    current.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 41_000})
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 99_000})
    writer.close()
    activity = background_main_activity(agent, "thread-a", {"task-a"})
    assert activity["phase"] == "tool"
    assert activity["context_usage"]["current_tokens"] == 41_000


def test_projection_error_preserves_original_chunk_and_close(tmp_path):
    class BrokenDisplay:
        def __call__(self, event):
            raise OSError("display unavailable")

        def close(self):
            raise OSError("display unavailable")

    path = tmp_path / "chunks.jsonl"
    writer = request_execution.BufferedChunkStreamWriter(path, rich_transcript=True, main_activity_sink=BrokenDisplay())
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 62_300})
    writer.close()
    assert "62300" in path.read_text()


def test_foreground_approval_wait_tracks_exact_pending_ids_and_keeps_context():
    from agent_py_agent.agent.gateway_parts.main_activity import GatewayMainActivitySink

    agent = SimpleNamespace()
    sink = GatewayMainActivitySink(agent, thread_id="thread-a", request_id="req-a", request={}, task_id="task-a")
    sink({"kind": "permission_requested", "permission": {"permission_id": "approval-a", "arguments": "private"}})
    sink({"kind": "permission_requested", "permission": {"permission_id": "approval-b"}})
    sink({"kind": "tool_progress", "progress": {"tool": "read_file", "phase": "finished"}})
    sink({"kind": "context_usage_updated", "context_usage": {"schema": "model_visible_context_usage.v1", "current_tokens": 62300}})
    sink({"kind": "permission_resolved", "permission_id": "other", "decision": "approved", "session_cached": True})
    sink({"kind": "permission_resolved", "permission_id": "approval-a", "decision": "denied"})
    row = background_main_activity(agent, "thread-a", {"task-a"})
    assert row["phase"] == "waiting_permission"
    assert row["activity"] == "等待工具审批"
    assert row["context_usage"]["current_tokens"] == 62300
    assert "private" not in str(row) and "approval-a" not in str(row)
    assert background_main_activity(agent, "thread-b", {"task-a"}) == {}
    sink({"kind": "permission_resolved", "permission_id": "approval-b", "decision": "cancelled"})
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "running"
    sink.close()
    sink({"kind": "permission_requested", "permission": {"permission_id": "late"}})
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "waiting"


def test_malformed_approval_does_not_create_a_wait():
    from agent_py_agent.agent.gateway_parts.main_activity import GatewayMainActivitySink

    agent = SimpleNamespace()
    sink = GatewayMainActivitySink(agent, thread_id="thread-a", request_id="req-a", request={}, task_id="task-a")
    for permission in ({}, {"permission_id": []}, {"permission_id": ""}, None):
        sink({"kind": "permission_requested", "permission": permission})
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "running"
