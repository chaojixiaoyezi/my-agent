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
from agent_py_agent.agent.agent_core.tool_model_generation import _model_chunk_callback
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


def test_first_real_model_segment_becomes_sanitized_commentary_at_tool_boundary(
    tmp_path,
) -> None:
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path)
    writer.write_model(
        "我先检查 /root/private/project/main.py。\n"
        "[TOOL_CALL]\n{\"name\":\"read_file\"}\n[/TOOL_CALL]\n"
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

    assert commentary == [{"t": commentary[0]["t"], "kind": "assistant_commentary", "text": "我先检查 main.py。"}]
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
    writer.begin_active_turn_input()
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

    assert _public_progress_text(
        event,
        "api_key=super-secret /owner/alice/report.txt",
        max_chars=200,
    ) == "[REDACTED] ~/.my-agent/owner/report.txt"
    assert _public_progress_text(
        event,
        "[RUN_TOOL_EVIDENCE_BLOCKED] private payload",
        max_chars=200,
    ) == "（内部运行状态已省略）"


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
