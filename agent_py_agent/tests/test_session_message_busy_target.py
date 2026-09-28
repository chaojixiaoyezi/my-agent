"""目标正忙时收到会话消息/派活正文，不能把目标自己的请求打断。

覆盖 2026-09-28 dsh-ae 在真实 gateway 上测出的缺陷 4：正文条目只带
origin_kind / origin_thread_id / dedupe_key，没有 expected_turn_id；提交批次校验
（`store_guidance_submission._validate_locked`）要求回执条目的
`metadata["expected_turn_id"]` 等于当前回合，于是**目标自己正在跑的那一轮**在提交补充
消息时抛 `DataCorruptionError("guidance submission reservation mismatch")`，请求直接失败。

这里走真实路径：真实 `GuidanceStore` + 真实写入函数（与 `send_session_message` /
`create_session_task` 同一条 `append_once` 语义）→ 真实 `inject_pending_guidance`
（认领 + 注入 + 登记 ack）→ 真实 `mark_injected_turn_input_submitted`（提交批次）。

不启动 Gateway、不调用模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.guidance import (
    inject_pending_guidance,
    mark_injected_turn_input_submitted,
)
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGE_ORIGIN_KIND,
    SESSION_TASK_ORIGIN_KIND,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_guidance_records import _guidance_input_digest
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_TARGET_THREAD = "thread-b"
_TARGET_TURN = "req-target-busy-1"
_TARGET_ATTEMPT = "attempt-1"
_SENDER = "thread-a"


def _store(tmp_path) -> ConversationStore:
    return ConversationStore(tmp_path / "conv")


def _bind_thread_task(store: ConversationStore, thread_id: str) -> str:
    """建立权威的线程→任务关联（会话跑一轮时由宿主写入），thread 邮箱才查得到。"""
    request_id = f"task-{thread_id}"
    store.threads.write(
        ConversationThread(
            thread_id=thread_id,
            canonical_user_id="local-agent",
            owner_id="main",
            owner_home="",
            title="目标会话",
        )
    )
    store.tasks.bind(
        {
            "thread_id": thread_id,
            "task_id": request_id,
            "goal": "会话当前任务",
            "status": "active",
        }
    )
    return request_id


def _params(turn_id: str = _TARGET_TURN) -> SimpleNamespace:
    """目标会话正在跑的那一轮的 run params（只有本用例需要的字段）。"""
    return SimpleNamespace(
        request_id=turn_id,
        task_id=f"task-{_TARGET_THREAD}",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="busy-target-run"),
        run_id=_TARGET_ATTEMPT,
        attempt_id=_TARGET_ATTEMPT,
        context_scope="",
        task_attributes={"conversation_thread_id": _TARGET_THREAD},
        live_archive_state={},
        tool_context=[],
        runtime_injections=[],
    )


def _agent(store: ConversationStore) -> SimpleNamespace:
    return SimpleNamespace(conversation_store=store)


def _write_message_body(store: ConversationStore, message: str) -> str:
    """与 send_session_message._queue_and_wake 同一条写入语义（线程消息箱 + 稳定幂等键）。"""
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET_THREAD,
            "message": message,
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_MESSAGE_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
            },
        },
        dedupe_key=f"session_message:{_SENDER}->{_TARGET_THREAD}",
    )
    return entry.guidance_id


def _write_task_body(store: ConversationStore, goal: str) -> str:
    """与 create_session_task._queue_body 同一条写入语义。"""
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET_THREAD,
            "message": goal,
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
            },
        },
        dedupe_key=f"body:session_task:{_SENDER}->{_TARGET_THREAD}:{goal}",
    )
    return entry.guidance_id


def _receipt_turn_id(store: ConversationStore, dedupe_key: str) -> str:
    receipt = store.guidance.receipt(dedupe_key)
    assert receipt is not None
    return str((receipt.entry.metadata or {}).get("expected_turn_id") or "").strip()


def _inject_and_submit(store: ConversationStore, params: SimpleNamespace) -> int:
    """目标回合真实的安全点：认领并注入，随后在模型调用前提交这一批。"""
    agent = _agent(store)
    injected = inject_pending_guidance(agent, params)
    assert injected, "目标回合必须能认领这条正文（否则它永远不会被消费）"
    return mark_injected_turn_input_submitted(agent, params, provider_call_id="call-busy-1")


def test_busy_target_message_does_not_break_target_request(tmp_path) -> None:
    """目标正忙时收到消息：认领→注入→提交全程不抛异常，且消息真的进了这一轮。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    message_id = _write_message_body(store, "帮我看看这个接口")
    params = _params()

    submitted = _inject_and_submit(store, params)

    assert submitted == 1
    assert message_id in params.live_archive_state["_guidance_ack_ids"]
    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == _TARGET_TURN


