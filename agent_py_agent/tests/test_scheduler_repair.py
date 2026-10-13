"""定时账本原子读、崩溃结算、健康与旧创建人迁移的隔离回归。"""

from __future__ import annotations

import errno
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRepository
from agent_py_agent.agent.scheduler.service import SchedulerService


# LLM: 只生成临时 owner 账本；测试不读取真实模型、身份、消息或配置。
# 函数用途: 为原子读取与进程恢复准备一项合法合成监控。
def _case(tmp_path):
    repo = SchedulerRepository(tmp_path / "scheduler", owner_provider="feishu", owner_kind="group",
        owner_id="group-synthetic", default_timezone="UTC")
    job, _ = repo.create_job(SchedulerJobCreateRequest(name="合成监控", prompt="合成检查", thread_id="plan",
        source_thread_id="origin", schedule={"kind": "every", "every_seconds": 60, "anchor_at": 1000},
        creator={"channel": "feishu", "actor_id": "ou-original"}, now=1000))
    return SimpleNamespace(repo=repo, job=job)


# LLM: 仅修改临时账本，模拟明确死亡的租约持有者；不探测或终止真实进程。
# 函数用途: 将一轮合法 running 变成过期死亡记录，给恢复入口提供真实 schema。
def _dead_run(case, monkeypatch, *, now=1061):
    run = case.repo.reserve_due_runs(now=now)[0]
    claim = case.repo.claim_run(run["run_id"], lease_seconds=10, now=now + 1)
    case.repo.mark_run_running(run["run_id"], claim["claim_id"], now=now + 2)
    store = json.loads(case.repo.store_path.read_text())
    store["runs"][run["run_id"]].update(runner_pid=919191, runner_start_time="1", claim_expires_at=now + 11)
    case.repo.store_path.write_text(json.dumps(store))
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    return run


