"""前台真实事件与同会话观察窗口共用 main 活动和上下文，不增发模型请求。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_activity import (
    BackgroundMainActivitySink,
    background_main_activity,
)
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter


def test_failed_background_activity_explains_recovery_and_redacts_credentials():
    from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError

    agent = SimpleNamespace()
    sink = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    sink.fail(RuntimeRecoveryRequiredError("上个工具结果待核对，token=private-value-abcdef"))
    activity = background_main_activity(agent, "thread-a", {"task-a"})
    assert activity["phase"] == "failed"
    assert all(word in activity["activity"] for word in ("RUN_RECOVERY_REQUIRED", "上个工具结果待核对", "/recover"))
    assert "private-value-abcdef" not in activity["activity"]


def test_foreground_writer_replaces_stale_background_activity(tmp_path):
    agent = SimpleNamespace()
    background = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    background.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 8_000})
    background.finish()
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
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


@pytest.mark.parametrize("change", ["attempt", "lease", "thread", "closed", "terminal"])
def test_late_gateway_progress_cannot_overwrite_a_new_attempt_or_terminal(tmp_path, change):
    import json

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_binding import record_gateway_request_progress

    paths = gateway_paths_from_root(tmp_path / "gateway")
    path = paths.processing / "gwreq-original.json"
    request = {"id": "gwreq-original", "status": "processing", "execution_attempt_id": "attempt-1", "lease_epoch": 1,
               "conversation_runtime": {"request_id": "gwreq-original", "thread_id": "thread-a", "task_id": "task-a"}}
    write_json_file_atomic(path, request)
    record_gateway_request_progress(path, request, thread_id="thread-a", phase="tool", status_message="正在使用 read_file", tool_rounds=340)
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["tool_rounds"] == 340 and current["status_phase"] == "tool"
    if change == "attempt":
        current["execution_attempt_id"] = "attempt-2"
    elif change == "lease":
        current["lease_epoch"] = 2
    elif change == "thread":
        current["conversation_runtime"]["thread_id"] = "thread-other"
    elif change == "closed":
        current["turn_phase"] = "closed"
    else:
        paths.terminal.mkdir(parents=True, exist_ok=True)
        write_json_file_atomic(paths.terminal / path.name, {"id": request["id"], "status": "done"})
    write_json_file_atomic(path, current)
    before = path.read_bytes()
    record_gateway_request_progress(path, request, thread_id="thread-a", phase="waiting", status_message="等待后续事件", tool_rounds=341)
    assert path.read_bytes() == before


@pytest.mark.parametrize("phase", ["completed", "superseded", "failed", "context_window_compacted"])
def test_background_compact_terminal_returns_to_working(phase):
    agent = SimpleNamespace()
    sink = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    progress = {"schema": "conversation_compaction_progress.v1", "generation": 1, "phase": "started", "stage": "summarizing"}
    assert sink.write_conversation_compact_progress(progress)
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "compacting"
    if phase == "context_window_compacted":
        assert sink.write_context_compaction({"schema": "model_visible_context_compaction.v1", "generation": 1})
    else:
        stage = {"completed": "completed", "superseded": "candidate_discarded", "failed": "failed"}[phase]
        assert sink.write_conversation_compact_progress({**progress, "phase": phase, "stage": stage})
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "working"
    sink.finish()


def test_gateway_progress_does_not_wait_on_turn_lock_while_writer_is_committing(tmp_path):
    import json
    import threading

    from agent_py_agent.agent.gateway_parts.io import (
        gateway_turn_transition,
        write_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    path = paths.processing / "gwreq-original.json"
    request = {"id": "gwreq-original", "status": "processing", "execution_attempt_id": "attempt-1",
               "conversation_runtime": {"request_id": "gwreq-original", "thread_id": "thread-a", "task_id": "task-a"}}
    write_json_file_atomic(path, request)
    writer = BufferedChunkStreamWriter(paths.processing / "gwreq-original.chunks.jsonl", rich_transcript=True)
    request_execution._configure_gateway_main_activity(
        SimpleNamespace(agent=SimpleNamespace(), on_chunk=writer, request_id=request["id"], request=request, request_path=path),
        SimpleNamespace(thread_id="thread-a", workspace_task=None),
    )
    turn_held, commit_held, completed = threading.Event(), threading.Event(), threading.Event()

    def complete_input():
        with gateway_turn_transition(paths, request["id"]):
            turn_held.set()
            if commit_held.wait(2):
                with writer._commit_lock:
                    completed.set()

    thread = threading.Thread(target=complete_input, daemon=True)
    thread.start()
    try:
        assert turn_held.wait(2)
        progress = {"round": 340, "call_index": 0, "tool": "read_file", "phase": "started"}
        with writer._commit_lock:
            commit_held.set()
            writer.write_progress(progress, "")
        thread.join(2)
        assert completed.is_set() and not thread.is_alive()
        # 锁忙时不缓存为已写入；同阶段下一事件仍会重试，并沿原请求/代次校验保存。
        writer.write_progress(progress, "")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["status_phase"] == "tool" and saved["tool_rounds"] == 340
    finally:
        commit_held.set()
        thread.join(2)
        writer.close()


def test_foreground_projection_follows_exact_promoted_task_and_not_conflicting_binding(tmp_path):
    agent = SimpleNamespace()
    request = {}
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
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
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl")
    context = SimpleNamespace(agent=agent, on_chunk=writer, request_id="gwreq-plain", request={})
    conversation = SimpleNamespace(thread_id="thread-a", workspace_task=None)
    request_execution._configure_gateway_main_activity(context, conversation)
    writer.write_thinking_delta("不得公开的正文")
    writer.write_context_usage({"schema": "model_visible_context_usage.v1", "current_tokens": 88_888})
    assert background_main_activity(agent, "thread-a", set()) == {}


def test_old_foreground_close_cannot_overwrite_new_background_phase(tmp_path):
    agent = SimpleNamespace()
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", rich_transcript=True)
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
    writer = BufferedChunkStreamWriter(path, rich_transcript=True, main_activity_sink=BrokenDisplay())
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


def test_foreground_retry_final_event_shows_registered_status_text() -> None:
    """收口状态行必须引用 request_errors 登记的短句常量，不能各自硬编码。"""
    from agent_py_agent.agent.gateway_parts.main_activity import GatewayMainActivitySink
    from agent_py_agent.agent.gateway_parts.request_errors import (
        PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED_STATUS_TEXT,
    )

    agent = SimpleNamespace()
    sink = GatewayMainActivitySink(agent, thread_id="thread-a", request_id="req-a", request={}, task_id="task-a")
    sink({
        "kind": "runtime_progress",
        "retry": {
            "scope": "model_turn",
            "attempt": 2,
            "total": 2,
            "wait_seconds": 25.0,
            "error_type": "ProviderTimeoutError",
            "final": True,
            "error_code": "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED",
        },
    })

    row = background_main_activity(agent, "thread-a", {"task-a"})
    assert row["phase"] == "retrying"
    assert row["activity"] == PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED_STATUS_TEXT


@pytest.mark.parametrize('level', ['on', 'stream', 'full'])
def test_feishu_progress_keeps_safe_command_summary_through_the_card_projection(tmp_path, level):
    import json

    from agent_py_agent.agent.delivery.execution_view import project_execution_view

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    writer.set_verbose_level(level)
    writer.write_progress({'tool': 'run_command', 'phase': 'started', 'call_id': 'call-demo',
        'input': {'command': 'demo-cli inspect --token=OWNED_SECRET_SENTINEL'}}, '执行命令')
    events = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()]
    steps = project_execution_view(events, request_id='request-demo')['steps']
    assert len(steps) == 1 and 'demo-cli inspect' in steps[0]['label']
    assert 'OWNED_SECRET_SENTINEL' not in steps[0]['label']
    if level != 'full':
        assert all('input' not in event.get('progress', {}) for event in events)


@pytest.mark.parametrize('channel,level', [('feishu', 'off'), ('local', 'stream')])
def test_command_summary_remains_scoped_to_enabled_feishu_progress(tmp_path, channel, level):
    import json

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel=channel)
    writer.set_verbose_level(level)
    writer.write_progress({'tool': 'run_command', 'phase': 'started', 'call_id': 'call-demo',
        'input': {'command': 'demo-cli inspect'}}, '执行命令')
    events = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()]
    assert all('label' not in event.get('progress', {}) for event in events)


def test_interim_commentary_preserves_actual_active_tool_until_its_terminal_event():
    from agent_py_agent.agent.gateway_parts.main_activity import GatewayMainActivitySink

    agent = SimpleNamespace()
    sink = GatewayMainActivitySink(agent, thread_id='thread-wait', request_id='request-wait',
                                   request={}, task_id='task-wait')
    sink({'kind': 'tool_progress', 'progress': {'round': 1, 'call_index': 0,
         'tool': 'run_command', 'phase': 'started'}})
    sink({'kind': 'tool_progress', 'progress': {'round': 1, 'call_index': 1,
         'tool': 'read_file', 'phase': 'started'}})
    sink({'kind': 'tool_progress', 'progress': {'round': 1, 'call_index': 1,
         'tool': 'read_file', 'phase': 'completed'}})
    sink({'kind': 'assistant_commentary', 'text': '命令已经结束的文字不构成终态证明'})
    row = background_main_activity(agent, 'thread-wait', {'task-wait'})
    assert row['phase'] == 'tool' and row['activity'] == '正在使用 run_command'
    sink({'kind': 'tool_progress', 'progress': {'round': 1, 'call_index': 0,
         'tool': 'run_command', 'phase': 'completed'}})
    sink({'kind': 'assistant_commentary', 'text': '现在生成新的回复'})
    assert background_main_activity(agent, 'thread-wait', {'task-wait'})['phase'] == 'responding'
    sink.close()
    sink({'kind': 'assistant_commentary', 'text': '迟到回调'})
    assert background_main_activity(agent, 'thread-wait', {'task-wait'})['phase'] == 'waiting'
