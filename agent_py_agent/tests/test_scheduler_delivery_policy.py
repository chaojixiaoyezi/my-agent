"""原调度执行与外发分离，全部使用隔离 owner、正式 Store/Tool 和模型/运输替身。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import BackgroundMainAgentScheduler
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
from agent_py_agent.agent.scheduler.delivery_policy import normalize_delivery
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRunFinish
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.tests.test_scheduler_im_l_integration import im_case
from agent_py_agent.tests.test_scheduler_repository import _every, _repository
from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery


@pytest.mark.parametrize("mode", ["local", "none"])
@pytest.mark.parametrize("reply", [".", "合成巡检完成，没有需要对外通知的内容。"])
def test_im_monitor_records_success_without_sending_each_final(tmp_path, mode, reply):
    case = im_case(tmp_path, delivery={"mode": mode})
    case.backend.generate = lambda *_args, **_kwargs: ModelResponse(text=reply, backend="test")
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    channels = _RetryDelivery()
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": case.store,
                                              "scheduler_service": case.agent.scheduler_service})
    reports = scheduler.tick(now=case.job["next_run_at"])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]["status"] == "done", history
    assert not channels.attempts, "内部收尾被错误投递到 IM"
    assert history[0]["delivery_status"] == "local_only"
    assert reports and reports[0].wake_handled
    assert case.store.wakes.pending_one(signal.wake_signal_id) is None
    assert not [r for r in case.store.messages.recent(case.thread.thread_id, limit=0) if r.role == "assistant"]
    rows = case.store.messages.recent(signal.thread_id, limit=0)
    assert any(r.role == "assistant" and r.content == reply for r in rows)


def test_new_im_default_is_silent_and_existing_job_can_update_without_rebinding(tmp_path):
    case = im_case(tmp_path)
    assert case.job["delivery"] == {"mode": "local"}
    result = ScheduleTool(case.agent).execute({"action": "update", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery": {"mode": "origin"}})
    assert result.ok, result.output
    updated = json.loads(result.output)["job"]
    assert updated["delivery"] == {"mode": "origin"}
    saved = case.agent.scheduler_repository.get_job(case.job["job_id"])
    assert saved["source_thread_id"] == case.job["source_thread_id"]
    assert saved["thread_id"] == case.job["thread_id"]


def test_update_delivery_only_applies_to_new_run_and_survives_restart(tmp_path):
    repo = _repository(tmp_path)
    job, _ = repo.create_job(SchedulerJobCreateRequest(name="合成检查", prompt="核验合成数据", thread_id="thread-original",
        schedule=_every(), now=900))  # 原底层记录保留 origin，不隐式迁移。
    run = repo.reserve_manual_run(job["job_id"], now=1000)
    changed = repo.update_job(job["job_id"], patch={"delivery": {"mode": "local"}}, expected_version=1, now=1001)
    assert repo.get_active_run(run["run_id"])["delivery"] == {"mode": "origin"}
    restarted = _repository(tmp_path)
    assert restarted.get_job(job["job_id"])["delivery"] == {"mode": "local"}
    claimed = restarted.claim_run(run["run_id"], lease_seconds=30, now=1002)
    restarted.finish_run(run["run_id"], claimed["claim_id"], SchedulerRunFinish(status="done", now=1003))
    new_run = restarted.reserve_manual_run(job["job_id"], now=1004)
    assert new_run["delivery"] == changed["delivery"]
    assert new_run["source_thread_id"] == "thread-original"


@pytest.mark.parametrize("bad", [{}, {"mode": []}, {"mode": "unknown"}, {"mode": "origin", "target": "another-user"}])
def test_notification_policy_cannot_select_another_recipient(bad):
    with pytest.raises(ScheduleValidationError):
        normalize_delivery(bad)
