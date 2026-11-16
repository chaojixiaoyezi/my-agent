# LLM: 用真实 RuntimeRepository 与真实持久化 JSON 文件验证退避窗零 I/O 读盘及到期边界恢复。
# 模块用途: 回归 Astra F4：advance_pending_closeout 在退避窗口内不读取/解码结果文件，且不破坏任何恢复语义。
from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask
from agent_py_agent.agent.subagents.services import runtime_closeout as closeout


def _make_cooling_fixture(
    tmp_path: Path,
    *,
    run_id: str = "child-cooling",
    closeout_state: str = closeout.CLOSEOUT_UNKNOWN_STATUS,
    attempts: int = closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT + 1,
    updated_at: float = 1000.0,
    delivery: str = "pending",
    payload_bytes: int = 262_144,
    db_status: str = "unknown",
    remove_result_file: bool = False,
):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id=run_id, goal="recover closeout")
    if db_status == "unknown":
        with repo.transaction() as conn:
            conn.execute(
                "UPDATE agent_runs SET status = 'unknown' WHERE agent_run_id = ?",
                (chain["agent_run_id"],),
            )
            conn.execute(
                "UPDATE agent_attempts SET status = 'unknown', ended_at = ? WHERE attempt_id = ?",
                (updated_at, chain["attempt_id"]),
            )
    result = SubAgentRunnerResult(
        run_id=run_id,
        dry_run=False,
        ok=True,
        status="DONE",
        verification_status="UNVERIFIED",
        message="m" * payload_bytes,
        turn_end_reason="completed",
    )
    result_path = tmp_path / f"{run_id}-result.json"
    if not remove_result_file:
        result_path.write_text(json.dumps(asdict(result)), encoding="utf-8")
    output_path = tmp_path / f"{run_id}-output.json"
    output_path.write_text(json.dumps({"refs": [f"artifact://{run_id}"], "summary": "s" * 16_384}), encoding="utf-8")
    fact = {
        "schema_version": closeout.RUNTIME_CLOSEOUT_SCHEMA,
        "run_id": run_id,
        "attempt_id": chain["attempt_id"],
        "agent_run_id": chain["agent_run_id"],
        "target_status": "DONE",
        "target_run_status": "done",
        "turn_end_reason": "completed",
        "runner_result_json": str(result_path),
        "output_json": str(output_path),
        "closeout_state": closeout_state,
        "closeout_reason": closeout_state,
        "delivery": delivery,
        "attempts": attempts,
        "created_at": updated_at,
        "updated_at": updated_at,
    }
    task = SubAgentTask(
        id=run_id,
        goal="recover closeout",
        thought="",
        plan=[],
        status="DONE",
        runner_result_json=str(result_path),
        output_json=str(output_path),
        attributes={closeout.RUNTIME_CLOSEOUT_ATTR: deepcopy(fact)},
    )
    return repo, chain, task, fact, result_path, output_path


def _install_io_and_side_effect_spies(monkeypatch, *, repo, result_path: Path, output_path: Path):
    counts = {
        "read_text": 0,
        "load_result": 0,
        "load_output": 0,
        "settle": 0,
        "tx_names": [],
    }
    original_read_text = Path.read_text
    original_load_result = closeout._load_persisted_runner_result
    original_load_output = closeout._load_persisted_output
    original_tx = closeout.gateway_short_transaction

    def spied_read_text(self, *args, **kwargs):
        if self in (result_path, output_path):
            counts["read_text"] += 1
        return original_read_text(self, *args, **kwargs)

    def spied_load_result(fact):
        counts["load_result"] += 1
        return original_load_result(fact)

    def spied_load_output(fact):
        counts["load_output"] += 1
        return original_load_output(fact)

    def spied_tx(name, *args, **kwargs):
        counts["tx_names"].append(name)
        return original_tx(name, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spied_read_text)
    monkeypatch.setattr(closeout, "_load_persisted_runner_result", spied_load_result)
    monkeypatch.setattr(closeout, "_load_persisted_output", spied_load_output)
    monkeypatch.setattr(closeout, "gateway_short_transaction", spied_tx)

    if repo is not None:
        original_settle = repo.settle_agent_run

        def spied_settle(**kwargs):
            counts["settle"] += 1
            return original_settle(**kwargs)

        repo.settle_agent_run = spied_settle

    return counts


