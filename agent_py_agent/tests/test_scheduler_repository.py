from __future__ import annotations

import json

import pytest

import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
    SchedulerStateError,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer, OwnerQuotaExceeded


def _repository(tmp_path, *, owner_id: str = "u-1") -> SchedulerRepository:
    return SchedulerRepository(
        tmp_path / owner_id / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id=owner_id,
        default_timezone="Asia/Shanghai",
    )


def _every(anchor: float = 1_000, seconds: int = 600) -> dict[str, object]:
    return {
        "kind": "every",
        "every_seconds": seconds,
        "anchor_at": anchor,
        "timezone": "UTC",
    }


def _create(
    repository: SchedulerRepository,
    *,
    name: str = "job",
    source_request_id: str = "request:call",
    now: float = 900,
    schedule: dict[str, object] | None = None,
    grace: int | None = None,
):
    return repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"execute {name}",
            thread_id="thread-1",
            source_task_id="task-1",
            schedule=schedule or _every(),
            misfire_grace_seconds=grace,
            skill_refs=[],
            source_request_id=source_request_id,
            now=now,
        )
    )


# LLM: Test-only helpers build a real persisted scheduler run and alter only its isolated claim facts.
# 函数用途: 统一准备到期 run，确保 claim fencing 用例经过真实 repository，而不是手拼模型。
def _claimed_due_run(repository: SchedulerRepository) -> dict[str, object]:
    _create(repository)
    run = repository.reserve_due_runs(now=1_000)[0]
    claimed = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_001)
    assert claimed is not None
    return claimed


# LLM: Mutate only the pytest temporary owner ledger to model an expired runner identity.
# 函数用途: 为死亡证明测试准备可控的 lease/PID/starttime（字符串指纹或旧数字记录），避免依赖真实进程状态。
def _expire_claim(
    repository: SchedulerRepository,
    claim: dict[str, object],
    *,
    runner_pid: int,
    runner_start_time: str | float | None,
) -> None:
    store = json.loads(repository.store_path.read_text(encoding="utf-8"))
    run_id = str(claim["run_id"])
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_011.0
    run["runner_pid"] = runner_pid
    if runner_start_time is None:
        run.pop("runner_start_time", None)
    else:
        run["runner_start_time"] = runner_start_time
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_crud_cas_and_same_tool_call_deduplication(tmp_path) -> None:
    repository = _repository(tmp_path)
    created, deduped = _create(repository)
    repeated, repeated_deduped = _create(repository)
    assert deduped is False
    assert repeated_deduped is True
    assert repeated["job_id"] == created["job_id"]

    # Empty request identities are never treated as a global idempotency key.
    first, _ = _create(repository, name="no-key", source_request_id="")
    second, _ = _create(repository, name="no-key", source_request_id="")
    assert first["job_id"] != second["job_id"]

    updated = repository.update_job(
        str(created["job_id"]),
        patch={"name": "renamed"},
        expected_version=1,
        now=910,
    )
    assert updated["name"] == "renamed"
    assert updated["version"] == 2
    with pytest.raises(SchedulerConflictError):
        repository.pause_job(str(created["job_id"]), expected_version=1, now=920)
    paused = repository.pause_job(str(created["job_id"]), expected_version=2, now=920)
    resumed = repository.resume_job(
        str(created["job_id"]), expected_version=int(paused["version"]), now=930
    )
    deleted = repository.delete_job(
        str(created["job_id"]), expected_version=int(resumed["version"]), now=940
    )
    assert deleted["status"] == "deleted"


def test_past_one_shot_error_reports_current_clock_for_structured_retry(tmp_path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ScheduleValidationError) as raised:
        _create(
            repository,
            now=1_784_423_093,
            schedule={"kind": "at", "at": "1970-01-01T00:01:30Z", "timezone": "UTC"},
        )

    message = str(raised.value)
    assert "1970-01-01T00:01:30Z" in message
    assert "Current time: 2026-07-19T01:04:53Z" in message
    assert "future absolute ISO-8601" in message


def test_due_reservation_advances_before_execution_and_does_not_starve(tmp_path) -> None:
    repository = _repository(tmp_path)
    first, _ = _create(repository, name="first", source_request_id="first")
    second, _ = _create(repository, name="second", source_request_id="second")
    third, _ = _create(repository, name="third", source_request_id="third")
    repository.reserve_manual_run(str(first["job_id"]), now=995)

    reserved = repository.reserve_due_runs(now=1_000, limit=2)
    assert {run["job_id"] for run in reserved} == {second["job_id"], third["job_id"]}
    for run in reserved:
        job = repository.get_job(str(run["job_id"]))
        assert job["next_run_at"] == 1_600
        assert job["version"] == 2


def test_due_reservation_poll_without_state_change_is_read_only(tmp_path, monkeypatch) -> None:
    owner = tmp_path / "u-1"
    repository = SchedulerRepository(
        owner / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        quota_enforcer=OwnerQuotaEnforcer(owner, max_bytes=1024 * 1024),
    )
    job, _deduped = _create(repository)

    # First cover an ordinary poll before the job is due.  Then cover a due
    # job which cannot be selected because it already has an active manual
    # run.  Neither path changes durable scheduler state.
    repository.reserve_manual_run(str(job["job_id"]), now=999)
    original_store = repository.store_path.read_bytes()
    original_mtime = repository.store_path.stat().st_mtime_ns

    def fail_if_quota_scanned(_root):
        raise AssertionError("a no-op scheduler poll must not scan owner quota")

    monkeypatch.setattr(
        "agent_py_agent.agent.user_space.owner_quota.owner_logical_usage_bytes",
        fail_if_quota_scanned,
    )

    assert repository.reserve_due_runs(now=999) == []
    assert repository.reserve_due_runs(now=1_000) == []
    assert repository.store_path.read_bytes() == original_store
    assert repository.store_path.stat().st_mtime_ns == original_mtime


