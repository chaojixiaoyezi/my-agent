"""SUB-35: restored active scheduler snapshots must not replay canonical terminal runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
    SchedulerStateError,
)


# LLM: Tests use a private canonical owner layout and real repository locks/history, never production data.
# 函数用途: 创建独立的真实调度仓库。
def _repository(root: Path) -> SchedulerRepository:
    return SchedulerRepository(root, owner_provider="local", owner_kind="main", owner_id="local/main")


# LLM: Public transitions produce the backup before a real terminal commit; old claims deliberately precede terminal epoch.
# 函数用途: 完成一次真实运行，同时保留指定活动阶段的旧 store 字节。
def _finished_backup(tmp_path, state, status="done"):
    repository = _repository(tmp_path / "owner" / "data" / "scheduler")
    job, _ = repository.create_job(SchedulerJobCreateRequest(
        name="older-restore", prompt="execute once", thread_id="thread-restore",
        schedule={"kind": "every", "every_seconds": 60, "anchor_at": 1000, "timezone": "UTC"},
        misfire_grace_seconds=600, now=900,
    ))
    run = repository.reserve_due_runs(now=1000)[0]
    backups = {"queued": repository.store_path.read_bytes()}
    claim = repository.claim_run(str(run["run_id"]), lease_seconds=10000, now=1001)
    assert claim is not None
    backups["claimed"] = repository.store_path.read_bytes()
    repository.mark_run_running(str(run["run_id"]), str(claim["claim_id"]), now=1002)
    backups["running"] = repository.store_path.read_bytes()
    if state == "waiting":
        repository.park_run_waiting(str(run["run_id"]), str(claim["claim_id"]), now=1003)
        backups["waiting"] = repository.store_path.read_bytes()
        repository.finish_waiting_run(str(run["run_id"]), SchedulerRunFinish(status=status, now=1004))
    else:
        repository.finish_run(str(run["run_id"]), str(claim["claim_id"]), SchedulerRunFinish(status=status, now=1004))
    return repository, str(job["job_id"]), str(run["run_id"]), backups[state]


# LLM: An operator can replace the inode or copy bytes in place; cached and cold objects must observe the restored version.
# 函数用途: 把旧 store 原样放回，不改变仍然存在的 history。
def _restore(repository, backup, cold):
    if cold:
        staged = repository.store_path.with_name("older-store.json")
        staged.write_bytes(backup)
        staged.replace(repository.store_path)
        return _repository(repository.root)
    repository.store_path.write_bytes(backup)
    return repository


# LLM: A failed claim is the execution admission boundary; unchanged history bytes prove reconciliation did not reappend a result.
# 函数用途: 旧排队/领取副本不再得到第二份执行权，所有规范终态都只留一条历史。
@pytest.mark.parametrize("state", ["queued", "claimed"])
@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "skipped", "unknown"])
@pytest.mark.parametrize("cold", [False, True])
def test_restored_finished_run_is_not_claimed_again(tmp_path, state, status, cold):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, state, status)
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, cold)
    assert repository.claim_run(run_id, lease_seconds=30, now=1005) is None
    assert repository.claim_run(run_id, lease_seconds=30, now=1006) is None
    assert repository.get_active_run(run_id) is None
    assert run_id not in json.loads(repository.store_path.read_bytes())["runs"]
    assert repository.history_path.read_bytes() == history
    rows, errors = repository.history()
    assert not errors and [(row["run_id"], row["status"]) for row in rows] == [(run_id, status)]


# LLM: Live-looking running/claimed leases and waiting rows must not block a new manual or due reservation once terminal history exists.
# 函数用途: 不靠租约过期或进程死亡，也能清掉旧活跃副本并继续预约同一任务。
@pytest.mark.parametrize("state", ["queued", "claimed", "running", "waiting"])
@pytest.mark.parametrize("entry", ["manual", "due", "recovery"])
def test_restored_terminal_run_does_not_block_job(tmp_path, state, entry):
    repository, job_id, run_id, backup = _finished_backup(tmp_path, state)
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, False)
    if entry == "recovery":
        repository.recover_interrupted_executions(now=1005)
        assert repository.get_active_run(run_id) is None
        reserved = repository.reserve_manual_run(job_id, now=1006)
    elif entry == "manual":
        reserved = repository.reserve_manual_run(job_id, now=1005)
    else:
        runs = repository.reserve_due_runs(now=1060)
        assert len(runs) == 1
        reserved = runs[0]
    assert reserved["job_id"] == job_id and reserved["run_id"] != run_id
    assert repository.get_active_run(run_id) is None
    assert repository.history_path.read_bytes() == history


# LLM: Missing canonical history supplies no terminal proof; normal claim and active-job exclusion remain unchanged.
# 函数用途: 历史被删或没有命中时，不把仍可运行的旧记录误清理。
@pytest.mark.parametrize("history_kind", ["missing", "unrelated", "nonterminal", "invalid", "foreign_owner", "foreign_job", "foreign_thread"])
def test_history_without_matching_valid_terminal_does_not_clear_run(tmp_path, history_kind):
    repository, job_id, run_id, backup = _finished_backup(tmp_path, "queued")
    terminal = json.loads(repository.history_path.read_bytes())
    if history_kind == "missing":
        repository.history_path.unlink()
    else:
        if history_kind == "unrelated":
            terminal["run_id"] = "srun_unrelated"
        elif history_kind == "nonterminal":
            terminal["status"] = "queued"
        elif history_kind == "invalid":
            terminal = {"run_id": run_id, "status": "done"}
        elif history_kind == "foreign_owner":
            terminal["owner"] = {**terminal["owner"], "id": "other-owner"}
        elif history_kind == "foreign_job":
            terminal["job_id"] = "job_other"
        else:
            terminal["thread_id"] = "thread-other"
        repository.history_path.write_text(json.dumps(terminal) + "\n")
    repository = _restore(repository, backup, False)
    history = repository.history_path.read_bytes() if repository.history_path.exists() else None
    with pytest.raises(SchedulerConflictError, match="active run"):
        repository.reserve_manual_run(job_id, now=1005)
    claim = repository.claim_run(run_id, lease_seconds=30, now=1005)
    assert claim is not None and claim["run_id"] == run_id
    assert repository.history_path.read_bytes() == history if history is not None else not repository.history_path.exists()


# LLM: Unreadable source bytes cannot authorize a terminal-membership decision or overwrite the restored snapshot.
# 函数用途: 历史不可读时保持失败关闭，不先清活动记录再发现错误。
def test_unreadable_history_does_not_mutate_restored_store(tmp_path):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "queued")
    with repository.history_path.open("ab") as handle:
        handle.write(b"\xff\n")
    repository = _restore(repository, backup, False)
    with pytest.raises(SchedulerStateError, match="history is unreadable"):
        repository.claim_run(run_id, lease_seconds=30, now=1005)
    assert repository.store_path.read_bytes() == backup


# LLM: Failed explicit version checks must precede any reconciliation commit.
# 函数用途: 错误版本的恢复请求不落盘清理。
def test_scoped_recovery_rejects_wrong_version_before_writing(tmp_path):
    repository, job_id, _run_id, backup = _finished_backup(tmp_path, "running")
    repository = _restore(repository, backup, False)
    with pytest.raises(SchedulerConflictError, match="version changed"):
        repository.recover_interrupted_executions(now=1005, job_id=job_id, expected_version=999)
    assert repository.store_path.read_bytes() == backup


# LLM: Two genuine histories share one owner; an explicit job control may settle only its selected run.
# 函数用途: 同 owner 多任务恢复仍保留 job 选择边界，并返回完成的运行编号。
def test_scoped_recovery_only_settles_selected_job(tmp_path):
    repository, job_id, run_id, backup = _finished_backup(tmp_path / "one", "running")
    other, _other_job, other_run, other_backup = _finished_backup(tmp_path / "two", "running")
    store = json.loads(backup)
    second = json.loads(other_backup)
    store["jobs"].update(second["jobs"])
    store["runs"].update(second["runs"])
    repository.history_path.write_bytes(repository.history_path.read_bytes() + other.history_path.read_bytes())
    repository = _restore(repository, json.dumps(store).encode(), False)
    version = int(store["jobs"][job_id]["version"])
    assert repository.recover_interrupted_executions(now=1005, job_id=job_id, expected_version=version) == [run_id]
    assert repository.get_active_run(run_id) is None
    assert repository.get_active_run(other_run) is not None
    assert repository.get_job(job_id)["last_run_status"] == "done"


# LLM: The existing synthetic runtime creates the real authenticated fallback marker, then the original queued bytes are restored.
# 函数用途: 合法来源线程兜底后，旧队列副本也不能重新领取；没有真实模型或网络调用。
@pytest.mark.parametrize("mode", ["followup", "isolated"])
def test_restored_pre_fallback_run_is_not_reclaimed(tmp_path, mode):
    from agent_py_agent.tests.test_v13_w1_scheduler_thread_quota import fallback_case

    case = fallback_case(tmp_path, mode=mode)
    repository = case.agent.scheduler_repository
    run_id = str(repository.reserve_due_runs(now=1000)[0]["run_id"])
    backup = repository.store_path.read_bytes()
    assert case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1000)
    rows, errors = repository.history()
    assert not errors and rows[0]["status"] == "done"
    assert rows[0]["thread_fallback"]["original_thread_id"] == case.source.thread_id
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, False)
    assert repository.claim_run(run_id, lease_seconds=30, now=1005) is None
    assert repository.get_active_run(run_id) is None
    assert repository.history_path.read_bytes() == history


# LLM: Heartbeats have no replay-admission authority and must not scan unrelated lifetime history.
# 函数用途: 续租不因这轮修复新增历史查询或整账扫描。
def test_normal_heartbeat_does_not_query_history(tmp_path, monkeypatch):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "claimed")
    repository.history_path.unlink()
    repository = _restore(repository, backup, False)
    claim = repository.get_active_run(run_id)

    def forbidden(_requested):
        pytest.fail("heartbeat must not query terminal history")

    monkeypatch.setattr(repository, "_history_membership_unlocked", forbidden)
    assert repository.heartbeat_run(run_id, str(claim["claim_id"]), lease_seconds=30, now=1005)
