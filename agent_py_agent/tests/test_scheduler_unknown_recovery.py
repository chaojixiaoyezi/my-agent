from __future__ import annotations

"""SLP-2A scheduler claim 栅栏回归：租约过期后只有死亡证明才清除旧 claim。

活着或身份不可核验时保留原 claim；死亡后先转 queued，再用新 epoch 唯一领取。
"""

import json

import pytest

import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)


def _repository(tmp_path) -> SchedulerRepository:
    return SchedulerRepository(
        tmp_path / "u-1" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        default_timezone="Asia/Shanghai",
    )


def _every(anchor: float = 1_000, seconds: int = 600) -> dict[str, object]:
    return {"kind": "every", "every_seconds": seconds, "anchor_at": anchor}


def _make_claimed_run(repo: SchedulerRepository) -> str:
    repo.create_job(
        SchedulerJobCreateRequest(
            name="任务",
            prompt="run",
            thread_id="th-1",
            source_task_id="t-1",
            schedule=_every(anchor=1_000, seconds=60),
            now=1_000.0,
        )
    )
    due = repo.reserve_due_runs(now=1_061.0)  # 刚 due(lateness=1<grace), 避开 misfire
    run_id = str(due[0]["run_id"])
    repo.claim_run(run_id, lease_seconds=10, now=1_062.0)
    return run_id


# LLM: Change only a temporary scheduler ledger so recovery tests use deterministic process identities.
# 函数用途: 在隔离存储里注入过期 lease 与假 PID/starttime，不探测真实 runner。
def _force_crash_state(repo: SchedulerRepository, run_id: str, *, pid: int) -> None:
    """模拟崩溃残留: 租约过期 + 指定 runner_pid。"""
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0  # 过期(now=1_006 时)
    run["runner_pid"] = pid
    run["runner_start_time"] = 7.0
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_dead_runner_is_cleared_then_reclaimed_with_one_new_epoch(tmp_path, monkeypatch) -> None:
    """死亡证明先清除旧 claim；只有一次新 epoch 可重新领取。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]
    assert repo.recover_interrupted_executions(now=1_006.5) == [], "第二个回收者看到已清成 queued，不再动"
    queued = repo.get_active_run(run_id)
    assert queued is not None
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    assert queued["claim_epoch"] == first["claim_epoch"]

    replacement = repo.claim_run(run_id, lease_seconds=10, now=1_007.0)
    assert replacement is not None
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1
    assert repo.claim_run(run_id, lease_seconds=10, now=1_008.0) is None
    assert repo.heartbeat_run(run_id, str(first["claim_id"]), lease_seconds=10, now=1_009) is False
    with pytest.raises(SchedulerConflictError):
        repo.finish_run(
            run_id,
            str(first["claim_id"]),
            SchedulerRunFinish(status="done", now=1_010),
        )
    assert repo.get_active_run(run_id) == replacement


# 函数用途: 已 mark running 的 run 执行者死亡后落 unknown 终态，不重新排队、不能再领取（SLP-2B 接线前不自动重跑）。
@pytest.mark.parametrize("entry", ["recover", "claim"])
def test_dead_runner_after_running_becomes_unknown_not_requeued(tmp_path, monkeypatch, entry) -> None:
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    repo.mark_run_running(run_id, str(first["claim_id"]), now=1_063.0)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    if entry == "recover":
        assert repo.recover_interrupted_executions(now=1_006.0) == [run_id]
    else:
        assert repo.claim_run(run_id, lease_seconds=10, now=1_006.0) is None
    assert repo.get_active_run(run_id) is None, "unknown 是终态"
    assert repo.claim_run(run_id, lease_seconds=10, now=1_007.0) is None
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    assert store["runs"][run_id]["status"] == "unknown"


def test_live_process_keeps_state_fail_closed(tmp_path, monkeypatch) -> None:
    """租约过期但进程存活 -> 保持原态(fail-closed 不猜), 不归 unknown。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "_process_start_time", lambda _pid: 7.0)

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == []  # 未证实死亡, 不动
    run = repo.get_active_run(run_id)
    assert run is not None
    assert run["status"] == "claimed"  # 状态不变


def test_clean_store_no_recovery(tmp_path) -> None:
    """无过期 claim -> 无恢复动作(无噪音)。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)  # claim 未过期(lease=10, expires=1012)
    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == []
    assert repo.get_active_run(run_id)["status"] == "claimed"


# ---------------------------------------------------------------- P1-4 收口(seq1562): fail-closed 三情形

def test_missing_pid_keeps_state_fail_closed(tmp_path) -> None:
    """runner_pid 缺失 -> 不可证实, 保持原态(不归 unknown)。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0  # 过期
    run.pop("runner_pid", None)  # 身份缺失
    run.pop("runner_start_time", None)
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"  # fail-closed 保持


def test_pid_reuse_detected_by_start_time_then_requeued(tmp_path, monkeypatch) -> None:
    """pid 存活但 start_time 不匹配(pid 复用) -> 旧 claim 清成 queued。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 71  # pid 存活(复用场景)
    run["runner_start_time"] = 1.0  # 记录的启动时刻(旧)
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 当前进程实际 start_time 与记录不同(模拟 pid 复用): 死亡证明成立
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "_process_start_time", lambda _pid: 999.0)

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]  # pid 复用 = 原进程已死
    queued = repo.get_active_run(run_id)
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    replacement = repo.claim_run(run_id, lease_seconds=10, now=1_007.0)
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1


def test_unverifiable_oserror_keeps_state_fail_closed(tmp_path, monkeypatch) -> None:
    """os.kill 抛非 ProcessLookupError 的 OSError(不可判定) -> 保持原态。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 71
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    def _raising_kill(pid, sig):  # 非 ProcessLookupError 的 OSError(不可判定)
        raise OSError("unverifiable")

    monkeypatch.setattr(repository_module.os, "kill", _raising_kill)
    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"  # fail-closed 保持
