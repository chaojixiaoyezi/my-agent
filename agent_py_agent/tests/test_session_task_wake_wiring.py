"""会话间派活在真实链路上的接线测试（无真实模型请求、无 Gateway）。

覆盖 2026-09-28 dev 批准的两个缺陷修复：
- **链深守卫**：`conversation_session_task_id` 必须由宿主按派活唤醒信封写入 `task_attributes`，
  `create_session_task` 从那里读链来源；**不得**依赖任何手工赋值的 agent 属性。
- **派活回合触发**：派活唤醒必须产出 `kind=session_task` 的 `TurnTrigger`，回合开头是宿主事件
  （`SESSION_TASK_FACTS_SOURCE`），而不是被落成用户轮。

这里只走真实入口（`_background_task_attributes` / `_background_model_inputs` / `create_session_task`），
不直接构造内部对象，也不手工写 agent 属性。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    CreateSessionTaskTool,
)
from agent_py_agent.agent.agent_core.runtime.turn_trigger import (
    SESSION_TASK_FACTS_SOURCE,
    SESSION_TASK_FIRST_LINE,
    TURN_TRIGGER_SESSION_TASK,
    TurnTrigger,
    current_turn_text,
    session_task_trigger,
    turn_trigger_recommendation,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.runtime import (
    _background_model_inputs,
    _background_task_attributes,
)

_SENDER = "thread-a"
_TARGET = "thread-b"
_TASK_ID = "stask-from-wake"


def _wake(session_task_id: str = _TASK_ID, *, origin: str = _SENDER) -> dict:
    """派活工具的 raise_signal 形状（结构化信封，不看正文）。"""
    return {
        "wake_signal_id": "wake-1",
        "reason": "session_task",
        "summary": "收到来自另一个会话的任务",
        "metadata": {
            "origin_kind": "session_task",
            "origin_thread_id": origin,
            "session_task_id": session_task_id,
        },
    }


def _request(wake: dict) -> SimpleNamespace:
    return SimpleNamespace(task_id="req-target-1", reason="session_task", wake_signal=wake)


def test_wake_writes_structured_session_task_id(tmp_path) -> None:
    """链来源由宿主写给 task_attributes——这正是 `_origin_task_id` 读的地方。"""
    attributes = _background_task_attributes(_TARGET, _request(_wake()), None)

    assert attributes is not None
    assert attributes[CONVERSATION_SESSION_TASK_ID_ATTR] == _TASK_ID


def test_wake_without_session_task_id_writes_nothing() -> None:
    """没有派活 id 的唤醒不写这个属性（不凭空造链来源）。"""
    wake = {"metadata": {"origin_kind": "session_message"}}

    attributes = _background_task_attributes(_TARGET, _request(wake), None)

    assert CONVERSATION_SESSION_TASK_ID_ATTR not in (attributes or {})


def test_origin_task_id_reads_structured_attributes(tmp_path) -> None:
    """工具侧读的就是上面写进去的属性：真实派活回合能拿到链来源。"""
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.tests.test_create_session_task_tool import _agent as build_agent

    config = CapabilityConfig()
    config.session_task_max_chain_depth = 1
    agent, store = build_agent(
        tmp_path,
        {
            _TARGET: SimpleNamespace(
                thread_id=_TARGET, status="active", channel_bindings=(), owner_id="main",
            ),
            "thread-c": SimpleNamespace(
                thread_id="thread-c", status="active", channel_bindings=(), owner_id="main",
            ),
        },
        config=config,
    )
    root = store.session_tasks.create(
        SessionTaskDraft(sender_thread_id="X", target_thread_id="Y", goal="根"), now=1.0
    )
    # 唯一允许的写入方式：宿主按唤醒信封写 task_attributes（与真实链路同一条路径）。
    agent._current_run_params.task_attributes.update(
        _background_task_attributes(_TARGET, _request(_wake(root.task_id)), None) or {}
    )
    # 当前回合所在会话是 thread-a（fake agent 默认），目标是另一个会话，避免自派。
    outcome = CreateSessionTaskTool(agent).execute(
        {"target_thread_id": "thread-c", "goal": "链上的下一跳"}
    )

    assert not outcome.ok
    assert outcome.error_code == "SESSION_TASK_CHAIN_LIMIT"


def test_session_task_trigger_from_wake() -> None:
    trigger = session_task_trigger_of(_wake())

    assert trigger is not None
    assert trigger.kind == TURN_TRIGGER_SESSION_TASK
    # 事实只含结构化字段，不复制任务正文。
    assert _TASK_ID in trigger.event_facts
    assert _SENDER in trigger.event_facts


def session_task_trigger_of(wake: dict) -> TurnTrigger | None:
    from agent_py_agent.agent.agent_core.runtime.turn_trigger import session_task_turn_trigger

    return session_task_turn_trigger(wake)


def test_session_task_trigger_requires_structured_id() -> None:
    """缺 session_task_id 时返回 None（fail closed，退化成普通后台片）。"""
    from agent_py_agent.agent.agent_core.runtime.turn_trigger import session_task_turn_trigger

    assert session_task_turn_trigger({}) is None
    assert session_task_turn_trigger({"metadata": {}}) is None
    assert session_task_turn_trigger({"metadata": {"session_task_id": "  "}}) is None


def test_background_model_inputs_uses_session_task_trigger() -> None:
    """派活唤醒走真实入口时分流到派活回合：turn_trigger 是 session_task，不是 None。"""
    objective, injections, trigger = _background_model_inputs(
        _request(_wake()), task_objective="", wake_prompt="兜底说明"
    )

    assert session_task_trigger(trigger) is not None
    # 派活回合不追加 lifecycle 的续跑注入。
    assert injections == []


def test_background_model_inputs_keeps_plain_turn_for_other_wakes() -> None:
    """非派活唤醒不受影响：仍旧是普通后台片。"""
    _, _, trigger = _background_model_inputs(
        _request({"metadata": {"reason": "other"}}), task_objective="", wake_prompt="x"
    )

    assert trigger is None


def test_session_task_turn_text_is_host_event() -> None:
    """回合开头是宿主事件 + 固定首句，任务正文绝不落在用户原话位置。"""
    trigger = session_task_trigger_of(_wake())
    text = current_turn_text("任务正文不应出现在这里", trigger)

    assert text.startswith("# Host Event")
    assert SESSION_TASK_FIRST_LINE in text
    assert not text.startswith("# User Task")


def test_session_task_recommendation_is_fixed_short_list() -> None:
    trigger = session_task_trigger_of(_wake())

    recommendation = turn_trigger_recommendation(trigger)

    assert recommendation is not None
    names, reason = recommendation
    assert "get_session_task" in names
    assert "send_session_message" in names
    assert reason
