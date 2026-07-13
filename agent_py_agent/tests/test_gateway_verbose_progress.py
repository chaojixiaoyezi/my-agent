from __future__ import annotations

import json
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
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.request_execution import (
    BufferedChunkStreamWriter,
    _gateway_conversation_context,
    _GatewayAskRunContext,
    _GatewayConversationLoadRequest,
    _run_gateway_ask,
)
from agent_py_agent.agent.settings import AgentConfig


def _conversation(user: str = "ou_alice", chat: str = "oc_one") -> dict[str, str]:
    return {
        "channel": "feishu",
        "channel_conversation_id": chat,
        "channel_user_id": user,
        "canonical_user_id": user,
    }


def _run_command(agent: SimpleAgent, tmp_path, request_id: str, prompt: str, conversation: dict):
    return _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": prompt, "conversation": conversation},
            tmp_path / f"{request_id}.request.json",
            tmp_path / f"{request_id}.response.json",
            request_id,
            lambda _text: None,
        )
    )


def test_verbose_command_is_persisted_per_conversation_without_calling_model(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    first = _run_command(agent, tmp_path, "gw-v1", "/verbose on", _conversation())
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

    assert first.backend == "conversation_directive"
    assert "工具步骤摘要" in first.response
    assert current.verbose_level == "on"
    assert other.verbose_level == "off"
    status = _run_command(agent, tmp_path, "gw-v4", "/v", _conversation())
    assert status.response == "当前详细过程模式：开启。"


def test_verbose_invalid_level_does_not_change_thread_state(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    result = _run_command(agent, tmp_path, "gw-v1", "/verbose everything", _conversation())
    current = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {"conversation": _conversation()},
            "gw-v2",
            "继续",
        )
    )
    assert result.response.startswith("用法：")
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


def test_full_progress_redacts_credentials_and_internal_protocol() -> None:
    request = SimpleNamespace(
        agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir="/owner/alice"))
    )
    event = ToolProgressEvent(request, 1, {}, "完成")

    assert _public_progress_text(
        event,
        "api_key=super-secret /owner/alice/report.txt",
        max_chars=200,
    ) == "[REDACTED] ~/.my-agent/owner/report.txt"
    assert _public_progress_text(
        event,
        "[MAIN_AGENT_DELIVERY_COMPLETE] private payload",
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
