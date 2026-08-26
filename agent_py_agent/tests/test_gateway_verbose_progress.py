from __future__ import annotations

import json
import time
from types import SimpleNamespace

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.manager import _render_gateway_progress
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolProgressEvent,
    _public_progress_text,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _model_chunk_callback,
    _publish_provider_thinking,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import (
    BufferedChunkStreamWriter,
    _gateway_conversation_context,
    _GatewayConversationLoadRequest,
    _handle_gateway_request,
)
from agent_py_agent.agent.settings import AgentConfig


def _conversation(user: str = "ou_alice", chat: str = "oc_one") -> dict[str, str]:
    return {
        "channel": "feishu",
        "channel_conversation_id": chat,
        "channel_user_id": user,
        "canonical_user_id": user,
    }


def _run_command(agent: SimpleAgent, prompt: str, conversation: dict):
    command = parse_conversation_control(prompt)
    assert command is not None
    return execute_gateway_conversation_control(
        agent,
        gateway_paths(agent),
        command,
        GatewayControlScope(
            conversation["channel_user_id"],
            conversation["channel"],
            conversation["channel_conversation_id"],
        ),
    )


def test_verbose_command_is_persisted_per_conversation_without_calling_model(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    agent.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("system command must not call model")
    )
    first = _run_command(agent, "/verbose on", _conversation())
    current = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {"conversation": _conversation()},
            "gw-v2",
            "继续",
        )
    )
    other = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {"conversation": _conversation(chat="oc_other")},
            "gw-v3",
            "另一个会话",
        )
    )

    assert first.ok is True
    assert "工具步骤摘要" in first.message
    assert current.verbose_level == "on"
    assert current.history == ()
    assert other.verbose_level == "off"
    status = _run_command(agent, "/v", _conversation())
    assert status.message == "当前详细过程模式：开启。"


def test_verbose_invalid_level_does_not_change_thread_state(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    result = _run_command(agent, "/verbose everything", _conversation())
    current = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {"conversation": _conversation()},
            "gw-v2",
            "继续",
        )
    )
    assert result.message.startswith("用法：")
    assert current.verbose_level == "off"


def test_progress_chunk_respects_on_and_full_levels(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    event = {"tool": "read_file", "status": "完成", "output": "secret result"}
    writer.set_verbose_level("on")
    writer.write_progress(event, "legacy")
    writer.set_verbose_level("full")
    writer.write_progress(event, "legacy")
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert "output" not in rows[0]["progress"]
    assert rows[1]["progress"]["output"] == "secret result"
    events, cursor = _read_public_progress_events(path, 0)
    assert cursor == 2
    assert [item["level"] for item in events] == ["on", "full"]
    assert "结果" not in _render_gateway_progress(events[0])
    assert "secret result" in _render_gateway_progress(events[1])


def test_rich_transcript_keeps_each_commentary_tool_display_and_thinking(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    writer.write_model("先看文件。")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "开始"},
        "legacy-1",
    )
    writer.write_model("再运行检查。")
    writer.write_progress(
        {
            "tool": "run_command",
            "phase": "started",
            "status": "开始",
            "output": "kept output",
            "display": {"kind": "command", "stdout": "ok"},
        },
        "legacy-2",
    )
    writer.write_thinking("provider thought", duration_seconds=2.4)
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["text"] for row in rows if row["kind"] == "assistant_commentary"] == [
        "先看文件。",
        "再运行检查。",
    ]
    progress = [row["progress"] for row in rows if row["kind"] == "tool_progress"][-1]
    assert progress["output"] == "kept output"
    assert progress["display"] == {"kind": "command", "stdout": "ok"}
    thinking = next(row for row in rows if row["kind"] == "assistant_thinking")
    assert thinking["text"] == "provider thought"
    assert thinking["duration_seconds"] == 2.4