# LLM: 退避窗内覆盖全部可重试 closeout_state，断言零磁盘读、零 JSON 解码、零 settle/save/notify。
# 函数用途: 回归 Astra F4 核心不变量：冷却期内的反复轮询不触发任何结果或输出文件的读取与解码。
@pytest.mark.parametrize("state", sorted(closeout.RETRYABLE_CLOSEOUT_STATES))
def test_cooldown_skips_persisted_result_and_output_reads_completely(tmp_path, monkeypatch, state):
    clock = [1010.0]
    monkeypatch.setattr(closeout.time, "time", lambda: clock[0])

    repo, _chain, task, fact, result_path, output_path = _make_cooling_fixture(
        tmp_path,
        closeout_state=state,
        attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT + 1,
        updated_at=1000.0,
    )
    assert result_path.stat().st_size > 256 * 1024
    counts = _install_io_and_side_effect_spies(
        monkeypatch, repo=repo, result_path=result_path, output_path=output_path
    )
    save_calls, notify_calls = [], []

    for tick in range(10):
        clock[0] = 1005.0 + tick
        outcome = closeout.advance_pending_closeout(
            repo,
            task,
            fact,
            save_task=lambda saved: save_calls.append(saved),
            notify_result=lambda *args, **kwargs: notify_calls.append(args) or "delivered",
        )
        assert outcome["advanced"] is False
        assert outcome["state"] == "backoff_wait"
        assert outcome["retry_after_seconds"] > 0

    summary = closeout.recover_pending_closeouts(
        repo,
        load_task=lambda run_id: task,
        save_task=lambda saved: save_calls.append(saved),
        list_tasks=lambda: [task],
        notify_result=lambda *args, **kwargs: notify_calls.append(args) or "delivered",
    )
    assert summary == {
        "runtime_closeouts_recovered": 0,
        "runtime_closeouts_pending": 1,
        "runtime_closeouts_rejected": 0,
        "runtime_closeouts_restored": 0,
    }
    assert counts["read_text"] == 0
    assert counts["load_result"] == 0
    assert counts["load_output"] == 0
    assert counts["settle"] == 0
    assert save_calls == []
    assert notify_calls == []
    assert counts["tx_names"] == ["child_advance_pending_closeout_commit"] * 11
    assert closeout.pending_closeout(task) is not None


# LLM: 到期边界前 1ms 保持零 I/O 退避，恰好到达 interval（含 3600s 封顶）时立即恢复读盘与收口。
# 函数用途: 验证 exact due boundary 零延迟恢复原收口与通知链路。
@pytest.mark.parametrize(
    "attempts,expected_interval",
    [
        (3, 3 * closeout.CLOSEOUT_RETRY_BACKOFF_BASE_SECONDS),
        (100, closeout.CLOSEOUT_RETRY_BACKOFF_MAX_SECONDS),
    ],
)
def test_exact_due_boundary_resumes_load_settle_and_notify(tmp_path, monkeypatch, attempts, expected_interval):
    updated_at = 1000.0
    clock = [updated_at + expected_interval - 0.001]
    monkeypatch.setattr(closeout.time, "time", lambda: clock[0])

    repo, chain, task, fact, result_path, output_path = _make_cooling_fixture(
        tmp_path,
        closeout_state=closeout.CLOSEOUT_WRITE_ERROR,
        attempts=attempts,
        updated_at=updated_at,
        db_status="created",
        payload_bytes=32_768,
    )
    counts = _install_io_and_side_effect_spies(
        monkeypatch, repo=repo, result_path=result_path, output_path=output_path
    )
    save_calls, notify_calls = [], []

    before_due = closeout.advance_pending_closeout(
        repo,
        task,
        fact,
        save_task=lambda saved: save_calls.append(closeout.pending_closeout(saved)),
        notify_result=lambda *args, **kwargs: notify_calls.append(kwargs.get("attempt_id")) or "delivered",
    )
    assert before_due == {"advanced": False, "state": "backoff_wait", "retry_after_seconds": 0.001}
    assert counts["read_text"] == 0 and counts["load_result"] == 0 and counts["settle"] == 0
    assert save_calls == [] and notify_calls == []

    clock[0] = updated_at + expected_interval
    at_due = closeout.advance_pending_closeout(
        repo,
        task,
        fact,
        save_task=lambda saved: save_calls.append(closeout.pending_closeout(saved)),
        notify_result=lambda *args, **kwargs: notify_calls.append(kwargs.get("attempt_id")) or "delivered",
    )
    assert at_due == {"advanced": True, "state": "delivered"}
    assert counts["load_result"] == 1
    assert counts["load_output"] == 1
    assert counts["read_text"] == 2
    assert counts["settle"] == 1
    assert notify_calls == [chain["attempt_id"]]
    assert [entry["delivery"] if entry else "clear" for entry in save_calls] == ["delivered", "clear"]
    assert closeout.pending_closeout(task) is None
    assert repo.agent_run_for_run_id(task.id)["status"] == "done"


