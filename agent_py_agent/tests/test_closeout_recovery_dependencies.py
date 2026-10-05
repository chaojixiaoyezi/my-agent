from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask
from agent_py_agent.agent.subagents.services import runtime_closeout as closeout


# LLM: 只在 pytest 临时目录保存本次结果及交付负载；不建立 manager 或运行模型。
# 函数用途: 构造原 WAL 能读取的持久文件，供显式依赖与无数据库恢复测试复用。
def _pending_task(tmp_path, run_id="child", *, delivery="pending"):
    result = SubAgentRunnerResult(
        run_id=run_id, dry_run=False, ok=True, status="DONE", verification_status="UNVERIFIED",
        message="原持久结果", turn_end_reason="completed",
    )
    result_path = tmp_path / f"{run_id}-result.json"
    result_path.write_text(json.dumps(asdict(result)), encoding="utf-8")
    payload = {"refs": [f"artifact://{run_id}"], "summary": "原交付内容"}
    output_path = tmp_path / f"{run_id}-output.json"
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    fact = {
        "schema_version": closeout.RUNTIME_CLOSEOUT_SCHEMA, "run_id": run_id,
        "attempt_id": f"{run_id}-attempt", "agent_run_id": f"{run_id}-runtime",
        "target_status": "DONE", "target_run_status": "done", "turn_end_reason": "completed",
        "runner_result_json": str(result_path), "output_json": str(output_path), "delivery": delivery,
    }
    task = SubAgentTask(
        id=run_id, goal="恢复交接", thought="", plan=[], status="DONE",
        runner_result_json=str(result_path), output_json=str(output_path),
        attributes={closeout.RUNTIME_CLOSEOUT_ATTR: deepcopy(fact)},
    )
    return task, fact, result, payload


@pytest.mark.parametrize("notification", ["delivered", "skipped", "failed", "raises"])
def test_file_mode_passes_exact_persisted_result_to_notification(tmp_path, notification):
    task, fact, result, payload = _pending_task(tmp_path)
    writes = []

    def save_task(saved):
        current = closeout.pending_closeout(saved)
        writes.append(current["delivery"] if current else "clear")

    def notify_result(actual_task, actual_result, actual_payload, *, attempt_id):
        assert actual_task is task
        assert actual_result == result and actual_payload == payload
        assert attempt_id == fact["attempt_id"]
        assert writes == []
        if notification == "raises":
            raise OSError("injected notify failure")
        return notification

    outcome = closeout.advance_pending_closeout(
        None, task, fact, save_task=save_task, notify_result=notify_result,
    )
    if notification in {"failed", "raises"}:
        assert outcome == {"advanced": False, "state": "delivery_failed"}
        assert writes == ["pending"]
    else:
        assert outcome == {"advanced": True, "state": notification}
        assert writes == ["delivered", "clear"]


def test_file_mode_delivered_fact_only_clears_without_notifying(tmp_path):
    task, fact, _, _ = _pending_task(tmp_path, delivery="delivered")
    writes = []
    outcome = closeout.advance_pending_closeout(
        None, task, fact, save_task=lambda saved: writes.append(closeout.pending_closeout(saved)),
        notify_result=lambda *args, **kwargs: pytest.fail("已交付不得重通知"),
    )
    assert writes == [None]
    assert outcome == {"advanced": True, "state": "already_delivered"}


def test_recovery_keeps_missing_result_and_advances_next_task_without_manager(tmp_path):
    missing, missing_fact, _, _ = _pending_task(tmp_path, "missing")
    valid, valid_fact, result, payload = _pending_task(tmp_path, "valid")
    (tmp_path / "missing-result.json").unlink()
    seen = []

    def notify_result(task, current, output, *, attempt_id):
        seen.append(task.id)
        assert current == result and output == payload
        assert attempt_id == valid_fact["attempt_id"]
        return "delivered"

    summary = closeout.recover_pending_closeouts(
        None, load_task=lambda run_id: pytest.fail("无数据库还原不能调用 load"),
        save_task=lambda task: None, list_tasks=lambda: [missing, valid], notify_result=notify_result,
    )
    assert summary == {
        "runtime_closeouts_recovered": 1, "runtime_closeouts_pending": 1,
        "runtime_closeouts_rejected": 0, "runtime_closeouts_restored": 0,
    }
    assert seen == [valid.id]
    assert closeout.pending_closeout(missing) == missing_fact
    assert closeout.pending_closeout(valid) is None