def test_non_rich_transcript_drops_display_and_provider_thinking(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_progress(
        {
            "tool": "run_command",
            "phase": "finished",
            "output": "hidden output",
            "display": {"kind": "command", "stdout": "hidden"},
        },
        "legacy",
    )
    writer.write_thinking("hidden thought", duration_seconds=1.0)
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert "output" not in rows[0]["progress"]
    assert "display" not in rows[0]["progress"]
    assert not any(row["kind"] == "assistant_thinking" for row in rows)


def test_model_finish_projects_only_explicit_thinking_blocks() -> None:
    captured: list[tuple[str, float]] = []

    class Sink:
        def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
            captured.append((text, duration_seconds))

    request = SimpleNamespace(
        params=SimpleNamespace(effective_on_chunk=Sink()),
    )
    response = SimpleNamespace(
        assistant_content_blocks=[
            {"type": "thinking", "thinking": "visible", "signature": "must-not-leak"},
            {"type": "redacted_thinking", "data": "must-not-leak"},
            {"type": "text", "text": "ordinary answer"},
        ]
    )

    _publish_provider_thinking(
        request,
        SimpleNamespace(started_at=time.monotonic() - 2.0),
        response,
    )

    assert captured[0][0] == "visible"
    assert captured[0][1] >= 2.0


def test_presentation_only_model_finish_never_projects_provider_thinking() -> None:
    captured: list[str] = []

    class Sink:
        def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
            del duration_seconds
            captured.append(text)

    request = SimpleNamespace(
        params=SimpleNamespace(
            context_scope="isolated",
            effective_on_chunk=Sink(),
        ),
    )
    response = SimpleNamespace(
        assistant_content_blocks=[{"type": "thinking", "thinking": "private draft"}]
    )

    _publish_provider_thinking(
        request,
        SimpleNamespace(started_at=time.monotonic()),
        response,
    )

    assert captured == []


def test_first_real_model_segment_becomes_sanitized_commentary_at_tool_boundary(
    tmp_path,
) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_model(
        "我先检查 /root/private/project/main.py。\n"
        '[TOOL_CALL]\n{"name":"read_file"}\n[/TOOL_CALL]\n'
    )
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy-1",
    )
    writer.write_model("接下来再检查测试。\n")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy-2",
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    commentary = [row for row in rows if row.get("kind") == "assistant_commentary"]
    events, cursor = _read_public_progress_events(path, 0)

    assert commentary == [
        {"t": commentary[0]["t"], "kind": "assistant_commentary", "text": "我先检查 main.py。"}
    ]
    assert events == [{"kind": "assistant_commentary", "text": "我先检查 main.py。"}]
    assert _render_gateway_progress(events[0]) == "我先检查 main.py。"
    assert cursor == len(rows)
    assert "接下来" not in json.dumps(commentary, ensure_ascii=False)
    assert writer._model_segment == []


def test_live_user_input_opens_one_new_model_commentary_segment(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_model("我先检查项目。\n")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy-1",
    )
    writer.write_model("这段旧工具轮文字不能发出。\n")
    writer.begin_active_turn_input(("steer-client-1",))
    writer.write_model("记得，原任务要输出 JSON 和 Markdown。\n")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy-2",
    )
    writer.write_model("同一条用户消息不能反复打开回复。\n")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy-3",
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    commentary = [row["text"] for row in rows if row.get("kind") == "assistant_commentary"]

    assert commentary == [
        "我先检查项目。",
        "记得，原任务要输出 JSON 和 Markdown。",
    ]
    assert "旧工具轮" not in json.dumps(commentary, ensure_ascii=False)
    assert "反复打开" not in json.dumps(commentary, ensure_ascii=False)


def test_rich_stream_confirms_exact_active_turn_input_ids(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)

    writer.begin_active_turn_input(("steer-1", "steer-2"))
    writer.complete_active_turn_input(("steer-1", "steer-2"))
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["kind"] == "active_turn_input_consumed"
    assert rows[0]["client_message_ids"] == ["steer-1", "steer-2"]

    ordinary_path = tmp_path / "ordinary.chunks.jsonl"
    ordinary = BufferedChunkStreamWriter(ordinary_path, rich_transcript=False)
    ordinary.begin_active_turn_input(("steer-private",))
    ordinary.close()
    ordinary_rows = (
        [
            json.loads(line)
            for line in ordinary_path.read_text(encoding="utf-8").splitlines()
        ]
        if ordinary_path.exists()
        else []
    )
    assert all(row.get("kind") != "active_turn_input_consumed" for row in ordinary_rows)


