"""R9 P1-R9-04：被取消选中的PTY退出/未启动证明先持久化到原SessionTask，内存有界历史淘汰后仍可核收。

真实业务取消、原注册表 start/_spawn/排空/关闭/裁剪与真实子进程；沙箱不在本合同内，按既有PTY用例直跑命令。
"""
from __future__ import annotations

import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
from agent_py_agent.agent.tooling.pty_sessions import (
    bind_cleanup_proof_consumer,
    pty_session_registry,
)
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    eventually_retired,
    native_cancellation_case,
    ready_index,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="stdlib pty is POSIX-only")

_SLEEP = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"


def _drop_retained_proofs():
    # 仅测试隔离：单例注册表跨用例共享，断言结束后丢弃本用例遗留的保留证明再清表。
    with pty_session_registry._lock:
        pty_session_registry._cleanup_proofs.clear()


# LLM: 只隔离测试单例的会话/预留/证明表；保留原表及未知退役资源，不重用ID，不更改生产容量或退出证明。
# 函数用途: 防止同批旧会话占容量，结束后清理本用例并恢复原注册表状态。
@pytest.fixture(autouse=True)
def _isolated_registry():
    with pty_session_registry._lock:
        saved = (pty_session_registry._sessions, pty_session_registry._pending_starts,
                 pty_session_registry._cleanup_proofs, pty_session_registry.idle_timeout_seconds)
        pty_session_registry._sessions = {}
        pty_session_registry._pending_starts = {}
        pty_session_registry._cleanup_proofs = {}
        pty_session_registry.idle_timeout_seconds = 0.0
    try:
        yield
    finally:
        _drop_retained_proofs()
        pty_session_registry.clear()
        with pty_session_registry._lock:
            sessions, pending, proofs, timeout = saved
            # clear不能证明退出的资源仍保留，不能为了隔离把未知进程从唯一账本抹掉。
            sessions.update(pty_session_registry._sessions)
            pending.update(pty_session_registry._pending_starts)
            proofs.update(pty_session_registry._cleanup_proofs)
            pty_session_registry._sessions, pty_session_registry._pending_starts = sessions, pending
            pty_session_registry._cleanup_proofs = proofs
            pty_session_registry.idle_timeout_seconds = timeout


@pytest.fixture
def direct_spawn(monkeypatch):
    monkeypatch.setattr(pty, "_sandbox_exec",
                        lambda command, target, owner_home=None, protected_persona_root=None, **kw: (command, True))


def _wait(predicate, timeout=20.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(message)


def _run_scope(agent, task, authority):
    return {"owner_home": str(agent.home_paths.owner_home_dir), "session_id": task.target_thread_id,
            "root_task_id": authority["task_id"], "task_id": f'gateway:{authority["task_id"]}',
            "run_id": authority["run_id"], "attempt_id": authority["attempt_id"]}


# LLM: 轮转的是退出历史，不是活跃容量；每次等待原退役谓词，不提高上限或伪造清理证明。
# 函数用途: 按顺序制造足量无关退出记录，使慢CI也能确定触发有界历史淘汰。
def _churn_unrelated(tmp_path, count):
    # 后续无关PTY正常结束、由排空线程走原裁剪，模拟常规有界历史淘汰。
    scope = {"owner_home": str(tmp_path / "unrelated-owner"), "session_id": "unrelated-thread",
             "root_task_id": "unrelated-task", "run_id": "unrelated-run", "attempt_id": "unrelated-attempt"}
    (tmp_path / "unrelated-owner").mkdir(exist_ok=True)
    sessions = []
    for _ in range(count):
        session = pty_session_registry.start("true", tmp_path, run_scope=scope)
        _wait(lambda: pty._source_resource_retired(session), message="unrelated PTY did not retire")
        sessions.append(session)
    pty_session_registry.sweep_idle()
    return sessions


# LLM: 单独核对历史轮转辅助函数的调度顺序，不以此替代下面真实PTY/持久证明验收。
# 函数用途: 确保慢退役时先等待前一条会话，不能靠true通常很快退出碰运气。
def test_churn_waits_for_previous_retirement(tmp_path, monkeypatch):
    from types import SimpleNamespace

    sessions = []

    def start(command, target, *, run_scope):
        assert command == "true" and target == tmp_path
        assert all(session.retired for session in sessions), "history churn started concurrent active PTYs"
        session = SimpleNamespace(retired=False, observations=0)
        sessions.append(session)
        return session

    def retired(session):
        session.observations += 1
        session.retired = session.observations >= 2
        return session.retired

    monkeypatch.setattr(pty_session_registry, "start", start)
    monkeypatch.setattr(pty_session_registry, "sweep_idle", lambda: None)
    monkeypatch.setattr(pty, "_source_resource_retired", retired)
    assert _churn_unrelated(tmp_path, 3) == sessions
    assert len(sessions) == 3 and all(session.retired for session in sessions)


def _registered():
    with pty_session_registry._lock:
        return dict(pty_session_registry._sessions)


def _pty_reference(store, task_id, session_id):
    refs = [ref for ref in store.session_tasks.require(task_id).metadata["cancellation_control"]["resource_refs"]
            if ref["kind"] == "pty" and ref["id"] == session_id]
    assert len(refs) == 1, refs
    return refs[0]


def _durable_proof(store, task_id, reference):
    control = store.session_tasks.require(task_id).metadata["cancellation_control"]
    matches = [item["proof"] for item in control.get("resource_cleanup_proofs", []) if item["resource_ref"] == reference]
    return matches[0] if matches else None


def _start_cancel_and_release(agent, backend, scheduler, call, task_id):
    store = agent.conversation_store
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15), "native Scheduler did not reach the blocked model call"
            task = store.session_tasks.require(task_id)
            authority = store.claims.load(task.target_thread_id)["runtime_authority"]
            original = pty_session_registry.start(_SLEEP, agent.home_paths.owner_home_dir,
                                                  run_scope=_run_scope(agent, task, authority))
            assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "pending"
            _wait(lambda: pty._source_resource_retired(original), message="original PTY did not retire")
        finally:
            backend.release.set()
            future.result(timeout=15)
    return original


