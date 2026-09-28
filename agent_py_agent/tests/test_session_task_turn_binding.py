"""派活回合在真实链路上必须自带可绑定的回合号（缺陷 2 的接线测试）。

真实 gateway 上测出的现象（2026-09-28 dsh-ae 验收）：
- 目标空闲时被派活唤醒，本片 `request_id`/`run_id`/`task_id` 全是空串；
- 于是 `inject_pending_guidance` 在 `if entries and turn_id:` 短路——正文**从不认领**、
  **从不确认**，`bind_session_task_turns` 从没执行，任务一直 `queued`；
- `_close_out_session_task_turn` 按空 turn_id 直接返回——**从不收尾、从不回报**。

这里只走真实入口：真实 `BackgroundRunRequest` → 真实 `_run_params` →
真实 `inject_pending_guidance`，不手工给 params 塞回合号。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import inject_pending_guidance
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_task_attributes,
    _run_params,
    _session_task_run_id,
)
from agent_py_agent.agent.conversation.session_messaging import SESSION_TASK_ORIGIN_KIND
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_SENDER = "thread-a"
_TARGET = "thread-b"
_TASK_ID = "stask-wake-1"
_GOAL = "把 X 做好"


def _wake(session_task_id: str = _TASK_ID) -> dict:
    """派活工具真实发出的唤醒信封形状（结构化字段，不看正文）。"""
    return {
        "wake_signal_id": "wake-1",
        "reason": "session_task",
        "summary": "收到来自另一个会话的任务",
        "metadata": {
            "origin_kind": SESSION_TASK_ORIGIN_KIND,
            "origin_thread_id": _SENDER,
            "session_task_id": session_task_id,
        },
    }


def _request(wake: dict) -> BackgroundRunRequest:
    """走真实构造入口，不直接拼 dataclass 字段。"""
    return BackgroundRunRequest(
        thread_id=_TARGET,
        task_id="",
        reason="session_task",
        wake_signal=wake,
    )


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
        _capability_config_runtime_snapshot=type(
            "_S", (), {"config": CapabilityConfig()}
        )(),
    )
    return agent, store


def _queue_body(store: ConversationStore) -> str:
    """与 create_session_task._queue_body 同一条写入语义。"""
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": _GOAL,
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
            },
        },
        dedupe_key=f"body:session_task:{_SENDER}->{_TARGET}:{_GOAL}",
    )
    return entry.guidance_id


def test_session_task_run_id_reads_structured_envelope() -> None:
    """回合号只从唤醒信封的结构化字段取，不从正文或摘要推断。"""
    assert _session_task_run_id(_request(_wake())) == _TASK_ID
    assert _session_task_run_id(_request({"metadata": {}})) == ""
    assert _session_task_run_id(
        BackgroundRunRequest(thread_id=_TARGET, reason="scheduled_progress_report")
    ) == ""


def test_run_params_carry_session_task_turn_id(tmp_path) -> None:
    """真实 `_run_params` 必须把派活回合号落到 request_id/run_id/task_id。"""
    agent, _ = _agent(tmp_path)

    params = _run_params(_TARGET, _request(_wake()), agent)

    assert params.request_id == _TASK_ID
    assert params.run_id == _TASK_ID
    assert params.task_id == _TASK_ID


def test_run_params_without_wake_id_stay_taskless(tmp_path) -> None:
    """没有结构化回合号的派活信封不凭空造编号（fail closed）。"""
    agent, _ = _agent(tmp_path)

    params = _run_params(_TARGET, _request({"metadata": {"origin_kind": SESSION_TASK_ORIGIN_KIND}}), agent)

    assert params.request_id == ""
    assert params.task_id == ""


def test_background_task_attributes_carry_target_thread(tmp_path) -> None:
    """本片必须带上自己的会话身份，thread 邮箱才查得到那条正文。"""
    agent, _ = _agent(tmp_path)

    attributes = _background_task_attributes(_TARGET, _request(_wake()), agent, thread=agent.conversation_store.threads.load(_TARGET))

    assert attributes is not None
    assert attributes["conversation_thread_id"] == _TARGET
    assert attributes[CONVERSATION_SESSION_TASK_ID_ATTR] == _TASK_ID


def test_idle_target_claims_and_acknowledges_task_body(tmp_path) -> None:
    """真实参数对象下，空闲目标必须能认领并确认这条派活正文（缺陷 2 的核心）。"""
    agent, store = _agent(tmp_path)
    body_id = _queue_body(store)
    params = _run_params(_TARGET, _request(_wake()), agent)
    params.tool_protocol_snapshot = make_test_protocol_snapshot(run_id="idle-target-run")
    # 运行循环在真实回合里挂载这个可变状态；这里只补上运行循环本来就会创建的容器，
    # 不预置任何回合号或投递事实。
    params.live_archive_state = {}

    injected = inject_pending_guidance(agent, params)

    assert injected is True
    assert body_id in params.live_archive_state["_guidance_ack_ids"]
    # 认领即把正文绑到这条派活回合：收尾才能按同一个回合号找回任务。
    receipt = store.guidance.receipt(f"body:session_task:{_SENDER}->{_TARGET}:{_GOAL}")
    assert receipt is not None
    assert str((receipt.entry.metadata or {}).get("expected_turn_id") or "") == _TASK_ID
    assert receipt.status == "reserved"
