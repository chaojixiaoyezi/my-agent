"""后台失败车道：配置等待、定时退避与用户隔离；不调用真实模型。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderRequestRejectedError,
)
from agent_py_agent.agent.settings.model_profiles import ModelProfileError
from agent_py_agent.cli import gateway_lane_retry


@pytest.mark.parametrize("error", [ModelNotConfiguredError(), ModelProfileError("模型引用已删除")])
def test_missing_model_waits_for_configuration_not_elapsed_time(monkeypatch, error):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = [100.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    retries.failed("owner-a", "thread", error, delay=30)
    now[0] += 24 * 3600
    assert not retries.ready("owner-a", "thread", model_ready=lambda: False)
    assert retries.ready("owner-b", "thread", model_ready=lambda: False)
    assert retries.ready("owner-a", "healthy", model_ready=lambda: False)
    assert retries.ready("owner-a", "thread", model_ready=lambda: True)
    retries.succeeded("owner-a", "thread")
    assert retries.ready("owner-a", "thread", model_ready=lambda: False)


@pytest.mark.parametrize("error", [RuntimeError("failure"), ProviderRequestRejectedError("400")])
@pytest.mark.parametrize("delay", [0, 30])
def test_other_errors_keep_monotonic_cooldown(monkeypatch, error, delay):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = [50.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    retries.failed("a", "t", error, delay=delay)
    assert retries.ready("a", "t", model_ready=lambda: False) is (delay == 0)
    now[0] += delay
    assert retries.ready("a", "t", model_ready=lambda: False)


def test_owner_eviction_and_capacity_do_not_accumulate_stale_records():
    retries = gateway_lane_retry.BackgroundLaneRetry()
    for index in range(1100):
        retries.failed("a", str(index), ModelNotConfiguredError(), delay=30)
    assert retries.count("a") == 1024
    retries.failed("b", "t", RuntimeError(), delay=30)
    retries.retain_owners(["b"])
    assert retries.count("a") == 0
    assert retries.count("b") == 1


def test_blocked_configuration_read_does_not_hold_retry_lock():
    from concurrent.futures import ThreadPoolExecutor

    retries = gateway_lane_retry.BackgroundLaneRetry()
    retries.failed("a", "t", ModelNotConfiguredError(), delay=30)

    def check_model():
        with ThreadPoolExecutor(max_workers=1) as pool:
            # 若配置读取仍持锁，这个不同线程的记录操作会超时。
            pool.submit(retries.succeeded, "a", "t").result(timeout=2)
        return True

    assert retries.ready("a", "t", model_ready=check_model)


def test_planner_skips_unconfigured_lane_and_resumes_exact_thread(tmp_path, monkeypatch, capsys):
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.cli import gateway_loops
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    host = host_with_store(tmp_path)
    host.config.model_backend = ""
    target = execute_local_model_operation(host, "old", "list", {})["thread_id"]
    requests = []
    prepared = []
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = host
    supervisor._inflight = {}
    supervisor._owner_schedulers = {}
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()

    def fail(thread_id):
        requests.append(thread_id)
        raise ModelNotConfiguredError()

    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=host), tick_thread=fail, prepare_tick=lambda **kw: None,
        ready_thread_ids=lambda **kw: (target, "healthy")[:kw["limit"]],
    )
    supervisor._base_scheduler = scheduler
    supervisor._submit_thread_candidates = lambda candidates, **kw: prepared.extend(candidates)
    supervisor._safe_thread_tick(scheduler, "base", target)
    for _ in range(3):
        prepared.clear()
        supervisor._submit_ready_thread_ticks()
        assert prepared[0][2] == ["healthy"]
    assert requests == [target]
    assert capsys.readouterr().err.count('"category": "model_not_configured"') == 1
    selected = add(host, model_name="configured-model")[0]
    # 只改用户的新会话默认值不会偷换旧会话；必须明确选择旧会话的模型。
    execute_local_model_operation(host, "new", "set_default", {"profile_id": selected})
    prepared.clear()
    supervisor._submit_ready_thread_ticks()
    assert prepared[0][2] == ["healthy"]
    execute_local_model_operation(host, "old", "select", {"profile_id": selected})
    prepared.clear()
    supervisor._submit_ready_thread_ticks()
    assert prepared[0][2] == [target, "healthy"]


@pytest.mark.parametrize("error,expected", [
    (ModelNotConfiguredError(), "active"), (ModelProfileError("已撤销"), "active"),
    (RuntimeError("bug"), "blocked"), (ProviderRequestRejectedError("400"), "blocked"),
])
def test_goal_configuration_failure_preserves_original_wake(tmp_path, error, expected):
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _handle_nonquota_wake_error,
    )
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": goal.task_id,
        "reason": "thread_goal_continue", "metadata": {"goal_id": goal.goal_id},
    })
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    released = []
    scheduler.scheduler_service = SimpleNamespace(release=lambda claim, **kw: released.append(claim))
    claim = object() if expected == "active" else None
    _handle_nonquota_wake_error(scheduler, signal, lifecycle_reason="thread_goal_continue", claim=claim, error=error)
    assert store.goals.load(thread.thread_id).status == expected
    pending = {wake.wake_signal_id for wake in store.wakes.pending()}
    assert (signal.wake_signal_id in pending) is (expected == "active")
    if expected == "active":
        assert released == [claim]
        assert store.tasks.load(goal.task_id).status == "active"
