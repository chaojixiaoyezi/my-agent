"""唤醒毒丸第 3 步 C4：唤醒被结案或来源已放弃时的领域收尾、宿主提示与"领域已是终态"判定。

走真实链路（test_session_task_real_chain 的 RealChain：真实 SimpleAgent、会话存储、Gateway 同款后台调度器，只替换供应商传输）：
A 给 B 派活或发消息，得到 B 的唤醒；再直接调收尾入口，或经调度器逐拍推进到结案，核对派活任务、会话消息回执、
派活方收到的回报和 B 的宿主提示。不联网、不调用真实模型。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.background_claim import SESSION_TASK_BODY_ABANDONED_ADMISSION
from agent_py_agent.agent.conversation.host_notices import (
    host_notice,
    pending_host_notices,
    queue_host_notice,
)
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGE_KEY_FIELD,
    SESSION_MESSAGE_RELEASE_LIMIT,
    SESSION_MESSAGE_RELEASE_LIMIT_REACHED,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTask, SessionTaskUpdate
from agent_py_agent.agent.conversation.wake_domain_closeout import (
    SESSION_TASK_WAKE_QUARANTINED,
    WAKE_POISON_NOTICE_SOURCE,
    close_out_abandoned_source,
    close_out_quarantined_wake,
    notify_stalled_wake,
    settle_abandoned_turn,
    wake_domain_status,
    wake_domain_terminal,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT,
    QuarantineDecision,
    WakeStallAlert,
)
from agent_py_agent.tests import test_session_task_real_chain as rc
from agent_py_agent.tests import test_wake_attempt_wiring as wiring

_DECISION = QuarantineDecision(reason_code="admission:skill_snapshot_error", same_cause_count=5, total_count=5,
                               redelivery_failures=0, mixed_causes=False)


# 函数用途: 搭真实链路，A 给 B 派活（kind=task）或发消息（kind=message），返回链路和 B 的那条唤醒。
def _chain_with_b_wake(tmp_path, monkeypatch, kind: str):
    chain = rc._real_chain(tmp_path, monkeypatch)
    if kind == "task":
        chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-DONE 整理三条要点。")
    else:
        chain.ask("A", f"RC-MESSAGE {chain.threads['B']} RC-NOTE-CLOSE 你好 B。")
    (wake,) = wiring._b_wakes(chain)
    return chain, wake


# 函数用途: B 会话上唤醒毒丸来源的宿主提示（code 列表按排队顺序）。
def _poison_notices(chain) -> list:
    rows = pending_host_notices(chain.agent.conversation_store, chain.threads["B"])
    return [row for row in rows if row.source == WAKE_POISON_NOTICE_SOURCE]


# 函数用途: 派活方 A 收到的、关于这条任务的回报（pending 投递里带 session_task_status 的那些）。
def _reports_to_a(chain, task_id: str) -> list:
    return [entry for entry in chain.pending_deliveries("A") if (entry.metadata or {}).get("session_task_id") == task_id
            and (entry.metadata or {}).get("session_task_status")]


# 函数用途: 这条会话消息唤醒指向的回执。
def _message_receipt(chain, wake):
    return chain.agent.conversation_store.guidance.receipt(wake.metadata[SESSION_MESSAGE_KEY_FIELD])


def test_quarantined_task_wake_fails_the_task_and_reports_the_code(tmp_path, monkeypatch) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    task = chain.task("RC-GOAL-DONE")
    assert wake_domain_terminal(chain.agent.conversation_store, wake) is False

    close_out_quarantined_wake(chain.agent, wake, _DECISION)
    close_out_quarantined_wake(chain.agent, wake, _DECISION)  # 重复收尾：任务已终态不再推进、不重复回报、提示同码替换

    failed = chain.agent.conversation_store.session_tasks.load(task.task_id)
    assert (failed.status, failed.failure_code) == ("failed", SESSION_TASK_WAKE_QUARANTINED)
    reports = _reports_to_a(chain, task.task_id)
    assert [(row.metadata["session_task_status"], row.metadata["session_task_failure_code"]) for row in reports] == [
        ("failed", SESSION_TASK_WAKE_QUARANTINED)]
    assert [row.code for row in _poison_notices(chain)] == [_DECISION.reason_code]
    assert wake_domain_terminal(chain.agent.conversation_store, wake) is True


def test_quarantined_message_wake_leaves_the_message_pending_and_says_so(tmp_path, monkeypatch) -> None:
    """做法 2（2026-09-29 3a 取代原裁定 b）：隔离的是唤醒这条执行路径，不代表消息有毒；回执不动，留给目标的下一次回合，
    宿主提示用结构化 details 注明这条消息仍待投递。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    assert _message_receipt(chain, wake).status == "pending"
    assert wake_domain_terminal(chain.agent.conversation_store, wake) is False

    close_out_quarantined_wake(chain.agent, wake, _DECISION)

    receipt = _message_receipt(chain, wake)
    assert receipt.status == "pending" and "rejection_code" not in receipt.migration, "隔离唤醒不能改会话消息的回执"
    (notice,) = _poison_notices(chain)
    assert notice.code == _DECISION.reason_code
    assert dict(notice.details) == {SESSION_MESSAGE_KEY_FIELD: wake.metadata[SESSION_MESSAGE_KEY_FIELD],
                                    "message_receipt_status": "pending"}
    assert wake_domain_terminal(chain.agent.conversation_store, wake) is False


