"""取消通知必须真的能到达发送方（真实 store，不是替身）。

真实 gateway 上测出的观察项：**发给派活方的取消通知一直是 pending**。
根因：`_notify_sender` 只把通知写进发送方 thread 邮箱，**从不唤醒发送方**；发送方空闲时
这条通知会一直停在 `pending`，直到它碰巧跑下一轮才被读到——与派活正文"写消息箱 + 空闲唤醒"
的投递语义不一致。

这里用真实 `ConversationStore`：真实 guidance 写入 + 真实 wake 队列，只断言结构化事实。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
    CancelSessionTaskTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore

_SENDER = "thread-a"
_TARGET = "thread-b"
_CANCEL_KEY_PREFIX = "session_task_cancelled:"


def _agent(tmp_path, *, sender_status: str = "active"):
    store = ConversationStore(tmp_path / "conv")
    store.threads.write(
        ConversationThread(
            thread_id=_SENDER,
            canonical_user_id="local-agent",
            owner_id="main",
            owner_home="",
            title="发送方",
            status=sender_status,
        )
    )
    store.threads.write(
        ConversationThread(
            thread_id=_TARGET,
            canonical_user_id="local-agent",
            owner_id="main",
            owner_home="",
            title="目标",
        )
    )
    agent = SimpleNamespace(
        conversation_store=store,
        _capability_config_runtime_snapshot=type("_S", (), {"config": CapabilityConfig()})(),
    )
    task = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread_id=_SENDER,
            target_thread_id=_TARGET,
            goal="把 X 做好",
            body_guidance_id="g-body",
        ),
        now=1.0,
    )
    return agent, store, task


def _cancel(agent, task) -> dict:
    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})
    assert outcome.ok
    return json.loads(outcome.output)


def _wakes_for(store, thread_id: str) -> list:
    """读真实唤醒队列里排给这个会话的信号。"""
    return [
        signal
        for signal in store.wakes.pending(limit=100)
        if str(getattr(signal, "thread_id", "") or "") == thread_id
    ]


def test_cancel_notice_wakes_an_idle_sender(tmp_path) -> None:
    """发送方空闲时也必须被唤醒——否则通知会永远停在 pending。"""
    agent, store, task = _agent(tmp_path)

    payload = _cancel(agent, task)

    assert payload["status"] == "cancelled"
    signals = _wakes_for(store, _SENDER)
    assert any(
        str((getattr(signal, "metadata", None) or {}).get("origin_kind") or "") == "session_message"
        for signal in signals
    ), "取消通知必须给发送方排一条唤醒信号"


def test_cancel_notice_is_reachable_from_the_sender_mailbox(tmp_path) -> None:
    """通知落在发送方 thread 邮箱里，且键稳定（同一任务重复取消不会重复写）。"""
    agent, store, task = _agent(tmp_path)

    _cancel(agent, task)

    pending = store.guidance.pending("thread", _SENDER)
    assert [entry.guidance_id for entry in pending], "发送方邮箱里应有一条取消通知"
    text = str(pending[0].message)
    assert task.task_id in text
    receipt = store.guidance.receipt(f"{_CANCEL_KEY_PREFIX}{task.task_id}")
    assert receipt is not None
    assert receipt.status == "pending"


def test_no_extra_wake_when_the_sender_thread_is_gone(tmp_path) -> None:
    """发送方线程不存在/非 active 时不排队唤醒（不凭空造投递目标）。"""
    agent, store, task = _agent(tmp_path, sender_status="closed")

    payload = _cancel(agent, task)

    # 取消本身仍然生效；只是不再排唤醒。
    assert payload["status"] == "cancelled"
    assert not _wakes_for(store, _SENDER)


def test_cancel_notice_does_not_break_when_guidance_write_fails(tmp_path) -> None:
    """通知失败不能改变"取消已生效"这个事实。"""
    agent, store, task = _agent(tmp_path)
    store.guidance = None

    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["status"] == "cancelled"
    assert payload["notified_sender"] is False
    assert store.session_tasks.load(task.task_id).status == "cancelled"
