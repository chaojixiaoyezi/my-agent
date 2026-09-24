from __future__ import annotations

import json
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