def test_other_wakes_only_get_a_notice(tmp_path, monkeypatch) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    other = type(wake)(**{**wake.to_dict(), "reason": "subagent_completed", "metadata": {}})
    close_out_quarantined_wake(chain.agent, other, _DECISION)
    assert _message_receipt(chain, wake).status == "pending"
    assert [row.code for row in _poison_notices(chain)] == [_DECISION.reason_code]
    assert wake_domain_terminal(chain.agent.conversation_store, other) is False


def test_notices_merge_by_reason_code(tmp_path, monkeypatch) -> None:
    """同一会话里同一原因码只留最新一条；不同原因码各留一条；别的来源的提示不受影响（默认的同来源替换不变）。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    store, thread_id = chain.agent.conversation_store, chain.threads["B"]
    queue_host_notice(store, thread_id, host_notice("model_effort", "E1", "别的来源"))
    notify_stalled_wake(chain.agent, wake, WakeStallAlert("error:transient:ProviderTransientError", 30, 1.0, 1))
    close_out_quarantined_wake(chain.agent, wake, _DECISION)
    notify_stalled_wake(chain.agent, wake, WakeStallAlert("error:transient:ProviderTransientError", 60, 1.0, 2))
    rows = pending_host_notices(store, thread_id)
    assert [(row.source, row.code) for row in rows] == [
        ("model_effort", "E1"), (WAKE_POISON_NOTICE_SOURCE, _DECISION.reason_code),
        (WAKE_POISON_NOTICE_SOURCE, "error:transient:ProviderTransientError")]
    assert "60" in rows[-1].text
    queue_host_notice(store, thread_id, host_notice("model_effort", "E2", "默认按来源替换"))
    assert [row.code for row in pending_host_notices(store, thread_id) if row.source == "model_effort"] == ["E2"]


def test_abandoned_task_body_fails_the_task_with_the_release_code(tmp_path, monkeypatch) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    task = chain.task("RC-GOAL-DONE")
    close_out_abandoned_source(chain.agent, {"wake_signal": wake}, "wake_source_changed")
    assert chain.agent.conversation_store.session_tasks.load(task.task_id).status == "queued", "别的准入码不收尾"

    close_out_abandoned_source(chain.agent, {"wake_signal": wake}, SESSION_TASK_BODY_ABANDONED_ADMISSION)

    failed = chain.agent.conversation_store.session_tasks.load(task.task_id)
    assert (failed.status, failed.failure_code) == ("failed", SESSION_MESSAGE_RELEASE_LIMIT_REACHED)
    assert [row.metadata["session_task_failure_code"] for row in _reports_to_a(chain, task.task_id)] == [
        SESSION_MESSAGE_RELEASE_LIMIT_REACHED]


def test_closeout_write_failure_is_logged_not_raised(tmp_path, monkeypatch, capsys) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    tasks = chain.agent.conversation_store.session_tasks

    def broken(*_args, **_kwargs):
        raise OSError("磁盘写不进去")

    monkeypatch.setattr(tasks, "advance", broken)
    close_out_quarantined_wake(chain.agent, wake, _DECISION)
    assert "wake_domain_closeout_failed" in capsys.readouterr().out
    assert [row.code for row in _poison_notices(chain)] == [_DECISION.reason_code], "领域写失败不影响宿主提示"


def test_session_task_failure_code_round_trips_and_tolerates_old_records() -> None:
    task = SessionTask(task_id="stask-1", sender_thread_id="a", target_thread_id="b", goal="g", failure_code="X")
    assert SessionTask.from_dict(task.to_dict()).failure_code == "X"
    old = {key: value for key, value in task.to_dict().items() if key != "failure_code"}
    assert SessionTask.from_dict(old).failure_code == ""


@pytest.mark.parametrize("kind", ["task", "message"])
def test_scheduler_closes_out_the_domain_when_a_wake_is_quarantined(tmp_path, monkeypatch, kind) -> None:
    """经调度器逐拍推进：未知准入码同因满上限结案时，派活任务收成 failed 带码并回报，会话消息回执不动（做法 2），B 有宿主提示。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, kind)
    wiring._inject_admission(monkeypatch, "host_delivery_consumed")
    for _index in range(WAKE_POISON_SAME_CAUSE_LIMIT):
        wiring._tick_after_backoff(chain, wake)
    assert wiring._b_wakes(chain) == [], "应当已结案"
    if kind == "task":
        task = chain.task("RC-GOAL-DONE")
        assert (task.status, task.failure_code) == ("failed", SESSION_TASK_WAKE_QUARANTINED)
        assert len(_reports_to_a(chain, task.task_id)) == 1
    else:
        assert _message_receipt(chain, wake).status == "pending", "做法 2：隔离唤醒不改会话消息的回执"
    assert [row.code for row in _poison_notices(chain)] == ["admission:host_delivery_consumed"]


