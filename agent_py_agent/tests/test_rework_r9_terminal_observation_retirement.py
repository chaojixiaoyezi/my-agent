"""R9-23：终态任务的观察兜底结案沿原观察账 mark_handled(ids, now=...)，只结确切观察，之后到期策略照常处理且不调模型。

真实 SimpleAgent、ConversationStore、TaskLink、唤醒/观察/进度策略账与后台调度；只把观察处理账的一次写入换成模拟 I/O 错误。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation import store_observations
from agent_py_agent.agent.conversation.models import THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentScheduler
from agent_py_agent.tests.test_background_main_agent_runtime import (
    _child_settlement_fixture,
    _NaturalCompletionBackend,
)

# 全部不可复活的 TaskLink 终态，外加历史大写写法（领取准入按大小写无关判定终态）。
_TERMINAL_STATUSES = sorted(THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES) + ["DONE"]


def _handled(store, thread_id):
    events, errors = store.observations.recent_report(thread_id, limit=0)
    assert not errors
    return {event.observation_id: event.handled_at for event in events if event.handled_at > 0}


# LLM: 观察与唤醒沿原账本 append/raise_signal 互相链接；另一条同根非主代理观察不进入批次，用来证明只结确切 ID。
# 函数用途: 建一个根任务已终态、带待处理完成唤醒、链接观察和到期进度策略的真实后台场景。
def _terminal_case(tmp_path, terminal=None):
    agent, store, thread, runtime, first, _second = _child_settlement_fixture(tmp_path)
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    linked = store.observations.append({
        "thread_id": thread.thread_id, "event_type": "subagent_runner_finished", "root_task_id": "task-root",
        "source_agent_id": first.id, "requires_main_agent": True, "requires_llm_report": True,
        "metadata": {"task_id": first.id, "status": "DONE"}, "now": 20.0,
    })
    bystander = store.observations.append({
        "thread_id": thread.thread_id, "event_type": "subagent_progress", "root_task_id": "task-root",
        "source_agent_id": first.id, "metadata": {"task_id": first.id}, "now": 21.0,
    })
    wake = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "reason": "subagent_runner_finished", "root_task_id": "task-root",
        "source_agent_id": first.id, "observation": linked,
        "metadata": {"task_id": first.id, "status": "DONE"}, "now": 20.0,
    })
    if terminal is not None:
        store.tasks.update_status({"task_id": "task-root", "status": terminal, "now": 30.0})
    policy = store.progress.create({
        "thread_id": thread.thread_id, "task_id": "task-root", "interval_seconds": 60,
        "route_channel": "internal", "route_target": "thread-1", "now": 12.0,
    })
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    return store, thread, backend, scheduler, (linked, bystander, wake, policy)


@pytest.mark.parametrize("terminal", _TERMINAL_STATUSES)
def test_ledger_error_after_wake_removal_retires_exact_observation_on_retry(tmp_path, monkeypatch, terminal):
    store, thread, backend, scheduler, (linked, bystander, wake, policy) = _terminal_case(tmp_path, terminal)
    original = store_observations.update_json_file_atomic
    failures = []

    def ledger_write(path, updater, *args, **kwargs):
        if path == store.storage.observation_handled_path and not failures:
            failures.append(path)
            raise OSError("simulated I/O error")
        return original(path, updater, *args, **kwargs)

    monkeypatch.setattr(store_observations, "update_json_file_atomic", ledger_write)
    # 唤醒先写 handled 回执并移出 pending，随后观察处理账写失败；失败照常上抛，不吞成成功。
    with pytest.raises(OSError, match="simulated I/O error"):
        scheduler.tick(now=100.0)
    assert len(failures) == 1
    assert store.wakes.pending_one(wake.wake_signal_id) is None
    assert _handled(store, thread.thread_id) == {}
    assert store.progress.load(policy.policy_id).enabled is True

    # 普通下一拍：观察兜底车道只结这条确切观察，再走到到期策略并按终态退休，全程不调模型。
    assert scheduler.tick(now=200.0) == []
    handled = _handled(store, thread.thread_id)
    assert set(handled) == {linked.observation_id}
    assert bystander.observation_id not in handled
    assert [row["reason"] for row in scheduler.last_progress_policy_suppressed] == ["terminal_task_link"]
    assert store.progress.load(policy.policy_id).enabled is False
    assert store.tasks.load("task-root").status == terminal
    assert backend.prompts == []

    # 重复拍幂等：不再重领、不改处理时间、不重开执行。
    for current in (300.0, 400.0):
        assert scheduler.tick(now=current) == []
        assert _handled(store, thread.thread_id) == handled
        assert store.progress.load(policy.policy_id).enabled is False
        assert store.tasks.load("task-root").status == terminal
    assert backend.prompts == []
    assert len(failures) == 1


@pytest.mark.parametrize("case", ["active_root", "foreign_root", "foreign_thread"])
def test_terminal_source_cleanup_never_retires_inexact_observation(tmp_path, case):
    from agent_py_agent.agent.conversation.runtime import _retire_terminal_background_source

    terminal = None if case == "active_root" else "completed"
    store, thread, _backend, _scheduler, (linked, _bystander, _wake, _policy) = _terminal_case(tmp_path, terminal)
    assert store.tasks.load("task-root").status == ("active" if terminal is None else terminal)
    kwargs = {"thread_id": thread.thread_id, "task_id": "task-root", "wake_signal": {
        "kind": "observation_fallback", "root_task_id": "task-root", "observation_ids": [linked.observation_id]}}
    if case == "foreign_root":
        kwargs["wake_signal"] = {**kwargs["wake_signal"], "root_task_id": "task-other"}
    if case == "foreign_thread":
        kwargs["thread_id"] = "thread-foreign"
    _retire_terminal_background_source(store, kwargs)
    assert _handled(store, thread.thread_id) == {}
