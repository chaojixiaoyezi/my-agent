from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation import history_projection
from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.tests.test_gateway_conversation_compact import (
    _agent,
    _context,
    _request,
    _SummaryBackend,
)


# LLM: The fake provider declares native tools but has no physical wire limit; this test keeps
# the production pressure policy small while isolating the summary backend's test capacity.
# 函数用途: 固定 fake 摘要后端容量，使本文件只测试会话来源与加载副作用。
@pytest.fixture(autouse=True)
def _fake_summary_capacity(monkeypatch):
    from agent_py_agent.agent.conversation import compact_request_budget

    monkeypatch.setattr(
        compact_request_budget,
        "resolve_model_context_window_tokens",
        lambda _agent: 1_000_000,
    )


# LLM: Seed only through the public Gateway append path, preserving the canonical request
# metadata and transcript/index behavior that a deferred load must keep intact.
# 函数用途: 先建真实会话再写入超出压缩触发线的完整轮次，不提前执行摘要。
def _pressure_thread(tmp_path):
    agent = _agent(tmp_path, context_tokens=16_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_deferred_source")
    initial = _context(agent, request, "gw-create", "开始")
    for index in range(12):
        content = ("紫藤暗号 " if index == 0 else "阶段资料 ") + ("内容" * 900)
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                initial,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=content,
            )
    return agent, backend, request, initial


def test_deferred_load_keeps_full_source_without_summary_or_checkpoint(tmp_path, monkeypatch) -> None:
    agent, backend, request, initial = _pressure_thread(tmp_path)
    store = agent.conversation_store
    before = store.threads.load(initial.thread_id)
    checkpoint = agent.home_paths.owner_compact_dir / "conversations" / f"{initial.thread_id}.jsonl"
    # The display projection is deliberately narrowed to prove it cannot supply Compact's source.
    monkeypatch.setattr(
        history_projection,
        "conversation_history_rows",
        lambda *_args, rows, **_kwargs: tuple(rows[:2]),
    )

    deferred = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-follow", "继续", defer_compact=True)
    )
    after = store.threads.load(initial.thread_id)
    source = deferred.compact_source

    assert deferred.load_errors == ()
    assert backend.calls == 0
    assert before is not None and after is not None
    assert before.compact_generation == after.compact_generation == deferred.compact_generation == 0
    assert after.compacted_through_message_id == before.compacted_through_message_id == ""
    assert not checkpoint.exists()
    assert source is not None
    assert source.thread.thread_id == initial.thread_id
    assert source.thread.compact_generation == 0
    assert len(source.messages) == 24
    assert len(deferred.history) == 2
    assert source.messages[0].content.startswith("紫藤暗号")
    assert source.policy.trigger_tokens > 0
    assert source.recent_operation_evidence == {}
    assert deferred.recent_operation_evidence == {}
    assert deferred.scope is not None and deferred.scope.thread_id == initial.thread_id
    assert deferred.cwd == initial.cwd
    assert agent.local_store.search("紫藤暗号", limit=5, source_type="conversation_message")


def test_default_load_still_compacts_same_pressure_source(tmp_path) -> None:
    agent, backend, request, initial = _pressure_thread(tmp_path)

    loaded = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-follow", "继续")
    )
    stored = agent.conversation_store.threads.load(initial.thread_id)
    checkpoint = agent.home_paths.owner_compact_dir / "conversations" / f"{initial.thread_id}.jsonl"

    assert loaded.load_errors == ()
    assert loaded.compact_source is None
    assert loaded.compact_generation == 1
    assert backend.calls == 1
    assert stored is not None and stored.compacted_through_byte_offset > 0
    assert checkpoint.is_file()


def test_deferred_source_excludes_only_unfinished_current_user_suffix(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request("ou_deferred_suffix")
    initial = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent, request, initial, request_id="gw-old", role=role, content=f"旧轮 {role}"
        )
    assert append_gateway_conversation_message(
        agent, request, initial, request_id="gw-current", role="user", content="当前待回复输入"
    )

    pending = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-current", "当前待回复输入", defer_compact=True)
    )
    assert pending.compact_source is not None
    assert [row.content for row in pending.compact_source.messages] == ["旧轮 user", "旧轮 assistant"]
    assert len(agent.conversation_store.messages.recent(initial.thread_id, limit=0)) == 3

    assert append_gateway_conversation_message(
        agent, request, initial, request_id="gw-current", role="assistant", content="当前阶段答复"
    )
    completed = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-current", "继续", defer_compact=True)
    )
    assert completed.compact_source is not None
    assert [row.content for row in completed.compact_source.messages] == [
        "旧轮 user", "旧轮 assistant", "当前待回复输入", "当前阶段答复",
    ]
    direct = load_conversation_compact_source(
        agent,
        agent.conversation_store,
        agent.conversation_store.threads.require(initial.thread_id),
        exclude_request_id="gw-current",
    )
    assert direct.messages == completed.compact_source.messages


def test_bad_canonical_source_raises_instead_of_becoming_empty(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request("ou_deferred_bad_source")
    initial = _context(agent, request, "gw-create", "开始")
    assert append_gateway_conversation_message(
        agent, request, initial, request_id="gw-old", role="user", content="必须保留的原文"
    )
    store = agent.conversation_store
    before = store.threads.load(initial.thread_id)
    monkeypatch.setattr(
        store.messages,
        "after_compact_report",
        lambda _thread: ([], [{"error_code": "transcript_corrupt"}]),
    )

    with pytest.raises(ConversationCompactError):
        gateway_conversation_context(
            GatewayConversationLoadRequest(agent, request, "gw-follow", "继续", defer_compact=True)
        )

    after = store.threads.load(initial.thread_id)
    assert before is not None and after is not None
    assert after.compact_generation == before.compact_generation == 0
    assert store.messages.recent(initial.thread_id, limit=0)[0].content == "必须保留的原文"
