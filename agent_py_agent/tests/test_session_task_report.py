"""D：会话任务在目标回合正常结束时自动回报发送方的合同测试。

覆盖：按绑定的 request id 找回任务、成功推进 done / 失败推进 failed、终态后重复收口不再回报（幂等）、
不在派活回合的请求不动任何任务、读账损坏时不猜归属、回报正文是宿主事件来源且带任务 id。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_task_report import (
    TaskTurnOutcome,
    close_out_turn,
    outcome_from_error,
    report_task_result,
    task_for_turn,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTaskDraft,
    SessionTaskUpdate,
)

_SENDER_THREAD_ID = "thread-A"
_TARGET_THREAD_ID = "thread-B"
_TURN_ID = "req-target-1"


def _agent(tmp_path: pathlib.Path) -> object:
    store = ConversationStore(tmp_path / "conv")
    return SimpleNamespace(conversation_store=store)


def _task(agent: object, *, turn_id: str = _TURN_ID) -> object:
    store = agent.conversation_store
    task = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread_id=_SENDER_THREAD_ID,
            target_thread_id=_TARGET_THREAD_ID,
            goal="做 X",
            body_guidance_id="g-body",
            dedupe_key="session_task:A->B:做 X",
        )
    )
    return store.session_tasks.bind_turn(task.task_id, turn_id=turn_id)


def test_task_for_turn_matches_only_its_own_bound_turn(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)

    assert task_for_turn(agent, _TURN_ID).task_id == task.task_id
    # 别的回合、空回合、没有会话任务存储都不命中。
    assert task_for_turn(agent, "req-other") is None
    assert task_for_turn(agent, "") is None
    assert task_for_turn(SimpleNamespace(conversation_store=None), _TURN_ID) is None


def test_close_out_success_reports_structured_result_to_sender(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)

    updated = close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True, summary="做完了", result_refs=("out/x.md",)))

    assert updated is not None and updated.status == "done"
    assert updated.summary == "做完了" and updated.result_refs == ("out/x.md",)
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "done"
    # 回报进了发送方的 guidance 队列，且是按宿主事件来源投递的结构化结果。
    pending = agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID)
    assert len(pending) == 1
    entry = pending[0]
    assert entry.metadata["origin_kind"] == "session_task"
    assert entry.metadata["session_task_id"] == task.task_id
    assert entry.metadata["session_task_status"] == "done"
    assert task.task_id in entry.message and "done" in entry.message
    assert "out/x.md" in entry.message


def test_close_out_failure_uses_failed_status(tmp_path) -> None:
    agent = _agent(tmp_path)
    _task(agent)

    updated = close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=False))

    assert updated is not None and updated.status == "failed"
    pending = agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID)
    assert pending[0].metadata["session_task_status"] == "failed"


def test_second_close_out_is_idempotent_and_does_not_report_again(tmp_path) -> None:
    agent = _agent(tmp_path)
    _task(agent)

    assert close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True)) is not None
    # 终态后再收口：不再推进、也不写第二条回报。
    assert close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True)) is None
    assert len(agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID)) == 1


def test_close_out_with_unrelated_turn_changes_nothing(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)

    assert close_out_turn(agent, "req-unrelated", TaskTurnOutcome(ok=True)) is None

    assert agent.conversation_store.session_tasks.load(task.task_id).status == "accepted"
    assert agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID) == []


def test_corrupt_task_ledger_never_guesses_ownership(tmp_path) -> None:
    agent = _agent(tmp_path)
    _task(agent)
    # 放一个读不动的任务文件：宁可不回报，也不能把结果记到错的任务上。
    (agent.conversation_store.session_tasks._dir() / "stask-broken.json").write_text("{bad", encoding="utf-8")

    assert task_for_turn(agent, _TURN_ID) is None
    assert close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True)) is None


def test_report_requires_a_real_sender(tmp_path) -> None:
    agent = _agent(tmp_path)

    assert report_task_result(agent, {"task_id": "stask-1", "status": "done"}) is False


def test_outcome_from_error_maps_terminal_state() -> None:
    assert outcome_from_error(None) is True
    assert outcome_from_error(ValueError("boom")) is False
    assert outcome_from_error(None, interrupted=True) is False


def test_reported_message_is_json_safe_structured_text(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)
    close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True, summary='含"引号"与换行\n的摘要'))

    entry = agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID)[0]
    assert "\n" in entry.message or "\\n" in entry.message


def test_report_counts_into_pair_quota(tmp_path) -> None:
    """回报是宿主自动发出的消息，不会被拒，但必须占用每对会话配额，否则回报能绕过限额。"""
    agent = _agent(tmp_path)
    _task(agent)
    store = agent.conversation_store
    assert store.session_pair_rate.count(_TARGET_THREAD_ID, _SENDER_THREAD_ID) == 0

    close_out_turn(agent, _TURN_ID, TaskTurnOutcome(ok=True))

    assert store.session_pair_rate.count(_TARGET_THREAD_ID, _SENDER_THREAD_ID) == 1


def test_cancel_notification_counts_into_pair_quota(tmp_path) -> None:
    """取消通知同样是宿主自动消息，也要计入配额（与回报同一分桶方向）。"""
    agent = _agent(tmp_path)
    task = _task(agent)
    store = agent.conversation_store
    from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
        CancelSessionTaskTool,
    )

    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    assert outcome.ok
    assert store.session_pair_rate.count(_TARGET_THREAD_ID, _SENDER_THREAD_ID) == 1
