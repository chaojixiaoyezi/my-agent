# LLM: Deletion recovery exercises one cached repository and the real scheduler CAS/history paths; no mock may hide a lost file or directory.
# 模块用途: 验证调度历史文件、SQLite 投影及其父目录被删除后按丢失范围自愈，同时保留损坏和链接故障的失败关闭。
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerNotFoundError,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.scheduler.schedule import build_schedule


# LLM: Each test owns a private scheduler tree and keeps the same repository object across the injected fault.
# 函数用途: 建立真实 owner/data/scheduler 目录和缓存仓库实例。
def _repository(tmp_path: Path) -> SchedulerRepository:
    owner = tmp_path / "owner"
    owner.mkdir()
    return SchedulerRepository(
        owner / "data" / "scheduler",
        owner_provider="local",
        owner_kind="main",
        owner_id="local/main",
    )


# LLM: Recreated jobs use the public writer so directory recovery is covered rather than synthesized by the test.
# 函数用途: 创建一个每分钟到期的真实调度任务。
def _create_job(repository: SchedulerRepository, *, now: float, name: str) -> str:
    schedule = build_schedule(
        kind="every",
        every_seconds=60,
        default_timezone="UTC",
        now=now,
    )
    job, deduped = repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt="resilience probe",
            thread_id="thread-res-w14",
            source_request_id=f"request-{name}",
            schedule=schedule,
            misfire_grace_seconds=600,
            now=now,
        )
    )
    assert deduped is False
    return str(job["job_id"])


# LLM: The helper traverses the production reserve/claim/running/finish CAS sequence and returns only persisted identities.
# 函数用途: 在指定时间完成一次真实到期运行。
def _finish_due_run(repository: SchedulerRepository, *, now: float) -> str:
    reserved = repository.reserve_due_runs(now=now)
    assert len(reserved) == 1
    run_id = str(reserved[0]["run_id"])
    claim = repository.claim_run(run_id, lease_seconds=300, now=now + 1)
    assert claim is not None
    claim_id = str(claim["claim_id"])
    repository.mark_run_running(run_id, claim_id, now=now + 2)
    terminal = repository.finish_run(
        run_id,
        claim_id,
        SchedulerRunFinish(status="done", now=now + 3),
    )
    assert terminal["status"] == "done"
    return run_id


# LLM: Maintenance stays on the existing bounded backfill API and never reads the ledger directly to force completion.
# 函数用途: 推进有限批次直到历史投影完整，并返回最后状态。
def _maintain_until_complete(repository: SchedulerRepository) -> dict[str, object]:
    for _attempt in range(32):
        result = repository.maintain_history_index()
        if result.get("complete"):
            return result
    pytest.fail(f"scheduler history maintenance did not complete: {result}")