def _retire(agent, call, view):
    ready_index(agent.conversation_store)
    call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
         expected_revision=view["revision"], idempotency_key="proof-retire")
    return eventually_retired(agent, view["agent_id"])


def test_cancelled_pty_exit_proof_survives_bounded_history(tmp_path, direct_spawn):
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    original = _start_cancel_and_release(agent, backend, scheduler, call, task_id)
    reference = _pty_reference(store, task_id, original.session_id)
    identity = {"pid": original.process.pid, "started_at": original.started_at,
                "scope": {"owner_home": original.execution_scope.owner_home, "thread_id": original.execution_scope.thread_id,
                          "root_task_id": original.execution_scope.root_task_id, "run_id": original.execution_scope.run_id,
                          "attempt_id": original.execution_scope.attempt_id}}
    assert reference["identity"] == identity
    _wait(lambda: _durable_proof(store, task_id, reference) is not None, message="exit proof was not persisted")
    proof = _durable_proof(store, task_id, reference)
    assert proof["outcome"] == "retired" and proof["identity"] == identity
    assert proof["termination"]["confirmed"] is True and proof["termination"]["method"] != "already_exited"
    _wait(lambda: original.session_id not in pty_session_registry._cleanup_proofs)
    unrelated = _churn_unrelated(tmp_path, pty._MAX_SESSION_COUNT + 1)
    registered = _registered()
    # 已确认持久化后原对象按默认上限正常淘汰；无关历史仍只保留最近32个。
    assert original.session_id not in registered
    assert sum(item.session_id in registered for item in unrelated) == pty._MAX_SESSION_COUNT
    done = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
    assert done["state"] == "completed", done
    assert not store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
    assert _retire(agent, call, view).metadata["background_agent"]["identity_state"] == "retired"
    assert not (agent.home_paths.owner_home_dir / "after-cancel-result.txt").exists()


def test_failed_checkpoint_retains_original_proof_until_normal_reconciliation(tmp_path, direct_spawn, monkeypatch):
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    record, failing = SessionTaskStore.record_resource_cleanup_proof, {"active": True, "calls": 0}

    def unavailable_then_original(self, *args, **kwargs):
        if failing["active"]:
            failing["calls"] += 1
            raise OSError("simulated proof write error")
        return record(self, *args, **kwargs)

    monkeypatch.setattr(SessionTaskStore, "record_resource_cleanup_proof", unavailable_then_original)
    original = _start_cancel_and_release(agent, backend, scheduler, call, task_id)
    reference = _pty_reference(store, task_id, original.session_id)
    _wait(lambda: failing["calls"] >= 1, message="no checkpoint was attempted")
    assert original.session_id in pty_session_registry._cleanup_proofs
    assert _durable_proof(store, task_id, reference) is None
    unrelated = _churn_unrelated(tmp_path, pty._MAX_SESSION_COUNT + 1)
    registered = _registered()
    # 未确认持久化：超过常规32条历史也保留原证明对象；它已退役，不占活跃容量；无关历史照常有界。
    assert registered.get(original.session_id) is original and pty._source_resource_retired(original)
    assert sum(item.session_id in registered for item in unrelated) == pty._MAX_SESSION_COUNT
    assert not any(not pty._source_resource_retired(item) for item in registered.values())
    failing["active"] = False
    # 不启动新PTY、无巡检线程：身份退休的正常取消对账重试原证明检查点并收口。
    assert _retire(agent, call, view).metadata["background_agent"]["identity_state"] == "retired"
    proof = _durable_proof(store, task_id, reference)
    assert proof is not None and proof["outcome"] == "retired" and proof["identity"] == reference["identity"]
    assert original.session_id not in pty_session_registry._cleanup_proofs
    assert not store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
    pty_session_registry.sweep_idle()
    assert original.session_id not in _registered()