# LLM: 非退避事实缺文件须立即报错；已处于冷却中的事实缺文件则在窗内延迟诊断、到期立即显式报告且不清账。
# 函数用途: 验证 missing-result 诊断对非退避与冷却态的双重边界。
def test_missing_result_diagnostics_for_non_cooling_and_cooling_facts(tmp_path, monkeypatch):
    clock = [1050.0]
    monkeypatch.setattr(closeout.time, "time", lambda: clock[0])

    # 1) 非冷却态（attempts 未超阈值）：立即读盘并诊断 runner_result_missing，保持事实。
    repo1, _chain1, task1, fact1, res1, out1 = _make_cooling_fixture(
        tmp_path / "noncooling",
        run_id="child-noncooling",
        attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT,
        updated_at=1000.0,
        remove_result_file=True,
    )
    counts1 = _install_io_and_side_effect_spies(monkeypatch, repo=repo1, result_path=res1, output_path=out1)
    immediate = closeout.advance_pending_closeout(
        repo1,
        task1,
        fact1,
        save_task=lambda saved: pytest.fail("missing result must not overwrite fact"),
        notify_result=lambda *args, **kwargs: pytest.fail("missing result must not notify"),
    )
    assert immediate == {"advanced": False, "state": "runner_result_missing"}
    assert counts1["load_result"] == 1
    assert closeout.pending_closeout(task1) is not None

    # 2) 已在冷却期内的事实缺文件：窗内零 I/O 返回 backoff_wait，到期后立即诊断 runner_result_missing。
    repo2, _chain2, task2, fact2, res2, out2 = _make_cooling_fixture(
        tmp_path / "cooling",
        run_id="child-cooling-missing",
        closeout_state=closeout.CLOSEOUT_WRITE_ERROR,
        attempts=closeout.CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT + 1,
        updated_at=1000.0,
        remove_result_file=True,
    )
    counts2 = _install_io_and_side_effect_spies(monkeypatch, repo=repo2, result_path=res2, output_path=out2)
    cooling = closeout.advance_pending_closeout(
        repo2,
        task2,
        fact2,
        save_task=lambda saved: pytest.fail("cooling missing fact must stay untouched"),
        notify_result=lambda *args, **kwargs: pytest.fail("cooling missing fact must not notify"),
    )
    assert cooling["advanced"] is False and cooling["state"] == "backoff_wait"
    assert counts2["load_result"] == 0 and counts2["read_text"] == 0
    assert closeout.pending_closeout(task2) is not None

    clock[0] = 1000.0 + 3 * closeout.CLOSEOUT_RETRY_BACKOFF_BASE_SECONDS
    due_missing = closeout.advance_pending_closeout(
        repo2,
        task2,
        fact2,
        save_task=lambda saved: pytest.fail("due missing fact must stay preserved"),
        notify_result=lambda *args, **kwargs: pytest.fail("due missing fact must not notify"),
    )
    assert due_missing == {"advanced": False, "state": "runner_result_missing"}
    assert counts2["load_result"] == 1
    assert closeout.pending_closeout(task2) is not None


