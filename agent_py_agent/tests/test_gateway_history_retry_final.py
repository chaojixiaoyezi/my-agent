"""同请求重试不能让中断的 final 吞掉后续完成结果。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_history as history


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    monkeypatch.setattr(history, "index_conversation_message", lambda *args: None)
    return SimpleNamespace(conversation_store=store), SimpleNamespace(thread_id=thread.thread_id)


def append(agent, conversation, attempt, reason, content):
    return history.append_gateway_conversation_message(
        agent, {"execution_attempt_id": attempt}, conversation,
        request_id="request", role="assistant", content=content,
        assistant_turn=history.GatewayAssistantTurn(end_reason=reason),
    )


@pytest.mark.parametrize("attempts", [("", ""), ("one", "one"), ("one", "two")])
@pytest.mark.parametrize("reason", ["error", "interrupted", "aborted", "max-tokens"])
def test_completed_retry_remains_visible_and_idempotent(runtime, attempts, reason):
    agent, conversation = runtime
    assert append(agent, conversation, attempts[0], reason, "失败" * 264)
    assert append(agent, conversation, attempts[1], "completed", "成功" * 1030)
    assert append(agent, conversation, attempts[1], "completed", "成功" * 1030)
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [(len(row.content), row.metadata["turn_end_reason"]) for row in rows] == [
        (528, reason), (2060, "completed")]


def test_legacy_final_without_reason_stays_idempotent(runtime):
    agent, conversation = runtime
    assert append(agent, conversation, "", "", "原回复")
    assert append(agent, conversation, "", "completed", "重复回复")
    assert len(agent.conversation_store.messages.recent(conversation.thread_id, limit=10)) == 1


def test_repair_keeps_both_attempts_and_completed_retry(runtime):
    agent, conversation = runtime
    for attempt, reason, content in [("one", "error", "中断"), ("one", "completed", "完成")]:
        history._queue_gateway_conversation_repair(
            agent, {"execution_attempt_id": attempt}, conversation,
            request_id="request", role="assistant", content=content,
            assistant_turn=history.GatewayAssistantTurn(end_reason=reason),
        )
    root = agent.conversation_store.storage.root
    assert len(list((root / "message_repairs").glob("*.json"))) == 2
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [row.content for row in rows] == ["中断", "完成"]
    assert len({row.content for row in rows}) == len(rows)


@pytest.mark.parametrize("late_reason", ["error", "interrupted", "completed", ""])
def test_completed_final_survives_late_attempt_repair(runtime, late_reason):
    agent, conversation = runtime
    assert append(agent, conversation, "new", "completed", "成功终稿")
    history._queue_gateway_conversation_repair(
        agent, {"execution_attempt_id": "old"}, conversation,
        request_id="request", role="assistant", content="迟到的旧稿",
        assistant_turn=history.GatewayAssistantTurn(end_reason=late_reason),
    )
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    assert append(agent, conversation, "old", late_reason, "迟到的旧稿")
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [row.content for row in rows] == ["成功终稿"]