@pytest.mark.parametrize("during_popen", [False, True])
def test_pending_start_selected_by_cancel_hands_off_exact_proof(tmp_path, direct_spawn, monkeypatch, during_popen):
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    owner, attr = (pty.subprocess, "Popen") if during_popen else (pty, "_sandbox_exec")
    entered, release, holder = threading.Event(), threading.Event(), {}
    original_call = getattr(owner, attr)

    def hold(*args, **kwargs):
        value = original_call(*args, **kwargs)
        if threading.get_ident() == holder.get("ident"):
            entered.set()
            assert release.wait(15)
        return value

    def start(scope):
        holder["ident"] = threading.get_ident()
        return pty_session_registry.start(_SLEEP, agent.home_paths.owner_home_dir, run_scope=scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            task = store.session_tasks.require(task_id)
            authority = store.claims.load(task.target_thread_id)["runtime_authority"]
            monkeypatch.setattr(owner, attr, hold)
            launch = pool.submit(start, _run_scope(agent, task, authority))
            assert entered.wait(15)
            assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "pending"
            refs = [ref for ref in store.session_tasks.require(task_id).metadata["cancellation_control"]["resource_refs"]
                    if ref["kind"] == "pty"]
            assert len(refs) == 1 and refs[0]["identity"] is None
            reference = refs[0]
            release.set()
            with pytest.raises(ToolCancelled):
                launch.result(timeout=15)
        finally:
            release.set()
            backend.release.set()
            future.result(timeout=15)
    _wait(lambda: _durable_proof(store, task_id, reference) is not None, message="start proof was not persisted")
    proof = _durable_proof(store, task_id, reference)
    if during_popen:
        # 出生即被取消：预留身份交接给出生会话，证明其完整树已退役。
        born = _registered()[reference["id"]]
        assert proof["outcome"] == "retired" and proof["identity"]["pid"] == born.process.pid
        assert proof["termination"]["confirmed"] is True
    else:
        # Popen 从未返回：正面证明未启动，不靠缺行推断。
        assert proof == {"schema": "pty_cleanup_proof.v1", "session_id": reference["id"], "outcome": "not_started",
                         "scope": proof["scope"], "identity": None, "termination": None}
        assert ProcessExecutionScope(**proof["scope"]).matches(ProcessExecutionScope(**reference["scope"]))
        assert reference["id"] not in _registered()
    _wait(lambda: reference["id"] not in pty_session_registry._cleanup_proofs)
    _churn_unrelated(tmp_path, pty._MAX_SESSION_COUNT + 1)
    assert reference["id"] not in _registered()
    done = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
    assert done["state"] == "completed", done
    assert _retire(agent, call, view).metadata["background_agent"]["identity_state"] == "retired"


def test_retained_proof_obligation_never_consumes_active_capacity(tmp_path, direct_spawn, monkeypatch):
    # 宿主绑定只在本线程本次request_stop可见；消费者未确认前原证明对象保留，但不计入活跃上限。
    calls, acknowledge = [], {"value": False}

    def consumer(proof):
        calls.append(proof)
        return acknowledge["value"]

    scope = {"owner_home": str(tmp_path), "session_id": "thread-a", "root_task_id": "task-a",
             "run_id": "run-a", "attempt_id": "attempt-a"}
    selected = pty_session_registry.start(_SLEEP, tmp_path, run_scope=scope)
    with bind_cleanup_proof_consumer(consumer):
        assert pty_session_registry.request_stop(owner_home=tmp_path, thread_id="thread-a", task_id="task-a") == (
            selected.session_id,)
    unbound = [pty_session_registry.start(_SLEEP, tmp_path, run_scope={**scope, "root_task_id": f"task-{name}"})
               for name in ("b", "c")]
    for name, session in zip(("b", "c"), unbound):
        assert pty_session_registry.request_stop(owner_home=tmp_path, thread_id="thread-a", task_id=f"task-{name}") == (
            session.session_id,)
    _wait(lambda: all(pty._source_resource_retired(item) for item in (selected, *unbound)))
    _wait(lambda: calls, message="retired proof was not offered to its consumer")
    assert set(pty_session_registry._cleanup_proofs) == {selected.session_id}
    assert calls[0]["outcome"] == "retired" and calls[0]["identity"]["pid"] == selected.process.pid
    monkeypatch.setattr(pty, "_MAX_SESSION_COUNT", 1)
    live = pty_session_registry.start(_SLEEP, tmp_path, run_scope={**scope, "root_task_id": "task-live"})
    with pytest.raises(OSError, match="PTY_SESSION_LIMIT"):
        pty_session_registry.start(_SLEEP, tmp_path, run_scope={**scope, "root_task_id": "task-over"})
    registered = _registered()
    # 无关历史按上限裁掉较旧者；更旧的未确认原证明对象保留且不占唯一活跃名额。
    assert registered.get(selected.session_id) is selected
    assert unbound[0].session_id not in registered and registered.get(unbound[1].session_id) is unbound[1]
    pty_session_registry.close(live.session_id)
    acknowledge["value"] = True
    assert set(pty_session_registry.checkpoint_cleanup_proofs([selected.session_id])) == {selected.session_id}
    assert not pty_session_registry._cleanup_proofs
    pty_session_registry.sweep_idle()
    assert selected.session_id not in _registered()