def test_abandoned_task_body_is_closed_out_by_the_admission_without_a_model_turn(tmp_path, monkeypatch) -> None:
    """派活正文反复没消费被放弃（回执 rejected 带 SESSION_MESSAGE_RELEASE_LIMIT_REACHED）：下一拍领取后准入判为来源已放弃，
    先把任务收成 failed 带码并回报派活方，再结案唤醒，不开模型回合（3a 裁定并入 C4）。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    task = chain.task("RC-GOAL-DONE")
    guidance = chain.agent.conversation_store.guidance
    for _index in range(SESSION_MESSAGE_RELEASE_LIMIT + 1):
        body = guidance.receipt(task.body_dedupe_key)
        assert guidance.claim_for_turn(body.entry, expected_turn_id=task.task_id, attempt_id="attempt-x",
                                       owning_task_id=task.task_id)
        guidance.recovery.reject_pending(task.task_id, reject_reserved=True, release_task_body=True,
                                         failure=RuntimeError("回合崩溃"))  # 只有会让回合崩溃的失败才计次（裁定 (a)）
    body = guidance.receipt(task.body_dedupe_key)
    assert (body.status, body.migration.get("rejection_code")) == ("rejected", SESSION_MESSAGE_RELEASE_LIMIT_REACHED)
    before = len(chain.wire.calls)

    chain.scheduler.tick(now=time.time())

    assert chain.wire.calls[before:] == [], "正文已放弃，唤醒仍开了模型回合"
    assert wiring._b_wakes(chain) == [], "唤醒没有结案"
    failed = chain.agent.conversation_store.session_tasks.load(task.task_id)
    assert (failed.status, failed.failure_code) == ("failed", SESSION_MESSAGE_RELEASE_LIMIT_REACHED)
    assert len(_reports_to_a(chain, task.task_id)) == 1


def test_admission_reports_abandoned_task_body_only(tmp_path, monkeypatch) -> None:
    """正文已消费不算来源处理完（派活回合会跨好几片）；读不到任务时照常开回合。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    store = chain.agent.conversation_store
    task = chain.task("RC-GOAL-DONE")
    admission = runtime_module._session_task_body_admission
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert admission(wake, guidance_store=store.guidance, session_tasks=store.session_tasks) == ""
    # 只读替身：正文回执已 consumed（真实链路里由模型返回确认写入），只关心准入判据怎么读它。
    consumed = SimpleNamespace(receipt=lambda _key: SimpleNamespace(status="consumed", migration={}))
    assert admission(wake, guidance_store=consumed, session_tasks=store.session_tasks) == ""
    plain_rejected = SimpleNamespace(receipt=lambda _key: SimpleNamespace(status="rejected", migration={}))
    assert admission(wake, guidance_store=plain_rejected, session_tasks=store.session_tasks) == "", "没有上限码的 rejected 不算放弃"
    missing = type(wake)(**{**wake.to_dict(), "metadata": {**wake.metadata, "session_task_id": "stask-missing"}})
    assert admission(missing, guidance_store=store.guidance, session_tasks=store.session_tasks) == ""


