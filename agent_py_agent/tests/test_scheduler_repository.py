from __future__ import annotations

import pytest

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


def test_misfire_claim_takeover_finish_and_history(tmp_path) -> None:
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
