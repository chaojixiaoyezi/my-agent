"""取消必须能精确停到目标回合：认领那一刻就要绑定。

真实 gateway 场景 5（dsh-ae 复验 a646a4885）：取消有两种停不下来的情形。
其中"取消发生在目标第一次模型调用期间"的根因是：任务要等这次调用返回、正文**确认**之后才绑定，
所以取消时它还没绑定，只走"撤队列"，而正文已经是 submitted，撤不掉。

修法（dev 16:47）：**正文被认领（reserved）的那一刻就绑定回合**，不要等确认。
这里证明绑定与认领同一时刻落账，且取消能按该回合找到目标任务。

认领正文的只能是这条任务自己的派活回合（回合号就是任务号，task_attributes 带同一个任务号）：没有归属任务号的回合
（前台、会话消息唤醒、定时）认领不到派活正文，派活回报不受影响（ae 复审 M2 的 R-b）。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import inject_pending_guidance
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_TASK_ORIGIN_KIND,
    SESSION_TASK_STATUS_FIELD,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SESSION_TASK_ACCEPTED,
    SessionTaskDraft,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_SENDER = "thread-a"
_TARGET = "thread-b"
_TASK_ID = "stask-bound-at-claim"
_FOREGROUND_TURN = "req-target-1"
_BODY_KEY = f"body:session_task:{_SENDER}->{_TARGET}:把 X 做好"


# 函数用途: 建一条派活任务和它在目标会话里的正文；默认给出这条任务自己的派活回合参数（回合号 = 任务号），
#   task_turn=False 时给出没有归属任务号的前台回合参数。
def _setup(tmp_path, *, task_turn: bool = True):
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
    turn_id = task.task_id if task_turn else _FOREGROUND_TURN
    attributes = {"conversation_thread_id": _TARGET}
    if task_turn:
        attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = task.task_id
    params = SimpleNamespace(
        request_id=turn_id,
        task_id=turn_id,
        run_id="attempt-1",
        attempt_id="attempt-1",
        context_scope="",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="claim-bind-run"),
        task_attributes=attributes,
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
    assert bound.conversation_request_id == task.task_id
    assert bound.status == SESSION_TASK_ACCEPTED


def test_cancel_can_find_the_target_turn_right_after_claim(tmp_path) -> None:
    """取消按绑定的回合号找回目标任务——这正是"停不下"缺的那一环。"""
    agent, store, task, params = _setup(tmp_path)
    inject_pending_guidance(agent, params)

    assert store.session_tasks.load(task.task_id).conversation_request_id == task.task_id


def test_binding_is_not_deferred_to_acknowledgement(tmp_path) -> None:
    """绑定发生在注入（认领）之后、确认之前——不是等模型答复才绑。"""
    agent, store, task, params = _setup(tmp_path)

    inject_pending_guidance(agent, params)
    # 此时尚未 acknowledge，但已经绑定。
    assert params.live_archive_state.get("_guidance_ack_ids")
    assert store.session_tasks.load(task.task_id).conversation_request_id == task.task_id


def test_turn_without_a_task_does_not_claim_the_task_body(tmp_path) -> None:
    """没有归属任务号的回合（例如目标正忙时的前台回合）认领不到派活正文：正文留在队列、任务不绑定，
    前台结束后由这条任务自己的派活回合认领、收口并回报。"""
    agent, store, task, params = _setup(tmp_path, task_turn=False)

    assert inject_pending_guidance(agent, params) is False

    assert store.guidance.receipt(_BODY_KEY).status == "pending"
    assert store.session_tasks.load(task.task_id).conversation_request_id == ""


def test_task_report_is_still_claimable_without_a_task(tmp_path) -> None:
    """派活回报（metadata 带任务状态键）不是正文：发送方没有归属任务号的回合照样能认领。"""
    agent, store, task, params = _setup(tmp_path, task_turn=False)
    store.guidance.mark_status(_BODY_KEY, "rejected")
    store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": "你派出的任务已结束：状态 done。",
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
                "session_task_id": task.task_id,
                SESSION_TASK_STATUS_FIELD: "done",
            },
        },
        dedupe_key=f"session_task_result:{task.task_id}:done",
    )

    assert inject_pending_guidance(agent, params) is True

    assert store.guidance.receipt(f"session_task_result:{task.task_id}:done").status == "reserved"


def test_legacy_task_body_without_task_id_keeps_the_old_behavior(tmp_path) -> None:
    """旧数据：正文写入时没记 session_task_id（归属无从核对），不算派活正文，保持原行为——没有归属任务号的回合照样能认领。"""
    agent, store, task, params = _setup(tmp_path, task_turn=False)
    store.guidance.mark_status(_BODY_KEY, "rejected")
    legacy_key = f"body:session_task:{_SENDER}->{_TARGET}:旧正文"
    store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": "旧版本写入的派活正文",
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {"origin_kind": SESSION_TASK_ORIGIN_KIND, "origin_thread_id": _SENDER},
        },
        dedupe_key=legacy_key,
    )

    assert inject_pending_guidance(agent, params) is True

    assert store.guidance.receipt(legacy_key).status == "reserved"
