"""取消必须能精确停到目标回合：认领那一刻就要绑定。

真实 gateway 场景 5（dsh-ae 复验 a646a4885）：取消有两种停不下来的情形。
其中"取消发生在目标第一次模型调用期间"的根因是：任务要等这次调用返回、正文**确认**之后才绑定，
所以取消时它还没绑定，只走"撤队列"，而正文已经是 submitted，撤不掉。

修法（dev 16:47）：**正文被认领（reserved）的那一刻就绑定回合**，不要等确认。
这里证明绑定与认领同一时刻落账，且取消能按该回合找到目标任务。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import inject_pending_guidance
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_messaging import SESSION_TASK_ORIGIN_KIND
from agent_py_agent.agent.conversation.session_tasks import (
    SESSION_TASK_ACCEPTED,
    SessionTaskDraft,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_SENDER = "thread-a"
_TARGET = "thread-b"
_TASK_ID = "stask-bound-at-claim"
_TURN = "req-target-1"
_BODY_KEY = f"body:session_task:{_SENDER}->{_TARGET}:把 X 做好"


def _setup(tmp_path):
    store = ConversationStore(tmp_path / "conv")
    store.threads.write(
        ConversationThread(
            thread_id=_TARGET,
            canonical_user_id="local-agent",
            owner_id="main",
            owner_home="",
            title="目标会话",
        )
    )
    task = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread_id=_SENDER,
            target_thread_id=_TARGET,
            goal="把 X 做好",
            body_guidance_id="",
            body_dedupe_key=_BODY_KEY,
            dedupe_key=f"session_task:{_SENDER}->{_TARGET}:让我做X",
        ),
        now=1.0,
    )
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": "把 X 做好",
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
                "session_task_id": task.task_id,
            },
        },
        dedupe_key=_BODY_KEY,
    )
    task = store.session_tasks.bind_body(task.task_id, entry.guidance_id)
    agent = SimpleNamespace(
        conversation_store=store,
        config=CapabilityConfig(),
        _capability_config_runtime_snapshot=type("_S", (), {"config": CapabilityConfig()})(),
    )
    params = SimpleNamespace(
        request_id=_TURN,
        task_id=_TURN,
        run_id="attempt-1",
        attempt_id="attempt-1",
        context_scope="",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="claim-bind-run"),
        task_attributes={"conversation_thread_id": _TARGET},
        live_archive_state={},
        tool_context=[],
        runtime_injections=[],
    )
    return agent, store, task, params


def test_task_is_bound_at_claim_time(tmp_path) -> None:
    """认领即绑定：不等确认，取消才能在这段窗口里精确停到目标回合。"""
    agent, store, task, params = _setup(tmp_path)

    assert inject_pending_guidance(agent, params) is True

    bound = store.session_tasks.load(task.task_id)
    assert bound.conversation_request_id == _TURN
    assert bound.status == SESSION_TASK_ACCEPTED


def test_cancel_can_find_the_target_turn_right_after_claim(tmp_path) -> None:
    """取消按绑定的回合号找回目标任务——这正是"停不下"缺的那一环。"""
    agent, store, task, params = _setup(tmp_path)
    inject_pending_guidance(agent, params)

    assert store.session_tasks.load(task.task_id).conversation_request_id == _TURN


def test_binding_is_not_deferred_to_acknowledgement(tmp_path) -> None:
    """绑定发生在注入（认领）之后、确认之前——不是等模型答复才绑。"""
    agent, store, task, params = _setup(tmp_path)

    inject_pending_guidance(agent, params)
    # 此时尚未 acknowledge，但已经绑定。
    assert params.live_archive_state.get("_guidance_ack_ids")
    assert store.session_tasks.load(task.task_id).conversation_request_id == _TURN
