"""业务结果只主动唤醒可接收回报的会话，不给普通无绑定线程增开执行回合。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.business_contracts import (
    BackgroundAgentIdentity,
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.conversation.session_task_report import (
    report_task_result,
    result_report_key,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate


def _report_case(tmp_path, *, sender_kind, status, business=True):
    store = ConversationStore(tmp_path / "conversation")
    sender = store.threads.create(owner_id="owner", canonical_user_id="local-agent")
    target = store.threads.create(owner_id="owner", canonical_user_id="local-agent")
    if sender_kind in {"tui", "feishu"}:
        binding = ChannelBinding(sender_kind, "chat-1", "user-1", "local-agent", sender.thread_id)
        store.threads.update_atomic(
            sender.thread_id, lambda current: replace(current, channel_bindings=(binding,))
        )
    elif sender_kind == "background":
        identity = BackgroundAgentIdentity(sender.thread_id, None, None, None, "available", "reuse", 1)
        store.threads.update_atomic(sender.thread_id, lambda current: replace(
            current, metadata={**current.metadata, "background_agent": identity.to_dict()}
        ))
    elif sender_kind == "controller":
        store.threads.update_atomic(sender.thread_id, lambda current: replace(
            current, metadata={**current.metadata, "business_controller": True}
        ))
    options = {}
    if business:
        options = {
            "business_context_intent": BusinessContextIntent("owner", None, None, None),
            "business_context_factory": lambda task_id: BusinessExecutionContext(
                "owner", None, None, task_id, None, None, (), (), "business-context.v1"
            ),
        }
    task = store.session_tasks.create(SessionTaskDraft(
        sender_thread_id=sender.thread_id, target_thread_id=target.thread_id, goal="整理结果"
    ), **options)
    store.session_tasks.bind_turn(task.task_id, turn_id="worker-turn")
    terminal = store.session_tasks.advance(
        task.task_id, SessionTaskUpdate(status=status, summary="任务已收口")
    )
    return SimpleNamespace(conversation_store=store), sender, terminal


@pytest.mark.parametrize("status", ["done", "failed"])
@pytest.mark.parametrize("sender_kind", ["ordinary", "tui", "feishu", "background", "controller"])
@pytest.mark.parametrize("business", [True, False], ids=["business-task", "plain-task"])
def test_result_report_preserves_passive_senders_and_wakes_business_recipients(
    tmp_path, sender_kind, status, business
):
    agent, sender, task = _report_case(
        tmp_path, sender_kind=sender_kind, status=status, business=business
    )
    assert report_task_result(agent, task) is True
    assert report_task_result(agent, task) is True
    store = agent.conversation_store
    pending, errors = store.guidance.pending_report("thread", sender.thread_id, limit=0)
    assert not errors
    assert len(pending) == 1
    assert pending[0].delivery == "next_turn"
    assert pending[0].metadata["session_task_id"] == task.task_id
    assert pending[0].metadata["session_task_status"] == status
    wakes = store.wakes.pending(limit=0)
    if business and sender_kind != "ordinary":
        assert [(wake.thread_id, wake.reason, wake.dedupe_key) for wake in wakes] == [
            (sender.thread_id, "session_message", result_report_key(task))
        ]
        assert wakes[0].metadata["origin_thread_id"] == task.target_thread_id
    else:
        assert wakes == []