def test_runtime_notice_is_not_mislabeled_as_model_commentary(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer("[provider_transient_auto_resume] 固定运行通知\n")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "任意展示文字"},
        "legacy",
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert not any(row.get("kind") == "assistant_commentary" for row in rows)


def test_rich_gateway_provider_retry_is_immediate_structured_runtime_progress(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)

    accepted = writer.write_provider_retry(
        scope="transport",
        attempt=1,
        total=3,
        delay_seconds=2.0,
        error_type="URLError",
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert accepted is True
    assert rows == [
        {
            "t": rows[0]["t"],
            "kind": "runtime_progress",
            "text": "模型服务暂时不可用，2 秒后自动重连（连接 1/3）",
            "verbose_level": "full",
            "retry": {
                "scope": "transport",
                "attempt": 1,
                "total": 3,
                "wait_seconds": 2.0,
                "error_type": "URLError",
            },
        }
    ]
    assert "api_base" not in json.dumps(rows, ensure_ascii=False)


def test_rich_gateway_tool_input_progress_keeps_only_public_counters(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)

    assert writer.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "started",
            "stream_index": 2,
            "tool": "write_file",
            "received_chars": 0,
            "partial_json": '{"path":"/root/secret","content":"private"}',
        }
    )
    assert writer.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "ready",
            "stream_index": 2,
            "tool": "write_file",
            "received_chars": 12_345,
        }
    )

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == [
        "tool_input_progress",
        "tool_input_progress",
    ]
    assert rows[-1]["progress"] == {
        "schema": "provider_tool_input_progress.v1",
        "phase": "ready",
        "stream_index": 2,
        "tool": "write_file",
        "received_chars": 12_345,
    }
    serialized = json.dumps(rows, ensure_ascii=False)
    assert "partial_json" not in serialized
    assert "/root/secret" not in serialized
    assert "private" not in serialized


def test_model_generation_prefers_typed_model_chunk_sink() -> None:
    model_chunks: list[str] = []
    generic_chunks: list[str] = []

    class Sink:
        def __call__(self, text: str) -> None:
            generic_chunks.append(text)

        def write_model(self, text: str) -> None:
            model_chunks.append(text)

    callback = _model_chunk_callback(Sink())
    callback("真实模型文字")

    assert model_chunks == ["真实模型文字"]
    assert generic_chunks == []

    plain_chunks: list[str] = []
    plain = _model_chunk_callback(plain_chunks.append)
    plain("兼容旧回调")
    assert plain_chunks == ["兼容旧回调"]


def test_legacy_queued_system_command_fails_closed_beside_claimed_request(
    tmp_path, monkeypatch
) -> None:
    owner_root = tmp_path / "owner-runtime"
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        owner_root,
    )
    request_id = "gw-owner-progress"
    request_path = tmp_path / "base-gateway" / "requests" / "processing" / f"{request_id}.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "goal": "/verbose on",
                "conversation": _conversation(),
            }
        ),
        encoding="utf-8",
    )
    opened: list[object] = []

    def capture_open(path):
        opened.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, time.time()

    monkeypatch.setattr(request_execution, "open_chunk_stream", capture_open)

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["error_code"] == "SYSTEM_COMMAND_ROUTING_ERROR"
    assert opened == [request_path.with_name(f"{request_id}.chunks.jsonl")]
    assert not opened[0].is_relative_to(owner_root)


def test_full_progress_redacts_credentials_and_internal_protocol() -> None:
    request = SimpleNamespace(
        agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir="/owner/alice"))
    )
    event = ToolProgressEvent(request, 1, {}, "finished", "完成")

    assert (
        _public_progress_text(
            event,
            "api_key=super-secret /owner/alice/report.txt",
            max_chars=200,
        )
        == "[REDACTED] ~/.my-agent/owner/report.txt"
    )
    assert (
        _public_progress_text(
            event,
            "[RUN_TOOL_EVIDENCE_BLOCKED] private payload",
            max_chars=200,
        )
        == "（内部运行状态已省略）"
    )