@pytest.mark.parametrize("failure", ["save", "consumed"])
def test_restore_does_not_consume_before_wal_and_reuses_saved_fact(tmp_path, failure):
    task, fact, _, _ = _pending_task(tmp_path)
    task.attributes = {}
    durable = deepcopy(task)
    consumed = False
    failed = False
    calls = []
    event = {
        "agent_run_id": fact["agent_run_id"], "attempt_id": fact["attempt_id"],
        "payload": {key: value for key, value in fact.items() if key != "delivery"},
    }

    def load_task(run_id):
        assert run_id == task.id
        return deepcopy(durable)

    def save_task(saved):
        nonlocal durable, failed
        calls.append("save")
        if failure == "save" and not failed:
            failed = True
            raise OSError("injected WAL failure")
        durable = deepcopy(saved)

    def append_event(**kwargs):
        nonlocal consumed, failed
        assert closeout.pending_closeout(durable)["attempt_id"] == fact["attempt_id"]
        assert kwargs["attempt_id"] == fact["attempt_id"]
        assert kwargs["agent_run_id"] == fact["agent_run_id"]
        assert kwargs["event_type"] == closeout.CLOSEOUT_RESTORED_EVENT
        calls.append("consumed")
        if failure == "consumed" and not failed:
            failed = True
            raise OSError("injected consumption failure")
        consumed = True

    repo = SimpleNamespace(
        pending_events_page=lambda *args, **kwargs: [] if consumed else [event],
        agent_run_for_run_id=lambda run_id: {
            "agent_run_id": fact["agent_run_id"], "current_attempt_id": fact["attempt_id"],
        },
        append_event=append_event,
    )
    assert closeout.restore_unpersisted_closeouts(repo, load_task=load_task, save_task=save_task) == 0
    assert not consumed
    assert calls == (["save"] if failure == "save" else ["save", "consumed"])
    restored = closeout.restore_unpersisted_closeouts(repo, load_task=load_task, save_task=save_task)
    assert restored == (1 if failure == "save" else 0)
    assert consumed
    assert calls == (["save", "save", "consumed"] if failure == "save" else ["save", "consumed", "consumed"])
    assert closeout.restore_unpersisted_closeouts(repo, load_task=load_task, save_task=save_task) == 0


def test_none_repository_and_absent_task_lister_remain_noop():
    def unexpected(*args, **kwargs):
        pytest.fail("无事件与列表时不得读写或通知")

    assert closeout.restore_unpersisted_closeouts(None, load_task=unexpected, save_task=unexpected) == 0
    summary = closeout.recover_pending_closeouts(
        None, load_task=unexpected, save_task=unexpected, list_tasks=None, notify_result=unexpected,
    )
    assert all(value == 0 for value in summary.values())


# ------------------------------------------------------------------ obsfix12
# LLM: 生产每天 400–1000 条 status_conflict 全来自同一个 unknown run：执行器死亡把 run 置
#   unknown（只有人工 /recover 能变），收口 WAL 每 60 秒重试一次 settle，每次都写一条
#   无去重的诊断事件。以下用例用真实 sqlite 运行库造出该形态，钉住四件事：诊断只写一次、
#   重试退避不再空转、人工 /recover 后恢复链能重新推进、旧 WAL 不会误收口新回合。
# 函数用途: 造一条"unknown run + unknown attempt + 待重试收口 WAL"的真实运行库形态。
def _unknown_run_closeout(tmp_path, *, attempts=None, closeout_state="unknown_status"):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id="child-unknown", goal="g")
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = 'unknown' WHERE agent_run_id = ?",
            (chain["agent_run_id"],),
        )
        conn.execute(
            "UPDATE agent_attempts SET status = 'unknown', ended_at = ? WHERE attempt_id = ?",
            (time.time(), chain["attempt_id"]),
        )
    task, fact, _, _ = _pending_task(tmp_path, "child-unknown")
    fact["attempt_id"] = chain["attempt_id"]
    fact["agent_run_id"] = chain["agent_run_id"]
    fact["closeout_state"] = closeout_state
    if attempts is not None:
        fact["attempts"] = attempts
    task.attributes = {closeout.RUNTIME_CLOSEOUT_ATTR: fact}
    return repo, chain, task


# LLM: 修法 A 的验收：同 run 的 unknown 诊断跨多次重试只写一条，重试本身照常发生。
# 函数用途: 验证反复重试 unknown run 时 status_conflict 事件恰好一条。
def test_unknown_status_conflict_event_written_once_across_retries(tmp_path):
    repo, chain, task = _unknown_run_closeout(tmp_path)
    settle_calls = []
    original_settle = repo.settle_agent_run

    def counting_settle(**kwargs):
        settle_calls.append(str(kwargs.get("agent_run_id") or ""))
        return original_settle(**kwargs)

    repo.settle_agent_run = counting_settle

    def run_scan():
        return closeout.recover_pending_closeouts(
            repo, load_task=lambda run_id: task, save_task=lambda saved: None,
            list_tasks=lambda: [task], notify_result=lambda *args, **kwargs: "delivered",
        )

    # 三次连跑：attempts 从 0 涨到 3，前三次都未过退避阈值，三次都真的重试。
    for _ in range(3):
        run_scan()

    assert len(settle_calls) == 3, settle_calls
    events = repo.events_for_attempt(chain["attempt_id"], limit=100)
    conflicts = [event for event in events if event["event_type"] == "status_conflict"]
    assert len(conflicts) == 1, conflicts
    fact = closeout.pending_closeout(task)
    assert fact is not None and int(fact["attempts"]) == 3


