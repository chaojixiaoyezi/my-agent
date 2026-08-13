from __future__ import annotations

"""HANDOFF P1-3 验收测试: 调度账本坏记录逐条隔离 + 结构化损坏报告。

HANDOFF_reliability-gaps-20260813.md P1-3: store.json 单条坏记录不得瘫痪
整 owner 调度; 坏记录跳过并记入结构化损坏报告(参照 jsonl 容错模式),
不覆盖原文件, 健康记录照常调度。
"""

import json

from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
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
    return {
        "kind": "every",
        "every_seconds": seconds,
        "anchor_at": anchor,
    }


def _inject_corruption(repo: SchedulerRepository) -> None:
    """向 store.json 注入一条坏 job + 一条坏 run(模拟截断写/手改)。"""
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    store["jobs"]["job_bad"] = "not-a-dict"  # 坏 job: 非对象
    store["runs"]["srun_bad"] = {"schema_version": 999}  # 坏 run: schema 不符
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_healthy_job_survives_corrupt_neighbor(tmp_path) -> None:
    """含坏记录 store.json: 健康 job 仍可被读取/调度, 不抛 SchedulerStateError。"""
    repo = _repository(tmp_path)
    job, _created = repo.create_job(
        SchedulerJobCreateRequest(
            name="健康任务",
            prompt="run",
            thread_id="th-1",
            source_task_id="t-1",
            schedule=_every(anchor=1_000, seconds=60),
            now=1_000.0,
        )
    )
    job_id = str(job["job_id"])
    _inject_corruption(repo)

    # 健康 job 照常可读可调度(坏记录被隔离)
    due = repo.reserve_due_runs(now=1_061.0)  # 刚 due(lateness=1<grace), 避开 misfire 跳过
    assert any(str(run.get("job_id")) == job_id for run in due)
    # 坏记录不被覆盖/清除: reserve 合法写 run 推进后仍原样保留
    after = json.loads(repo.store_path.read_text(encoding="utf-8"))
    assert after["jobs"]["job_bad"] == "not-a-dict"
    assert after["runs"]["srun_bad"] == {"schema_version": 999}


def test_corruption_report_isolates_bad_records(tmp_path) -> None:
    """坏记录被隔离并报告: 结构化报告含坏 job/run 错误, 健康记录不在内。"""
    repo = _repository(tmp_path)
    repo.create_job(
        SchedulerJobCreateRequest(
            name="健康任务",
            prompt="run",
            thread_id="th-1",
            source_task_id="t-1",
            schedule=_every(anchor=1_000, seconds=60),
            now=1_000.0,
        )
    )
    _inject_corruption(repo)

    report = repo.corruption_report()
    assert report["corrupt"] is True
    assert "SCHEDULER_JOB_INVALID" in report["corrupt_jobs"]
    assert "SCHEDULER_RUN_INVALID" in report["corrupt_runs"]
    assert "SCHEDULER_JOB_INVALID" not in report["corrupt_runs"]
    # 健康 job 不在损坏报告里
    assert len(report["corrupt_jobs"]) == 1  # 只有 job_bad 一条


def test_clean_store_reports_no_corruption(tmp_path) -> None:
    """健康 store: 损坏报告为空(无噪音)。"""
    repo = _repository(tmp_path)
    repo.create_job(
        SchedulerJobCreateRequest(
            name="健康任务",
            prompt="run",
            thread_id="th-1",
            source_task_id="t-1",
            schedule=_every(anchor=1_000, seconds=60),
            now=1_000.0,
        )
    )
    report = repo.corruption_report()
    assert report["corrupt"] is False
    assert report["corrupt_jobs"] == []
    assert report["corrupt_runs"] == []
