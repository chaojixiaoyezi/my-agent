from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runner.gate import (
    ConcurrentRunnerParams,
    run_concurrent_runners,
)
from agent_py_agent.agent.concurrency import DurableDaemonThreadPoolExecutor


def test_durable_daemon_executor_uses_daemon_workers() -> None:
    executor = DurableDaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="durable-test")
    try:
        assert executor.submit(lambda: threading.current_thread().daemon).result(timeout=1) is True
    finally:
        executor.shutdown(wait=True)


def test_durable_daemon_executor_shutdown_does_not_wait_for_wedged_work() -> None:
    started = threading.Event()
    release = threading.Event()
    executor = DurableDaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="wedged-test")

    def wedge() -> None:
        started.set()
        release.wait(5)

    executor.submit(wedge)
    assert started.wait(1)
    before = time.monotonic()
    executor.shutdown(wait=False, cancel_futures=True)
    elapsed = time.monotonic() - before
    release.set()

    assert elapsed < 0.2


def test_concurrent_subagent_runners_use_daemon_workers(monkeypatch) -> None:
    observed_daemon_flags: list[bool] = []

    def fake_run_subagent_worker(_params):
        observed_daemon_flags.append(threading.current_thread().daemon)
        return "ok"

    from agent_py_agent.agent.agent_core.runner import dispatch

    monkeypatch.setattr(dispatch, "_run_subagent_worker", fake_run_subagent_worker)
    task = SimpleNamespace(
        id="child",
        attributes={},
        role="worker",
        goal="",
        plan=[],
        parent_id="root",
        root_id="root",
    )
    manager = SimpleNamespace(load=lambda _run_id: task)
    agent = SimpleNamespace(
        config=SimpleNamespace(
            runner_timeout_seconds="off",
            runner_timeout_by_role={},
        ),
        root="/tmp/my-agent-test",
        local_store=None,
        subagents=manager,
    )

    completed = run_concurrent_runners(
        ConcurrentRunnerParams(
            agent=agent,
            pending_jobs=[("child-1", task, ""), ("child-2", task, "")],
            runner_concurrency=2,
            runner_timeout_seconds=0,
            instruction="",
            start_runners=True,
            max_cards=1,
            probe=False,
        )
    )

    assert set(completed) == {"child-1", "child-2"}
    assert observed_daemon_flags == [True, True]