def test_reply_worker_advances_progress_cursor_without_resubmitting_task() -> None:
    store = GatewayReplyDeliveryStore(None)
    progress_messages: list[str] = []
    final_messages: list[str] = []

    def poll_progress(record: PendingGatewayReply) -> tuple[list[str], int]:
        return (["执行完成：read_file"] if record.progress_cursor == 0 else []), 3

    final = {"value": None}
    worker = GatewayReplyDeliveryWorker(
        store,
        poll_response=lambda _request_id: final["value"],
        deliver_response=lambda _record, text: final_messages.append(text) is None,
        poll_progress=poll_progress,
        deliver_progress=lambda _record, text: progress_messages.append(text) is None,
    )
    worker.enqueue(PendingGatewayReply("req-1", "feishu", "ou-1", "om-1"))

    assert worker.run_once() == 0
    assert progress_messages == ["执行完成：read_file"]
    assert store.pending()[0].progress_cursor == 3
    assert worker.run_once() == 0
    assert progress_messages == ["执行完成：read_file"]
    final["value"] = "最终答案"
    assert worker.run_once() == 1
    assert final_messages == ["最终答案"]
    assert store.pending() == []


def test_failed_commentary_delivery_is_not_retried_or_allowed_to_delay_final() -> None:
    store = GatewayReplyDeliveryStore(None)
    progress_attempts: list[str] = []
    final = {"value": None}
    worker = GatewayReplyDeliveryWorker(
        store,
        poll_response=lambda _request_id: final["value"],
        deliver_response=lambda _record, _text: True,
        poll_progress=lambda record: (
            (["我先检查项目。"] if record.progress_cursor == 0 else []),
            4,
        ),
        deliver_progress=lambda _record, text: progress_attempts.append(text) is None and False,
    )
    worker.enqueue(PendingGatewayReply("req-1", "feishu", "ou-1", "om-1"))

    assert worker.run_once() == 0
    assert progress_attempts == ["我先检查项目。"]
    assert store.pending()[0].progress_cursor == 4
    assert worker.run_once() == 0
    assert progress_attempts == ["我先检查项目。"]

    final["value"] = "最终答案"
    assert worker.run_once() == 1
    assert store.pending() == []


# LLM: 候选消息流式——rich 客户端逐批收到脱敏 model_delta，普通客户端保持旧契约。
def test_rich_writer_streams_live_model_deltas(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True, flush_chars=16)
    writer.write_model("第一段")
    assert not any(row["kind"] == "model_delta" for row in _rows(path))
    writer.write_model("，继续")
    writer.close()

    deltas = [row for row in _rows(path) if row.get("kind") == "model_delta"]
    assert [row["text"] for row in deltas] == ["第一段，继续"]
    commentary = [row for row in _rows(path) if row.get("kind") == "assistant_commentary"]
    assert commentary == []


def test_non_rich_writer_never_emits_model_delta(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_model("普通客户端正文")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "开始"},
        "legacy",
    )
    writer.close()

    assert not any(row["kind"] == "model_delta" for row in _rows(path))
    commentary = [row for row in _rows(path) if row.get("kind") == "assistant_commentary"]
    assert [row["text"] for row in commentary] == ["普通客户端正文"]


def test_model_deltas_flush_before_commentary_boundary(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True, flush_chars=8)
    writer.write_model("先看文件。")
    writer.write_progress(
        {"tool": "read_file", "phase": "started", "status": "开始"},
        "legacy-1",
    )
    writer.close()

    kinds = [row["kind"] for row in _rows(path)]
    assert kinds.index("model_delta") < kinds.index("assistant_commentary")
    assert kinds.index("model_delta") < kinds.index("tool_progress")


