from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.compact import _projected_context_tokens
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_execution import (
    _append_gateway_conversation_message,
    _conversation_prompt_section,
    _gateway_conversation_context,
    _GatewayConversationLoadRequest,
)
from agent_py_agent.agent.settings import AgentConfig


class _SummaryBackend:
    name = "summary-test"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _prompt: str, **_kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="用户的暗号是紫藤；较早工作已经讨论，仍需继续后续步骤。",
            backend=self.name,
        )


def test_conversation_pressure_does_not_reserve_unspent_future_output() -> None:
    prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    small_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64))
    large_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64_000))

    small_projection = _projected_context_tokens(
        small_output,
        "较早摘要",
        [],
        "当前消息",
    )
    large_projection = _projected_context_tokens(
        large_output,
        "较早摘要",
        [],
        "当前消息",
    )

    assert small_projection == large_projection


def _agent(tmp_path, *, context_tokens: int, max_turns: int = 20) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        max_tokens=64,
        model_context_window_tokens=context_tokens,
        memory_compact_auto_trigger_percent=50,
        conversation_history_max_turns=max_turns,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    return SimpleAgent(config, tmp_path)


def _request(user_id: str = "ou_alice") -> dict:
    return {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": f"oc_{user_id}",
            "channel_user_id": user_id,
            "canonical_user_id": user_id,
        }
    }


def _context(agent: SimpleAgent, request: dict, request_id: str, prompt: str):
    return _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, request_id, prompt)
    )


def test_same_thread_accumulates_beyond_recent_turn_setting_until_compact(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=2)
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(30):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} 内容",
            )

    followup = _context(agent, request, "gw-follow", "继续")

    assert followup.compact_generation == 0
    assert len(followup.history) == 60
    assert followup.history[0][1] == "第 0 轮 user 内容"


def test_compact_keeps_raw_transcript_and_indexes_old_messages_per_owner(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=12_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(12):
        content = ("紫藤暗号 " if index == 0 else "阶段资料 ") + ("内容" * 900)
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=content,
            )

    followup = _context(agent, request, "gw-follow", "我们接着做")
    stored = agent.conversation_store.load_thread(first.thread_id)
    raw_rows = agent.conversation_store.recent_messages(first.thread_id, limit=0)
    hits = agent.local_store.search(
        "紫藤暗号",
        limit=5,
        source_type="conversation_message",
    )

    assert followup.load_errors == ()
    assert followup.compact_generation >= 1
    assert followup.compact_summary.startswith("用户的暗号是紫藤")
    assert backend.calls >= 1
    assert stored is not None
    assert stored.compacted_through_message_id
    assert stored.compacted_through_byte_offset > 0
    assert len(raw_rows) == 24
    assert hits
    assert hits[0].metadata["thread_id"] == first.thread_id
    assert str(agent.home_paths.owner_home_dir) == followup.scope.owner_home
    section = _conversation_prompt_section(followup)
    assert "Earlier Conversation Summary" in section
    assert "原始逐条记录仍是事实源" in section
    tail, tail_errors = agent.conversation_store.messages_after_compact_report(stored)
    assert tail_errors == []
    assert tail
    assert all(row.message_id != stored.compacted_through_message_id for row in tail)


def test_conversation_search_index_does_not_cross_owner_local_stores(tmp_path) -> None:
    alice = _agent(tmp_path / "alice", context_tokens=1_000_000)
    bob = _agent(tmp_path / "bob", context_tokens=1_000_000)
    alice_context = _context(alice, _request("ou_alice"), "gw-a", "开始")
    assert _append_gateway_conversation_message(
        alice,
        {"metadata": {"channel": "feishu"}},
        alice_context,
        request_id="gw-a",
        role="user",
        content="只属于 Alice 的银杏计划",
    )

    assert alice.local_store.search("银杏计划", source_type="conversation_message")
    assert bob.local_store.search("银杏计划", source_type="conversation_message") == []
