from __future__ import annotations

"""HANDOFF P0-4 验收测试: 崩溃执行按进程死亡证明归 unknown(fail-closed)。

HANDOFF_reliability-gaps-20260813.md P0-4: 崩溃/被杀进程留下的 run 只靠
租约过期回收, 无 completed/failed/unknown 三态分类; 缺「owner 进程死亡
证明」(pid 探活)后的结构化归类。进程未证实死亡保持原态(fail-closed 不猜)。
"""

import json
import os

from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRepository


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


def _force_crash_state(repo: SchedulerRepository, run_id: str, *, pid: int) -> None:
    """模拟崩溃残留: 租约过期 + 指定 runner_pid。"""
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0  # 过期(now=1_006 时)
    run["runner_pid"] = pid
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_crashed_run_recovered_to_unknown(tmp_path) -> None:
    """租约过期 + 进程证实死亡(pid 不存在) -> 归 unknown 终态。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    _force_crash_state(repo, run_id, pid=999_999)  # 不存在的进程

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]
    run = repo.get_active_run(run_id)
    assert run is None  # unknown 是终态, 不再 active
    # 终态不可复活: 不能再 claim
    assert repo.claim_run(run_id, lease_seconds=10, now=1_007.0) is None


def test_live_process_keeps_state_fail_closed(tmp_path) -> None:
    """租约过期但进程存活 -> 保持原态(fail-closed 不猜), 不归 unknown。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    _force_crash_state(repo, run_id, pid=os.getpid())  # 当前测试进程存活

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


def test_pid_reuse_detected_by_start_time(tmp_path, monkeypatch) -> None:
    """pid 存活但 start_time 不匹配(pid 复用) -> 原进程已死, 归 unknown。"""
    import agent_py_agent.agent.scheduler.repository as repo_mod

    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = os.getpid()  # pid 存活(复用场景)
    run["runner_start_time"] = 1.0  # 记录的启动时刻(旧)
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 当前进程实际 start_time 与记录不同(模拟 pid 复用): 死亡证明成立
    monkeypatch.setattr(repo_mod, "_process_start_time", lambda pid: 999.0)

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]  # pid 复用 = 原进程已死
    assert repo.get_active_run(run_id) is None  # 归 unknown 终态


def test_unverifiable_oserror_keeps_state_fail_closed(tmp_path, monkeypatch) -> None:
    """os.kill 抛非 ProcessLookupError 的 OSError(不可判定) -> 保持原态。"""
    import agent_py_agent.agent.scheduler.repository as repo_mod

    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = os.getpid()
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    def _raising_kill(pid, sig):  # 非 ProcessLookupError 的 OSError(不可判定)
        raise OSError("unverifiable")

    monkeypatch.setattr(repo_mod.os, "kill", _raising_kill)
    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"  # fail-closed 保持