def test_misfire_claim_takeover_finish_and_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    repository = _repository(tmp_path)
    skipped_job, _ = _create(
        repository,
        name="skip",
        source_request_id="skip",
        grace=0,
    )
    assert repository.reserve_due_runs(now=1_001) == []
    skipped = repository.history(job_id=str(skipped_job["job_id"]))[0]
    assert skipped[0]["status"] == "skipped"
    assert skipped[0]["error_code"] == "SCHEDULER_MISFIRE_GRACE_EXPIRED"

    job, _ = _create(repository, name="run", source_request_id="run", grace=100)
    run = repository.reserve_due_runs(now=1_000)[0]
    first_claim = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_001)
    assert first_claim is not None
    assert repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_005) is None
    takeover = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_012)
    assert takeover is not None
    with pytest.raises(SchedulerConflictError):
        repository.finish_run(
            str(run["run_id"]),
            str(first_claim["claim_id"]),
            SchedulerRunFinish(status="done", now=1_013),
        )
    terminal = repository.finish_run(
        str(run["run_id"]),
        str(takeover["claim_id"]),
        SchedulerRunFinish(
            status="done",
            response="finished",
            delivery_status="sent",
            now=1_014,
        ),
    )
    assert terminal["status"] == "done"
    history, errors = repository.history(job_id=str(job["job_id"]))
    assert errors == []
    assert history[0]["response"] == "finished"
    assert repository.get_active_run(str(run["run_id"])) is None


def test_expired_claim_does_not_transfer_while_runner_is_alive(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "7")

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is None
    current = repository.get_active_run(str(first["run_id"]))
    assert current["claim_id"] == first["claim_id"]
    assert current["claim_epoch"] == first["claim_epoch"]


@pytest.mark.parametrize("unverifiable", ["missing_pid", "permission_error", "missing_start_time"])
def test_expired_claim_fails_closed_without_death_proof(tmp_path, monkeypatch, unverifiable) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    if unverifiable == "missing_pid":
        _expire_claim(repository, first, runner_pid=0, runner_start_time=None)
    else:
        _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    if unverifiable == "permission_error":
        def deny_process_probe(_pid, _signal):
            raise PermissionError("process existence cannot be confirmed")

        monkeypatch.setattr(repository_module.os, "kill", deny_process_probe)
        monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: None)
    elif unverifiable == "missing_start_time":
        monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
        monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: None)

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is None
    current = repository.get_active_run(str(first["run_id"]))
    assert current["claim_id"] == first["claim_id"]
    assert current["status"] == "claimed"


def test_dead_runner_is_cas_cleared_before_one_new_epoch_claim(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    writes: list[str] = []
    write_store = repository._write_store_unlocked

    # LLM: 观察真实 repository 持锁写出的中间状态，不替换状态迁移实现。
    # 函数用途: 证明死亡证明先把旧 claim CAS 回 queued，之后才持久化唯一新 epoch。
    def capture_write(store, admission, *, history_records=None):
        writes.append(str(store["runs"][str(first["run_id"])]["status"]))
        return write_store(store, admission, history_records=history_records)

    monkeypatch.setattr(repository, "_write_store_unlocked", capture_write)

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is not None
    assert writes == ["queued", "claimed"]
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1
    assert repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_013) is None


def test_finish_and_heartbeat_reject_claim_epoch_mismatch(tmp_path) -> None:
    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    store = json.loads(repository.store_path.read_text(encoding="utf-8"))
    run = store["runs"][str(claim["run_id"])]
    run["claim_epoch"] = int(run.get("claim_epoch") or 0) + 1
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert repository.heartbeat_run(
        str(claim["run_id"]), str(claim["claim_id"]), lease_seconds=10, now=1_002
    ) is False
    with pytest.raises(SchedulerConflictError):
        repository.finish_run(
            str(claim["run_id"]),
            str(claim["claim_id"]),
            SchedulerRunFinish(status="done", now=1_003),
        )


def test_sleep_past_misfire_grace_keeps_one_skipped_run_per_job(tmp_path) -> None:
    repository = _repository(tmp_path)
    first, _ = _create(repository, name="first", source_request_id="first", grace=30)
    second, _ = _create(repository, name="second", source_request_id="second", grace=30)

    assert repository.reserve_due_runs(now=10_000) == []
    history, errors = repository.history()
    assert errors == []
    assert len(history) == 2
    assert {str(run["job_id"]) for run in history} == {str(first["job_id"]), str(second["job_id"])}
    assert all(run["status"] == "skipped" for run in history)
    assert repository.reserve_due_runs(now=10_001) == []
    assert len(repository.history()[0]) == 2


def test_owner_identity_and_corrupt_store_fail_closed(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    other = SchedulerRepository(
        repository.root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-2",
    )
    with pytest.raises(SchedulerStateError):
        other.list_jobs()

    repository.store_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SchedulerStateError):
        repository.list_jobs()


def test_scheduler_store_and_history_share_owner_quota_admission(tmp_path) -> None:
    owner = tmp_path / "u-1"
    repository = SchedulerRepository(
        owner / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        quota_enforcer=OwnerQuotaEnforcer(owner, max_bytes=1),
    )

    with pytest.raises(OwnerQuotaExceeded):
        _create(repository)

    assert not repository.store_path.exists()
    assert not repository.history_path.exists()
