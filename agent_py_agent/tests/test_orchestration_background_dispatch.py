"""Focused tests for background subagent dispatch helpers."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


def test_background_dispatch_project_root_is_repo_root():
    """后台 dispatch 子进程必须从仓库根启动，避免误导入旧安装版 CLI。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    root = background_dispatch._project_root()

    assert (root / "agent_py_agent").is_dir()
    assert (root / "pyproject.toml").is_file()


def test_new_background_launch_does_not_inherit_previous_pid():
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    task = SimpleNamespace(
        id="child-1",
        attributes={
            "background_start": {
                "launch_id": "old-launch",
                "status": "running",
                "pid": 111,
            }
        },
    )

    class Manager:
        def load(self, run_id):
            assert run_id == "child-1"
            return task

        def save(self, _task):
            return None

    request = background_dispatch._BackgroundDispatchRequest(
        agent=SimpleNamespace(subagents=Manager()),
        run_ids=["child-1"],
        launch_id="new-launch",
        router=object(),
        cfg=object(),
        params=object(),
    )

    assert background_dispatch.mark_background_start(
        request,
        status="launching",
    ) == []
    assert task.attributes["background_start"]["launch_id"] == "new-launch"
    assert "pid" not in task.attributes["background_start"]

    assert background_dispatch.mark_background_start(
        request,
        status="running",
        pid=333,
    ) == []
    assert task.attributes["background_start"]["pid"] == 333


def test_background_result_without_explicit_ok_is_failed():
    """后台 dispatch 结果必须明确 ok=True 才能标 finished。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    agent = SimpleNamespace(
        _background_subagent_dispatches={
            "launch-1": {"run_ids": ["child-1"], "thread_name": "thread-1", "status": "running"}
        }
    )

    background_dispatch._remember_background_result(agent, "launch-1", {"summary": "missing ok"})

    assert agent._background_subagent_dispatches["launch-1"]["status"] == "failed"


def test_background_dispatch_worker_uses_captured_backend_override():
    """in-process auto-start 要使用创建时捕获的 backend，不能吃到后续全局 backend 改动。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    captured_backend = object()
    seen: dict[str, object] = {}
    task = SimpleNamespace(id="child-1", attributes={})

    class Manager:
        def load(self, run_id):
            assert run_id == "child-1"
            return task

        def save(self, next_task):
            seen["last_background_status"] = next_task.attributes["background_start"]["status"]

    class Agent:
        subagents = Manager()

        def dispatch_subagents(self, *_args, **_kwargs):
            seen["backend_override"] = getattr(self, "_subagent_worker_backend_override", None)
            return SimpleNamespace(summary="ok", records=[])

    agent = Agent()
    request = background_dispatch._BackgroundDispatchRequest(
        agent=agent,
        run_ids=["child-1"],
        launch_id="launch-1",
        router=object(),
        cfg=object(),
        params=object(),
        backend_override=captured_backend,
    )

    background_dispatch._background_dispatch_worker(request)

    assert seen["backend_override"] is captured_backend
    assert seen["last_background_status"] == "finished"
    assert not hasattr(agent, "_subagent_worker_backend_override")


def test_background_dispatch_does_not_capture_shared_provider():
    """Gateway 恢复线程的未配置 backend、父级 A 都不能覆盖 child 的 B。"""
    from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
        _captured_backend_override,
    )
    from agent_py_agent.agent.backends.base import UnconfiguredBackend

    agent = SimpleNamespace(config=SimpleNamespace(my_agent_owner_provider="local", my_agent_owner_kind="user",
                                                 my_agent_owner_id="alice"), backend=UnconfiguredBackend())
    assert _captured_backend_override(agent) is None
    agent.backend = SimpleNamespace(name="parent-A")
    assert _captured_backend_override(agent) is None
    explicit = agent._subagent_worker_backend_override = object()
    assert _captured_backend_override(agent) is explicit