# LLM: 修法 B 的验收：超过重试阈值的可重试形态按 attempts 拉长间隔，窗口内不再 settle。
# 函数用途: 验证退避窗口内不再尝试 settle，事实保留且诊断不增长。
def test_unknown_closeout_retry_backoff_stops_repeated_settle(tmp_path):
    repo, chain, task = _unknown_run_closeout(
        tmp_path, attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT
    )
    settle_calls = []
    original_settle = repo.settle_agent_run

    def counting_settle(**kwargs):
        settle_calls.append(str(kwargs.get("agent_run_id") or ""))
        return original_settle(**kwargs)

    repo.settle_agent_run = counting_settle

    def run_scan():
        return closeout.recover_pending_closeouts(
            repo, load_task=lambda run_id: task, save_task=lambda saved: None,
            list_tasks=lambda: [task], notify_result=lambda *args, **kwargs: "delivered",
        )

    first, second, third = run_scan(), run_scan(), run_scan()

    # 第 1 次照常重试（attempts=2 未过阈值），此后 attempts=3 进入退避：第 2/3 次不再 settle。
    assert len(settle_calls) == 1, settle_calls
    assert first["runtime_closeouts_pending"] == 1, first
    assert second["runtime_closeouts_pending"] == 1 and third["runtime_closeouts_pending"] == 1
    fact = closeout.pending_closeout(task)
    assert fact is not None and int(fact["attempts"]) == 3
    outcome = closeout.advance_pending_closeout(
        repo, task, fact, save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: pytest.fail("退避期间不得通知"),
    )
    assert outcome["state"] == "backoff_wait", outcome
    assert outcome["retry_after_seconds"] > 0
    events = repo.events_for_attempt(chain["attempt_id"], limit=100)
    conflicts = [event for event in events if event["event_type"] == "status_conflict"]
    assert len(conflicts) == 1, conflicts


# LLM: 人工 /recover 是 unknown 的唯一出口；恢复后恢复链必须能重新推进（退避最多延迟
#   1 小时，不能永远挡住）。
# 函数用途: 验证 /recover 释放 unknown 后收口 WAL 能成功推进、清账并通知一次。
def test_manual_recovery_resumes_closeout_after_backoff_window(tmp_path):
    repo, chain, task = _unknown_run_closeout(
        tmp_path, attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT
    )
    notified = []

    def run_scan():
        return closeout.recover_pending_closeouts(
            repo, load_task=lambda run_id: task, save_task=lambda saved: None,
            list_tasks=lambda: [task],
            notify_result=lambda actual, result, payload, *, attempt_id: (
                notified.append(attempt_id) or "delivered"
            ),
        )

    run_scan()
    assert closeout.pending_closeout(task) is not None
    recovered = repo.recover_attempt_unknown(
        chain["attempt_id"], operator="test", effect_disposition="confirmed_noop",
    )
    assert recovered["recovered"] is True, recovered
    assert repo.agent_run_for_run_id("child-unknown")["status"] == "created"
    # 模拟退避窗口已过：把事实的刷新时间拨到过去，恢复链下一巡即可推进。
    fact = closeout.pending_closeout(task)
    fact["updated_at"] = 0.0
    task.attributes = {closeout.RUNTIME_CLOSEOUT_ATTR: fact}

    summary = run_scan()

    assert summary["runtime_closeouts_recovered"] == 1, summary
    assert repo.agent_run_for_run_id("child-unknown")["status"] == "done"
    assert closeout.pending_closeout(task) is None
    assert notified == [chain["attempt_id"]]


# LLM: 旧 WAL 的 exact attempt 在换代后失去执行权；恢复链必须拒绝它并清账，
#   绝不能把新回合的 run 按旧结果收口。
# 函数用途: 验证 /recover 后新回合开始时旧 WAL 被拒（stale_attempt）且 run 不被误收口。
def test_stale_wal_after_new_attempt_is_rejected_not_settled(tmp_path):
    repo, chain, task = _unknown_run_closeout(
        tmp_path, attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT
    )
    def run_scan():
        return closeout.recover_pending_closeouts(
            repo, load_task=lambda run_id: task, save_task=lambda saved: None,
            list_tasks=lambda: [task], notify_result=lambda *args, **kwargs: "delivered",
        )

    run_scan()
    recovered = repo.recover_attempt_unknown(
        chain["attempt_id"], operator="test", effect_disposition="confirmed_noop",
    )
    assert recovered["recovered"] is True, recovered
    repo.create_attempt(chain["agent_run_id"])
    fact = closeout.pending_closeout(task)
    fact["updated_at"] = 0.0
    task.attributes = {closeout.RUNTIME_CLOSEOUT_ATTR: fact}

    summary = run_scan()

    assert summary["runtime_closeouts_rejected"] == 1, summary
    assert repo.agent_run_for_run_id("child-unknown")["status"] == "created"
    assert closeout.pending_closeout(task) is None
    events = repo.events_for_attempt(chain["attempt_id"], limit=100)
    blocked = [event for event in events if event["event_type"] == "closeout_blocked"]
    assert blocked, events