def test_read_queries_need_neither_writable_lock_nor_write_admission(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _dead_run(case, monkeypatch)
    before = case.repo.store_path.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise OSError(errno.EROFS, "合成只读文件系统")

    monkeypatch.setattr(repository_module, "locked_json_path", forbidden)
    monkeypatch.setattr(case.repo.quota_enforcer, "admission", forbidden)
    assert len(case.repo.list_jobs()[0]) == 1
    assert case.repo.get_job(case.job["job_id"])["name"] == "合成监控"
    assert len(case.repo.active_runs_by_job()[0]) == 1
    assert case.repo.queued_runs(now=1080) == []
    assert case.repo.waiting_runs() == ([], [])
    assert case.repo.runtime_snapshot()["health"] == "healthy"
    assert case.repo.corruption_report()["corrupt"] is False
    assert case.repo.store_path.read_bytes() == before


def test_recovery_writes_unknown_history_once_and_preserves_deleted_job(tmp_path, monkeypatch):
    case = _case(tmp_path)
    run = _dead_run(case, monkeypatch)
    case.repo.delete_job(case.job["job_id"], expected_version=None, now=1080)
    assert case.repo.get_active_run(run["run_id"])["status"] == "running"
    assert case.repo.recover_interrupted_executions(now=1090) == [run["run_id"]]
    assert run["run_id"] not in case.repo._store_snapshot()["runs"]
    history, errors = case.repo.history(job_id=case.job["job_id"])
    assert errors == [] and len(history) == 1 and history[0]["status"] == "unknown"
    assert history[0]["claim_epoch"] == 1
    assert history[0]["error_code"] == "SCHEDULER_RUNNER_DIED"
    assert case.repo.get_job(case.job["job_id"], include_deleted=True)["status"] == "deleted"
    assert case.repo.recover_interrupted_executions(now=1100) == []
    assert len(case.repo.history()[0]) == 1 and not case.repo.corruption_report()["corrupt"]


def test_service_startup_and_tick_recover_running_without_replaying_old_run(tmp_path, monkeypatch):
    case = _case(tmp_path)
    run = _dead_run(case, monkeypatch)
    store = SimpleNamespace(threads=SimpleNamespace(load=lambda _id: None))
    monkeypatch.setattr("agent_py_agent.agent.scheduler.service.time.time", lambda: 1090)
    service = SchedulerService(case.repo, conversation_store=store)
    assert case.repo.get_active_run(run["run_id"]) is None
    assert case.repo.history()[0][0]["status"] == "unknown"
    new_run = _dead_run(case, monkeypatch, now=1121)
    service.enqueue_ready_runs(now=1140)
    assert case.repo.get_active_run(new_run["run_id"]) is None
    assert {row["run_id"] for row in case.repo.history()[0]} == {run["run_id"], new_run["run_id"]}


def test_health_reports_invalid_run_instead_of_healthy_zero(tmp_path):
    case = _case(tmp_path)
    store = case.repo._store_snapshot()
    store["runs"]["srun_bad"] = {"schema_version": "scheduler_run.v1", "owner": case.repo.owner}
    case.repo.store_path.write_text(json.dumps(store))
    snapshot = case.repo.runtime_snapshot()
    assert snapshot["health"] == "degraded"
    assert snapshot["load_error_codes"] == ["SCHEDULER_RUN_INVALID"]


def test_legacy_unknown_run_is_moved_to_history_without_reexecuting(tmp_path, monkeypatch):
    case = _case(tmp_path)
    run = _dead_run(case, monkeypatch)
    store = case.repo._store_snapshot()
    store["runs"][run["run_id"]].update(status="unknown", ended_at=1080, updated_at=1080,
        error_code="SYNTHETIC_OPERATION_UNKNOWN", error_message="原动作结果未确认")
    case.repo.store_path.write_text(json.dumps(store))
    assert case.repo.recover_interrupted_executions(now=1090) == [run["run_id"]]
    assert case.repo.history()[0][0]["status"] == "unknown"
    assert case.repo.history()[0][0]["error_code"] == "SYNTHETIC_OPERATION_UNKNOWN"
    assert case.repo.queued_runs(now=1090) == []
    assert case.repo.corruption_report()["corrupt"] is False


def test_store_error_codes_distinguish_json_schema_and_owner(tmp_path):
    case = _case(tmp_path)
    original = case.repo._store_snapshot()
    samples = [(b"{ incomplete", "SCHEDULER_STORE_INVALID_CONTENT"),
        (json.dumps({**original, "schema_version": "bad"}).encode(), "SCHEDULER_STORE_SCHEMA_MISMATCH"),
        (json.dumps({**original, "owner": {"provider": "feishu", "kind": "group", "id": "other"}}).encode(),
         "SCHEDULER_STORE_OWNER_MISMATCH")]
    for content, code in samples:
        case.repo.store_path.write_bytes(content)
        with pytest.raises(repository_module.SchedulerStateError) as caught:
            case.repo.list_jobs()
        assert caught.value.error_code == code


def test_permission_error_and_busy_snapshot_keep_distinct_errno(tmp_path, monkeypatch):
    import agent_py_agent.agent.scheduler.store_snapshot as snapshots

    case = _case(tmp_path)
    original_read = snapshots.read_bytes_beneath

    def unreadable(*_args, **_kwargs):
        raise OSError(errno.EROFS, "合成只读错误")

    monkeypatch.setattr(snapshots, "read_bytes_beneath", unreadable)
    with pytest.raises(repository_module.SchedulerStateError) as caught:
        case.repo.list_jobs()
    assert caught.value.error_code == "SCHEDULER_STORE_UNREADABLE"
    assert caught.value.errno == errno.EROFS
    monkeypatch.setattr(snapshots, "read_bytes_beneath", original_read)
    count = []
    size = case.repo.store_path.stat().st_size

    def changed(_path):
        count.append(1)
        return len(count), size, 1, 1

    monkeypatch.setattr(snapshots, "snapshot_signature", changed)
    with pytest.raises(repository_module.SchedulerStateError) as caught:
        case.repo.list_jobs()
    assert caught.value.error_code == "SCHEDULER_STORE_BUSY" and caught.value.errno == errno.EAGAIN
    assert len(count) == 6, "三次有界复核，不无限等待"


def test_recovery_preserves_terminal_history_committed_before_store_failure(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    case = _case(tmp_path)
    run = _dead_run(case, monkeypatch)
    claim = case.repo.get_active_run(run["run_id"])
    writer = repository_module.write_json_file_atomic_unlocked

    def fail_rename(*_args, **_kwargs):
        raise OSError(errno.EIO, "合成替换失败")

    monkeypatch.setattr(repository_module, "write_json_file_atomic_unlocked", fail_rename)
    with pytest.raises(OSError):
        case.repo.finish_run(run["run_id"], claim["claim_id"], SchedulerRunFinish(status="done", now=1070))
    monkeypatch.setattr(repository_module, "write_json_file_atomic_unlocked", writer)
    assert case.repo.get_active_run(run["run_id"]) is not None
    assert case.repo.recover_interrupted_executions(now=1090) == [run["run_id"]]
    history, errors = case.repo.history()
    assert errors == [] and len(history) == 1 and history[0]["status"] == "done"
    assert case.repo.get_job(case.job["job_id"])["last_run_status"] == "done"


def test_read_queries_complete_while_writer_holds_original_lock(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    case = _case(tmp_path)
    ready, release = threading.Event(), threading.Event()

    def writer():
        with repository_module.locked_json_path(case.repo.store_path):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(2)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            queries = executor.submit(lambda: [case.repo.list_jobs()[0] for _index in range(10)])
            assert len(queries.result(timeout=2)) == 10
    finally:
        release.set()
        thread.join(timeout=2)


# LLM: 冻结来源的 actor 和群会话的最新 actor 故意不同，迁移不得读取最新发言人。
# 函数用途: 创建准确原线程事实替身，验证旧记录迁移的身份来源与越权拒绝。
def _frozen_store(owner, *, foreign=False):
    from agent_py_agent.agent.conversation.execution_threads import EXECUTION_ORIGIN_KEY

    origin = ConversationThread(thread_id="origin", canonical_user_id="ou-later", owner_id=owner,
        owner_home="synthetic-home", channel_bindings=(ChannelBinding(channel="feishu", thread_id="origin", canonical_user_id="ou-later",
            channel_conversation_id="oc-group", channel_user_id="ou-later"),))
    plan = ConversationThread(thread_id="plan", canonical_user_id="ou-later", owner_id=owner,
        owner_home="synthetic-home", metadata={EXECUTION_ORIGIN_KEY: {"source_thread_id": "origin",
            "kind": "scheduled", "operation_key": "request-original", "binding": {"channel": "feishu",
            "conversation_id": "oc-group", "actor_id": "ou-original"}}})
    if foreign:
        origin = replace(origin, owner_id="foreign-owner")
    rows = {"origin": origin, "plan": plan}
    return SimpleNamespace(threads=SimpleNamespace(load=rows.get, require=lambda identifier: rows[identifier]))


@pytest.mark.parametrize("foreign", [False, True])
def test_creator_migration_uses_frozen_actor_or_reports_unmanaged(tmp_path, foreign):
    from agent_py_agent.agent.scheduler.creator_migration import (
        migrate_monitor_creators,
        unmanaged_monitor_ids,
    )

    case = _case(tmp_path)
    store = case.repo._store_snapshot()
    store["jobs"][case.job["job_id"]]["creator"] = None
    case.repo.store_path.write_text(json.dumps(store))
    conversation = _frozen_store(case.repo.owner["id"], foreign=foreign)
    result = migrate_monitor_creators(case.repo, conversation, now=1100)
    current = case.repo.get_job(case.job["job_id"])
    if foreign:
        assert result["unmanaged"] == 1 and current["creator"] is None
        assert unmanaged_monitor_ids(case.repo) == [case.job["job_id"]]
    else:
        assert result["migrated"] == 1
        assert current["creator"] == {"channel": "feishu", "actor_id": "ou-original"}
        assert unmanaged_monitor_ids(case.repo) == []
    before = case.repo.store_path.read_bytes()
    migrate_monitor_creators(case.repo, conversation, now=1200)
    assert case.repo.store_path.read_bytes() == before
