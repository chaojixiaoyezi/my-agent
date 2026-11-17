"""F-LIVE-3: an overflow refresh finishes a progressing history repair; a stalled one fails closed with a clear notice."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.gateway_parts import request_context, request_execution, request_history
from agent_py_agent.agent.gateway_parts.history_ledger import GatewayHistoryLedger
from agent_py_agent.agent.gateway_parts.request_errors import (
    ConversationPersistenceError,
    GatewayHistoryPreparationPending,
    GatewayHistoryRepairUnsettledError,
)
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_gateway_model_adoption import fake_http
from agent_py_agent.tests.test_gateway_model_observation import install_backend


def _load(fixture, *, defer=False):
    context = fixture.context
    return request_context.gateway_conversation_context(request_context.GatewayConversationLoadRequest(
        fixture.agent, context.request, context.request_id, context.request["prompt"], allow_history_defer=defer))


# 函数用途: 让下一次历史修复需要多片：排队 101 条补交（每片 100 条），或像转录轮换那样让去重索引失效重建。
def _unsettle(agent, thread_id, kind):
    store = agent.conversation_store
    if kind == "queued_repairs":
        conversation = SimpleNamespace(thread_id=thread_id)
        for index in range(101):
            request_history._queue_gateway_conversation_repair(
                agent, {}, conversation, request_id=f"old-reply-{index}", role="assistant", content=f"old reply {index}")
        return
    for index in range(620):
        store.messages.append({"thread_id": thread_id, "role": "user", "content": f"旧消息 {index}",
                               "metadata": {"gateway_request_id": f"old-{index}"}})
    ledger = GatewayHistoryLedger(store)
    with ledger.connection() as conn:
        while not ledger.catch_up(conn, thread_id):
            pass
        conn.execute("UPDATE coverage SET identity=? WHERE thread=?", ('["rotated"]', thread_id))


def _ledger_state(agent, thread_id):
    with GatewayHistoryLedger(agent.conversation_store).connection() as conn:
        repairs = conn.execute("SELECT COUNT(*) FROM repairs WHERE thread=?", (thread_id,)).fetchone()[0]
        cursor = conn.execute("SELECT cursor FROM coverage WHERE thread=?", (thread_id,)).fetchone()[0]
    return repairs, cursor


@pytest.mark.parametrize("kind", ["queued_repairs", "ledger_reset"])
def test_overflow_refresh_finishes_a_progressing_history_repair(tmp_path, monkeypatch, kind):
    fixture = _history_request(tmp_path)
    install_backend(monkeypatch, fixture)
    agent, seen = fixture.agent, []

    def on_business(_wire):
        host = _HOST.get()
        if not seen:
            seen.append("overflow")
            _unsettle(agent, fixture.thread_id, kind)
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if host.compact_recovery is not None and not host.compact_recovery.committed:
            seen.append("summary")
            return
        seen.append("restored")

    fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(fixture.context)
    assert seen == ["overflow", "summary", "restored"]
    assert _ledger_state(agent, fixture.thread_id)[0] == 0
    rows = agent.conversation_store.messages.recent(fixture.thread_id, limit=2000)
    finals = [row for row in rows if row.role == "assistant"
              and row.metadata.get("gateway_request_id") == fixture.context.request_id]
    assert len(finals) == 1


def test_first_claim_still_yields_after_one_bounded_slice(tmp_path):
    fixture = _history_request(tmp_path)
    _unsettle(fixture.agent, fixture.thread_id, "queued_repairs")
    with pytest.raises(GatewayHistoryPreparationPending):
        _load(fixture, defer=True)
    assert _ledger_state(fixture.agent, fixture.thread_id)[0] == 1


def test_stalled_repair_fails_closed_after_one_slice(tmp_path, monkeypatch):
    fixture = _history_request(tmp_path)
    calls = []

    def stalled(self, conn, thread, *, progress=None):
        calls.append(thread)
        return False

    monkeypatch.setattr(GatewayHistoryLedger, "catch_up", stalled)
    with pytest.raises(ConversationPersistenceError) as caught:
        _load(fixture)
    assert type(caught.value) is ConversationPersistenceError and len(calls) == 1


def test_repair_still_progressing_at_the_deadline_tells_the_user_to_continue(tmp_path, monkeypatch):
    fixture = _history_request(tmp_path)
    clock, calls = [0.0], []

    def advancing(self, conn, thread, *, progress=None):
        calls.append(thread)
        clock[0] += 7.0
        if progress is not None:
            progress.update(cursor_before=len(calls), cursor_after=len(calls) + 1, source_end=10_000, reset_removed=0)
        return False

    monkeypatch.setattr(GatewayHistoryLedger, "catch_up", advancing)
    monkeypatch.setattr(request_context, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(GatewayHistoryRepairUnsettledError) as caught:
        _load(fixture)
    assert 1 < len(calls) < 10
    response = {}
    request_execution._apply_gateway_request_failure(fixture.agent, {"request": fixture.context.request}, response, caught.value)
    assert response["error_code"] == "CONVERSATION_PERSISTENCE_UNAVAILABLE" and response["ok"] is False
    assert "下一条消息" in response["user_error"] and "不要重做" in response["user_error"]
