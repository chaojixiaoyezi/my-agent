"""调度策略沿真实 owner 仓库、wake 和 due index 生效，不使用模型或真实时钟等待。"""
import pytest

from agent_py_agent.agent.scheduler.due_index import _earliest_due_at
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRunFinish
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.tests.test_scheduler_service import _setup


# LLM: 单一 job/run 仍由真实 repository 构造；只把时间与显式策略固定为合成测试输入。
# 函数用途: 为调度超期、失败和恢复测试建立一个可重复的计划。
def _job(repository, thread_id, policy):
    return repository.create_job(SchedulerJobCreateRequest(
        name="合成检查", prompt="核对合成目录", thread_id=thread_id,
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"},
        misfire_grace_seconds=60, policy=policy, now=900,
    ))[0]


@pytest.mark.parametrize("misfire,expected", [("skip", 0), ("coalesce", 1)])
def test_restart_misfire_skips_or_coalesces_once_without_replaying_all_ticks(tmp_path, misfire, expected):
    store, thread, repo = _setup(tmp_path)
    job = _job(repo, thread.thread_id, {"misfire": misfire})
    runs = repo.reserve_due_runs(now=10000)
    assert len(runs) == expected
    assert repo.get_job(job["job_id"])["next_run_at"] == 10600
    assert repo.reserve_due_runs(now=10000) == []
    if runs:
        assert runs[0]["scheduled_for"] == 1000 and runs[0]["lateness_seconds"] == 9000
        assert runs[0]["misfire_policy"] == "coalesce"
    else:
        history, _ = repo.history(job_id=job["job_id"])
        assert history[0]["status"] == "skipped"


def test_busy_origin_queue_expires_before_any_task_or_model_starts(tmp_path):
    store, thread, repo = _setup(tmp_path)
    job = _job(repo, thread.thread_id, {"queue_timeout_seconds": 10})
    service = SchedulerService(repo, conversation_store=store)
    service.enqueue_ready_runs(now=1000)
    signal = store.wakes.pending()[0]
    run_id = signal.root_task_id
    assert repo.get_active_run(run_id)["queue_deadline_at"] == 1010
    # 模拟重启后原车道一直忙，新的 service 沿同一账本结算过期。
    restarted = SchedulerService(repo, conversation_store=store)
    assert restarted.enqueue_ready_runs(now=1011) == []
    assert restarted.claim_wake(signal, now=1012).status == "stale"
    assert store.tasks.load(run_id) is None
    rows, _ = repo.history(job_id=job["job_id"])
    assert rows[0]["status"] == "skipped" and rows[0]["error_code"] == "SCHEDULER_QUEUE_EXPIRED"
    assert rows[0]["started_at"] == 0


def test_consecutive_failure_backoff_pause_and_manual_success_reset(tmp_path):
    store, thread, repo = _setup(tmp_path)
    job = _job(repo, thread.thread_id, {"failure_pause_after": 2, "failure_backoff_seconds": 1200})
    run = repo.reserve_due_runs(now=1000)[0]
    claim = repo.claim_run(run["run_id"], lease_seconds=60, now=1000)
    repo.finish_run(run["run_id"], claim["claim_id"], SchedulerRunFinish(status="failed", now=1001))
    first = repo.get_job(job["job_id"])
    assert first["consecutive_failures"] == 1 and first["backoff_until"] == 2201
    assert _earliest_due_at({"jobs": {job["job_id"]: first}}, now=1001) == 2201
    assert repo.reserve_due_runs(now=1600) == []
    # 手动执行是显式操作；不会把失败 run 重开或复用其副作用身份。
    second = repo.reserve_manual_run(job["job_id"], now=1601)
    assert second["run_id"] != run["run_id"]
    claim = repo.claim_run(second["run_id"], lease_seconds=60, now=1601)
    repo.finish_run(second["run_id"], claim["claim_id"], SchedulerRunFinish(status="unknown", now=1602))
    paused = repo.get_job(job["job_id"])
    assert paused["status"] == "paused" and paused["paused_reason"] == "consecutive_failures"
    assert paused["consecutive_failures"] == 2 and paused["backoff_until"] == 4002
    third = repo.reserve_manual_run(job["job_id"], now=2000)
    claim = repo.claim_run(third["run_id"], lease_seconds=60, now=2000)
    repo.finish_run(third["run_id"], claim["claim_id"], SchedulerRunFinish(status="done", now=2001))
    saved = repo.get_job(job["job_id"])
    assert saved["status"] == "paused"  # 成功不撤销用户需要处理的暂停
    assert saved["consecutive_failures"] == 0 and saved["backoff_until"] == 0


@pytest.mark.parametrize("policy", [{"misfire": "guess"}, {"queue_timeout_seconds": True},
    {"failure_backoff_seconds": -1}, {"unknown": 1}])
def test_invalid_policy_has_no_job_write(tmp_path, policy):
    store, thread, repo = _setup(tmp_path)
    with pytest.raises(ScheduleValidationError):
        _job(repo, thread.thread_id, policy)
    assert repo.list_jobs()[0] == []