def test_model_deltas_are_redacted_and_steering_clears_pending(tmp_path) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True, flush_chars=4)
    writer.set_identifier_redactions((("om_secret_conversation", "当前会话"),))
    writer.write_model("检查 /root/private/secret 与 om_secret_conversation")
    writer.close()

    deltas = [row for row in _rows(path) if row.get("kind") == "model_delta"]
    assert deltas
    assert "om_secret_conversation" not in deltas[-1]["text"]
    assert "/root/private/secret" not in deltas[-1]["text"]

    steered_path = tmp_path / "steered.chunks.jsonl"
    steered = BufferedChunkStreamWriter(steered_path, rich_transcript=True, flush_chars=4)
    steered.write_model("旧候选")
    steered.begin_active_turn_input(("client-1",))
    steered.write_model("新候选的说明")
    steered.write_progress(
        {"tool": "read_file", "phase": "started", "status": "开始"},
        "legacy",
    )
    steered.close()

    steered_deltas = [row for row in _rows(steered_path) if row.get("kind") == "model_delta"]
    assert [row["text"] for row in steered_deltas] == ["新候选的说明"]


def _rows(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rich_writer_streams_thinking_deltas_and_redacts(tmp_path) -> None:
    path = tmp_path / "thinking.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    writer.set_identifier_redactions((("om_secret_thread", "当前会话"),))
    writer.write_thinking_delta("先分析 om_secret_thread 的权限")
    writer.write_thinking_delta("再决定动作")
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    deltas = [row for row in rows if row.get("kind") == "thinking_delta"]
    assert [row["text"] for row in deltas] == ["先分析 当前会话 的权限", "再决定动作"]


def test_non_rich_writer_never_emits_thinking_delta(tmp_path) -> None:
    path = tmp_path / "thinking-plain.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_thinking_delta("隐藏的思考")
    writer.close()

    if not path.exists():
        return  # 非 rich writer 不落任何事件，文件甚至不会创建
    assert not any(
        json.loads(line).get("kind") == "thinking_delta"
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_gateway_round_end_settles_pending_guidance(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts import request_execution

    settled: list[tuple[str, bool]] = []

    class FakeStore:
        def reject_pending_guidance_for_turn(self, turn_id, *, reject_reserved=False):
            settled.append((turn_id, reject_reserved))
            return {"rejected": 0}

    agent = SimpleNamespace(conversation_store=FakeStore())

    request_execution._settle_pending_gateway_guidance(agent, "req-1")
    assert settled == [("req-1", True)]

    request_execution._settle_pending_gateway_guidance(agent, "")
    assert len(settled) == 1


def test_gateway_session_approval_cache_reuses_approved_call(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.gateway_parts.request_execution import (
        wait_for_gateway_permission_decision,
    )

    path = tmp_path / "session.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True, interactive_approvals=True)
    request = {
        "permission_id": "approval:abc",
        "request_id": "req-1",
        "tool_name": "run_command",
        "round": 1,
        "call_index": 1,
        "title": "Tool use",
        "description": "run_command(ls)",
        "binding": {
            "tool_name": "run_command",
            "run_id": "run-1",
            "operation_id": "op-1",
            "idempotency_key": "idem-1",
            "args_hash": "h1",
        },
        "options": [
            {"id": "allow_once", "label": "Yes", "decision": "approved"},
            {"id": "allow_session", "label": "Yes session", "decision": "approved_session"},
            {"id": "deny", "label": "No", "decision": "denied"},
        ],
    }
    # 第一次：模拟用户在审批框选了"本次会话允许"
    monkeypatch.setattr(
        request_execution,
        "wait_for_gateway_permission_decision",
        lambda chunk_path, req, cancellation_token=None: ToolApprovalDecision(
            req.permission_id, "approved_session"
        ),
    )
    first = writer.request_permission(request)
    assert first["decision"] == "approved_session"

    # 第二次：同 binding 直接放行（不弹框，不再等待）
    calls: list[str] = []

    def fail_wait(chunk_path, req, cancellation_token=None):
        calls.append("wait")
        raise AssertionError("session-approved call must not wait")

    monkeypatch.setattr(request_execution, "wait_for_gateway_permission_decision", fail_wait)
    request2 = dict(request)
    request2["permission_id"] = "approval:abc2"
    second = writer.request_permission(request2)
    assert second["decision"] == "approved"
    assert calls == []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["kind"] == "permission_resolved"
    assert rows[-1]["decision"] == "approved"
