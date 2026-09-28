"""缺陷 3 的消失证明：未被确认的派活正文不会再被注入到后来的回合里。

真实 gateway 上的现象（2026-09-28 dsh-ae 验收）：A 派出 B2 之后，B 那一轮看到的是**更早的 B1**
并把它完成了；B2 被取消时还是 queued，只走了"从队列撤回"，没有发出停止控制。

根因是缺陷 2：派活片没有回合号 → 正文从不被认领、也从不被确认 → 它一直停在 `pending`，
于目标**后来的任意回合**里再次出现（`claim_for_turn` 对没有回合号的条目放行任意回合），
谁先跑到就先把旧正文做掉。

这里证明修好缺陷 2 之后的行为：正文一旦被它自己的派活回合认领并确认，就**不会再**出现在
后续回合里；而且每个回合只认领属于自己回合号的那条正文。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_guidance,
    mark_injected_turn_input_submitted,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.conversation.session_messaging import SESSION_TASK_ORIGIN_KIND
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_SENDER = "thread-a"
_TARGET = "thread-b"


def _wake(session_task_id: str) -> dict:
    return {
        "wake_signal_id": f"wake-{session_task_id}",
        "reason": "session_task",
        "summary": "收到来自另一个会话的任务",
        "metadata": {
            "origin_kind": SESSION_TASK_ORIGIN_KIND,
            "origin_thread_id": _SENDER,
            "session_task_id": session_task_id,
        },
    }


def _agent(tmp_path) -> tuple[SimpleNamespace, ConversationStore]:
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
    agent = SimpleNamespace(
        conversation_store=store,
        config=CapabilityConfig(),
        _capability_config_runtime_snapshot=type("_S", (), {"config": CapabilityConfig()})(),
    )
    return agent, store


def _dispatch(store: ConversationStore, task_id: str, goal: str) -> str:
    """与 create_session_task._queue_body 同一条写入语义（含正文自带的任务编号）。"""
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": goal,
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
                "session_task_id": task_id,
            },
        },
        dedupe_key=f"body:session_task:{_SENDER}->{_TARGET}:{task_id}",
    )
    return entry.guidance_id


def _turn(agent, task_id: str):
    params = _run_params(
        _TARGET,
        BackgroundRunRequest(thread_id=_TARGET, task_id="", reason="session_task", wake_signal=_wake(task_id)),
        agent,
    )
    params.tool_protocol_snapshot = make_test_protocol_snapshot(run_id=f"run-{task_id}")
    params.live_archive_state = {}
    # 运行循环在真实回合里挂载这两个可变容器；这里只补上循环本来就会创建的容器，
    # 不预置任何回合号或投递事实。
    params.tool_context = []
    params.runtime_injections = []
    return params


def test_consumed_body_is_never_injected_again(tmp_path) -> None:
    """B1 在自己的派活回合被认领并确认后，不会在 B2 的回合里再出现。"""
    agent, store = _agent(tmp_path)
    b1 = _dispatch(store, "stask-b1", "B1：先做这个")
    b2 = _dispatch(store, "stask-b2", "B2：之后做这个")

    # B1 自己的回合。
    first = _turn(agent, "stask-b1")
    assert inject_pending_guidance(agent, first) is True
    rendered_first = "\n".join(str(item) for item in first.tool_context)
    assert "B1：先做这个" in rendered_first
    assert "B2：之后做这个" not in rendered_first
    # 真实顺序：注入之后、模型调用之前先提交这一批，模型答复后再确认。
    assert mark_injected_turn_input_submitted(agent, first, provider_call_id="call-b1") == 1
    assert acknowledge_injected_turn_input(agent, first) == 1

    # B2 的回合：只应看到 B2，绝不能重放已经确认过的 B1。
    second = _turn(agent, "stask-b2")
    assert inject_pending_guidance(agent, second) is True
    rendered_second = "\n".join(str(item) for item in second.tool_context)
    assert "B2：之后做这个" in rendered_second
    assert "B1：先做这个" not in rendered_second
    assert b1 and b2


def test_each_turn_only_claims_its_own_body(tmp_path) -> None:
    """跨回合不认领：B1 的正文不能被 B2 的回合抢走（这正是旧行为里的"做错任务"）。"""
    agent, store = _agent(tmp_path)
    _dispatch(store, "stask-b1", "B1：先做这个")

    other = _turn(agent, "stask-b2")

    assert inject_pending_guidance(agent, other) is False
    # B1 仍然完好地等着自己的回合。
    assert [entry.message for entry in store.guidance.pending("thread", _TARGET)] == ["B1：先做这个"]


def test_body_is_not_replayed_between_two_turns_of_the_same_task(tmp_path) -> None:
    """同一任务的同一回合内也不重复注入（已注入集合按回合生效）。"""
    agent, store = _agent(tmp_path)
    _dispatch(store, "stask-b1", "B1：先做这个")
    params = _turn(agent, "stask-b1")

    assert inject_pending_guidance(agent, params) is True
    assert inject_pending_guidance(agent, params) is False