def test_busy_target_task_body_does_not_break_target_request(tmp_path) -> None:
    """目标正忙时收到派活正文：同样不能把目标当前请求打断。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_task_body(store, "把 X 做好")
    params = _params()

    submitted = _inject_and_submit(store, params)

    assert submitted == 1
    body_key = f"body:session_task:{_SENDER}->{_TARGET_THREAD}:把 X 做好"
    assert _receipt_turn_id(store, body_key) == _TARGET_TURN


def test_host_delivery_without_expected_turn_id_fails_before_fix(tmp_path) -> None:
    """根因回归：这条回执在**认领前**确实没有 expected_turn_id——正是缺陷 4 的触发条件。

    认领补记是修复点；一旦有人在别处手工预置了该字段，这条断言会提示语义已经变化。
    """
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_message_body(store, "帮我看看这个接口")

    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == ""

    params = _params()
    inject_pending_guidance(_agent(store), params)

    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == _TARGET_TURN


def test_target_receipt_is_bound_to_the_target_turn_not_the_sender(tmp_path) -> None:
    """绑定的是**目标回合号**；发送方拿到的是别的回合号时不能影响目标那条回执。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_message_body(store, "帮我看看这个接口")
    sender_params = SimpleNamespace(
        request_id="req-sender-1",
        task_id="task-thread-a",
        run_id="attempt-sender",
        attempt_id="attempt-sender",
        context_scope="",
        task_attributes={"conversation_thread_id": _SENDER},
        live_archive_state={},
        tool_context=[],
        runtime_injections=[],
    )

    # 发送方自己的回合看不到、也认领不到目标线程的消息箱。
    assert inject_pending_guidance(_agent(store), sender_params) is False
    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == ""

    _inject_and_submit(store, _params())

    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == _TARGET_TURN


def test_interactive_steering_receipt_is_untouched(tmp_path) -> None:
    """交互式插话（已有 expected_turn_id）不被改写：认领不覆盖、也不拒绝它。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET_THREAD,
            "message": "普通用户插话",
            "sender": "user",
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "dedupe_key": "user-steer-1",
                "expected_turn_id": _TARGET_TURN,
            },
        },
        dedupe_key="user-steer-1",
    )
    assert entry.guidance_id

    submitted = _inject_and_submit(store, _params())

    assert submitted == 1
    assert _receipt_turn_id(store, "user-steer-1") == _TARGET_TURN


def test_reclaimed_host_delivery_keeps_single_turn_binding(tmp_path) -> None:
    """同一目标回合内重复注入不会二次认领，也不会把绑定改到别的回合。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_message_body(store, "帮我看看这个接口")
    params = _params()

    _inject_and_submit(store, params)

    assert inject_pending_guidance(_agent(store), params) is False
    assert not store.guidance.pending("thread", _TARGET_THREAD)
    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == _TARGET_TURN


def test_dedupe_digest_ignores_claim_time_turn_binding() -> None:
    """指纹只覆盖首次写入的输入：认领时补的 expected_turn_id 不能把同键重试判成"异文"。"""
    request = {
        "target_type": "thread",
        "target_id": _TARGET_THREAD,
        "message": "帮我看看这个接口",
        "sender": _SENDER,
        "priority": "normal",
        "delivery": "next_turn",
        "metadata": {
            "origin_kind": SESSION_MESSAGE_ORIGIN_KIND,
            "origin_thread_id": _SENDER,
            "dedupe_key": "session_message:x",
        },
    }
    bound = {
        **request,
        "metadata": {**request["metadata"], "expected_turn_id": _TARGET_TURN},
    }

    assert _guidance_input_digest(request) == _guidance_input_digest(bound)


def test_busy_target_message_still_renders_as_host_event(tmp_path) -> None:
    """注入到目标那一轮的正文仍按宿主事件呈现，不冒充目标自己的用户原话。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_task_body(store, "把 X 做好")
    params = _params()

    _inject_and_submit(store, params)

    rendered = "\n".join(str(item) for item in params.tool_context)
    assert "[SESSION_MESSAGE_HOST_EVENT]" in rendered
    assert "把 X 做好" in rendered


@pytest.mark.parametrize("origin_kind", [SESSION_MESSAGE_ORIGIN_KIND, SESSION_TASK_ORIGIN_KIND])
def test_other_senders_do_not_inherit_binding(tmp_path, origin_kind: str) -> None:
    """另一条来源不同的投递是独立回执，不能被前一条的绑定串到。"""
    store = _store(tmp_path)
    _bind_thread_task(store, _TARGET_THREAD)
    _write_message_body(store, "第一条")
    key = f"session_task:{_SENDER}->{_TARGET_THREAD}:goal-2"
    store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET_THREAD,
            "message": "第二条",
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {"origin_kind": origin_kind, "origin_thread_id": _SENDER},
        },
        dedupe_key=f"body:{key}",
    )

    params = _params()
    inject_pending_guidance(_agent(store), params)

    assert _receipt_turn_id(store, f"session_message:{_SENDER}->{_TARGET_THREAD}") == _TARGET_TURN
    assert _receipt_turn_id(store, f"body:{key}") == _TARGET_TURN
