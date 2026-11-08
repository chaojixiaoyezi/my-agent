"""R6O-W2：启动轮询窗口之后才非零退出、轮次仍 pending 的后台派工子进程，回收时收敛为持久启动失败。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch


def _request(run_ids):
    params = SimpleNamespace(expected_attempt_ids={run_id: f"attempt-{run_id}" for run_id in run_ids})
    return dispatch._BackgroundDispatchRequest(agent=None, run_ids=list(run_ids), launch_id="launch-1",
                                               router=None, cfg=None, params=params)


def _agent(statuses, *, broken=()):
    def get_attempt(attempt_id):
        if attempt_id in broken:
            raise OSError("runtime.db unreadable")
        status = statuses.get(attempt_id)
        return None if status is None else {"status": status}

    return SimpleNamespace(subagents=SimpleNamespace(runtime_db=SimpleNamespace(get_attempt=get_attempt)))


@pytest.fixture
def settled(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "_reap_background_process", lambda agent, launch_id, process: None)
    monkeypatch.setattr(dispatch, "_background_process_startup_failure", calls.append)
    return calls


def _reap(agent, request, returncode):
    process = SimpleNamespace(returncode=returncode, pid=4242, wait=lambda: returncode)
    dispatch._start_background_process_reaper(agent, request, process)
    # 回收线程是 daemon：测试里等它结束再断言。
    import threading

    for thread in threading.enumerate():
        if thread.name == f"my-agent-reap-{request.launch_id}":
            thread.join(timeout=10)
    return process


def test_late_nonzero_exit_settles_only_pending_attempts(settled):
    agent = _agent({"attempt-a": "pending", "attempt-b": "running", "attempt-c": "pending"})
    request = _request(["a", "b", "c"])
    process = _reap(agent, request, 1)
    assert len(settled) == 1
    failure = settled[0]
    assert failure.dispatch.run_ids == ["a", "c"] and failure.dispatch.launch_id == "launch-1"
    assert failure.returncode == 1 and failure.process is process and failure.mark_errors == []


@pytest.mark.parametrize("returncode", [0, None])
def test_clean_or_unknown_exit_is_left_to_runner(settled, returncode):
    _reap(_agent({"attempt-a": "pending"}), _request(["a"]), returncode)
    assert settled == []


def test_activated_or_unreadable_attempts_are_untouched(settled):
    agent = _agent({"attempt-a": "running", "attempt-b": "failed"}, broken={"attempt-c"})
    _reap(agent, _request(["a", "b", "c", "d"]), 2)
    assert settled == []