def test_concurrent_dispatch_injections_are_thread_local(monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.core import SimpleAgent

    barrier = threading.Barrier(2)
    seen = []

    class Agent:
        _subagent_worker_backend_override = SimpleAgent._subagent_worker_backend_override

        def dispatch_subagents(self, router, *_args, **_kwargs):
            barrier.wait(timeout=5)
            seen.append((router, self._subagent_worker_backend_override))
            barrier.wait(timeout=5)
            return SimpleNamespace(summary="ok", records=[])

    agent = Agent()
    monkeypatch.setattr(dispatch, "mark_background_start", lambda *_a, **_k: [])
    monkeypatch.setattr(dispatch, "_remember_background_result", lambda *_a, **_k: None)

    def check(backend):
        request = dispatch._BackgroundDispatchRequest(agent, [], str(id(backend)), backend, None, None, backend)
        previous = agent._subagent_worker_backend_override = object()
        dispatch._background_dispatch_worker(request)
        assert agent._subagent_worker_backend_override is previous
        del agent._subagent_worker_backend_override
        assert not hasattr(agent, "_subagent_worker_backend_override")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(check, [object(), object()]))
    assert len(seen) == 2 and all(expected is actual for expected, actual in seen)
    assert not hasattr(agent, "_subagent_worker_backend_override")


def test_parallel_tool_context_preserves_only_explicit_dispatch_injection():
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        _install_parallel_thread_context,
        _restore_parallel_thread_context,
    )
    from agent_py_agent.agent.core import SimpleAgent

    class Agent:
        _subagent_worker_backend_override = SimpleAgent._subagent_worker_backend_override

    agent = Agent()
    injected = agent._subagent_worker_backend_override = object()
    context = _capture_parallel_thread_context(agent)

    def run():
        assert not hasattr(agent, "_subagent_worker_backend_override")
        previous = _install_parallel_thread_context(agent, context)
        try:
            assert agent._subagent_worker_backend_override is injected
        finally:
            _restore_parallel_thread_context(agent, previous)
        assert not hasattr(agent, "_subagent_worker_backend_override")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run).result(timeout=5)
    assert agent._subagent_worker_backend_override is injected


@pytest.mark.parametrize("base_model", ["", "parent-A"])
def test_worker_recovers_child_model_without_dispatch_backend(tmp_path, monkeypatch, base_model):
    """续派必须恢复 child thread 的模型，包括 Gateway 无默认、父子异模型和 child 后续换模型。"""
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
        _captured_backend_override,
    )
    from agent_py_agent.agent.agent_core.runner.worker import (
        RunSubagentWorkerParams,
        _build_worker_agent,
    )
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.tests.test_model_profiles import Host, add

    class Worker(Host):
        def __init__(self, config, root):
            super().__init__(root / "config")
            self.config = config
            self.backend = SimpleNamespace(name=config.model_name)
            self.subagents = SimpleNamespace(owner_id="alice", workspace_root=root)
            self.conversation_store = ConversationStore(root / "conversations")

    base = AgentConfig(model_backend="openai_compatible", model_name=base_model, my_agent_owner_provider="local",
                       my_agent_owner_kind="user", my_agent_owner_id="alice")
    host = Worker(base, tmp_path)
    a, _ = add(host, model_name="child-A")
    b, _ = add(host, model_name="child-B")
    task = SimpleNamespace(attributes={"host_model_profile.v1": {"profile_id": a}}, agent_thread_id="child-thread")
    monkeypatch.setattr("agent_py_agent.agent.agent_core.runner.worker.authorize_operation", lambda *_a: task)
    params = RunSubagentWorkerParams(base, tmp_path, "child-run", "", False, 4, False, "",
                                   backend_override=_captured_backend_override(host))
    worker = _build_worker_agent(Worker, params)
    assert worker.config.model_name == worker.backend.name == "child-A"
    host.conversation_store.threads.write(ConversationThread("child-thread", "alice", owner_id="alice", model_profile_id=b))
    execute_model_profile_operation(host, "set_default", {"profile_id": "default"})
    recovered = _build_worker_agent(Worker, replace(params, backend_override=_captured_backend_override(host)))
    assert recovered.config.model_name == recovered.backend.name == "child-B"