def test_notice_details_survive_the_thread_record_round_trip(tmp_path, monkeypatch) -> None:
    """details 随线程记录落盘、读回不丢（线程记录解析原先只保留四个键）；没有 details 的旧提示字典形状不变。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    store, thread_id = chain.agent.conversation_store, chain.threads["B"]
    queue_host_notice(store, thread_id, host_notice("model_effort", "E1", "旧形状"))
    close_out_quarantined_wake(chain.agent, wake, _DECISION)
    rows = [row.to_dict() for row in pending_host_notices(store, thread_id)]
    assert "details" not in rows[0]
    assert rows[1]["details"][SESSION_MESSAGE_KEY_FIELD] == wake.metadata[SESSION_MESSAGE_KEY_FIELD]


def test_tracker_stall_alert_leaves_a_notice(tmp_path, monkeypatch) -> None:
    """唤醒车道发长时间不计数提醒时，除了运维日志也给会话留一条宿主提示（同原因码合并）。"""
    from agent_py_agent.agent.conversation.wake_attempt_tracking import wake_uncounted_stalled

    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    wake_uncounted_stalled(chain.scheduler, wake, WakeStallAlert("error:transient:ProviderTransientError", 30, 1.0, 1))
    assert [row.code for row in _poison_notices(chain)] == ["error:transient:ProviderTransientError"]


@pytest.mark.parametrize("kind", ["task", "message", "other"])
def test_wake_domain_status_reports_the_terminal_state_only(tmp_path, monkeypatch, kind) -> None:
    """wake_domain_status 是领域终态的唯一判定（第 4 步 /wakes 导入它）：结束时返回任务或回执的状态，没结束返回空串；
    wake_domain_terminal 就是它是否非空。reason 大小写与空白不影响判定。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task" if kind == "task" else "message")
    store = chain.agent.conversation_store
    if kind == "other":
        wake = type(wake)(**{**wake.to_dict(), "reason": "subagent_completed"})
    assert (wake_domain_status(store, wake), wake_domain_terminal(store, wake)) == ("", False)
    if kind == "task":
        close_out_quarantined_wake(chain.agent, wake, _DECISION)
        expected = "failed"
    elif kind == "message":
        store.guidance.mark_status(wake.metadata[SESSION_MESSAGE_KEY_FIELD], "rejected")
        expected = "rejected"
    else:
        expected = ""
    shouted = type(wake)(**{**wake.to_dict(), "reason": f"  {wake.reason.upper()} "})
    assert wake_domain_status(store, shouted) == expected
    assert wake_domain_terminal(store, shouted) is bool(expected)


# 函数用途: 记下 settle_abandoned_turn 对 reject_pending 的调用（回合号与关键字参数），不真的改回执。
def _spy_reject_pending(chain, monkeypatch, *, error: BaseException | None = None) -> list:
    calls: list = []
    recovery = chain.agent.conversation_store.guidance.recovery

    def spy(turn_id, **kwargs):
        calls.append((turn_id, kwargs))
        if error is not None:
            raise error
        return {}

    monkeypatch.setattr(recovery, "reject_pending", spy)
    return calls


@pytest.mark.parametrize("cancelled", [False, True], ids=["task-not-cancelled", "task-cancelled"])
def test_abandoned_turn_releases_the_task_body_only_when_the_task_is_not_cancelled(tmp_path, monkeypatch, cancelled) -> None:
    """C6：死掉那一片的回合按统一规则收尾，派活正文只在任务没被取消时退回（与后台片异常结束同口径），释放不计次。"""
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "task")
    if cancelled:
        task = chain.task("RC-GOAL-DONE")
        chain.agent.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    calls = _spy_reject_pending(chain, monkeypatch)
    settle_abandoned_turn(chain.agent, wake, "stask-dead-turn")
    assert calls == [("stask-dead-turn", {"reject_reserved": True, "release_task_body": not cancelled})]


def test_abandoned_turn_without_a_turn_id_is_not_settled(tmp_path, monkeypatch) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    calls = _spy_reject_pending(chain, monkeypatch)
    settle_abandoned_turn(chain.agent, wake, "  ")
    assert calls == []


def test_abandoned_turn_settle_failure_is_only_logged(tmp_path, monkeypatch, capsys) -> None:
    chain, wake = _chain_with_b_wake(tmp_path, monkeypatch, "message")
    _spy_reject_pending(chain, monkeypatch, error=OSError("disk full"))
    settle_abandoned_turn(chain.agent, wake, "wake-dead-turn")
    assert "wake_abandoned_turn_settle_failed" in capsys.readouterr().out