# LLM: SQLite sidecars belong to the disposable projection; removing all of them models the operator fault exactly.
# 函数用途: 删除历史索引主文件和可能残留的 WAL/SHM/journal。
def _delete_index(index_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{index_path}{suffix}")
        if candidate.exists() or candidate.is_symlink():
            candidate.unlink()


# LLM: A cached object must recover only the deleted layer and keep completing new runs without a process restart.
# 函数用途: 覆盖索引、history、scheduler 目录和 data 目录四种运行时删除。
@pytest.mark.parametrize("fault", ["index_file", "history_file", "scheduler_dir", "data_dir"])
def test_cached_repository_self_heals_deleted_scheduler_history_layers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    repository = _repository(tmp_path)
    _create_job(repository, now=1_000, name="before-fault")
    first_run = _finish_due_run(repository, now=1_061)
    caplog.clear()
    caplog.set_level("WARNING")

    if fault == "index_file":
        _delete_index(repository._history_index.path)
    elif fault == "history_file":
        repository.history_path.unlink()
    elif fault == "scheduler_dir":
        shutil.rmtree(repository.root)
        _create_job(repository, now=1_100, name="after-scheduler-delete")
    else:
        shutil.rmtree(repository.root.parent)
        _create_job(repository, now=1_100, name="after-data-delete")

    second_run = _finish_due_run(repository, now=1_200)
    third_run = _finish_due_run(repository, now=1_300)
    assert _maintain_until_complete(repository)["complete"] is True

    rows, errors = repository.history()
    assert errors == []
    listed = {str(row["run_id"]) for row in rows}
    assert {second_run, third_run} <= listed
    if fault == "index_file":
        assert first_run in listed
    active, active_errors = repository.active_runs_by_job()
    assert active == {}
    assert active_errors == []

    messages = [record.getMessage() for record in caplog.records]
    if fault == "index_file":
        assert sum(message.startswith("SQLITE_SCHEMA_REINSTALLED store=scheduler_history ") for message in messages) == 1
    else:
        assert sum(message.startswith("SCHEDULER_HISTORY_SOURCE_MISSING ") for message in messages) == 1


# LLM: Reinstalling a lost projection must expose every surviving canonical row only after bounded maintenance completes.
# 函数用途: 验证三条旧历史在索引删除后全部重建。
def test_deleted_history_index_backfills_all_surviving_runs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _repository(tmp_path)
    _create_job(repository, now=1_000, name="backfill")
    expected = {
        _finish_due_run(repository, now=1_061),
        _finish_due_run(repository, now=1_200),
        _finish_due_run(repository, now=1_300),
    }
    caplog.clear()
    caplog.set_level("WARNING")
    _delete_index(repository._history_index.path)

    assert _maintain_until_complete(repository)["complete"] is True
    rows, errors = repository.history()

    assert errors == []
    assert {str(row["run_id"]) for row in rows} == expected
    messages = [record.getMessage() for record in caplog.records]
    assert sum(message.startswith("SQLITE_SCHEMA_REINSTALLED store=scheduler_history ") for message in messages) == 1


# LLM: A deleted canonical history is accepted as empty once, but claim CAS still prevents a terminal row from being written twice.
# 函数用途: 验证 history 重建后每个完成运行恰有一条终态记录。
def test_recreated_history_keeps_one_terminal_row_per_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _create_job(repository, now=1_000, name="dedupe")
    _finish_due_run(repository, now=1_061)
    repository.history_path.unlink()

    reserved = repository.reserve_due_runs(now=1_200)
    assert len(reserved) == 1
    run_id = str(reserved[0]["run_id"])
    claim = repository.claim_run(run_id, lease_seconds=300, now=1_201)
    assert claim is not None
    claim_id = str(claim["claim_id"])
    repository.mark_run_running(run_id, claim_id, now=1_202)
    result = SchedulerRunFinish(status="done", now=1_203)
    repository.finish_run(run_id, claim_id, result)
    with pytest.raises(SchedulerNotFoundError):
        repository.finish_run(run_id, claim_id, result)

    rows = [json.loads(line) for line in repository.history_path.read_text(encoding="utf-8").splitlines()]
    assert [str(row.get("run_id") or "") for row in rows].count(run_id) == 1


# LLM: Reinstall is allowed only for absence; alternate filesystem identities remain invalid even on writable calls.
# 函数用途: 验证历史索引软链接和多硬链接继续失败关闭。
@pytest.mark.parametrize("fault", ["symlink", "hardlink"])
def test_history_index_link_faults_still_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    repository = _repository(tmp_path)
    index_path = repository._history_index.path
    sibling = index_path.with_name(f"{index_path.name}.{fault}")
    if fault == "symlink":
        index_path.rename(sibling)
        index_path.symlink_to(sibling)
    else:
        try:
            os.link(index_path, sibling)
        except PermissionError:
            original_lstat = Path.lstat

            # LLM: Some test sandboxes forbid link(2); only that host limit is substituted, while production _connect remains real.
            # 函数用途: 让目标索引的 lstat 精确呈现第二硬链接，其余路径继续读取真实元数据。
            def two_links(path: Path):
                info = original_lstat(path)
                if path != index_path:
                    return info
                fields = list(info)
                fields[3] = 2
                return os.stat_result(fields)

            monkeypatch.setattr(Path, "lstat", two_links)

    with pytest.raises(OSError, match="invalid history index file"):
        repository._history_index._connect()