def test_start_background_dispatch_reports_mark_errors(monkeypatch):
    """后台启动状态写不进子代理账本时，父代理要能看到结构化错误。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    agent = SimpleNamespace(
        config=SimpleNamespace(model_backend="minimax"),
        subagents=SimpleNamespace(load=lambda _run_id: (_ for _ in ()).throw(ValueError("state broken"))),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(pid=12345),
    )
    result = background_dispatch._start_background_dispatch(agent, ["child-broken"])

    assert result["status"] == "started"
    assert result["background_mark_errors"][0]["run_id"] == "child-broken"
    assert result["background_mark_errors"][0]["context"] == "background_dispatch.mark_start.load"
    assert result["background_mark_errors"][0]["category"] == "data_parse"


def test_start_background_dispatch_marks_channel_failure_on_immediate_process_exit(monkeypatch):
    """后台子进程一启动就退出时，任务不能继续伪装成 PLANNING。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    task = SimpleNamespace(
        id="child-1",
        status="PLANNING",
        channel_status="OK",
        failure_type="",
        attributes={},
        result="",
        updated_at=0.0,
    )
    saved = {}

    class Manager:
        def load(self, run_id):
            assert run_id == "child-1"
            return task

        def save(self, next_task):
            saved["task"] = next_task

    agent = SimpleNamespace(
        config=SimpleNamespace(model_backend="minimax"),
        subagents=Manager(),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(pid=12345, poll=lambda: 2),
    )
    result = background_dispatch._start_background_dispatch(agent, ["child-1"])

    assert result["status"] == "failed"
    assert result["failure_type"] == "background_dispatch_startup"
    assert saved["task"].status == "CHANNEL_ERROR"
    assert saved["task"].channel_status == "BROKEN"
    assert saved["task"].failure_type == "background_dispatch_startup"


def test_start_background_dispatch_persists_pid_before_startup_poll(monkeypatch):
    """父进程先落 PID，再让子进程继续启动，避免双方争抢同一任务锁。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    events: list[str] = []
    agent = SimpleNamespace(
        config=SimpleNamespace(model_backend="minimax"),
        subagents=SimpleNamespace(),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "mark_background_start",
        lambda _request, *, status, **_kwargs: events.append(status) or [],
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(
            pid=12345,
            poll=lambda: events.append("poll") or None,
        ),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_background_process_started_payload",
        lambda _agent, _request, _process: {"status": "started"},
    )

    result = background_dispatch._start_background_dispatch(agent, ["child-1"])

    assert result["status"] == "started"
    assert events[:3] == ["launching", "running", "poll"]


def test_background_process_reaper_waits_child_and_records_exit():
    """成功启动的后台派工子进程必须由父进程主动 wait，不能留下 zombie。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    agent = SimpleNamespace()
    launch_id = "launch-reap"
    background_dispatch._remember_background_dispatch(agent, launch_id, ["child-1"], "pid:pending")
    request = background_dispatch._BackgroundDispatchRequest(
        agent=agent,
        run_ids=["child-1"],
        launch_id=launch_id,
        router=object(),
        cfg=object(),
        params=object(),
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    background_dispatch._start_background_process_reaper(agent, request, process)
    deadline = time.time() + 5
    while time.time() < deadline:
        if agent._background_subagent_dispatches[launch_id]["status"] != "running":
            break
        time.sleep(0.01)

    recorded = agent._background_subagent_dispatches[launch_id]
    assert recorded["status"] == "finished"
    assert recorded["returncode"] == 0
    assert process.returncode == 0