# LLM: 保持已交付清账立即推进、stale_attempt 拒绝并清账、以及人工 /recover 后冷却到期正常收口。
# 函数用途: 确认退避前置检查不改变交付清账、换代拒绝和 /recover 恢复合同。
def test_preserves_already_delivered_cleanup_stale_rejection_and_manual_recovery(tmp_path, monkeypatch):
    clock = [1020.0]
    monkeypatch.setattr(closeout.time, "time", lambda: clock[0])

    # A) already_delivered（closeout_state=settled）即使 attempts>2 且刚更新也不退避，直接清账不重通知。
    repo_del, _c1, task_del, fact_del, _r1, _o1 = _make_cooling_fixture(
        tmp_path / "delivered",
        run_id="child-delivered",
        closeout_state=closeout.CLOSEOUT_SETTLED,
        attempts=5,
        updated_at=1019.0,
        delivery="delivered",
        db_status="created",
        payload_bytes=1024,
    )
    assert closeout.advance_pending_closeout(
        repo_del,
        task_del,
        fact_del,
        save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: pytest.fail("already_delivered must not notify"),
    ) == {"advanced": True, "state": "already_delivered"}
    assert closeout.pending_closeout(task_del) is None

    # B) /recover + create_attempt 换代后：冷却期内保持零读盘 backoff_wait，到期后拒绝 stale_attempt 并清账。
    repo_stale, chain_stale, task_stale, fact_stale, res_stale, out_stale = _make_cooling_fixture(
        tmp_path / "stale",
        run_id="child-stale",
        closeout_state=closeout.CLOSEOUT_UNKNOWN_STATUS,
        attempts=3,
        updated_at=1000.0,
        payload_bytes=8192,
    )
    assert repo_stale.recover_attempt_unknown(
        chain_stale["attempt_id"], operator="test", effect_disposition="confirmed_noop"
    )["recovered"] is True
    repo_stale.create_attempt(chain_stale["agent_run_id"])
    counts_stale = _install_io_and_side_effect_spies(
        monkeypatch, repo=repo_stale, result_path=res_stale, output_path=out_stale
    )
    assert closeout.advance_pending_closeout(
        repo_stale, task_stale, fact_stale,
        save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: pytest.fail("must not notify during cooldown"),
    )["state"] == "backoff_wait"
    assert counts_stale["read_text"] == 0 and closeout.pending_closeout(task_stale) is not None

    clock[0] = 1000.0 + 3 * closeout.CLOSEOUT_RETRY_BACKOFF_BASE_SECONDS
    rejected = closeout.advance_pending_closeout(
        repo_stale, task_stale, fact_stale,
        save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: pytest.fail("stale attempt must not notify"),
    )
    assert rejected == {"advanced": False, "state": "stale_attempt", "rejected": True}
    assert closeout.pending_closeout(task_stale) is None
    assert repo_stale.agent_run_for_run_id("child-stale")["status"] == "created"

    # C) 人工 /recover 释放 unknown 且无新 attempt：冷却到期后正常收口并通知一次。
    repo_rec, chain_rec, task_rec, fact_rec, res_rec, out_rec = _make_cooling_fixture(
        tmp_path / "recover",
        run_id="child-recover",
        closeout_state=closeout.CLOSEOUT_UNKNOWN_STATUS,
        attempts=3,
        updated_at=1000.0,
        payload_bytes=8192,
    )
    assert repo_rec.recover_attempt_unknown(
        chain_rec["attempt_id"], operator="test", effect_disposition="confirmed_noop"
    )["recovered"] is True
    clock[0] = 1020.0
    assert closeout.advance_pending_closeout(
        repo_rec, task_rec, fact_rec,
        save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: pytest.fail("still cooling"),
    )["state"] == "backoff_wait"
    clock[0] = 1000.0 + 3 * closeout.CLOSEOUT_RETRY_BACKOFF_BASE_SECONDS
    notified = []
    recovered = closeout.advance_pending_closeout(
        repo_rec, task_rec, fact_rec,
        save_task=lambda saved: None,
        notify_result=lambda *args, **kwargs: notified.append(kwargs["attempt_id"]) or "delivered",
    )
    assert recovered == {"advanced": True, "state": "delivered"}
    assert notified == [chain_rec["attempt_id"]]
    assert closeout.pending_closeout(task_rec) is None
    assert repo_rec.agent_run_for_run_id("child-recover")["status"] == "done"


# LLM: 模拟积压队列（50 个大文件冷却中 + 1 个可推进任务），证明扫描只读取那 1 个到期任务。
# 函数用途: 确认多任务积压下冷却任务不产生任何结果文件 I/O 放大。
def test_backlog_sweep_reads_only_eligible_task_and_skips_cooling_tasks(tmp_path, monkeypatch):
    clock = [1010.0]
    monkeypatch.setattr(closeout.time, "time", lambda: clock[0])

    tasks = []
    cooling_paths = set()
    for idx in range(50):
        _r, _c, task, _f, res, out = _make_cooling_fixture(
            tmp_path / f"cool-{idx}",
            run_id=f"cool-{idx}",
            closeout_state=closeout.CLOSEOUT_WRITE_ERROR,
            attempts=5,
            updated_at=1000.0,
            db_status="created",
            payload_bytes=65_536,
        )
        tasks.append(task)
        cooling_paths.update((res, out))

    repo, chain, ready_task, _f, ready_res, ready_out = _make_cooling_fixture(
        tmp_path / "ready",
        run_id="ready-task",
        closeout_state=closeout.CLOSEOUT_WRITE_ERROR,
        attempts=5,
        updated_at=500.0,
        db_status="created",
        payload_bytes=2048,
    )
    tasks.append(ready_task)

    read_paths = []
    original_read_text = Path.read_text

    def track_read_text(self, *args, **kwargs):
        if self in cooling_paths or self in (ready_res, ready_out):
            read_paths.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_read_text)
    notified = []
    summary = closeout.recover_pending_closeouts(
        repo,
        load_task=lambda run_id: pytest.fail("no restore needed"),
        save_task=lambda saved: None,
        list_tasks=lambda: tasks,
        notify_result=lambda task, _res, _out, *, attempt_id: notified.append((task.id, attempt_id)) or "delivered",
    )
    assert summary == {
        "runtime_closeouts_recovered": 1,
        "runtime_closeouts_pending": 50,
        "runtime_closeouts_rejected": 0,
        "runtime_closeouts_restored": 0,
    }
    assert read_paths == [ready_res, ready_out]
    assert notified == [("ready-task", chain["attempt_id"])]
    assert closeout.pending_closeout(ready_task) is None
    assert all(closeout.pending_closeout(t) is not None for t in tasks[:-1])
